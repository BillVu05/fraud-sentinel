"""Train the champion model and the baselines it has to beat.

Usage:
    python src/train.py --data data/train_transaction.csv

Writes to artifacts/: model.json, feature_order.json, vocab.json, metrics.json,
and holdout_scores.csv (input to governance/checks.py).

Two rules this file exists to enforce:
  1. Split on time, never shuffle. Fraud drifts; a shuffled split leaks the
     future and every reported number becomes fiction.
  2. Build every feature through features.build_vector, the same call the
     Lambda makes. No shortcut vectorisation, even though it would be faster.
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

import features

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"

# A fraud team can only work so many alerts a day. Precision at that budget is
# the number they care about; AUC is not something anyone in ops optimises.
ALERT_BUDGET_RATE = 0.005


def load(path):
    df = pd.read_csv(path, low_memory=False)
    df = df.sort_values("TransactionDT").reset_index(drop=True)
    print(f"loaded {len(df):,} rows, fraud rate {df.isFraud.mean():.4%}")
    return df


def time_split(df, train_frac=0.8):
    cut_dt = df.TransactionDT.quantile(train_frac)
    train = df[df.TransactionDT <= cut_dt]
    holdout = df[df.TransactionDT > cut_dt]
    print(f"split at TransactionDT={cut_dt:.0f}: {len(train):,} train / {len(holdout):,} holdout")
    print(f"  train fraud {train.isFraud.mean():.4%} | holdout fraud {holdout.isFraud.mean():.4%}")
    return train, holdout


def build_vocab(train_df):
    """Ordinal vocab from the TRAINING window only. Fitting it on the holdout
    too would leak which categories exist in the future."""
    vocab = {}
    for field in features.CATEGORICAL_FIELDS:
        values = (
            train_df[field].astype(str).str.strip().str.lower()
            if field in train_df.columns
            else pd.Series([""], dtype=str)
        )
        # Rare categories collapse to MISSING rather than getting their own
        # ordinal off 3 examples.
        counts = values.value_counts()
        keep = [v for v in counts.index if counts[v] >= 20]
        vocab[field] = {v: i for i, v in enumerate(keep)}
    return vocab


def vectorise(df, vocab, carry_state=None):
    """Replay rows in time order through the exact online code path.

    Slow on purpose. Vectorising this with pandas would be ~50x faster and would
    quietly drift from what the Lambda computes, which is the failure this whole
    project is about.
    """
    state_by_card = carry_state if carry_state is not None else {}
    rows = df.to_dict("records")
    out = np.empty((len(rows), len(features.FEATURE_ORDER)), dtype=np.float32)

    t0 = time.time()
    for i, txn in enumerate(rows):
        cid = features.card_id(txn)
        state = state_by_card.get(cid, features.empty_state())
        out[i] = features.build_vector(txn, state, vocab)
        state_by_card[cid] = features.update_state(txn, state)  # AFTER scoring
        if i and i % 100_000 == 0:
            print(f"  {i:,}/{len(rows):,} ({time.time() - t0:.0f}s)")
    print(f"  vectorised {len(rows):,} in {time.time() - t0:.0f}s")
    return out, state_by_card


# PSI needs the training distribution to bin against. A 10k sample per feature
# is ample for 10 bins and keeps the file around 3MB; the full 470k rows would
# be ~150MB for no extra resolution.
DRIFT_SAMPLE_N = 10_000


def save_train_distributions(X_train, path, seed=0):
    """Reference distributions for governance/checks.py PSI.

    Values are rounded to 4dp to match what handler.py writes into each
    decision's feature_snapshot. Binning the two sides at different precision
    would show as drift that is really just rounding.
    """
    rng = np.random.default_rng(seed)
    n = min(DRIFT_SAMPLE_N, len(X_train))
    rows = rng.choice(len(X_train), size=n, replace=False)
    sample = X_train[rows]

    dists = {
        name: [round(float(v), 4) for v in sample[:, i]]
        for i, name in enumerate(features.FEATURE_ORDER)
    }
    Path(path).write_text(json.dumps(dists))
    print(f"wrote drift baseline: {n:,} rows x {len(dists)} features -> {path}")


def precision_at_budget(y_true, scores, rate=ALERT_BUDGET_RATE):
    k = max(int(len(scores) * rate), 1)
    order = np.argsort(-scores)
    top = order[:k]
    caught = int(y_true[top].sum())
    return {
        "alerts": k,
        "precision": caught / k,
        "recall": caught / max(int(y_true.sum()), 1),
        "frauds_caught": caught,
        # the score the Lambda must exceed to raise an alert at this budget
        "threshold": float(scores[order[k - 1]]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/train_transaction.csv")
    ap.add_argument("--rounds", type=int, default=400)
    args = ap.parse_args()

    ARTIFACTS.mkdir(exist_ok=True)
    df = load(args.data)
    train_df, hold_df = time_split(df)

    vocab = build_vocab(train_df)
    print("vectorising train...")
    X_tr, state = vectorise(train_df, vocab)
    print("vectorising holdout (state carries over, as it would in production)...")
    X_ho, _ = vectorise(hold_df, vocab, carry_state=state)
    y_tr = train_df.isFraud.to_numpy()
    y_ho = hold_df.isFraud.to_numpy()

    pos_weight = (len(y_tr) - y_tr.sum()) / max(y_tr.sum(), 1)
    booster = xgb.train(
        {
            "objective": "binary:logistic",
            "eval_metric": "aucpr",
            "max_depth": 6,
            "eta": 0.05,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "scale_pos_weight": pos_weight,
            "missing": features.MISSING,
        },
        xgb.DMatrix(X_tr, label=y_tr, missing=features.MISSING),
        num_boost_round=args.rounds,
    )

    scores = booster.predict(xgb.DMatrix(X_ho, missing=features.MISSING))

    # Two trivial baselines. If the model cannot beat "flag the biggest
    # transactions" and "flag the busiest cards", it has earned nothing.
    amt_col = features.FEATURE_ORDER.index("amt_log")
    vel_col = features.FEATURE_ORDER.index("vel_count_1h")
    results = {
        "model": precision_at_budget(y_ho, scores),
        "baseline_largest_amount": precision_at_budget(y_ho, X_ho[:, amt_col]),
        "baseline_highest_velocity": precision_at_budget(y_ho, X_ho[:, vel_col]),
        "holdout_fraud_rate": float(y_ho.mean()),
        "alert_budget_rate": ALERT_BUDGET_RATE,
        "n_features": len(features.FEATURE_ORDER),
        "n_train": int(len(y_tr)),
        "n_holdout": int(len(y_ho)),
    }

    booster.save_model(ARTIFACTS / "model.json")
    (ARTIFACTS / "feature_order.json").write_text(json.dumps(features.FEATURE_ORDER, indent=2))
    (ARTIFACTS / "vocab.json").write_text(json.dumps(vocab))
    (ARTIFACTS / "metrics.json").write_text(json.dumps(results, indent=2))
    save_train_distributions(X_tr, ARTIFACTS / "train_distributions.json")
    pd.DataFrame({
        "TransactionID": hold_df.TransactionID.to_numpy(),
        "isFraud": y_ho,
        "score": scores,
    }).to_csv(ARTIFACTS / "holdout_scores.csv", index=False)

    print("\n--- precision at a %.1f%% alert budget ---" % (ALERT_BUDGET_RATE * 100))
    for name in ("model", "baseline_largest_amount", "baseline_highest_velocity"):
        r = results[name]
        print(f"  {name:28s} precision {r['precision']:.3f}  recall {r['recall']:.3f}  ({r['frauds_caught']}/{r['alerts']})")
    lift = results["model"]["precision"] / max(
        results["baseline_largest_amount"]["precision"],
        results["baseline_highest_velocity"]["precision"],
        1e-9,
    )
    print(f"\n  lift over best baseline: {lift:.2f}x")
    if lift < 1.2:
        print("  NOTE: that is not a convincing win. Say so in the README.")


if __name__ == "__main__":
    main()
