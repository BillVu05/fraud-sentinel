# PLAN.md

Working plan for fraud-sentinel. Written 2026-09-20. Pick this up cold.

---

## Start here tomorrow

Four commands, in this order. Nothing else is blocked on anything else.

```powershell
cd C:\Users\tiend\OneDrive\Documents\GitHub\fraud-sentinel

winget install Amazon.AWSCLI Hashicorp.Terraform
aws configure                 # region: ap-southeast-2, output: json
.\run.ps1 setup               # installs python deps, prints what is left
```

Then set the billing alarm and download the data (both below), and you are into
Week 1 Day 1.

---

## Why this project exists

Three gaps in `cv.md` block AI/ML Engineer roles at Australian banks:

1. **AWS is the thinnest claim on the CV.** It appears once, with no named
   service and no artifact anyone can verify. Every bank JD asks for it.
2. **Zero financial domain.** HR, marketing, aviation, news media, traffic.
   Nothing with money, risk, or a regulator.
3. **Everything is batch.** Bank fraud decisioning is real-time by definition.

A fourth is the opening: **no model-governance artifact**, despite that being
the existing strength. Traffic-CV reported "no detector beat the baseline".
Pacific Wings deploys a model only if it beats the baseline by a margin. That is
model-validation behaviour, and APRA's 30 April 2026 industry letter says banks
are short of it.

Scored 4.2/5 on full scope, 2.85/5 if the AWS and governance layers get
dropped. **Scope discipline is the whole game.**

---

## The one idea to not lose

IEEE-CIS ships 394 columns. `V1`-`V339`, `C1`-`C14` and `D1`-`D15` are Vesta's
own precomputed aggregates. They cannot be recomputed from one incoming
transaction, so a model trained on them **cannot be served in real time**.

We dropped all of them and compute our own velocity features instead.
**394 columns in, 35 out.**

The score will be worse than the Kaggle leaderboard. That is the correct result
and it gets reported as such.

The consequence: `src/features.py` is imported by both `train.py` and
`handler.py`. One function, two callers. Training-serving skew is structurally
impossible, not merely discouraged.

**The interview answer this buys:** *"I deleted 339 features because I could not
compute them at serving time. Training-serving skew is the actual hard part of
production ML, not the model."*

---

## Where things stand

### Done and committed (`c932587`)

| File | State |
|---|---|
| `src/features.py` | Complete. 35 features. The parity boundary. |
| `src/train.py` | Complete. Time split, vocab, XGBoost, 2 baselines, precision@budget. |
| `src/handler.py` | Complete. Kinesis to score to DynamoDB + S3, TreeSHAP reason codes. |
| `src/replay.py` | Complete. Holdout into Kinesis at a fixed rate. |
| `src/narrate.py` | Complete. Bedrock case note + groundedness rejection. |
| `governance/checks.py` | Complete. PSI, segment disparity, champion/challenger. |
| `governance/model-card.md` | Written, `TBD` fields awaiting real numbers. |
| `infra/*.tf` | Complete. Kinesis, Lambda, ECR, DynamoDB, S3, IAM, CloudWatch, billing alarm. |
| `tests/test_parity.py` | 6 tests, passing. |
| `tests/test_drift.py` | 7 tests, passing. Proves the PSI monitor fires. |
| `run.ps1` | Complete. All tasks. |
| `README.md` | Written, all metrics `TBD`. |

### Verified

- `python -m pytest tests/ -q` → 13 passed
- Drift monitor verified firing: 0/35 features flagged on unshifted data,
  `vel_amt_sum_24h` ALERT at PSI 2.44 on a +25 mean shift and 1.01 on a 3x
  variance change
- All Python compiles
- Terraform files brace-balanced

### Not verified

- `terraform validate` has never run. Terraform is not installed.
- **Nothing has touched AWS.** No resources, no spend.
- Every number in `README.md` and `governance/model-card.md` is `TBD`.

### Not done at all

- AWS CLI and Terraform not installed
- `aws configure` not run
- Billing alarm not set
- `data/train_transaction.csv` not downloaded
- `docs/architecture.png` does not exist
- `governance/validation-report.md` does not exist
- `docs/postmortem.md` does not exist
- No demo recording

---

## Week 1: prove the pipe, ugly

**Goal: a transaction travels end to end. No model this week.** The Lambda can
return a hardcoded score. This week carries all the unknown risk, so it gets
spent entirely on infrastructure.

### Day 0: setup

