"""Append-only record of operator actions."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditAction, AuditEvent, User


async def record(
    db: AsyncSession,
    *,
    actor: User,
    action: AuditAction,
    target_type: str,
    summary: str,
    target_id: uuid.UUID | None = None,
    target_label: str | None = None,
) -> AuditEvent:
    """Add an audit row to the caller's transaction.

    Deliberately does not commit. The row must land in the same transaction as
    the change it describes, or a rollback leaves the trail claiming something
    happened that did not — which is worse than no trail, because it is
    believed.
    """
    event = AuditEvent(
        actor_id=actor.id,
        actor_email=actor.email,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        summary=summary,
    )
    db.add(event)
    return event
