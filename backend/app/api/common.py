from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..core.security import Principal
from ..models import User
from ..services.runtime import Actor


def actor_of(p: Principal, kind: str | None = None) -> Actor:
    return Actor(p.id, p.role, p.org_id, kind or ("mcp" if p.via == "token" else "user"), p.user.username)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def username(db: Session, user_id: str | None, default: str = "—") -> str:
    if not user_id:
        return default
    u = db.get(User, user_id)
    return u.username if u else default


def like_pattern(q: str) -> str:
    """A case-insensitive 'contains' pattern with LIKE wildcards in the user's text escaped (use with escape="\\")."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
