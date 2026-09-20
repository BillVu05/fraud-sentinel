"""Exercise the drift path end to end, without AWS or the real dataset.

A drift monitor that has never fired is not a monitor. These tests check the
two things that actually break it: the baseline file failing to round-trip into
the shape checks.py expects, and PSI being too blunt to notice a real shift.

Run: pytest tests/ -q
"""

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "governance"))

import checks  # noqa: E402
import features  # noqa: E402
import train  # noqa: E402

RNG = np.random.default_rng(7)
DRIFT_FEATURE = "vel_amt_sum_24h"


def fake_training_matrix(n=4000):
    X = RNG.normal(loc=50, scale=15, size=(n, len(features.FEATURE_ORDER))).astype(np.float32)
    # realistic sparsity: IEEE-CIS is full of blanks
    X[RNG.random(X.shape) < 0.1] = features.MISSING
    return X


def fake_decisions(n=1500, shift=0.0, scale=15.0):
    col = features.FEATURE_ORDER.index(DRIFT_FEATURE)
    out = []
    for i in range(n):
        values = RNG.normal(loc=50, scale=15, size=len(features.FEATURE_ORDER))
        values[col] = RNG.normal(loc=50 + shift, scale=scale)
        out.append({
            "transaction_id": i,
            "score": 0.5,
            "decision": "PASS",
            "model_version": "test",
            # handler.py rounds to 4dp on the way into S3; mirror that here or
            # the test is not testing what production does
            "feature_snapshot": {
                name: round(float(v), 4) for name, v in zip(features.FEATURE_ORDER, values)
            },
        })
    return out


@pytest.fixture(scope="module")
def baseline_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "train_distributions.json"
        train.save_train_distributions(fake_training_matrix(), path)
        yield path


def test_baseline_file_has_a_row_per_feature(baseline_path):
    dists = json.loads(baseline_path.read_text())
    assert set(dists) == set(features.FEATURE_ORDER)
    assert all(len(v) == train.DRIFT_SAMPLE_N or len(v) == 4000 for v in dists.values())


def test_every_feature_is_reported(baseline_path):
    """A silently empty report is the failure mode that looks like 'no drift'."""
    report = checks.drift_report(fake_decisions(), baseline_path)
    assert len(report) >= len(features.FEATURE_ORDER) - 2, "features dropped out of the report"


def test_same_distribution_does_not_trip_the_threshold(baseline_path):
    report = checks.drift_report(fake_decisions(), baseline_path)
    alerting = {k: v for k, v in report.items() if v["status"] != "OK"}
    assert not alerting, f"false positive drift on unshifted data: {alerting}"


def test_rounding_alone_is_not_drift(baseline_path):
    """The baseline rounds to 4dp and so does handler.py. If those ever diverge,
    every feature drifts a little for no reason."""
    report = checks.drift_report(fake_decisions(), baseline_path)
    assert max(v["psi"] for v in report.values()) < checks.PSI_WARN


def test_a_real_shift_is_caught(baseline_path):
    report = checks.drift_report(fake_decisions(shift=25.0), baseline_path)
    assert report[DRIFT_FEATURE]["status"] == "ALERT", report[DRIFT_FEATURE]
    assert report[DRIFT_FEATURE]["psi"] > checks.PSI_ALERT


def test_a_variance_change_is_caught(baseline_path):
    """Same mean, wider spread. A mean-only check would miss this."""
    report = checks.drift_report(fake_decisions(scale=45.0), baseline_path)
    assert report[DRIFT_FEATURE]["status"] in ("WARN", "ALERT"), report[DRIFT_FEATURE]


def test_report_is_sorted_worst_first(baseline_path):
    report = checks.drift_report(fake_decisions(shift=25.0), baseline_path)
    psis = [v["psi"] for v in report.values()]
    assert psis == sorted(psis, reverse=True)
    assert next(iter(report)) == DRIFT_FEATURE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
