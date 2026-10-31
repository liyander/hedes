from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import get_db, utcnow
from ..core.security import (COOKIE, Principal, current_principal, hash_password, issue_session, new_api_token,
                             require_human, require_role, sha256, verify_password)
from ..models import ApiToken, Invitation, Membership, Organization, User
from ..services import audit
from .common import iso, username

router = APIRouter(prefix="/api/v1", tags=["auth & organisation"])
_FAILS: dict[str, deque] = defaultdict(deque)


class LoginIn(BaseModel):
    username: str = Field(max_length=255)
    password: str = Field(max_length=200)


def _throttle(key: str) -> None:
    q, now = _FAILS[key], time.monotonic()
    while q and now - q[0] > 300:
        q.popleft()
    if len(q) >= 8:
        raise HTTPException(429, "Too many failed sign-in attempts. Wait a few minutes.")


def _set_cookie(resp: Response, token: str) -> None:
    s = get_settings()
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=s.cookie_secure, max_age=s.session_hours * 3600, path="/")


def me_view(db: Session, p: Principal) -> dict:
    org = db.get(Organization, p.org_id)
    return {"id": p.id, "username": p.user.username, "email": p.user.email, "name": p.user.name, "role": p.role,
            "organization": {"id": org.id, "name": org.name}}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    key = f"{request.client.host if request.client else '-'}:{body.username.lower()}"
    _throttle(key)
    ident = body.username.strip().lower()
    user = db.scalars(select(User).where(or_(User.username == ident, User.email == ident))).first()
    if not user or user.status != "active" or not verify_password(body.password, user.password_hash):
        _FAILS[key].append(time.monotonic())
        raise HTTPException(401, "Incorrect username or password")
    m = db.scalars(select(Membership).where(Membership.user_id == user.id)).first()
    if not m:
        raise HTTPException(403, "This account is not a member of any organisation")
    _FAILS.pop(key, None)
    _set_cookie(response, issue_session(user.id, m.organization_id))
    audit.record(db, m.organization_id, user, "auth.login", user.username)
    db.commit()
    return me_view(db, Principal(user, m.organization_id, m.role, "session"))


