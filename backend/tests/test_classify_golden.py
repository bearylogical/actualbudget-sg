"""FakeActual's duplicate classification against the golden cases dedup.js runs too
(actual-bridge/test/fixtures/classify.json) — the port and the real one cannot drift apart."""
import json
from pathlib import Path

import pytest

from fake_actual import classify

GOLDEN = json.loads((Path(__file__).resolve().parents[2] / "actual-bridge/test/fixtures/classify.json").read_text())


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=[c["name"] for c in GOLDEN["cases"]])
def test_classify_matches_dedup_js(case):
    new, dup, verify = classify(case["incoming"], case["existing"])
    assert {"clearly_new": [t["imported_id"] for t in new],
            "clearly_dup": [t["imported_id"] for t in dup],
            "needs_verify": [[t["imported_id"], t["reason"]] for t in verify]} == case["expect"]
