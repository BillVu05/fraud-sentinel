"""Lambda entrypoint: Kinesis record -> score + reason codes -> DynamoDB + S3.

Model, vocab and feature order are baked into the container image at build time,
so a running Lambda can never drift onto a different model than the one its
decisions are stamped with.
"""

import base64
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path

import boto3
import numpy as np
import xgboost as xgb

import features

ARTIFACTS = Path(os.environ.get("ARTIFACTS_DIR", "/var/task/artifacts"))
TABLE_NAME = os.environ["STATE_TABLE"]
BUCKET = os.environ["DECISION_BUCKET"]
MODEL_VERSION = os.environ.get("MODEL_VERSION", "dev")
THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "0.9"))

# Cold-start work, deliberately at import time so it is paid once per container
# rather than once per batch.
_booster = xgb.Booster()
_booster.load_model(str(ARTIFACTS / "model.json"))
_vocab = json.loads((ARTIFACTS / "vocab.json").read_text())
_order = json.loads((ARTIFACTS / "feature_order.json").read_text())

# The model is positional. If the image was built from a features.py that
# disagrees with the trained model's column order, every score is garbage and we
# would rather fail at cold start than serve nonsense.
assert _order == features.FEATURE_ORDER, "feature order drift between model and code"

_ddb = boto3.resource("dynamodb").Table(TABLE_NAME)
_s3 = boto3.client("s3")


def _to_ddb(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_ddb(v) for v in obj]
    return obj


def _from_ddb(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_ddb(v) for v in obj]
    return obj


def reason_codes(contribs, vector, top_n=3):
    """Top contributions pushing this transaction TOWARD fraud.

    xgboost's pred_contribs gives exact TreeSHAP values natively, so the `shap`
    package (and numba, and llvmlite) stays out of the image entirely.
    Last element is the bias term, not a feature.
    """
    feature_contribs = contribs[:-1]
    ranked = np.argsort(-feature_contribs)[:top_n]
    return [
        {
            "feature": features.FEATURE_ORDER[i],
            "value": None if vector[i] == features.MISSING else round(float(vector[i]), 4),
            "contribution": round(float(feature_contribs[i]), 5),
        }
        for i in ranked
        if feature_contribs[i] > 0
    ]


def score_one(txn):
    cid = features.card_id(txn)
    item = _ddb.get_item(Key={"card_id": cid}).get("Item")
    state = _from_ddb(item).get("state", features.empty_state()) if item else features.empty_state()

    vector = features.build_vector(txn, state, _vocab)
    matrix = xgb.DMatrix(np.array([vector], dtype=np.float32), missing=features.MISSING)
    score = float(_booster.predict(matrix)[0])
    contribs = _booster.predict(matrix, pred_contribs=True)[0]

    _ddb.put_item(Item={
        "card_id": cid,
        "state": _to_ddb(features.update_state(txn, state)),
        # Velocity state is worthless once it ages past the 24h window. TTL
        # reaps it so the table does not grow without bound.
        "expires_at": int(time.time()) + 2 * features.WINDOW_24H,
    })

    return {
        "transaction_id": txn.get("TransactionID"),
        "card_id": cid,
        "score": round(score, 6),
        "decision": "ALERT" if score >= THRESHOLD else "PASS",
        "threshold": THRESHOLD,
        "reason_codes": reason_codes(contribs, vector),
        "model_version": MODEL_VERSION,
        # the exact inputs behind this decision, so an alert stays explainable
        # after the fact without re-reading state that has since moved on
        "feature_snapshot": dict(zip(features.FEATURE_ORDER, [round(float(v), 4) for v in vector])),
    }


def lambda_handler(event, context):
    t0 = time.perf_counter()
    decisions, failures = [], []

    for record in event.get("Records", []):
        seq = record["kinesis"]["sequenceNumber"]
        try:
            txn = json.loads(base64.b64decode(record["kinesis"]["data"]))
            decisions.append(score_one(txn))
        except Exception as exc:  # one poison record must not drop the batch
            print(f"ERROR seq={seq}: {type(exc).__name__}: {exc}")
            failures.append({"itemIdentifier": seq})

    if decisions:
        _s3.put_object(
            Bucket=BUCKET,
            Key=f"decisions/model={MODEL_VERSION}/{int(time.time())}-{uuid.uuid4().hex[:8]}.jsonl",
            Body="\n".join(json.dumps(d) for d in decisions).encode(),
        )

    elapsed_ms = (time.perf_counter() - t0) * 1000
    alerts = sum(1 for d in decisions if d["decision"] == "ALERT")
    # Picked up by a CloudWatch metric filter; see infra/main.tf.
    print(json.dumps({
        "scored": len(decisions), "alerts": alerts, "failed": len(failures),
        "batch_ms": round(elapsed_ms, 2),
        "per_txn_ms": round(elapsed_ms / max(len(decisions), 1), 3),
    }))

    # Partial batch response: only the bad records get retried, not the batch.
    return {"batchItemFailures": failures}