@router.post("/auth/refresh")
def refresh_session(response: Response, p: Principal = Depends(current_principal)):
    if p.via != "session":
        raise HTTPException(400, "Only browser sessions can be refreshed")
    _set_cookie(response, issue_session(p.id, p.org_id))
    return {"ok": True}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def get_current_user(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return me_view(db, p)


class AcceptIn(BaseModel):
    token: str
    username: str = Field(pattern=r"^[a-z0-9_.-]{3,32}$")
    name: str = Field(default="", max_length=120)
    password: str = Field(min_length=10, max_length=200)


@router.post("/auth/accept-invite")
def accept_invite(body: AcceptIn, response: Response, db: Session = Depends(get_db)):
    inv = db.scalars(select(Invitation).where(Invitation.token_hash == sha256(body.token))).first()
    if not inv or inv.accepted_at or inv.expires_at < utcnow():
        raise HTTPException(400, "This invitation is invalid or has expired")
    if db.scalars(select(User).where(or_(User.username == body.username, User.email == inv.email))).first():
        raise HTTPException(409, "That username or email is already registered")
    u = User(email=inv.email, username=body.username, name=body.name, password_hash=hash_password(body.password))
    db.add(u)
    db.flush()
    db.add(Membership(user_id=u.id, organization_id=inv.organization_id, role=inv.role))
    inv.accepted_at = utcnow()
    audit.record(db, inv.organization_id, u, "member.join", u.username, role=inv.role)
    db.commit()
    _set_cookie(response, issue_session(u.id, inv.organization_id))
    return {"ok": True}


# ------------------------------------------------------------------------------------ organisation

@router.get("/organizations/current")
def current_org(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    org = db.get(Organization, p.org_id)
    members = db.execute(select(Membership, User).join(User, Membership.user_id == User.id)
                         .where(Membership.organization_id == p.org_id).order_by(User.username)).all()
    out = {"id": org.id, "name": org.name, "policy": {k: v for k, v in (org.policy or {}).items() if k != "notifications"},
           "members": [{"id": m.id, "user_id": u.id, "username": u.username, "name": u.name, "email": u.email, "role": m.role, "status": u.status}
                       for m, u in members]}
    if p.at_least("admin"):
        invs = db.scalars(select(Invitation).where(Invitation.organization_id == p.org_id, Invitation.accepted_at.is_(None))).all()
        out["invitations"] = [{"id": i.id, "email": i.email, "role": i.role, "expires_at": iso(i.expires_at)} for i in invs]
    return out


class InviteIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)
    role: str = Field(pattern="^(user|admin)$")


@router.post("/organizations/invitations")
def invite_member(body: InviteIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    token = secrets.token_urlsafe(24)
    db.add(Invitation(organization_id=p.org_id, email=body.email.lower(), role=body.role, token_hash=sha256(token),
                      invited_by=p.id, expires_at=utcnow() + timedelta(days=7)))
    audit.record(db, p.org_id, p, "member.invite", body.email, role=body.role)
    db.commit()
    return {"token": token, "link": f"{get_settings().public_base_url.rstrip('/')}/#/invite/{token}", "expires_in_days": 7}


class RoleIn(BaseModel):
    role: str = Field(pattern="^(user|admin|owner)$")


@router.patch("/members/{membership_id}/role")
def assign_role(membership_id: str, body: RoleIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    m = db.get(Membership, membership_id)
    if not m or m.organization_id != p.org_id:
        raise HTTPException(404, "Member not found")
    if (body.role == "owner" or m.role == "owner") and p.role != "owner":
        raise HTTPException(403, "Only a platform owner can grant or remove the owner role")
    if m.user_id == p.id:
        raise HTTPException(409, "You cannot change your own role")
    old, m.role = m.role, body.role
    audit.record(db, p.org_id, p, "member.role", username(db, m.user_id), old=old, new=body.role)
    db.commit()
    return {"ok": True}


class PolicyIn(BaseModel):
    allowed_domains: list[str] | None = None
    ai_monthly_budget_usd: float | None = Field(default=None, ge=0, le=100000)
    require_tests: bool | None = None
    repair_after_failures: int | None = Field(default=None, ge=1, le=50)
    credential_rotation_days: int | None = Field(default=None, ge=7, le=730)
    source_failure_alert_after: int | None = Field(default=None, ge=1, le=20)


@router.patch("/organizations/current/policy")
def update_policy(body: PolicyIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    org = db.get(Organization, p.org_id)
    changes = body.model_dump(exclude_none=True)
    if "require_tests" in changes and p.role != "owner":
        raise HTTPException(403, "Only the platform owner can change the testing requirement")
    if "allowed_domains" in changes:
        changes["allowed_domains"] = sorted({d.strip().lower().lstrip(".") for d in changes["allowed_domains"] if d.strip()})
    org.policy = {**(org.policy or {}), **changes}
    audit.record(db, p.org_id, p, "policy.update", org.name, **changes)
    db.commit()
    return {k: v for k, v in org.policy.items() if k != "notifications"}


# ------------------------------------------------------------------------------------ API tokens

class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("/tokens")
def list_tokens(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(ApiToken).where(ApiToken.user_id == p.id, ApiToken.revoked.is_(False)).order_by(ApiToken.created_at.desc())).all()
    return [{"id": t.id, "name": t.name, "prefix": t.prefix, "created_at": iso(t.created_at), "last_used_at": iso(t.last_used_at)} for t in rows]


@router.post("/tokens")
def create_token(body: TokenIn, p: Principal = Depends(require_human("user")), db: Session = Depends(get_db)):
    token, digest = new_api_token()
    db.add(ApiToken(user_id=p.id, organization_id=p.org_id, name=body.name, prefix=token[:10], token_hash=digest))
    audit.record(db, p.org_id, p, "token.create", body.name)
    db.commit()
    return {"token": token, "note": "Shown once. Use as 'Authorization: Bearer <token>' for the REST API or MCP endpoint."}


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    t = db.get(ApiToken, token_id)
    if not t or t.user_id != p.id:
        raise HTTPException(404, "Token not found")
    t.revoked = True
    audit.record(db, p.org_id, p, "token.revoke", t.name)
    db.commit()
    return {"ok": True}
