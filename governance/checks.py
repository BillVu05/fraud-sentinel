"""Post-deployment checks: drift, segment disparity, champion vs challenger.

Usage:
    python governance/checks.py --decisions artifacts/decisions.jsonl

Thresholds are set HERE, in advance, not chosen after seeing the numbers. That
ordering is the entire point of a control: a threshold picked afterwards is a
description, not a check.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import features  # noqa: E402

PSI_WARN = 0.10   # conventional: 0.1-0.25 investigate, >0.25 material shift
PSI_ALERT = 0.25
MIN_SEGMENT_N = 200  # below this, report "inconclusive" rather than a ratio


def psi(expected, actual, bins=10):
    """Population Stability Index. Bin edges come from the TRAINING
    distribution, so the number answers "has live traffic moved away from what
    the model was fitted on", not "are these two samples different"."""
    expected, actual = np.asarray(expected, float), np.asarray(actual, float)
    expected = expected[expected != features.MISSING]
    actual = actual[actual != features.MISSING]
    if len(expected) < 100 or len(actual) < 100:
        return None

    edges = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return None
    edges[0], edges[-1] = -np.inf, np.inf

    e = np.histogram(expected, edges)[0] / len(expected)
    a = np.histogram(actual, edges)[0] / len(actual)
    eps = 1e-6
    e, a = np.clip(e, eps, None), np.clip(a, eps, None)
    return float(np.sum((a - e) * np.log(a / e)))


def load_decisions(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def drift_report(decisions, baseline_path):
    """Feature and score drift, live window vs the training window."""
    baseline = json.loads(Path(baseline_path).read_text())
    out = {}
    for name in features.FEATURE_ORDER:
        if name not in baseline:
            continue
        live = [d["feature_snapshot"].get(name, features.MISSING) for d in decisions]
        value = psi(baseline[name], live)
        if value is None:
            continue
        out[name] = {
            "psi": round(value, 4),
            "status": "ALERT" if value > PSI_ALERT else "WARN" if value > PSI_WARN else "OK",
        }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["psi"]))


def segment_report(decisions, labels=None):
    """Alert rate per segment, and precision where labels exist.

    A segment under MIN_SEGMENT_N reports inconclusive. "Inconclusive at n=41"
    is a finding; a precision computed off 41 rows is a decoration.
    """
    groups = defaultdict(list)
    for d in decisions:
        snap = d.get("feature_snapshot", {})
        groups[("ProductCD", snap.get("ProductCD"))].append(d)
        groups[("DeviceType", snap.get("DeviceType"))].append(d)

    out = {}
    for (field, value), rows in groups.items():
        key = f"{field}={value}"
        if len(rows) < MIN_SEGMENT_N:
            out[key] = {"n": len(rows), "status": "inconclusive"}
            continue
        alerts = [r for r in rows if r["decision"] == "ALERT"]
        entry = {"n": len(rows), "alert_rate": round(len(alerts) / len(rows), 4), "status": "ok"}
        if labels and alerts:
            hits = sum(labels.get(str(r["transaction_id"]), 0) for r in alerts)
            entry["precision"] = round(hits / len(alerts), 4)
        out[key] = entry
    return dict(sorted(out.items()))


def champion_challenger(decisions, labels, budget_rate=0.005):
    """Precision at the same alert budget, per model version, on the same stream."""
    by_version = defaultdict(list)
    for d in decisions:
        by_version[d.get("model_version", "unknown")].append(d)

    out = {}
    for version, rows in by_version.items():
        k = max(int(len(rows) * budget_rate), 1)
        top = sorted(rows, key=lambda r: -r["score"])[:k]
        hits = sum(labels.get(str(r["transaction_id"]), 0) for r in top)
        out[version] = {"n_scored": len(rows), "alerts": k, "precision": round(hits / k, 4)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions", required=True, help="jsonl pulled from the S3 decision log")
    ap.add_argument("--baseline", default="artifacts/train_distributions.json")
    ap.add_argument("--labels", default="artifacts/holdout_scores.csv")
    args = ap.parse_args()

    decisions = load_decisions(args.decisions)
    print(f"loaded {len(decisions):,} decisions\n")

    labels = {}
    if Path(args.labels).exists():
        import csv
        with open(args.labels) as fh:
            labels = {r["TransactionID"]: int(r["isFraud"]) for r in csv.DictReader(fh)}

    if Path(args.baseline).exists():
        print("--- drift (PSI vs training window) ---")
        for name, r in list(drift_report(decisions, args.baseline).items())[:10]:
            print(f"  {name:24s} {r['psi']:7.4f}  {r['status']}")
    else:
        print(f"--- drift: skipped, no {args.baseline} ---")

    print("\n--- segment disparity ---")
    for key, r in segment_report(decisions, labels).items():
        if r["status"] == "inconclusive":
            print(f"  {key:28s} inconclusive (n={r['n']})")
        else:
            p = f"  precision {r['precision']:.4f}" if "precision" in r else ""
            print(f"  {key:28s} n={r['n']:<7} alert_rate {r['alert_rate']:.4f}{p}")

    if labels:
        print("\n--- champion vs challenger ---")
        for version, r in champion_challenger(decisions, labels).items():
            print(f"  {version:20s} precision@budget {r['precision']:.4f}  ({r['alerts']} alerts / {r['n_scored']} scored)")


if __name__ == "__main__":
    main()