- [ ] `winget install Amazon.AWSCLI Hashicorp.Terraform`
- [ ] Create an IAM user with programmatic access. Do not use the root account.
- [ ] `aws configure` with region `ap-southeast-2`
- [ ] `aws sts get-caller-identity` returns your account
- [ ] **Billing alarm at $10, in the console, before creating anything.**
      Terraform creates one too, but that one only exists after the first
      `apply`, which is too late to be a guardrail.
- [ ] Enable "Receive Billing Alerts" in Billing preferences, otherwise the
      `EstimatedCharges` metric never publishes
- [ ] Create the `fraud-sentinel` repo on GitHub and push
- [ ] Download `train_transaction.csv` into `data/` from
      https://www.kaggle.com/competitions/ieee-fraud-detection/data
      (requires a Kaggle account and accepting the competition rules)
- [ ] `.\run.ps1 setup`

### Days 1 to 4: get Terraform applying

- [ ] `terraform -chdir=infra init`
- [ ] `terraform -chdir=infra validate` and fix whatever it finds. This has
      never run, so expect something.
- [ ] `terraform -chdir=infra plan` reads clean
- [ ] Temporarily stub `handler.py` to return a fixed score so there is no
      dependency on a trained model yet
- [ ] `.\run.ps1 deploy`

  **The bootstrap ordering is the most likely thing to bite you.** The Lambda
  cannot be created until an image exists in ECR, and the image cannot be pushed
  until the repo exists. `run.ps1 deploy` handles this with a targeted apply on
  `aws_ecr_repository.scorer` first. If it still fails, apply that target by
  hand, push the image, then apply the rest.

- [ ] `.\run.ps1 replay -N 100` and confirm objects appear under
      `s3://<bucket>/decisions/`
- [ ] `.\run.ps1 destroy`

### Day 5: measure

- [ ] `.\run.ps1 replay -N 1000`
- [ ] `.\run.ps1 report` for p50/p95
- [ ] **Write the latency number into `README.md` now**, before there is
      anything to be proud of
- [ ] `.\run.ps1 destroy`

### `terraform destroy` at the end of every single session

Kinesis provisioned at 1 shard is about $0.36/day and is the only line item
that matters. Leaving it up for a month is roughly $11 for nothing.

---

## KILL CRITERION

**If by end of Week 2 a transaction has not gone through Kinesis to Lambda and
come back with a score, stop.**

Either drop to SQS (free tier, loses the resume word) or abandon the project.
Do not spend Week 3 improving a model that has nowhere to run.

The classifier-only variant of this project scores 2.85/5, which is worth less
than the projects already on the CV. Shipping it would be worse than shipping
nothing.

---

## Week 2: the model

- [ ] `.\run.ps1 train`

  Expect 20 to 40 minutes. `vectorise()` replays 590k rows through pure Python
  on purpose, so the trainer walks the exact code path the Lambda walks.
  Vectorising it with pandas would be about 50x faster and would quietly drift
  from what the Lambda computes, which is the failure this whole project is
  about. Do not optimise it.

- [ ] Read the lift line it prints. If lift over the best baseline is under
      1.2x, **that goes in the README as a finding**, not as something to hide
      by tuning until it looks better
- [ ] Copy `model.threshold` from `artifacts/metrics.json` into the README
- [ ] Restore the real `handler.py` (undo the Day 1 stub)
- [ ] `.\run.ps1 deploy` with the real model
- [ ] `.\run.ps1 replay -N 5000`
- [ ] `.\run.ps1 report`
- [ ] Fill in the Results table in `README.md`: precision@budget, recall, lift,
      p50/p95, cost per million decisions
- [ ] Compute cost per million from the actual `.\run.ps1 cost` output, do not
      estimate it
- [ ] `.\run.ps1 destroy`

Drift works out of the box: `train.py` writes
`artifacts/train_distributions.json` (10k sampled rows per feature) and
`tests/test_drift.py` proves the monitor fires. Nothing to do here.

---

## Week 3: the differentiator, then stop

**Do not skip this to polish the model.** This layer is the entire reason the
project scores 4.2 instead of 2.85.

- [ ] Train a second model version (different `max_depth` or feature subset),
      deploy as a new image tag with a new `model_version`, replay the same
      slice, and let `champion_challenger()` report the delta
- [ ] `.\run.ps1 report` with drift working, and record the PSI numbers
- [ ] Fill every `TBD` in `governance/model-card.md`
- [ ] Write `governance/validation-report.md`: what was tested, what passed,
      what was inconclusive and at what n
