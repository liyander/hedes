from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.db import new_id
from ..models import Alert, AuditLog


def record(db: Session, org_id: str | None, actor, action: str, target: str = "", **meta) -> None:
    """actor: a Principal, a User, a string label (system actors such as 'ai-agent'), or None."""
    actor_id = getattr(actor, "id", None) if not isinstance(actor, str) else None
    user = getattr(actor, "user", actor)
    label = actor if isinstance(actor, str) else (getattr(user, "username", "") or "system")
    db.add(AuditLog(organization_id=org_id, actor_id=actor_id, actor_label=label, action=action, target=target, meta=meta))


def alert(db: Session, org_id: str, kind: str, message: str, severity: str = "medium",
          integration_id: str | None = None, link: str = "") -> None:
    alert_id = new_id()
    db.add(Alert(id=alert_id, organization_id=org_id, kind=kind, message=message, severity=severity,
                 integration_id=integration_id, link=link))
    # Delivered to Slack / email / WhatsApp only after the surrounding transaction commits (see services/notify.py).
    db.info.setdefault("outbound_alerts", []).append({"id": alert_id, "org_id": org_id, "kind": kind, "severity": severity, "message": message,
                                                      "link": link, "integration_id": integration_id})
