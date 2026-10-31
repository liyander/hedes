"""Passwords, session tokens, API tokens and role-based access control."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db, utcnow

COOKIE = "integra_session"
ROLE_RANK = {"user": 1, "admin": 2, "owner": 3}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_session(user_id: str, org_id: str) -> str:
    s = get_settings()
    now = utcnow()
    payload = {"sub": user_id, "org": org_id, "iat": now, "exp": now + timedelta(hours=s.session_hours), "typ": "session"}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def new_api_token() -> tuple[str, str]:
    token = "iat_" + secrets.token_urlsafe(32)
    return token, sha256(token)


@dataclass
class Principal:
    user: "object"
    org_id: str
    role: str
    via: str  # session | token

    @property
    def id(self) -> str:
        return self.user.id

    def at_least(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, 0) >= ROLE_RANK[role]


def _membership(db: Session, user_id: str, org_id: str | None):
    from ..models import Membership
    q = select(Membership).where(Membership.user_id == user_id)
    if org_id:
        q = q.where(Membership.organization_id == org_id)
    return db.scalars(q.order_by(Membership.id)).first()


def principal_from_token(db: Session, raw: str) -> Principal | None:
    from ..models import ApiToken, User
    tok = db.scalars(select(ApiToken).where(ApiToken.token_hash == sha256(raw), ApiToken.revoked.is_(False))).first()
    if not tok:
        return None
    user = db.get(User, tok.user_id)
    m = _membership(db, tok.user_id, tok.organization_id)
    if not user or user.status != "active" or not m:
        return None
    tok.last_used_at = utcnow()
    db.commit()
    return Principal(user, m.organization_id, m.role, "token")


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    from ..models import User
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        p = principal_from_token(db, auth[7:].strip())
        if not p:
            raise HTTPException(401, "Invalid or revoked API token")
        return p
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise HTTPException(401, "Not signed in")
    try:
        claims = jwt.decode(raw, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired")
    # Cookie sessions must carry the custom header on state-changing requests (CSRF defence:
    # a cross-site form or fetch cannot set it without a CORS preflight, which we never allow).
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "integra":
        raise HTTPException(403, "Missing X-Requested-With header")
    user = db.get(User, claims.get("sub"))
    m = _membership(db, claims.get("sub"), claims.get("org"))
    if not user or user.status != "active" or not m:
        raise HTTPException(401, "Account unavailable")
    return Principal(user, m.organization_id, m.role, "session")


def require_role(role: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.at_least(role):
            raise HTTPException(403, f"Requires the {role} role")
        return p
    return dep


def require_human(role: str):
    """For governance actions (approve, publish, policy): an interactive session, never an API/MCP token."""
    def dep(p: Principal = Depends(require_role(role))) -> Principal:
        if p.via != "session":
            raise HTTPException(403, "This action requires an interactive administrator session")
        return p
    return dep
