"""WhatsApp alert delivery (Meta WhatsApp Cloud API) with one-tap fix links.

Each administrator opts in with their own number, verified by a 6-digit code. Alerts at or above that person's threshold
(and, if they choose, every pending approval or repair) are sent to them with a short-lived fix link, ``/fix/<token>``.
The link opens the Fix page for that alert, which shows the issue and the actions that apply: approve or reject a repair,
retry, pause or resume a data source, acknowledge, mute. The link only identifies the alert and its recipient: every
action still needs that person's own signed-in session and role, so a forwarded link is useless to anyone else and
approval stays bound to the reviewed configuration hash exactly as in Approvals.

Messages outside WhatsApp's 24-hour customer-service window must be approved templates; without one, plain text is
sent, which only arrives if the recipient messaged the business number in the last 24 hours (fine for trying it out).
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import utcnow
from ..core.security import ROLE_RANK, sha256
from ..models import (Alert, ApprovalRequest, FixLink, IngestSource, Integration, IntegrationVersion, Membership, Organization,
                      RepairProposal, User, WhatsAppRecipient)
from . import integrations, vault

log = logging.getLogger("integra.whatsapp")
GRAPH = "https://graph.facebook.com"
LINK_HOURS = 24
CODE_MINUTES = 10
CODE_RESEND_SECONDS = 60
CODE_MAX_ATTEMPTS = 5
RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
APPROVAL_KINDS = {"approval", "repair"}
CREDENTIAL_KINDS = {"credential", "auth_invalid", "forbidden", "credential_missing"}
TRANSPORT: httpx.BaseTransport | None = None  # tests swap in a mock transport

# Meta error codes worth explaining in plain words.
ERRORS = {
    131047: "More than 24 hours since this person last messaged the business number. Set an approved message template in "
            "Settings, or have them send any message to the business number first.",
    131030: "This number is not in the allowed recipient list of the Meta test number (WhatsApp Manager > API Setup > To).",
    131026: "The message could not be delivered (the number may not use WhatsApp).",
    132000: "The template's number of parameters does not match. The alert template needs exactly 3 body variables.",
    132001: "The template does not exist in this language, or is not approved yet.",
    190: "The access token is invalid or expired. Create a permanent System User token in Meta Business Settings.",
    100: "Meta rejected a parameter (check the phone number ID).",
}


class WhatsAppError(RuntimeError):
    pass


def normalize_phone(raw: str) -> str:
    """Return the number as E.164 digits (no "+"). Accepts spaces, dashes, brackets and a leading + or 00."""
    s = re.sub(r"[\s\-().]", "", raw or "")
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("00"):
        s = s[2:]
    if not s.isdigit() or not 8 <= len(s) <= 15 or s[0] == "0":
        raise ValueError("Enter the number in international format with the country code, e.g. +44 7700 900123")
    return s


def mask_phone(phone: str) -> str:
    return f"+{phone[:2]}…{phone[-3:]}" if phone else ""


# ---------- organisation configuration ----------

def config_of(org: Organization | None) -> dict:
    return dict((((org.policy if org else None) or {}).get("notifications") or {}).get("whatsapp") or {})


def save_config(org: Organization, cfg: dict) -> None:
    notifications = dict(((org.policy or {}).get("notifications")) or {})
    notifications["whatsapp"] = cfg
    org.policy = {**(org.policy or {}), "notifications": notifications}


def configured(cfg: dict) -> bool:
    return bool(cfg.get("phone_number_id") and cfg.get("token_ref"))


def public_config(db: Session, org: Organization) -> dict:
    cfg = config_of(org)
    token = vault.read_secret(db, cfg.get("token_ref"), org.id) if cfg.get("token_ref") else None
    count = db.query(WhatsAppRecipient).filter(WhatsAppRecipient.organization_id == org.id, WhatsAppRecipient.verified_at.isnot(None),
                                               WhatsAppRecipient.enabled.is_(True)).count()
    return {"enabled": bool(cfg.get("enabled")), "configured": configured(cfg), "phone_number_id": cfg.get("phone_number_id", ""),
            "token_hint": vault.mask(token["access_token"]) if token else "", "api_version": cfg.get("api_version", "v22.0"),
            "alert_template": cfg.get("alert_template", ""), "template_lang": cfg.get("template_lang", "en"),
            "template_has_button": bool(cfg.get("template_has_button")), "otp_template": cfg.get("otp_template", ""),
            "display_phone": cfg.get("display_phone", ""), "verified_name": cfg.get("verified_name", ""),
            "fix_url_prefix": f"{get_settings().public_base_url.rstrip('/')}/fix/", "recipients": count,
            "chat_enabled": bool(cfg.get("chat_enabled")), "chat_approvals": cfg.get("chat_approvals", True) is not False,
            "has_app_secret": bool(cfg.get("app_secret_ref")), "verify_token": cfg.get("verify_token", ""),
            "webhook_url": f"{get_settings().public_base_url.rstrip('/')}/api/v1/whatsapp/webhook"}


def org_for_phone_number_id(db: Session, phone_number_id: str) -> Organization | None:
    """The organisation whose WhatsApp business number received an inbound webhook."""
    for org in db.scalars(select(Organization)).all():
        if phone_number_id and config_of(org).get("phone_number_id") == phone_number_id:
            return org
    return None


def app_secret(db: Session, org: Organization) -> str:
    ref = config_of(org).get("app_secret_ref")
    return ((vault.read_secret(db, ref, org.id) or {}).get("app_secret") or "") if ref else ""


# ---------- sending ----------

def _clean(text: str, limit: int) -> str:
    # Template parameters may not contain new lines, tabs or more than 4 spaces in a row.
    return re.sub(r"\s+", " ", text or "").strip()[:limit] or "-"


def _token(db: Session, org: Organization, cfg: dict) -> str:
    if not configured(cfg):
        raise WhatsAppError("WhatsApp is not configured (phone number ID and access token are required)")
    token = (vault.read_secret(db, cfg["token_ref"], org.id) or {}).get("access_token")
    if not token:
        raise WhatsAppError("The WhatsApp access token is missing from the vault; save it again in Settings")
    return token


def _error(r: httpx.Response, data: dict, what: str) -> WhatsAppError:
    err = data.get("error") or {}
    code = err.get("code")
    detail = ERRORS.get(code) or err.get("error_user_msg") or err.get("message") or f"HTTP {r.status_code}"
    log.warning("WhatsApp %s failed: HTTP %s %s", what, r.status_code, err or r.text[:300])
    return WhatsAppError(f"WhatsApp rejected the {what}: {detail}" + (f" (code {code})" if code else ""))


def _call(db: Session, org: Organization, method: str, path: str, what: str, **kw) -> dict:
    cfg = config_of(org)
    token = _token(db, org, cfg)
    url = f"{GRAPH}/{cfg.get('api_version') or 'v22.0'}/{cfg['phone_number_id']}{path}"
    with httpx.Client(transport=TRANSPORT, timeout=15, follow_redirects=False) as client:
        r = client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 300 or "error" in data:
        raise _error(r, data, what)
    return data


def _post(db: Session, org: Organization, payload: dict) -> str:
    data = _call(db, org, "POST", "/messages", "message",
                 json={"messaging_product": "whatsapp", "recipient_type": "individual", **payload})
    msg_id = ((data.get("messages") or [{}])[0]).get("id", "")
    # Accepted is not delivered: Meta reports delivery later (a text outside the 24-hour window is silently dropped then).
    log.info("WhatsApp accepted a %s message to %s (%s)", payload.get("type"), mask_phone(payload.get("to", "")), msg_id)
    return msg_id


def send_text(db: Session, org: Organization, phone: str, text: str) -> str:
    return _post(db, org, {"to": phone, "type": "text", "text": {"preview_url": False, "body": text[:4096]}})


def send_list(db: Session, org: Organization, phone: str, body: str, button: str, rows: list[dict], title: str = "Choose") -> str:
    """An interactive list (max 10 rows). rows: [{"id", "title", "description"}]; WhatsApp limits titles to 24 and descriptions to 72 chars."""
    rows = [{"id": r["id"][:200], "title": _clean(r["title"], 24), **({"description": _clean(r["description"], 72)} if r.get("description") else {})}
            for r in rows[:10]]
    return _post(db, org, {"to": phone, "type": "interactive", "interactive": {
        "type": "list", "body": {"text": body[:1024]},
        "action": {"button": _clean(button, 20), "sections": [{"title": _clean(title, 24), "rows": rows}]}}})


def send_buttons(db: Session, org: Organization, phone: str, body: str, buttons: list[tuple[str, str]]) -> str:
    """Up to 3 reply buttons: [(id, title)]; titles max 20 chars."""
    if not buttons:
        return send_text(db, org, phone, body)
    return _post(db, org, {"to": phone, "type": "interactive", "interactive": {
        "type": "button", "body": {"text": body[:1024]},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i[:256], "title": _clean(t, 20)}} for i, t in buttons[:3]]}}})


def mark_read(db: Session, org: Organization, message_id: str) -> None:
    try:
        _call(db, org, "POST", "/messages", "read receipt", json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id})
    except WhatsAppError:
        pass  # cosmetic only


def check(db: Session, org: Organization) -> dict:
    """Read the business number's details: proves the phone number ID and token work, and gives the number to message."""
    data = _call(db, org, "GET", "", "connection check",
                 params={"fields": "display_phone_number,verified_name,quality_rating,code_verification_status"})
    return {"display_phone": data.get("display_phone_number", ""), "verified_name": data.get("verified_name", ""),
            "quality_rating": data.get("quality_rating", ""), "code_verification_status": data.get("code_verification_status", "")}


