"""The one check that must never fail.

If the vector built offline by train.py differs from the vector built online by
handler.py for the same transaction, every number in this repo is a lie. The
realistic ways that happens are not "someone edited the maths": they are the
DynamoDB round trip turning floats into Decimals, and the JSON round trip
turning the vocab into something subtly different.

Run: pytest tests/ -q
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import features  # noqa: E402

TXN_A = {
    "TransactionID": "1",
    "TransactionDT": "86400",
    "TransactionAmt": "59.00",
    "ProductCD": "W",
    "card1": "13926",
    "card2": "361",
    "card3": "150",
    "card4": "discover",
    "card5": "142",
    "card6": "credit",
    "addr1": "315",
    "addr2": "87",
    "dist1": "19",
    "dist2": "",
    "P_emaildomain": "gmail.com",
    "R_emaildomain": "",
    "DeviceType": "desktop",
    "DeviceInfo": "Windows",
    "M1": "T", "M2": "T", "M3": "T", "M4": "M0",
    "M5": "F", "M6": "T", "M7": "", "M8": "", "M9": "",
}

TXN_B = {**TXN_A, "TransactionID": "2", "TransactionDT": "88000", "TransactionAmt": "820.00", "addr1": "299"}
TXN_C = {**TXN_A, "TransactionID": "3", "TransactionDT": "89000", "TransactionAmt": "75.50"}

VOCAB = {
    "ProductCD": {"w": 0, "c": 1, "h": 2},
    "card4": {"discover": 0, "visa": 1, "mastercard": 2},
    "card6": {"credit": 0, "debit": 1},
    "P_emaildomain": {"gmail.com": 0, "yahoo.com": 1},
    "R_emaildomain": {"": 0},
    "DeviceType": {"desktop": 0, "mobile": 1},
    "DeviceInfo": {"windows": 0, "ios device": 1},
    "M1": {"t": 0, "f": 1}, "M2": {"t": 0, "f": 1}, "M3": {"t": 0, "f": 1},
    "M4": {"m0": 0, "m1": 1, "m2": 2}, "M5": {"t": 0, "f": 1}, "M6": {"t": 0, "f": 1},
    "M7": {"": 0}, "M8": {"": 0}, "M9": {"": 0},
}


def to_dynamo(obj):
    """Mimic what boto3's DynamoDB resource does to numbers on write/read."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, int) and not isinstance(obj, bool):
        return Decimal(obj)
    if isinstance(obj, dict):
        return {k: to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_dynamo(v) for v in obj]
    return obj


def from_dynamo(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_dynamo(v) for v in obj]
    return obj


def offline_state(txns):
    """How train.py materialises state: replay in time order, update after each."""
    state = features.empty_state()
    for t in txns:
        state = features.update_state(t, state)
    return state


def test_vector_length_matches_declared_order():
    v = features.build_vector(TXN_A, features.empty_state(), VOCAB)
    assert len(v) == len(features.FEATURE_ORDER)
    assert len(set(features.FEATURE_ORDER)) == len(features.FEATURE_ORDER), "duplicate feature name"


def test_offline_and_online_vectors_are_identical():
    """train.py holds state in memory. handler.py reads it back from DynamoDB."""
    state = offline_state([TXN_A, TXN_B])

    offline_vec = features.build_vector(TXN_C, state, VOCAB)

    round_tripped = from_dynamo(to_dynamo(state))
    vocab_round_tripped = json.loads(json.dumps(VOCAB))
    online_vec = features.build_vector(TXN_C, round_tripped, vocab_round_tripped)

    assert offline_vec == online_vec


def test_current_transaction_is_not_in_its_own_velocity():
    """Leakage guard: state must be updated AFTER scoring, never before."""
    state = offline_state([TXN_A, TXN_B])
    vel = features.velocity_features(TXN_C, state)
    assert vel["vel_count_24h"] == 2.0, "TXN_C counted itself"

    after = features.update_state(TXN_C, state)
    assert features.velocity_features(TXN_C, after)["vel_count_24h"] == 3.0


def test_unseen_category_does_not_collide_with_a_real_ordinal():
    odd = {**TXN_A, "card4": "amex-not-in-vocab"}
    vec = features.build_vector(odd, features.empty_state(), VOCAB)
    assert vec[features.FEATURE_ORDER.index("card4")] == features.MISSING


def test_state_stays_bounded():
    txns = [{**TXN_A, "TransactionDT": str(86400 + i * 10)} for i in range(200)]
    state = offline_state(txns)
    assert len(state["events"]) <= features.STATE_MAX_EVENTS


def test_blank_and_missing_fields_do_not_crash():
    sparse = {"TransactionID": "9", "TransactionDT": "86400", "TransactionAmt": ""}
    vec = features.build_vector(sparse, features.empty_state(), VOCAB)
    assert len(vec) == len(features.FEATURE_ORDER)
    assert all(isinstance(x, float) for x in vec)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
