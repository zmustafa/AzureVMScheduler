"""Writing audit entries.

Extracted from main so feature routers can record their own actions without importing the whole
application and creating an import cycle.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog, User


def audit(db: AsyncSession, user: User | None, action: str, target_type: str, target_id: str | None, detail: dict[str, Any] | None = None) -> None:
    db.add(AuditLog(
        actor_id=user.id if user else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=json.dumps(detail or {}, separators=(",", ":")),
    ))
