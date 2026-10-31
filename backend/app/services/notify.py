"""Outbound alert delivery: Slack incoming webhook, SMTP email and WhatsApp, configured per organisation in Settings.

Alerts are always shown in the app. When channels are enabled they are also pushed out, once the transaction that
raised them has committed, from a background thread (a slow mail server must never block a request or an ingestion run).
Secrets (webhook URL, SMTP password, WhatsApp token) live in the credential vault. WhatsApp goes to each opted-in
administrator's own number with a fix link and has its own thresholds (see services/whatsapp.py). Integrations muted from a
fix link are skipped on every outbound channel for the mute period; their alerts still appear in the app.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

import httpx
from sqlalchemy import event
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import session_scope
from ..models import Organization
from . import egress, email_templates, slack_bot, vault, whatsapp

log = logging.getLogger("integra.notify")
RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_RECENT: dict[str, float] = {}
_RECENT_LOCK = threading.Lock()
DEDUPE_SECONDS = 600
GMAIL_HOSTS = {"smtp.gmail.com", "smtp.googlemail.com"}


def is_gmail(host: str) -> bool:
    return (host or "").strip().lower() in GMAIL_HOSTS


def normalize_gmail(email: dict, password: str | None) -> tuple[dict, str | None]:
    """Gmail SMTP rules: TLS on 587 (STARTTLS) or 465 (SSL), sign in with the full Gmail address and a 16-character app
    password (Google shows it in groups of four; the spaces are not part of it), send From that same address."""
    email = {**email, "host": email["host"].strip().lower()}
    if (email.get("port"), email.get("security")) not in ((587, "starttls"), (465, "ssl")):
        email["port"], email["security"] = 587, "starttls"
    user = (email.get("username") or "").strip()
    if "@" not in user:
        raise ValueError("For Gmail the username is your full Gmail / Google Workspace address, e.g. alerts@gmail.com")
    email["username"] = user
    email["from"] = (email.get("from") or "").strip() or user
    if password is not None:
        password = "".join(password.split())
        if len(password) != 16 or not password.isalpha():
            raise ValueError("Gmail needs a 16-letter app password (Google Account > Security > 2-Step Verification > App passwords), "
                             "not your normal Google password")
    return email, password


def config_of(org: Organization | None) -> dict:
    return dict(((org.policy if org else None) or {}).get("notifications") or {})


def public_view(db: Session, org: Organization) -> dict:
    cfg = config_of(org)
    email = cfg.get("email") or {}
    slack = vault.read_secret(db, cfg.get("slack_secret_ref"), org.id) if cfg.get("slack_secret_ref") else None
    return {"enabled": bool(cfg.get("enabled")), "min_severity": cfg.get("min_severity", "high"),
            "slack": {"configured": bool(slack), "hint": vault.mask(slack["webhook_url"]) if slack else ""},
            "email": {"configured": bool(email.get("host") and email.get("to")), "host": email.get("host", ""), "port": email.get("port", 587),
                      "security": email.get("security", "starttls"), "username": email.get("username", ""),
                      "has_password": bool(email.get("password_ref")), "from": email.get("from", ""), "to": email.get("to", [])}}


def _slack(db: Session, org: Organization, cfg: dict, text: str) -> None:
    secret = vault.read_secret(db, cfg.get("slack_secret_ref"), org.id) or {}
    url = secret.get("webhook_url")
    if not url:
        raise RuntimeError("Slack webhook is not configured")
    egress.check_url(url)
    r = httpx.post(url, json={"text": text}, timeout=10, follow_redirects=False)
    if r.status_code >= 300:
        raise RuntimeError(f"Slack answered HTTP {r.status_code}")


def _email(db: Session, org: Organization, cfg: dict, subject: str, text: str, html: str | None = None) -> None:
    e = cfg.get("email") or {}
    if not (e.get("host") and e.get("to") and e.get("from")):
        raise RuntimeError("Email is not fully configured (host, from and to are required)")
    password = (vault.read_secret(db, e.get("password_ref"), org.id) or {}).get("password") if e.get("password_ref") else None
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject[:200], e["from"], ", ".join(e["to"])
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")  # multipart/alternative: mail apps show the HTML, others the text
    security, port = e.get("security", "starttls"), int(e.get("port") or 587)
    ctx = ssl.create_default_context()
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(e["host"], port, timeout=15, context=ctx)
    else:
        smtp = smtplib.SMTP(e["host"], port, timeout=15)
    with smtp:
        if security == "starttls":
            smtp.starttls(context=ctx)
        if e.get("username") and password:
            try:
                smtp.login(e["username"], password)
            except smtplib.SMTPAuthenticationError as err:
                if is_gmail(e["host"]):
                    raise RuntimeError("Gmail rejected the sign-in. Use an app password (2-Step Verification must be on) and the full "
                                       "Gmail address as the username; a normal Google password does not work.") from err
                raise
        smtp.send_message(msg)


def send(db: Session, org: Organization, subject: str, text: str, channels: tuple[str, ...] = ("slack", "email"),
         html: str | None = None, slack_text: str | None = None) -> dict:
    """Deliver to every configured channel. Returns {channel: 'sent' | error text} (used by the Settings test button)."""
    cfg = config_of(org)
    results: dict[str, str] = {}
    if "slack" in channels and cfg.get("slack_secret_ref"):
        try:
            _slack(db, org, cfg, f"*{subject}*\n{slack_text if slack_text is not None else text}")
            results["slack"] = "sent"
        except Exception as e:  # noqa: BLE001 - report any delivery problem to the caller
            results["slack"] = f"{type(e).__name__}: {str(e)[:200]}"
    if "email" in channels and (cfg.get("email") or {}).get("host"):
        try:
            _email(db, org, cfg, subject, text, html)
            results["email"] = "sent"
        except Exception as e:  # noqa: BLE001
            results["email"] = f"{type(e).__name__}: {str(e)[:200]}"
    return results


def _dispatch(alerts: list[dict]) -> None:
    try:
        with session_scope() as db:
            for a in alerts:
                org = db.get(Organization, a["org_id"])
                if not org or whatsapp.mute_until(org, a.get("integration_id")):
                    continue
                key = f"{a['org_id']}|{a['message']}"
                with _RECENT_LOCK:
                    now = time.monotonic()
                    if now - _RECENT.get(key, -1e9) < DEDUPE_SECONDS:
                        continue
                    _RECENT[key] = now
                    if len(_RECENT) > 5000:  # forget expired keys so the map does not grow for the life of the process
                        for k in [k for k, t in _RECENT.items() if now - t > DEDUPE_SECONDS]:
                            _RECENT.pop(k, None)
                cfg = config_of(org)
                res: dict[str, str] = {}
                if cfg.get("enabled") and RANK.get(a["severity"], 0) >= RANK.get(cfg.get("min_severity", "high"), 3):
                    base = get_settings().public_base_url.rstrip("/")
                    link = f"{base}/{a['link']}" if a.get("link") else base
                    mail = email_templates.render(email_templates.template_for(a["kind"]), kind=a["kind"], severity=a["severity"], message=a["message"],
                                                  organization=org.name, link=link, overrides=cfg.get("templates"))
                    res = send(db, org, mail["subject"], mail["text"], html=mail["html"], slack_text=f"{a['message']}\n{link}")
                if a.get("id"):
                    res.update({f"whatsapp {k}": v for k, v in whatsapp.deliver(db, org, a).items()})
                posted = slack_bot.post_alert(db, org, a)
                if posted:
                    res["slack alert channel"] = posted
                for channel, outcome in res.items():
                    if outcome != "sent":
                        log.warning("alert delivery via %s failed: %s", channel, outcome)
                if res and "sent" not in res.values():
                    with _RECENT_LOCK:
                        _RECENT.pop(key, None)  # nothing got through: the next identical alert may try again
    except Exception:
        log.exception("alert delivery failed")


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending = session.info.pop("outbound_alerts", None)
    if pending:
        threading.Thread(target=_dispatch, args=(pending,), name="notify", daemon=True).start()


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("outbound_alerts", None)
