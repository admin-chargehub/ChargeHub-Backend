"""Payment providers.

Only a mock exists today. The point of the `PaymentProvider` protocol is that
the booking state machine is written against the shape a real gateway needs —
an initialise step, a reference, and an out-of-band confirmation — so adding
Paystack later is a new implementation rather than a reshaping of the flow.

What the mock deliberately keeps:

* a `reference` that the confirmation is keyed on, because real confirmation
  arrives by webhook and has to be matched to something;
* confirmation as a **separate call** from initialisation, so the code cannot
  quietly come to depend on payment being synchronous — with Paystack it is not;
* idempotent confirmation, because gateways retry.

What it skips: an authorization URL, card data, signatures, and webhooks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    reference: str
    amount_kobo: int
    #: Where the customer is sent to pay. None for the mock, which has no
    #: hosted page — the client treats None as "nothing to redirect to".
    authorization_url: str | None


@dataclass(frozen=True, slots=True)
class PaymentResult:
    reference: str
    paid: bool
    provider: str
    #: Gateway-side identifier, for reconciliation.
    provider_ref: str | None = None
    message: str | None = None


class PaymentProvider(Protocol):
    name: str

    def initialize(self, *, amount_kobo: int, email: str, booking_id: uuid.UUID) -> PaymentIntent:
        ...

    def verify(self, reference: str) -> PaymentResult:
        ...


class MockProvider:
    """Always succeeds, immediately.

    For demos and local development only. `get_provider()` refuses to hand this
    out in production — a payment system that cannot fail is worse than none,
    because every downstream path that handles failure goes untested.
    """

    name = "mock"

    def initialize(self, *, amount_kobo: int, email: str, booking_id: uuid.UUID) -> PaymentIntent:
        return PaymentIntent(
            reference=f"mock_{uuid.uuid4().hex[:20]}",
            amount_kobo=amount_kobo,
            authorization_url=None,
        )

    def verify(self, reference: str) -> PaymentResult:
        return PaymentResult(
            reference=reference,
            paid=True,
            provider=self.name,
            provider_ref=reference,
            message="Mock payment accepted",
        )


def get_provider() -> PaymentProvider:
    if settings.is_production:
        raise RuntimeError(
            "No real payment provider is configured. Refusing to run the mock "
            "provider in production — it marks every booking as paid."
        )
    return MockProvider()
