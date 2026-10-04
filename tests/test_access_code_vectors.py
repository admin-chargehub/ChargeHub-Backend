"""Hold the vendored access-code module to the contracts test vectors.

`app/vendor/chargehub_codes.py` is a verbatim copy from ChargeHub-Contracts,
where CI already checks it against the C implementation the firmware uses. This
test guards the copy: if someone edits the vendored file, or re-copies a version
that disagrees with the vectors, the platform silently stops agreeing with the
stations on the one value both sides must share. That is precisely the class of
bug the contracts repo exists to prevent, so it fails here instead.

Skips rather than fails when the contracts repo is not checked out alongside
this one — CI for the backend should not require a sibling clone.
"""

import json
from pathlib import Path

import pytest

from app.vendor import chargehub_codes as codes

VECTORS = (
    Path(__file__).resolve().parents[2]
    / "Chargehub Contracts"
    / "vectors"
    / "access-code.vectors.json"
)

pytestmark = pytest.mark.skipif(
    not VECTORS.exists(), reason=f"contracts vectors not found at {VECTORS}"
)


def _vectors() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_check_digit_vectors():
    for case in _vectors()["check_digit"]:
        assert codes.check_digit(case["payload"]) == case["expected"], case


def test_is_valid_vectors():
    for case in _vectors()["is_valid"]:
        assert codes.is_valid(case["code"]) is case["expected"], case


def test_normalise_vectors():
    for case in _vectors()["normalise"]:
        if case["expected"] is None:
            with pytest.raises(codes.InvalidCode):
                codes.normalise(case["raw"])
        else:
            assert codes.normalise(case["raw"]) == case["expected"], case


def test_generated_codes_are_always_valid():
    for _ in range(300):
        code = codes.generate()
        assert len(code) == codes.CODE_LENGTH
        assert codes.is_valid(code)
