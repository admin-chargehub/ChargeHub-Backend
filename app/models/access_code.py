import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin


class AccessCode(UUIDMixin, TimestampMixin, Base):
    """The code a customer types on the station keypad.

    Format and rules live in ChargeHub-Contracts (`docs/ACCESS_CODE_FORMAT.md`);
    the generator is vendored at `app/vendor/chargehub_codes.py`.

    Two representations are stored, deliberately:

    * ``code_sha256`` is what goes to the station in the allowlist, and what a
      redemption is matched against. The station never receives a plaintext
      code — the 2016 prototype wrote every live PIN in clear text to an
      unencrypted SD card, so lifting the card yielded every active code.
    * ``code_encrypted`` exists only so the app can show the customer their own
      code again and resend it. The format spec §7 says to keep a hash and drop
      the plaintext, which the "Send PIN" button in the design makes impossible;
      encrypting at rest is the compromise, and it needs a contracts PR to make
      official rather than being a silent deviation.
    """

    __tablename__ = "access_codes"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: Lowercase hex SHA-256 of the normalised 10-digit code.
    code_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    #: Fernet token. Readable only by the platform, and only until not_after.
    code_encrypted: Mapped[str] = mapped_column(String(255), nullable=False)

    not_before: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    not_after: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    single_use: Mapped[bool] = mapped_column(default=True, nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: Failed keypad attempts against this code, for the rate limit in spec §5.
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:
        return f"<AccessCode booking={self.booking_id} …{self.code_sha256[-6:]}>"