- [ ] `.\run.ps1 narrate -Id <some alert>` and confirm the groundedness check
      passes. **Then deliberately break it:** hand-edit a decision JSON to
      remove a field the narrative cites, rerun, and confirm it gets rejected.
      A control you have never seen fire is not a control.
- [ ] `docs/architecture.png`. Excalidraw or draw.io, 10 minutes, do not gold
      plate it
- [ ] `docs/postmortem.md`: what the model did not beat, what got cut on cost,
      where the drift monitor would false-alarm, what a real bank would need
      that this does not have (data lineage, CPS 234 controls, vendor risk,
      four-eyes release)
- [ ] **The 90-second recording.** Terminal split: `replay.py` left, CloudWatch
      tail right, then the generated case note. If it is not demoable in 90
      seconds it is not finished.
- [ ] `.\run.ps1 destroy`

Anything you think of in Week 4 goes in the README under "not built, and why".
That sentence is itself a hiring signal.

---

## Ship gate

All five, or it is not done:

1. `pytest tests/` green
2. p95 latency and cost per million decisions in the README, **measured, not
   estimated**
3. Model beats both baselines at the fixed alert budget, **or** the README says
   plainly that it does not
4. Governance pack complete: model card, drift numbers, segment disparity,
   champion/challenger delta
5. 90-second recording exists

---

## Decisions already made. Do not relitigate mid-build.

| Choice | Why |
|---|---|
| Kinesis provisioned, 1 shard | ~$0.36/day. On-demand bills ~$28.80/month for an idle stream. |
| Lambda container image via ECR | Docker already installed. No layer packaging pain. xgboost + numpy is ~200MB against a 10GB limit. |
| `predict(pred_contribs=True)` | Native exact TreeSHAP. Keeps `shap`, numba and llvmlite out of the image. |
| Bedrock Claude Haiku 4.5, `au.` profile | Newer Claude models are not callable in-region from Sydney without an inference profile. The `au.` one pins inference to the Australian boundary, which is a real APRA talking point. |
| One dataset, time-split | Kaggle's `test_transaction.csv` is unlabelled and therefore useless. Split `train_transaction.csv` on `TransactionDT` and replay the holdout. One schema, labels everywhere. |
| Precision at a fixed alert budget | A fraud team can only work N alerts a day. Nobody in ops optimises AUC. |
| `run.ps1`, not a Makefile | `make` is not installed on this machine. PowerShell is. |
| No SageMaker | A Lambda container serves one XGBoost model for free-tier money. A SageMaker endpoint bills hourly to do the same thing here. |

---

## Cost discipline

- Billing alarm at $10, set before the first `apply`
- `.\run.ps1 destroy` at the end of every session, without exception
- `.\run.ps1 cost` weekly
- Expected total for the whole project: **under $5**
- Lambda, DynamoDB, S3, ECR sit in the free tier at this volume. Bedrock is
  fractions of a cent per case note. Kinesis is the only thing that bills while
  idle.

---

## After it ships

- [ ] Add to `cv.md` as a Projects entry, 5 bullets maximum
- [ ] **Trim Pacific Wings to its 5 strongest bullets.** Two tight projects read
      far better than one sprawling project plus a new one.
- [ ] Add proof points to `career-ops/article-digest.md`
- [ ] Book AWS Certified Machine Learning Engineer Associate (MLA-C01). It
      removes the "claims AWS, cannot name a service" doubt.
- [ ] Optional: fill `target_roles` in `career-ops/config/profile.yml` and
      retune the archetypes in `modes/_profile.md` toward financial services.
      Both are still on shipped template defaults.

---

## References

- [CommBank fraud agent: 80M signals/day, human-in-the-loop](https://www.commbank.com.au/articles/newsroom/2026/04/ai-agent-spots-fraud-in-real-time.html)
- [APRA AI risk management letter, 30 April 2026](https://www.licentium.io/post/apra-letter-ai-risk-management-governance-30-april-2026)
- [APRA CPS 230](https://www.apra.gov.au/standards/cps-230)
- [AWS serverless real-time fraud detection](https://aws.amazon.com/blogs/machine-learning/real-time-fraud-detection-using-aws-serverless-and-machine-learning-services)
- [Bedrock model availability, ap-southeast-2](https://modelavailability.com/platforms/aws/regions/ap-southeast-2)
- [Kinesis Data Streams pricing](https://aws.amazon.com/kinesis/data-streams/pricing/)
- [IEEE-CIS Fraud Detection dataset](https://www.kaggle.com/competitions/ieee-fraud-detection/data)
