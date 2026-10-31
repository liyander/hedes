"""Alert delivery settings (Slack webhook, SMTP email) - configured in the UI, secrets go to the vault."""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..core.db import get_db
from ..core.security import Principal, require_human
from ..models import Organization
from ..services import audit, egress, notify, vault

router = APIRouter(prefix="/api/v1/settings/notifications", tags=["notifications"])


class EmailIn(BaseModel):
    host: str = Field(default="", max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    security: str = Field(default="starttls", pattern="^(starttls|ssl|none)$")
    username: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=1000)  # omitted = keep the stored one
    from_address: str = Field(default="", alias="from", max_length=255)
    to: list[str] = Field(default_factory=list, max_length=20)

    model_config = {"populate_by_name": True}

    @field_validator("to")
    @classmethod
    def _emails(cls, v: list[str]) -> list[str]:
        for a in v:
            if "@" not in a or len(a) > 255 or any(c in a for c in "\r\n,;"):
                raise ValueError(f"{a!r} is not an email address")
        return v


class NotificationsIn(BaseModel):
    enabled: bool = False
    min_severity: str = Field(default="high", pattern="^(info|low|medium|high|critical)$")
    slack_webhook_url: str | None = Field(default=None, max_length=500)  # omitted = keep, "" = remove
    email: EmailIn | None = None


def _org(db: Session, p: Principal) -> Organization:
    return db.get(Organization, p.org_id)


@router.get("")
def get_settings(p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    return notify.public_view(db, _org(db, p))


@router.put("")
def save_settings(body: NotificationsIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    org = _org(db, p)
    cfg = notify.config_of(org)
    cfg["enabled"], cfg["min_severity"] = body.enabled, body.min_severity
    if body.slack_webhook_url is not None:
        vault.delete_secret(db, cfg.get("slack_secret_ref"))
        if body.slack_webhook_url.strip():
            url = body.slack_webhook_url.strip()
            if not url.startswith("https://"):
                raise HTTPException(422, "The Slack webhook must be an https:// URL")
            try:
                egress.static_check(url)
            except egress.EgressDenied as e:
                raise HTTPException(422, f"Webhook rejected: {e}")
            cfg["slack_secret_ref"] = vault.store_secret(db, p.org_id, {"webhook_url": url})
        else:
            cfg.pop("slack_secret_ref", None)
    if body.email is not None:
        e = body.email
        if e.host:
            try:
                ipaddress.ip_address(e.host)
                raise HTTPException(422, "Use the SMTP server's hostname, not an IP address")
            except ValueError:
                pass
        old = cfg.get("email") or {}
        new = {"host": e.host.strip(), "port": e.port, "security": e.security, "username": e.username.strip(), "from": e.from_address.strip(),
               "to": e.to, "password_ref": old.get("password_ref")}
        password = e.password or None
        if notify.is_gmail(new["host"]):
            if not password and not old.get("password_ref"):
                raise HTTPException(422, "Gmail needs an app password")
            try:
                new, password = notify.normalize_gmail(new, password)
            except ValueError as err:
                raise HTTPException(422, str(err))
        if password:
            vault.delete_secret(db, old.get("password_ref"))
            new["password_ref"] = vault.store_secret(db, p.org_id, {"password": password})
        cfg["email"] = new
    org.policy = {**(org.policy or {}), "notifications": cfg}
    audit.record(db, p.org_id, p, "settings.notifications", org.name, enabled=body.enabled, min_severity=body.min_severity)
    db.commit()
    return notify.public_view(db, org)


@router.post("/test")
async def send_test(p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    org = _org(db, p)
    results = await run_in_threadpool(notify.send, db, org, "[H.E.D.E.S] Test notification", "If you can read this, alert delivery works.")
    if not results:
        raise HTTPException(409, "No channel is configured yet. Add a Slack webhook or SMTP details and save first.")
    return {"results": results, "ok": all(v == "sent" for v in results.values())}
