"""Credential connections. Secrets go straight into the vault; the API only ever returns a masked hint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_db, utcnow
from ..core.security import Principal, current_principal, require_role
from ..models import Connection, Integration, IntegrationVersion
from ..services import audit, egress, engine, vault
from ..services.integrations import org_policy
from .common import iso, username

router = APIRouter(prefix="/api/v1/connections", tags=["credentials"])
AUTH_TYPES = "^(bearer|api_key_header|api_key_query|basic)$"


class ConnectionIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    auth_type: str = Field(pattern=AUTH_TYPES)
    token: str | None = Field(default=None, max_length=4000)
    api_key: str | None = Field(default=None, max_length=4000)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=4000)
    test_url: str = Field(default="", max_length=500)


def _payload(body) -> tuple[dict, str]:
    if body.auth_type == "bearer" and body.token:
        return {"token": body.token.strip()}, vault.mask(body.token.strip())
    if body.auth_type.startswith("api_key") and body.api_key:
        return {"api_key": body.api_key.strip()}, vault.mask(body.api_key.strip())
    if body.auth_type == "basic" and body.username and body.password:
        return {"username": body.username, "password": body.password}, f"{body.username} / ••••"
    raise HTTPException(422, "Provide the secret that matches the authentication type")


def view(db: Session, c: Connection) -> dict:
    used_by = []
    for i in db.scalars(select(Integration).where(Integration.organization_id == c.organization_id)).all():
        v = db.get(IntegrationVersion, i.published_version_id or i.latest_version_id) if (i.published_version_id or i.latest_version_id) else None
        if v and (v.config["integration"].get("authentication") or {}).get("credential_ref") == c.name:
            used_by.append({"id": i.id, "name": i.name})
    return {"id": c.id, "name": c.name, "auth_type": c.auth_type, "hint": c.hint, "status": c.status, "test_url": c.test_url,
            "last_tested_at": iso(c.last_tested_at), "last_test_result": c.last_test_result,
            "created_by": username(db, c.created_by), "created_at": iso(c.created_at), "used_by": used_by}


@router.get("")
def list_connections(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(Connection).where(Connection.organization_id == p.org_id).order_by(Connection.name)).all()
    return [view(db, c) for c in rows]


@router.post("")
def create_connection(body: ConnectionIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    payload, hint = _payload(body)
    if body.test_url:
        try:
            egress.static_check(body.test_url)
        except egress.EgressDenied as e:
            raise HTTPException(422, f"Test URL rejected: {e}")
    existing = db.scalars(select(Connection).where(Connection.organization_id == p.org_id, Connection.name == body.name)).first()
    if existing and existing.status != "revoked":
        raise HTTPException(409, f"A connection named {body.name!r} already exists - rotate its secret instead")
    if existing:
        db.delete(existing)
        db.flush()
    c = Connection(organization_id=p.org_id, name=body.name, auth_type=body.auth_type, hint=hint, test_url=body.test_url,
                   secret_ref=vault.store_secret(db, p.org_id, payload), created_by=p.id)
    db.add(c)
    audit.record(db, p.org_id, p, "connection.create", body.name, auth_type=body.auth_type)
    db.commit()
    return view(db, c)


def _owned(db: Session, p: Principal, cid: str) -> Connection:
    c = db.get(Connection, cid)
    if not c or c.organization_id != p.org_id:
        raise HTTPException(404, "Connection not found")
    if c.created_by != p.id and not p.at_least("admin"):
        raise HTTPException(403, "Only the creator or an administrator can change this connection")
    return c


class RotateIn(BaseModel):
    auth_type: str = Field(pattern=AUTH_TYPES)
    token: str | None = None
    api_key: str | None = None
    username: str | None = None
    password: str | None = None


@router.put("/{cid}/secret")
def rotate_secret(cid: str, body: RotateIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    c = _owned(db, p, cid)
    if body.auth_type != c.auth_type:
        raise HTTPException(422, "Authentication type cannot change; create a new connection")
    payload, hint = _payload(body)
    vault.delete_secret(db, c.secret_ref)
    c.secret_ref, c.hint, c.status = vault.store_secret(db, p.org_id, payload), hint, "active"
    audit.record(db, p.org_id, p, "connection.rotate", c.name)
    db.commit()
    return view(db, c)


@router.post("/{cid}/test")
def test_connection(cid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    c = _owned(db, p, cid)
    if c.status == "revoked":
        raise HTTPException(409, "Connection is revoked")
    secret = vault.read_secret(db, c.secret_ref, p.org_id)
    target = None
    if c.test_url:
        base, path = c.test_url.rstrip("/"), ""
        target = {"schema_version": "1.0", "integration": {"id": "conn-test", "name": "test", "version": "1.0.0", "transport": "http",
                  "base_url": base, "authentication": {"type": c.auth_type, "credential_ref": c.name, "header_name": "X-API-Key", "param_name": "api_key"},
                  "operations": [{"id": "probe", "method": "GET", "path": path or "/", "input_schema": {"type": "object"}, "response": {"status_codes": [200, 204]}}]}}
        op_id = "probe"
    else:
        for i in db.scalars(select(Integration).where(Integration.organization_id == p.org_id)).all():
            v = db.get(IntegrationVersion, i.published_version_id or i.latest_version_id) if (i.published_version_id or i.latest_version_id) else None
            if not v or (v.config["integration"].get("authentication") or {}).get("credential_ref") != c.name:
                continue
            op = next((o for o in v.config["integration"]["operations"] if o["method"] == "GET"
                       and not (o["input_schema"].get("required")) and "{" not in o["path"]), None)
            if op:
                target, op_id = v.config, op["id"]
                break
    if not target:
        return {"ok": None, "message": "Nothing safe to test against: no read-only operation without inputs uses this connection. Set a test URL."}
    res = engine.execute(target, op_id, {}, credential=secret, allowed_domains=org_policy(db, p.org_id).get("allowed_domains") or [])
    ok = res.status == "succeeded" or res.error_class == "response_drift"
    c.last_tested_at = utcnow()
    c.last_test_result = "ok" if ok else f"{res.error_class}: {res.error[:150]}"
    if res.error_class == "auth_invalid":
        c.status = "invalid"
    elif ok:
        c.status = "active"
    audit.record(db, p.org_id, p, "connection.test", c.name, result=c.last_test_result)
    db.commit()
    return {"ok": ok, "message": c.last_test_result, "http_status": res.http_status, "request": res.request_preview}


@router.delete("/{cid}")
def revoke_connection(cid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    c = _owned(db, p, cid)
    vault.delete_secret(db, c.secret_ref)
    c.status, c.secret_ref, c.hint = "revoked", "", ""
    audit.record(db, p.org_id, p, "connection.revoke", c.name)
    db.commit()
    return view(db, c)
