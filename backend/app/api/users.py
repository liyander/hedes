"""User management for administrators: create accounts, change roles, disable/enable, reset passwords.

Rules: admins manage builders and admins; only an owner can create, change or reset an owner. Nobody can change their
own role or disable themselves, and the organisation always keeps at least one active owner.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.db import get_db, utcnow
from ..core.security import Principal, hash_password, require_human, require_role
from ..models import Membership, User
from ..services import audit
from .common import iso

router = APIRouter(prefix="/api/v1/admin/users", tags=["user management"])
ROLE = "^(user|admin|owner)$"


def _view(m: Membership, u: User) -> dict:
    return {"id": m.id, "user_id": u.id, "username": u.username, "name": u.name, "email": u.email, "role": m.role,
            "status": u.status, "created_at": iso(u.created_at)}


def _member(db: Session, p: Principal, mid: str) -> tuple[Membership, User]:
    m = db.get(Membership, mid)
    if not m or m.organization_id != p.org_id:
        raise HTTPException(404, "User not found")
    return m, db.get(User, m.user_id)


def _guard_owner(p: Principal, *roles: str) -> None:
    if "owner" in roles and p.role != "owner":
        raise HTTPException(403, "Only an owner can create, change or reset an owner account")


def _active_owners(db: Session, org_id: str) -> int:
    return len(db.execute(select(Membership.id).join(User, User.id == Membership.user_id).where(
        Membership.organization_id == org_id, Membership.role == "owner", User.status == "active")).all())


@router.get("")
def list_users(p: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)):
    rows = db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                      .where(Membership.organization_id == p.org_id).order_by(User.username)).all()
    return [_view(m, u) for m, u in rows]


class UserIn(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9_.-]{3,32}$")
    name: str = Field(default="", max_length=120)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)
    role: str = Field(default="user", pattern=ROLE)
    password: str = Field(min_length=10, max_length=200)


@router.post("")
def create_user(body: UserIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    _guard_owner(p, body.role)
    email = body.email.lower()
    if db.scalars(select(User).where(or_(User.username == body.username, User.email == email))).first():
        raise HTTPException(409, "That username or email is already in use")
    u = User(username=body.username, name=body.name.strip(), email=email, password_hash=hash_password(body.password))
    db.add(u)
    db.flush()
    m = Membership(user_id=u.id, organization_id=p.org_id, role=body.role)
    db.add(m)
    audit.record(db, p.org_id, p, "user.create", u.username, role=body.role)
    db.commit()
    return _view(m, u)


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern=ROLE)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)


@router.patch("/{mid}")
def update_user(mid: str, body: UserPatch, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    m, u = _member(db, p, mid)
    _guard_owner(p, m.role, body.role or "")
    changes = body.model_dump(exclude_none=True)
    if u.id == p.id and ({"role", "status"} & changes.keys()):
        raise HTTPException(409, "You cannot change your own role or disable yourself")
    losing_owner = m.role == "owner" and u.status == "active" and (
        (body.role and body.role != "owner") or body.status == "disabled")
    if losing_owner and _active_owners(db, p.org_id) <= 1:
        raise HTTPException(409, "The organisation must keep at least one active owner")
    if body.email and body.email.lower() != u.email:
        if db.scalars(select(User).where(User.email == body.email.lower(), User.id != u.id)).first():
            raise HTTPException(409, "That email is already in use")
        u.email = body.email.lower()
    if body.name is not None:
        u.name = body.name.strip()
    if body.role:
        m.role = body.role
    if body.status:
        u.status = body.status  # disabled accounts cannot sign in, and their sessions and API tokens stop working
        if body.status == "disabled":
            u.sessions_valid_after = utcnow()  # re-enabling the account does not bring its old sessions back
    audit.record(db, p.org_id, p, "user.update", u.username, **changes)
    db.commit()
    return _view(m, u)


class PasswordIn(BaseModel):
    password: str = Field(min_length=10, max_length=200)


@router.post("/{mid}/password")
def reset_password(mid: str, body: PasswordIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    m, u = _member(db, p, mid)
    _guard_owner(p, m.role)
    u.password_hash = hash_password(body.password)
    u.sessions_valid_after = utcnow()  # signs the account out everywhere
    audit.record(db, p.org_id, p, "user.password_reset", u.username)
    db.commit()
    return {"ok": True}
