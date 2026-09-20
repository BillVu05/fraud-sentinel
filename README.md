# fraud-sentinel

Real-time card fraud scoring on AWS, with the model-governance pack a regulated
lender would actually ask for.

Transactions arrive on Kinesis, a Lambda scores them against XGBoost using
velocity features it computes itself from DynamoDB, and every decision is
written to S3 with its reason codes, feature snapshot and model version
attached. An optional Bedrock call drafts the investigator case note, and is
refused if it cites a number the system never computed.

> Status: scaffolded, not yet measured. Numbers marked `TBD` below get filled in
> from real runs, not estimates. Nothing in this README is aspirational.

---

## The decision this project is actually about

IEEE-CIS ships 394 columns. 339 of them (`V1`-`V339`), plus the `C*` and `D*`
groups, are **Vesta's own precomputed aggregates**. They cannot be recomputed
from a single incoming transaction, so a model trained on them cannot be served
in real time. Most published work on this dataset trains on them anyway, because
the leaderboard does not care whether the features exist at serving time.

This project drops all of them and recomputes its own velocity signals instead:
card transaction count over 1h and 24h, amount sum and max over 24h, this
charge as a ratio of the card's recent maximum, seconds since the last
transaction, and distinct billing addresses in the window.

**394 columns in, 35 features out.** The score will be worse than the
leaderboard. It will also be a score you can actually serve.

The consequence that matters: `src/features.py` is imported by both `train.py`
and `handler.py`. One function, two callers. Training-serving skew is not
managed by discipline here, it is structurally impossible, and
`tests/test_parity.py` fails the build if that stops being true.

---

## Architecture

```
replay.py ──> Kinesis (1 shard) ──> Lambda (container, xgboost)
                                      │
                      ┌───────────────┼────────────────┐
                      │               │                │
                 DynamoDB            S3            CloudWatch
             (card velocity     (decision log:    (per-txn latency
              state, 24h TTL)    score, reason     via metric filter)
                                 codes, feature
                                 snapshot, model
                                 version)
                                      │
                                 narrate.py ──> Bedrock
                                             (Claude Haiku 4.5,
                                              au. inference profile)
```

Region is `ap-southeast-2`. Transaction data and Bedrock inference both stay
inside the Australian boundary, which is the reason the `au.*` inference
profile is used rather than the cheaper global one.

---

## Results

| | Precision @ 0.5% alert budget | Recall | Lift vs best baseline |
|---|---|---|---|
| XGBoost, 35 servable features | TBD | TBD | TBD |
| Baseline: flag largest amounts | TBD | TBD | 1.0x |
| Baseline: flag busiest cards | TBD | TBD | - |

Precision at a fixed alert budget, not AUC. A fraud team can only work so many
alerts a day, so the question is "of the N we can investigate, how many are
real", which is the number operations actually lives on.

**If the model does not beat both baselines, that will be stated here plainly.**

| Operational | |
|---|---|
| p50 / p95 per-transaction latency | TBD |
| Cost per million decisions | TBD |
| Total AWS spend for the project | TBD |

---

## Governance

- `governance/model-card.md` — owner, purpose, training window, the 339-feature
  exclusion and why, known limitations, retrain trigger.
- `governance/checks.py` — PSI drift against the training distribution
  (thresholds 0.10 / 0.25 fixed in advance, not chosen after seeing results),
  segment disparity across `ProductCD` and `DeviceType`, and champion vs
  challenger precision on the same stream.
- Segments under n=200 report **inconclusive** rather than a precision figure.
  "Inconclusive at n=41" is a finding. A precision computed off 41 rows is a
  decoration.
- Every decision carries the feature values behind it, so an alert stays
  explainable after the state that produced it has moved on.
- `src/narrate.py` fences the evidence as data, and `check_grounded()` rejects
  any narrative containing a number absent from that evidence. A fluent,
  plausible case note citing a transaction amount the system never saw is the
  failure worth catching, and it is the one a human will not catch at 200 alerts
  a day.

Nothing in this repo approves, blocks, or writes a detection rule. A human does
that.

---

## Running it

```powershell
.\run.ps1 setup            # python deps, then the manual steps it prints
.\run.ps1 test             # parity test + lint
.\run.ps1 train            # time-split, train, score against baselines
.\run.ps1 deploy           # terraform apply + build and push the image
.\run.ps1 replay -N 1000   # holdout transactions into Kinesis
.\run.ps1 report           # pull decisions, latency p50/p95, governance checks
.\run.ps1 narrate -Id 3577209
.\run.ps1 destroy          # every session, without exception
```

Prerequisites: Python 3.12, Docker, AWS CLI v2, Terraform, an AWS account, and
`data/train_transaction.csv` from the
[IEEE-CIS competition](https://www.kaggle.com/competitions/ieee-fraud-detection/data).

**Cost.** Kinesis provisioned at 1 shard is about $0.36/day and is the only line
item that matters. On-demand mode bills roughly $28.80/month for an idle stream,
which is why it is not used. Lambda, DynamoDB, S3 and ECR sit inside the free
tier at this volume. Bedrock is fractions of a cent per case note. `destroy`
after every session and the whole project costs a few dollars.

A billing alarm at $10 is created by Terraform. Set it up before the first
`apply`, not after.

---

## Not built, and why

- **SageMaker.** A Lambda container image serves one XGBoost model at a few
  hundred transactions per second for free-tier money. A SageMaker endpoint
  bills by the hour to do the same thing here.
- **The `shap` package.** XGBoost's `predict(pred_contribs=True)` returns exact
  TreeSHAP natively, so `shap`, numba and llvmlite stay out of the image
  entirely. Same numbers, a fraction of the cold start.
- **A dashboard.** The decision log is JSONL in S3 and the numbers above come
  from it. A dashboard would be a week of React proving nothing this README does
  not already prove.
- **Data lineage, CPS 234 controls, four-eyes release, vendor risk assessment.**
  A real deployment needs all of them. This is a portfolio project and says so
  rather than pretending otherwise.