def fix_url(raw_token: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/fix/{raw_token}"


def send_alert(db: Session, org: Organization, phone: str, title: str, body: str, raw_token: str) -> str:
    """With a template: body {{1}} title, {{2}} details, {{3}} fix link (and optionally a URL button ending in {{1}} = token).
    raw_token "settings" points at Settings instead of a Fix page (verification codes sent through the alert template)."""
    cfg = config_of(org)
    url = fix_url(raw_token)
    if cfg.get("alert_template"):
        components = [{"type": "body", "parameters": [{"type": "text", "text": _clean(title, 120)},
                                                      {"type": "text", "text": _clean(body, 700)},
                                                      {"type": "text", "text": url}]}]
        if cfg.get("template_has_button"):
            components.append({"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": raw_token}]})
        return _post(db, org, {"to": phone, "type": "template", "template": {
            "name": cfg["alert_template"], "language": {"code": cfg.get("template_lang") or "en"}, "components": components}})
    text = f"*{title}*\n{body}\n\nFix it: {url}\n(link valid {LINK_HOURS} h, opens H.A.D.E.S)"
    return _post(db, org, {"to": phone, "type": "text", "text": {"preview_url": False, "body": text[:4000]}})


def send_code(db: Session, org: Organization, phone: str, code: str) -> str:
    """Returns how it was sent: 'template' (always delivered) or 'text' (only within 24 h of the person messaging the business number)."""
    cfg = config_of(org)
    text = f"Your H.A.D.E.S verification code is {code}. It expires in {CODE_MINUTES} minutes. If you did not ask for it, ignore this message."
    if cfg.get("otp_template"):
        # Meta "authentication" templates: the code goes in the body and in the copy-code button.
        _post(db, org, {"to": phone, "type": "template", "template": {
            "name": cfg["otp_template"], "language": {"code": cfg.get("template_lang") or "en"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": code}]},
                           {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": code}]}]}})
        return "template"
    if cfg.get("alert_template"):
        send_alert(db, org, phone, "H.A.D.E.S verification code", text, "settings")
        return "template"
    _post(db, org, {"to": phone, "type": "text", "text": {"preview_url": False, "body": text}})
    return "text"


# ---------- recipients ----------

def recipient(db: Session, user_id: str, org_id: str) -> WhatsAppRecipient | None:
    return db.scalars(select(WhatsAppRecipient).where(WhatsAppRecipient.user_id == user_id, WhatsAppRecipient.organization_id == org_id)).first()


def _code_hash(rec_id: str, code: str) -> str:
    return sha256(f"{rec_id}:{code}")


def start_verification(db: Session, org: Organization, user_id: str, phone: str) -> str:
    rec = recipient(db, user_id, org.id)
    now = utcnow()
    if rec and rec.code_sent_at and (now - rec.code_sent_at).total_seconds() < CODE_RESEND_SECONDS:
        raise HTTPException(429, f"A code was sent less than {CODE_RESEND_SECONDS} seconds ago. Wait a moment and try again.")
    if not rec:
        rec = WhatsAppRecipient(user_id=user_id, organization_id=org.id, phone=phone)
        db.add(rec)
        db.flush()
    if rec.phone != phone:
        rec.phone, rec.verified_at, rec.enabled = phone, None, False
    code = f"{secrets.randbelow(10 ** 6):06d}"
    rec.code_hash, rec.code_expires_at, rec.code_sent_at, rec.code_attempts = _code_hash(rec.id, code), now + timedelta(minutes=CODE_MINUTES), now, 0
    return send_code(db, org, phone, code)


def confirm(rec: WhatsAppRecipient, code: str) -> None:
    if not rec.code_hash or not rec.code_expires_at or rec.code_expires_at < utcnow():
        raise HTTPException(409, "No valid code. Send a new one.")
    if rec.code_attempts >= CODE_MAX_ATTEMPTS:
        raise HTTPException(429, "Too many wrong codes. Send a new one.")
    rec.code_attempts += 1
    if not hmac.compare_digest(rec.code_hash, _code_hash(rec.id, code.strip())):
        raise HTTPException(422, "That code is not right.")
    rec.verified_at, rec.enabled, rec.code_hash, rec.code_expires_at = utcnow(), True, "", None


def recipient_view(rec: WhatsAppRecipient | None) -> dict:
    if not rec:
        return {"phone": "", "verified": False, "enabled": False, "min_severity": "high", "approvals": True, "code_pending": False}
    return {"phone": mask_phone(rec.phone), "verified": rec.verified_at is not None, "enabled": rec.enabled, "min_severity": rec.min_severity,
            "approvals": rec.approvals, "code_pending": bool(rec.code_hash and rec.code_expires_at and rec.code_expires_at > utcnow())}


def _targets(db: Session, org_id: str, alert: dict) -> list[WhatsAppRecipient]:
    rows = db.execute(select(WhatsAppRecipient, Membership.role).join(
        Membership, (Membership.user_id == WhatsAppRecipient.user_id) & (Membership.organization_id == WhatsAppRecipient.organization_id)).join(
        User, User.id == WhatsAppRecipient.user_id).where(
        WhatsAppRecipient.organization_id == org_id, WhatsAppRecipient.enabled.is_(True), WhatsAppRecipient.verified_at.isnot(None),
        User.status == "active")).all()
    sev = RANK.get(alert["severity"], 0)
    return [rec for rec, role in rows if ROLE_RANK.get(role, 0) >= ROLE_RANK["admin"]
            and (sev >= RANK.get(rec.min_severity, 3) or (rec.approvals and alert["kind"] in APPROVAL_KINDS))]


# ---------- fix links ----------

def issue_link(db: Session, org_id: str, user_id: str, alert: dict | None) -> str:
    raw = secrets.token_urlsafe(24)
    approval_id = None
    if alert:
        m = re.match(r"#/approvals/([0-9a-f-]{36})$", alert.get("link") or "")
        approval_id = m.group(1) if m else None
    db.add(FixLink(organization_id=org_id, user_id=user_id, token_hash=sha256(raw), alert_id=alert["id"] if alert else None,
                   integration_id=(alert or {}).get("integration_id"), approval_request_id=approval_id,
                   expires_at=utcnow() + timedelta(hours=LINK_HOURS)))
    db.flush()
    return raw


def resolve_link(db: Session, raw: str, p) -> FixLink:
    link = db.scalars(select(FixLink).where(FixLink.token_hash == sha256(raw or ""))).first()
    if not link or link.organization_id != p.org_id:
        raise HTTPException(404, "This fix link is not valid.")
    if link.user_id != p.id:
        raise HTTPException(403, "This fix link was sent to another person. Open the alert from Monitoring instead.")
    if link.expires_at < utcnow():
        raise HTTPException(410, "This fix link has expired. Open the alert from Monitoring instead.")
    if p.via != "session":
        raise HTTPException(403, "Fix links need an interactive session")
    link.last_used_at = utcnow()
    return link


def deliver(db: Session, org: Organization, alert: dict) -> dict[str, str]:
    """Send one alert to every opted-in administrator. Returns {masked phone: 'sent' | error}."""
    cfg = config_of(org)
    if not cfg.get("enabled") or not configured(cfg):
        return {}
    results = {}
    title = f"H.A.D.E.S {alert['severity'].upper()}: {alert['kind']}"
    for rec in _targets(db, org.id, alert):
        try:
            send_alert(db, org, rec.phone, title, alert["message"], issue_link(db, org.id, rec.user_id, alert))
            results[mask_phone(rec.phone)] = "sent"
        except Exception as e:  # noqa: BLE001 - one bad number must not stop the others
            results[mask_phone(rec.phone)] = f"{type(e).__name__}: {str(e)[:200]}"
    return results


# ---------- mutes ----------

def mute_until(org: Organization, integration_id: str | None) -> datetime | None:
    raw = ((org.policy or {}).get("alert_mutes") or {}).get(integration_id or "")
    until = datetime.fromisoformat(raw) if raw else None
    return until if until and until > utcnow() else None


def set_mute(org: Organization, integration_id: str, minutes: int | None) -> None:
    now = utcnow()
    mutes = {k: v for k, v in ((org.policy or {}).get("alert_mutes") or {}).items() if datetime.fromisoformat(v) > now}
    if minutes:
        mutes[integration_id] = (now + timedelta(minutes=minutes)).isoformat()
    else:
        mutes.pop(integration_id, None)
    org.policy = {**(org.policy or {}), "alert_mutes": mutes}


# ---------- the Fix page ----------

def _approval(db: Session, link: FixLink, integ: Integration | None) -> ApprovalRequest | None:
    req = db.get(ApprovalRequest, link.approval_request_id) if link.approval_request_id else None
    if req and req.decision != "pending" and integ:
        newer = db.scalars(select(ApprovalRequest).where(ApprovalRequest.integration_id == integ.id, ApprovalRequest.decision == "pending")).first()
        req = newer or req  # a superseded request: show the one that replaced it
    if not req and integ:
        req = db.scalars(select(ApprovalRequest).where(ApprovalRequest.integration_id == integ.id, ApprovalRequest.decision == "pending")
                         .order_by(ApprovalRequest.created_at.desc())).first()
    return req if req and req.organization_id == link.organization_id else None


def context(db: Session, link: FixLink, p) -> dict:
    org = db.get(Organization, link.organization_id)
    alert = db.get(Alert, link.alert_id) if link.alert_id else None
    integ = db.get(Integration, link.integration_id) if link.integration_id else None
    if integ and integ.organization_id != link.organization_id:
        integ = None
    admin = p.at_least("admin")
    actions: list[dict] = []
    out: dict = {"test": alert is None and not link.integration_id, "expires_at": link.expires_at.isoformat() + "Z", "actions": actions,
                 "alert": None, "integration": None, "approval": None, "sources": [], "muted_until": None}
    if alert:
        out["alert"] = {"id": alert.id, "kind": alert.kind, "severity": alert.severity, "message": alert.message, "link": alert.link,
                        "acknowledged": alert.acknowledged, "created_at": alert.created_at.isoformat() + "Z"}
    if integ:
        out["integration"] = {"id": integ.id, "name": integ.name, "slug": integ.slug, "status": integ.status}
    req = _approval(db, link, integ)
    if req:
        v = db.get(IntegrationVersion, req.version_id)
        ri = db.get(Integration, req.integration_id)
        rp = db.scalars(select(RepairProposal).where(RepairProposal.approval_request_id == req.id)).first()
        tr = integrations.latest_test(db, v)
        blockers = integrations.approval_blockers(db, req, v, ri, p.id) if req.decision == "pending" else []
        out["approval"] = {"id": req.id, "kind": req.kind, "decision": req.decision, "integration": ri.name, "version": v.version,
                           "risk": (req.risk or {}).get("level"), "test_result": tr.result if tr else None, "blockers": blockers,
                           "summary": (rp.diagnosis or {}).get("summary") if rp else v.notes,
                           "changes": (rp.diagnosis or {}).get("mapping_changes") if rp else None}
        if req.decision == "pending" and admin:
            label = "Approve fix & publish" if req.kind == "repair" else "Approve & publish"
            actions.append({"id": "approve", "label": label, "style": "primary", "disabled": bool(blockers)})
            actions.append({"id": "reject", "label": "Reject", "style": "danger", "needs_comment": True})
    if integ:
        for src in db.scalars(select(IngestSource).where(IngestSource.integration_id == integ.id)).all():
            out["sources"].append({"id": src.id, "operation": src.operation_id, "enabled": src.enabled, "state": src.state,
                                   "consecutive_failures": src.consecutive_failures, "last_error": src.last_error[:300]})
            if src.enabled:
                actions.append({"id": "retry", "label": f"Retry {src.operation_id} now", "style": "primary" if not req else "", "source_id": src.id})
                if admin:
                    actions.append({"id": "pause", "label": f"Pause {src.operation_id}", "style": "", "source_id": src.id})
            elif admin:
                actions.append({"id": "resume", "label": f"Resume {src.operation_id}", "style": "", "source_id": src.id})
    if alert and alert.kind in CREDENTIAL_KINDS:
        actions.append({"id": "reconnect", "label": "Reconnect credential", "style": "", "href": "#/credentials"})
    if alert and not alert.acknowledged:
        actions.append({"id": "ack", "label": "Acknowledge", "style": ""})
    if integ and admin:
        until = mute_until(org, integ.id)
        out["muted_until"] = until.isoformat() + "Z" if until else None
        if until:
            actions.append({"id": "unmute", "label": "Unmute alerts", "style": ""})
        else:
            actions.append({"id": "mute", "label": "Mute 1 h", "style": "", "minutes": 60})
            actions.append({"id": "mute", "label": "Mute 24 h", "style": "", "minutes": 1440})
    if alert and alert.link:
        actions.append({"id": "open", "label": "Open details", "style": "", "href": alert.link})
    return out
