"""FR-8 — the audit log.

Every dispatch, every override, every import, every metric computation.
"Every displayed metric must trace to source data and survive the audit log."
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog

# Actions.  Keep this list closed — a free-text action string is an audit log
# nobody can query.
DISPATCH = "DISPATCH"
DISPATCH_OVERRIDE = "DISPATCH_OVERRIDE"
SMART_DECISION_APPLIED = "SMART_DECISION_APPLIED"
IMPORT_DMS = "IMPORT_DMS"
IMPORT_TIME_CLOCK = "IMPORT_TIME_CLOCK"
METRICS_COMPUTE = "METRICS_COMPUTE"
TECH_CREATED = "TECH_CREATED"
TECH_UPDATED = "TECH_UPDATED"
TECH_DELETED = "TECH_DELETED"
SETTINGS_UPDATED = "SETTINGS_UPDATED"
OPCODE_MAP_UPDATED = "OPCODE_MAP_UPDATED"


async def record(
    session: AsyncSession,
    *,
    dealer_id: uuid.UUID,
    actor: Optional[uuid.UUID],
    action: str,
    entity: str,
    entity_id: Optional[uuid.UUID] = None,
    payload: Optional[dict[str, Any]] = None,
) -> AuditLog:
    entry = AuditLog(
        dealer_id=dealer_id,
        actor=actor,
        action=action,
        entity=entity,
        entity_id=entity_id,
        payload=payload or {},
    )
    session.add(entry)
    return entry
