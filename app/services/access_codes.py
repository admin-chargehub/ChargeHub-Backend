"""Issuing and reading back station access codes.

Format, entropy and generation rules are specified in ChargeHub-Contracts
(`docs/ACCESS_CODE_FORMAT.md`). The generator itself is vendored verbatim at
`app/vendor/chargehub_codes.py` — do not reimplement it here, or the platform
and the firmware will drift apart on the one value they both have to agree on.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import AccessCode, Booking
from app.models.base import utcnow
from app.vendor import chargehub_codes as codes

#: Attempts to find an unused code before giving up. At 10^9 and a few thousand
#: live codes a collision is vanishingly unlikely, but "unlikely" is not "never"
#: and silently issuing a duplicate would let one customer open another's outlet.
MAX_GENERATION_ATTEMPTS = 8


def _fernet() -> Fernet:
    """Encryption key for codes at rest, derived from SECRET_KEY.

    Deriving rather than configuring a second secret keeps deployment simple,
    at the cost of coupling the two: rotating SECRET_KEY makes existing codes
    unreadable. That is acceptable while codes live hours, and is why
    `reveal()` treats a decryption failure as "not available" rather than an
    error. A production deployment should move this to a dedicated KMS key.
    """
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def hash_code(code: str) -> str:
    """Lowercase hex SHA-256 of a normalised code — what the station allowlist carries."""
    return hashlib.sha256(codes.normalise(code).encode("utf-8")).hexdigest()


async def issue(db: AsyncSession, booking: Booking) -> tuple[AccessCode, str]:
    """Mint a code for `booking`. Returns the row and the plaintext.

    The plaintext is returned, never logged, and never stored unencrypted. The
    caller hands it to the customer exactly once in the payment response; after
    that it is only available through `reveal()`.
    """
    for _ in range(MAX_GENERATION_ATTEMPTS):
        plaintext = codes.generate()
        digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

        clash = (
            await db.execute(
                select(AccessCode.id)
                .where(AccessCode.code_sha256 == digest)
                .where(AccessCode.not_after > utcnow())
            )
        ).scalar_one_or_none()
        if clash is not None:
            continue

        record = AccessCode(
            booking_id=booking.id,
            code_sha256=digest,
            code_encrypted=_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8"),
            not_before=booking.starts_at,
            not_after=booking.ends_at,
            single_use=True,
        )
        db.add(record)
        await db.flush()
        return record, plaintext

    raise RuntimeError("could not generate a unique access code")


def reveal(record: AccessCode, at: datetime | None = None) -> str | None:
    """The plaintext code, or None once it has expired or cannot be decrypted.

    Refusing to return an expired code is deliberate: the booking screen should
    stop showing a code that no longer opens anything.
    """
    now = at or utcnow()
    if record.revoked_at is not None or record.not_after <= now:
        return None
    try:
        return _fernet().decrypt(record.code_encrypted.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # Key rotated, or the row predates the current SECRET_KEY.
        return None


async def for_booking(db: AsyncSession, booking_id) -> AccessCode | None:
    return (
        await db.execute(
            select(AccessCode)
            .where(AccessCode.booking_id == booking_id)
            .where(AccessCode.revoked_at.is_(None))
            .order_by(AccessCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
