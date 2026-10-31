"""WhatsApp alerts: organisation setup (Meta Cloud API), each administrator's own number, and the fix-link actions."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..core.db import get_db, utcnow
from ..core.security import Principal, current_principal, require_human
from ..models import Alert, IngestSource, Organization
from ..services import audit, ingest, integrations, vault, whatsapp, whatsapp_bot

log = logging.getLogger("integra.whatsapp")

router = APIRouter(prefix="/api/v1", tags=["whatsapp"])
fix_router = APIRouter(include_in_schema=False)


def _org(db: Session, p: Principal) -> Organization:
    return db.get(Organization, p.org_id)


# ---------- organisation setup (admin) ----------

class WhatsAppConfigIn(BaseModel):
    enabled: bool = False
    phone_number_id: str = Field(default="", pattern=r"^\d{0,32}$")
    access_token: str | None = Field(default=None, max_length=1000)  # omitted = keep, "" = remove
    api_version: str = Field(default="v22.0", pattern=r"^v\d{1,3}\.\d{1,2}$")
    alert_template: str = Field(default="", pattern=r"^[a-z0-9_]{0,512}$")
    template_lang: str = Field(default="en", pattern=r"^[A-Za-z]{2,3}(_[A-Za-z]{2,4})?$")
    template_has_button: bool = False
    otp_template: str = Field(default="", pattern=r"^[a-z0-9_]{0,512}$")
    chat_enabled: bool = False
    chat_approvals: bool = True
    app_secret: str | None = Field(default=None, max_length=200)  # omitted = keep, "" = remove


@router.get("/settings/whatsapp")
def get_config(p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    return whatsapp.public_config(db, _org(db, p))


@router.put("/settings/whatsapp")
def save_config(body: WhatsAppConfigIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    org = _org(db, p)
    cfg = whatsapp.config_of(org)
    if body.access_token is not None:
        vault.delete_secret(db, cfg.get("token_ref"))
        cfg.pop("token_ref", None)
        if body.access_token.strip():
            cfg["token_ref"] = vault.store_secret(db, p.org_id, {"access_token": body.access_token.strip()})
    if body.app_secret is not None:
        vault.delete_secret(db, cfg.get("app_secret_ref"))
        cfg.pop("app_secret_ref", None)
        if body.app_secret.strip():
            cfg["app_secret_ref"] = vault.store_secret(db, p.org_id, {"app_secret": body.app_secret.strip()})
    cfg.update(body.model_dump(exclude={"access_token", "app_secret"}))
    cfg.setdefault("verify_token", secrets.token_urlsafe(24))
    if cfg["enabled"] and not whatsapp.configured(cfg):
        raise HTTPException(422, "Add the phone number ID and an access token before turning WhatsApp on")
    if cfg["chat_enabled"] and not (whatsapp.configured(cfg) and cfg.get("app_secret_ref")):
        raise HTTPException(422, "The chatbot needs the phone number ID, access token and the Meta app secret (to verify incoming messages)")
    whatsapp.save_config(org, cfg)
    audit.record(db, p.org_id, p, "settings.whatsapp", org.name, enabled=body.enabled, template=body.alert_template)
    db.commit()
    check_error = ""
    if whatsapp.configured(cfg):
        try:
            _store_check(db, org)
        except whatsapp.WhatsAppError as e:
            check_error = str(e)
    return {**whatsapp.public_config(db, org), "check_error": check_error}


def _store_check(db: Session, org: Organization) -> dict:
    info = whatsapp.check(db, org)
    cfg = whatsapp.config_of(org)
    cfg.update(display_phone=info["display_phone"], verified_name=info["verified_name"])
    whatsapp.save_config(org, cfg)
    db.commit()
    return info


@router.post("/settings/whatsapp/check")
def check_connection(p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    try:
        return _store_check(db, _org(db, p))
    except whatsapp.WhatsAppError as e:
        raise HTTPException(502, str(e))


# ---------- my number (admin opt-in) ----------

class PhoneIn(BaseModel):
    phone: str = Field(max_length=32)


class CodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=10)


class PrefsIn(BaseModel):
    enabled: bool | None = None
    min_severity: str | None = Field(default=None, pattern="^(info|low|medium|high|critical)$")
    approvals: bool | None = None


def _me_view(db: Session, p: Principal) -> dict:
    org = _org(db, p)
    cfg = whatsapp.config_of(org)
    return {**whatsapp.recipient_view(whatsapp.recipient(db, p.id, p.org_id)),
            "org_ready": whatsapp.configured(cfg), "org_enabled": bool(cfg.get("enabled")),
            "has_template": bool(cfg.get("otp_template") or cfg.get("alert_template")), "business_phone": cfg.get("display_phone", "")}


@router.get("/me/whatsapp")
def my_whatsapp(p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    return _me_view(db, p)


@router.post("/me/whatsapp/code")
def send_code(body: PhoneIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    try:
        phone = whatsapp.normalize_phone(body.phone)
    except ValueError as e:
        raise HTTPException(422, str(e))
    try:
        via = whatsapp.start_verification(db, _org(db, p), p.id, phone)
    except whatsapp.WhatsAppError as e:
        db.rollback()
        raise HTTPException(502, str(e))
    audit.record(db, p.org_id, p, "whatsapp.code_sent", whatsapp.mask_phone(phone), via=via)
    db.commit()
    return {**_me_view(db, p), "sent_via": via}


@router.post("/me/whatsapp/verify")
def verify(body: CodeIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    rec = whatsapp.recipient(db, p.id, p.org_id)
    if not rec:
        raise HTTPException(409, "Send a code to your number first")
    try:
        whatsapp.confirm(rec, body.code)
    except HTTPException:
        db.commit()  # keep the attempt count
        raise
    audit.record(db, p.org_id, p, "whatsapp.verified", whatsapp.mask_phone(rec.phone))
    db.commit()
    return _me_view(db, p)


@router.patch("/me/whatsapp")
def update_prefs(body: PrefsIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    rec = whatsapp.recipient(db, p.id, p.org_id)
    if not rec or not rec.verified_at:
        raise HTTPException(409, "Verify your number first")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(rec, k, v)
    db.commit()
    return _me_view(db, p)


@router.delete("/me/whatsapp")
def remove_number(p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    rec = whatsapp.recipient(db, p.id, p.org_id)
    if rec:
        audit.record(db, p.org_id, p, "whatsapp.removed", whatsapp.mask_phone(rec.phone))
        db.delete(rec)
        db.commit()
    return _me_view(db, p)


@router.post("/me/whatsapp/test")
def send_test(p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    rec = whatsapp.recipient(db, p.id, p.org_id)
    if not rec or not rec.verified_at:
        raise HTTPException(409, "Verify your number first")
    org = _org(db, p)
    try:
        token = whatsapp.issue_link(db, p.org_id, p.id, None)
        whatsapp.send_alert(db, org, rec.phone, "H.E.D.E.S test", "If you can read this, WhatsApp alerts work. Tap the link to check fix links open.", token)
    except whatsapp.WhatsAppError as e:
        db.rollback()
        raise HTTPException(502, str(e))
    db.commit()
    return {"ok": True}


# ---------- fix links ----------

@fix_router.get("/fix/{token}")
def open_fix_link(token: str):
    # The token travels in the fragment from here on, so it never reaches server logs or a Referer header again.
    return RedirectResponse("/#/settings" if token == "settings" else f"/#/fix/{token}", status_code=303)


@router.get("/fix/{token}")
def fix_details(token: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    link = whatsapp.resolve_link(db, token, p)
    db.commit()
    return whatsapp.context(db, link, p)


class FixActionIn(BaseModel):
    source_id: str | None = None
    comment: str = Field(default="", max_length=4000)
    minutes: int | None = Field(default=None, ge=15, le=10080)


def _admin(p: Principal) -> None:
    if not p.at_least("admin"):
        raise HTTPException(403, "Requires the admin role")


def _source(db: Session, link, source_id: str | None) -> IngestSource:
    src = db.get(IngestSource, source_id) if source_id else None
    if not src or src.organization_id != link.organization_id or src.integration_id != link.integration_id:
        raise HTTPException(404, "Data source not found for this alert")
    return src


@router.post("/fix/{token}/{action}")
async def fix_action(token: str, action: str, body: FixActionIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    link = whatsapp.resolve_link(db, token, p)
    org = _org(db, p)
    alert = db.get(Alert, link.alert_id) if link.alert_id else None
    target, result = "", ""
    if action in ("approve", "reject"):
        _admin(p)
        req = whatsapp.context(db, link, p)["approval"]
        if not req or req["decision"] != "pending":
            raise HTTPException(409, "There is no pending approval for this alert any more")
        if action == "approve":
            a = integrations.approve(db, p.org_id, req["id"], p, body.comment or "Approved from a WhatsApp fix link")
        else:
            a = integrations.decide_negative(db, p.org_id, req["id"], p, "rejected", body.comment)
        target, result = f"{req['integration']}@{req['version']}", a.decision
    elif action == "retry":
        src = _source(db, link, body.source_id)
        if not src.enabled:
            raise HTTPException(409, "This source is paused")
        if not ingest.try_acquire(src.id):
            raise HTTPException(409, "A run is already in progress")
        try:
            run = await run_in_threadpool(ingest.run_source, db, src, "manual")
        except ValueError as e:
            raise HTTPException(409, str(e))
        finally:
            ingest.release(src.id)
        target, result = f"{src.integration_id}.{src.operation_id}", run.status
        if run.status == "succeeded" and alert:
            alert.acknowledged = True
    elif action in ("pause", "resume"):
        _admin(p)
        src = _source(db, link, body.source_id)
        src.enabled = action == "resume"
        if src.enabled:
            src.next_run_at, src.consecutive_failures = utcnow(), 0
        target, result = f"{src.integration_id}.{src.operation_id}", "enabled" if src.enabled else "paused"
    elif action == "ack":
        if not alert:
            raise HTTPException(409, "This link has no alert")
        alert.acknowledged, target, result = True, alert.kind, "acknowledged"
    elif action in ("mute", "unmute"):
        _admin(p)
        if not link.integration_id:
            raise HTTPException(409, "This alert is not about an integration")
        whatsapp.set_mute(org, link.integration_id, (body.minutes or 60) if action == "mute" else None)
        target, result = link.integration_id, f"muted {body.minutes or 60} min" if action == "mute" else "unmuted"
    else:
        raise HTTPException(404, "Unknown action")
    audit.record(db, p.org_id, p, f"fix.{action}", target, via="whatsapp-fix-link", alert_id=link.alert_id, result=result)
    db.commit()
    return {"result": result, **whatsapp.context(db, link, p)}


# ---------- inbound webhook (Meta -> chatbot, delivery statuses) ----------

@router.get("/whatsapp/webhook", include_in_schema=False)
def webhook_verify(request: Request, db: Session = Depends(get_db)):
    """Meta's one-time subscription check: echo hub.challenge when hub.verify_token matches an organisation's token."""
    q = request.query_params
    token = q.get("hub.verify_token", "")
    if q.get("hub.mode") == "subscribe" and token:
        for org in db.scalars(select(Organization)).all():
            mine = whatsapp.config_of(org).get("verify_token", "")
            if mine and hmac.compare_digest(mine, token):
                return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


@router.post("/whatsapp/webhook", include_in_schema=False)
async def webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    signature = request.headers.get("x-hub-signature-256", "")
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            org = whatsapp.org_for_phone_number_id(db, (value.get("metadata") or {}).get("phone_number_id", ""))
            if not org:
                continue
            secret = whatsapp.app_secret(db, org)
            expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest() if secret else ""
            if not expected or not hmac.compare_digest(expected, signature):
                log.warning("WhatsApp webhook rejected: bad or missing signature (check the app secret in Settings)")
                raise HTTPException(403, "Invalid signature")
            for st in value.get("statuses") or []:
                if st.get("status") == "failed":
                    errs = "; ".join(f"{e.get('code')} {e.get('title') or e.get('message', '')}" for e in st.get("errors") or [])
                    log.warning("WhatsApp could not deliver a message to %s: %s %s", whatsapp.mask_phone(st.get("recipient_id", "")), errs,
                                whatsapp.ERRORS.get(((st.get("errors") or [{}])[0]).get("code"), ""))
            for msg in value.get("messages") or []:
                whatsapp_bot.enqueue(org.id, msg)
    return {"ok": True}
