"""The parity boundary.

Imported by BOTH train.py (offline, replaying history) and handler.py (online,
against live DynamoDB state). Every feature the model sees is built here and
nowhere else. tests/test_parity.py exists to keep that true.

No pandas in this module: handler.py runs it inside a Lambda container and
pandas is ~50MB of cold start for zero benefit on a single dict.
"""

import math

# Transaction fields available at scoring time. IEEE-CIS V1-V339, C1-C14 and
# D1-D15 are deliberately absent: they are Vesta's own precomputed aggregates
# and cannot be recomputed from one incoming transaction, so a model trained on
# them cannot be served in real time. We recompute our own velocity signals
# instead (see velocity_features).
NUMERIC_FIELDS = [
    "TransactionAmt",
    "card1",
    "card2",
    "card3",
    "card5",
    "addr1",
    "addr2",
    "dist1",
    "dist2",
]

CATEGORICAL_FIELDS = [
    "ProductCD",
    "card4",
    "card6",
    "P_emaildomain",
    "R_emaildomain",
    "DeviceType",
    "DeviceInfo",
    "M1",
    "M2",
    "M3",
    "M4",
    "M5",
    "M6",
    "M7",
    "M8",
    "M9",
]

VELOCITY_FIELDS = [
    "vel_count_1h",
    "vel_count_24h",
    "vel_amt_sum_24h",
    "vel_amt_max_24h",
    "vel_amt_ratio_24h",
    "vel_secs_since_last",
    "vel_distinct_addr_24h",
]

DERIVED_FIELDS = ["amt_log", "hour_of_day", "day_of_week"]

# Fixed column order. The vector is positional, so this list IS the contract
# between the trained model and the Lambda. train.py writes it to
# artifacts/feature_order.json; handler.py asserts the loaded copy matches.
FEATURE_ORDER = (
    DERIVED_FIELDS + NUMERIC_FIELDS + CATEGORICAL_FIELDS + VELOCITY_FIELDS
)

MISSING = -999.0

# ponytail: rolling window capped at 50 events / 24h to bound the DynamoDB item.
# A card doing >50 txns/day gets a truncated velocity view. Raise the cap, or
# move to a DynamoDB counter per hour bucket, if that turns out to matter.
STATE_MAX_EVENTS = 50
WINDOW_24H = 86400
WINDOW_1H = 3600


def _num(value):
    """Coerce a raw CSV/JSON field to float, mapping blanks and NaN to MISSING."""
    if value is None or value == "":
        return MISSING
    try:
        f = float(value)
    except (TypeError, ValueError):
        return MISSING
    return MISSING if math.isnan(f) else f


def card_id(txn):
    """Partition key for velocity state.

    card1 is the closest thing IEEE-CIS gives us to a card identifier. It is not
    unique per physical card, so velocity here is 'this card bucket' rather than
    'this card'. Named honestly so nobody reads more into the feature than is
    there.
    """
    raw = txn.get("card1")
    return f"card1:{raw}" if raw not in (None, "") else "card1:unknown"


def empty_state():
    return {"events": []}


def event_features(txn):
    """Features carried by the transaction itself."""
    dt = _num(txn.get("TransactionDT"))
    amt = _num(txn.get("TransactionAmt"))

    out = {
        "amt_log": math.log1p(amt) if amt > 0 else MISSING,
        # TransactionDT is seconds from an unstated reference epoch. Absolute
        # calendar time is unknowable, but time-of-day and day-of-week cycles
        # are still recoverable and both matter for fraud.
        "hour_of_day": (dt % WINDOW_24H) // 3600 if dt != MISSING else MISSING,
        "day_of_week": (dt // WINDOW_24H) % 7 if dt != MISSING else MISSING,
    }
    for f in NUMERIC_FIELDS:
        out[f] = _num(txn.get(f))
    for f in CATEGORICAL_FIELDS:
        raw = txn.get(f)
        out[f] = "" if raw is None else str(raw).strip().lower()
    return out


def velocity_features(txn, state):
    """Features derived from this card's recent history.

    Only events strictly BEFORE the current transaction are in state, so there
    is no label or future leakage here by construction: train.py appends to
    state only after scoring, exactly as handler.py does.
    """
    dt = _num(txn.get("TransactionDT"))
    amt = _num(txn.get("TransactionAmt"))
    events = state.get("events", [])

    if dt == MISSING or not events:
        return {
            "vel_count_1h": 0.0,
            "vel_count_24h": 0.0,
            "vel_amt_sum_24h": 0.0,
            "vel_amt_max_24h": 0.0,
            "vel_amt_ratio_24h": MISSING,
            "vel_secs_since_last": MISSING,
            "vel_distinct_addr_24h": 0.0,
        }

    recent_24h = [e for e in events if dt - e["dt"] <= WINDOW_24H]
    recent_1h = [e for e in recent_24h if dt - e["dt"] <= WINDOW_1H]
    amounts = [e["amt"] for e in recent_24h] or [0.0]
    amt_sum = sum(amounts)
    amt_max = max(amounts)

    return {
        "vel_count_1h": float(len(recent_1h)),
        "vel_count_24h": float(len(recent_24h)),
        "vel_amt_sum_24h": amt_sum,
        "vel_amt_max_24h": amt_max,
        # "is this charge unusual for this card lately" in one number
        "vel_amt_ratio_24h": (amt / amt_max) if (amt_max > 0 and amt != MISSING) else MISSING,
        "vel_secs_since_last": float(dt - max(e["dt"] for e in events)),
        "vel_distinct_addr_24h": float(len({e["addr"] for e in recent_24h})),
    }


def build_vector(txn, state, vocab):
    """Return the positional float vector the model consumes.

    vocab maps categorical field -> {value: ordinal}, built once by train.py.
    Unseen categories score MISSING rather than colliding onto a real ordinal.
    """
    feats = event_features(txn)
    feats.update(velocity_features(txn, state))

    vector = []
    for name in FEATURE_ORDER:
        value = feats[name]
        if name in CATEGORICAL_FIELDS:
            vector.append(float(vocab.get(name, {}).get(value, MISSING)))
        else:
            vector.append(float(value))
    return vector


def update_state(txn, state):
    """State AFTER this transaction. Called post-scoring in both paths."""
    dt = _num(txn.get("TransactionDT"))
    if dt == MISSING:
        return state

    events = list(state.get("events", []))
    events.append({
        "dt": dt,
        "amt": max(_num(txn.get("TransactionAmt")), 0.0),
        "addr": str(txn.get("addr1") or ""),
    })
    events = [e for e in events if dt - e["dt"] <= WINDOW_24H]
    events.sort(key=lambda e: e["dt"])
    return {"events": events[-STATE_MAX_EVENTS:]}
