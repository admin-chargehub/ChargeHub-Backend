"""Housekeeping endpoints, driven by cron rather than by a user.

Two sweepers existed in the services layer from the day they were written and
were called from nowhere:

* `bookings.sweep_pending` — cancels bookings that were never paid for, which
  otherwise keep conflicting with the PostgreSQL exclusion constraint on
  write even though the query-level TTL already hides them from availability;
* `sessions.sweep_expired` — ends sessions whose paid window has closed,
  which otherwise stay ACTIVE until the customer happens to reopen their own
  session screen.

Writing a sweeper and never scheduling it is the same as not having one, and
the second case is exactly what made the operator console show sessions
running days after they finished.

Deliberately not a background task inside the app process: with more than one
worker every worker would run it, and a restart would silently stop it with
nothing reporting that housekeeping had died. An external schedule that can
fail loudly is the right shape, and it is the same shape in production
(EventBridge, a Kubernetes CronJob) as it is locally.

    curl -X POST localhost:8000/api/v1/internal/sweep -H "X-Internal-Token: ..."
"""

import hmac
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status

from app.api.deps import DbSession
from app.core.config import settings
from app.services import bookings as booking_service
from app.services import sessions as session_service

router = APIRouter(prefix="/internal", tags=["internal"])


def _check(token: str | None) -> None:
    """Shared-secret auth.

    Not JWT: the caller is a cron job, not a person, and giving a scheduler a
    user account means a credential that can do everything that user can.
    `compare_digest` because a plain `==` on a secret leaks its prefix through
    timing.
    """
    expected = settings.internal_token
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "INTERNAL_TOKEN is not configured, so housekeeping is disabled.",
        )
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad internal token")


@router.post("/sweep")
async def sweep(
    db: DbSession,
    x_internal_token: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    """Run both sweepers. Idempotent, so a missed run only costs lateness."""
    _check(x_internal_token)
    return {
        "bookings_cancelled": await booking_service.sweep_pending(db),
        "sessions_ended": await session_service.sweep_expired(db),
    }
