"""ChargeHub access code v1 — reference implementation.

Normative spec: ../../docs/ACCESS_CODE_FORMAT.md
Conformance vectors: ../../vectors/access-code.vectors.json

This file and its C counterpart (`../c/chargehub_codes.c`) must agree on every
vector. `tools/conformance.py` proves it in CI. If you change one, change both.

Dependency-free and stdlib-only on purpose, so the backend can vendor it
verbatim rather than reimplementing it — a reimplementation is exactly how the
two sides drift apart.
"""

from __future__ import annotations

import secrets

CODE_LENGTH = 10
PAYLOAD_LENGTH = 9


class InvalidCode(ValueError):
    """Raised when a value is not a well-formed access code."""


def _digits(value: str) -> list[int]:
    if not value.isdigit() or not value.isascii():
        raise InvalidCode("access codes contain ASCII digits only")
    return [ord(c) - 48 for c in value]


def check_digit(payload: str) -> str:
    """Luhn mod-10 check digit for a 9-digit payload."""
    if len(payload) != PAYLOAD_LENGTH:
        raise InvalidCode(f"payload must be {PAYLOAD_LENGTH} digits, got {len(payload)}")

    total = 0
    for i, digit in enumerate(reversed(_digits(payload))):
        if i % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - total % 10) % 10)


def is_valid(code: str) -> bool:
    """Whether `code` is a well-formed 10-digit code with a correct check digit.

    Validity here means well-formed, nothing more. It says nothing about whether
    the code was ever issued, is within its time window, or is still unredeemed.
    Never treat a True from this function as authorisation.
    """
    if len(code) != CODE_LENGTH:
        return False
    try:
        digits = _digits(code)
    except InvalidCode:
        return False

    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def generate() -> str:
    """A new access code using a cryptographically secure source.

    `secrets`, never `random` — the prototype's PHP used an unseeded weak PRNG,
    which made issued codes predictable from each other.

    Uniqueness against live codes is the caller's job; see spec §7.
    """
    payload = "".join(str(secrets.randbelow(10)) for _ in range(PAYLOAD_LENGTH))
    return payload + check_digit(payload)


def normalise(raw: str) -> str:
    """Strip presentation formatting and validate.

    Accepts the `XXXXX-XXXXX` display form, and tolerates surrounding whitespace
    from copy-paste. Raises InvalidCode if what remains is not well-formed.
    """
    cleaned = raw.strip().replace("-", "").replace(" ", "")
    if not is_valid(cleaned):
        raise InvalidCode("not a valid ChargeHub access code")
    return cleaned


def format_for_display(code: str) -> str:
    """Render as `XXXXX-XXXXX`. Presentation only — never store this form."""
    if len(code) != CODE_LENGTH:
        raise InvalidCode(f"code must be {CODE_LENGTH} digits")
    return f"{code[:5]}-{code[5:]}"
