# Model Card: fraud-sentinel scorer

Fill the `TBD` fields from `artifacts/metrics.json` after a real training run.
Do not estimate any of them.

## Identity

| | |
|---|---|
| Model | fraud-sentinel scorer |
| Version | TBD (matches the ECR image tag and the `model_version` on every decision) |
| Owner | Tien Dung Vu |
| Type | XGBoost binary classifier, `binary:logistic` |
| Trained | TBD |
| Status | Portfolio project. Not deployed against real customer money. |

## Purpose

Scores card-not-present transactions for fraud risk in real time and raises an
alert when the score clears a threshold set by a fixed daily alert budget.

The output is a **ranking aid for a human investigator**. It does not block
transactions, approve them, or write detection rules.

## Data

| | |
|---|---|
| Source | IEEE-CIS Fraud Detection (Vesta Corporation), via Kaggle |
| Rows | 590,540 card-not-present e-commerce transactions |
| Base rate | 3.5% fraud |
| Split | Time-based on `TransactionDT`. First 80% of the time range trains, last 20% is holdout. Never shuffled. |
| Train / holdout rows | TBD / TBD |

A shuffled split would leak the future into training and inflate every number in
this card. Fraud patterns drift, so the only honest question is how the model
does on transactions that happened after the ones it learned from.

## Features: 35, down from 394

**Excluded by design.** `V1`-`V339`, `C1`-`C14` and `D1`-`D15` are Vesta's own
precomputed aggregates. They cannot be recomputed from a single incoming
transaction, so a model that depends on them cannot be served in real time.
Training on them would produce a better number and an unservable model.

**Event features (20)** carried by the transaction itself: `TransactionAmt` and
its log, hour of day, day of week, `card1`-`card6`, `addr1`, `addr2`, `dist1`,
`dist2`, `ProductCD`, `P_emaildomain`, `R_emaildomain`, `DeviceType`,
`DeviceInfo`, `M1`-`M9`.

**Velocity features (7)** computed from this card's recent history in DynamoDB:
transaction count over 1h and 24h, amount sum and max over 24h, this charge as a
ratio of the recent maximum, seconds since the last transaction, and distinct
billing addresses in the window.

Categoricals are ordinal-encoded from a vocabulary fitted on the **training
window only**, with categories under 20 occurrences collapsed to missing.
Unseen categories at serving time score as missing rather than colliding onto a
real ordinal.

## Performance

At a 0.5% daily alert budget on the time-based holdout:

| | Precision | Recall | Frauds caught / alerts |
|---|---|---|---|
| Model | TBD | TBD | TBD |
| Baseline: largest amounts | TBD | TBD | TBD |
| Baseline: highest velocity | TBD | TBD | TBD |

Alert threshold in production: TBD (the score at the k-th ranked holdout
transaction, carried into the Lambda as `ALERT_THRESHOLD`).

## Known limitations

1. **`card1` is not a card.** It is the closest identifier IEEE-CIS provides, so
   velocity here means "this card bucket", not "this physical card". Every
   velocity feature inherits that imprecision.
2. **Velocity state is capped at 50 events / 24h** to bound the DynamoDB item. A
   card exceeding that gets a truncated view of its own history.
3. **The dataset is US e-commerce, circa 2019.** Nothing here is calibrated to
   Australian payment patterns, card-present fraud, or scam typologies.
4. **No account-level or merchant-level features.** The dataset does not support
   them, and they are among the strongest signals a real bank has.
5. **Cold-start cards score on event features alone.** The first transaction on
   an unseen card has no velocity history, and the model is not separately
   calibrated for that case.
6. **Fairness testing is thin.** Only `ProductCD` and `DeviceType` are available
   as segments. The dataset carries no demographic attributes, so this model has
   not been tested for the disparities that would matter most in a lending or
   customer-impacting context.

## Monitoring and retraining

| Control | Threshold | Fixed |
|---|---|---|
| Feature PSI vs training window | investigate > 0.10, alert > 0.25 | in advance, in `checks.py` |
| Score distribution PSI | same | in advance |
| Segment alert-rate disparity | reported; segments under n=200 marked inconclusive | in advance |
| Champion vs challenger | precision at the same budget on the same stream | per deployment |

**Retrain trigger:** score PSI above 0.25, or precision at budget falling more
than 20% relative from the figure in this card, whichever comes first.

## Human oversight

Every alert reaches a person. The Bedrock case note is a drafting convenience
and is withheld entirely if `check_grounded()` finds a number in it that is not
in the stored evidence. No component of this system writes or activates a
detection rule.
