"""Alert delivery settings (Slack webhook, SMTP email) - configured in the UI, secrets go to the vault."""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..core.config import get_settings as app_settings
from ..core.db import get_db
from ..core.security import Principal, require_human
from ..models import Organization
from ..services import audit, egress, email_templates, notify, vault

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


class TestIn(BaseModel):
    template: str = Field(default="test", max_length=40)


@router.post("/test")
async def send_test(body: TestIn | None = None, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    """Send a sample alert through the configured channels, rendered with the chosen email template."""
    org = _org(db, p)
    key = (body or TestIn()).template
    if key not in email_templates.TEMPLATES:
        raise HTTPException(422, f"Unknown template {key!r}")
    mail = email_templates.render_sample(key, org.name, app_settings().public_base_url, notify.config_of(org).get("templates"))
    results = await run_in_threadpool(lambda: notify.send(db, org, mail["subject"], mail["text"], html=mail["html"],
                                                          slack_text=email_templates.SAMPLES[key][2]))
    if not results:
        raise HTTPException(409, "No channel is configured yet. Add a Slack webhook or SMTP details and save first.")
    return {"results": results, "ok": all(v == "sent" for v in results.values())}


def _templates_view(org: Organization) -> list[dict]:
    overrides = notify.config_of(org).get("templates") or {}
    base = app_settings().public_base_url
    return [{"key": k, "name": t["name"], "kinds": t["kinds"], "default": {f: t[f] for f in email_templates.EDITABLE},
             "override": overrides.get(k) or {}, "preview": email_templates.render_sample(k, org.name, base, overrides)}
            for k, t in email_templates.TEMPLATES.items()]


@router.get("/templates")
def list_templates(p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    return {"templates": _templates_view(_org(db, p)), "placeholders": list(email_templates.PLACEHOLDERS)}


class TemplatesIn(BaseModel):
    # {template_key: {"subject": "...", "intro": "..."}}; an empty value restores the built-in text
    overrides: dict[str, dict[str, str]] = Field(default_factory=dict)


@router.put("/templates")
def save_templates(body: TemplatesIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    org = _org(db, p)
    cfg = notify.config_of(org)
    stored: dict[str, dict[str, str]] = {}
    try:
        for key, fields in body.overrides.items():
            for field, value in fields.items():
                value = email_templates.validate_override(key, field, value or "")
                if value and value != email_templates.TEMPLATES[key][field]:
                    stored.setdefault(key, {})[field] = value
    except email_templates.TemplateError as e:
        raise HTTPException(422, str(e))
    cfg["templates"] = stored
    org.policy = {**(org.policy or {}), "notifications": cfg}
    audit.record(db, p.org_id, p, "settings.email_templates", org.name, customised=sorted(stored))
    db.commit()
    return {"templates": _templates_view(org), "placeholders": list(email_templates.PLACEHOLDERS)}


class PreviewIn(BaseModel):
    template: str = Field(max_length=40)
    subject: str = Field(default="", max_length=400)
    intro: str = Field(default="", max_length=2000)


@router.post("/templates/preview")
def preview_template(body: PreviewIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    """Render a template with unsaved edits (nothing is stored or sent)."""
    org = _org(db, p)
    try:
        edits = {f: email_templates.validate_override(body.template, f, getattr(body, f)) for f in email_templates.EDITABLE}
    except email_templates.TemplateError as e:
        raise HTTPException(422, str(e))
    return email_templates.render_sample(body.template, org.name, app_settings().public_base_url, {body.template: edits})
