"""Outbound alert delivery: Slack incoming webhook and/or SMTP email, configured per organisation in Settings.

Alerts are always shown in the app. When channels are enabled they are also pushed out, once the transaction that
raised them has committed, from a background thread (a slow mail server must never block a request or an ingestion run).
Secrets (webhook URL, SMTP password) live in the credential vault.
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
from . import egress, vault

log = logging.getLogger("integra.notify")
RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_RECENT: dict[str, float] = {}
_RECENT_LOCK = threading.Lock()
DEDUPE_SECONDS = 600


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


def _email(db: Session, org: Organization, cfg: dict, subject: str, text: str) -> None:
    e = cfg.get("email") or {}
    if not (e.get("host") and e.get("to") and e.get("from")):
        raise RuntimeError("Email is not fully configured (host, from and to are required)")
    password = (vault.read_secret(db, e.get("password_ref"), org.id) or {}).get("password") if e.get("password_ref") else None
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject[:200], e["from"], ", ".join(e["to"])
    msg.set_content(text)
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
            smtp.login(e["username"], password)
        smtp.send_message(msg)


def send(db: Session, org: Organization, subject: str, text: str, channels: tuple[str, ...] = ("slack", "email")) -> dict:
    """Deliver to every configured channel. Returns {channel: 'sent' | error text} (used by the Settings test button)."""
    cfg = config_of(org)
    results: dict[str, str] = {}
    if "slack" in channels and cfg.get("slack_secret_ref"):
        try:
            _slack(db, org, cfg, f"*{subject}*\n{text}")
            results["slack"] = "sent"
        except Exception as e:  # noqa: BLE001 - report any delivery problem to the caller
            results["slack"] = f"{type(e).__name__}: {str(e)[:200]}"
    if "email" in channels and (cfg.get("email") or {}).get("host"):
        try:
            _email(db, org, cfg, subject, text)
            results["email"] = "sent"
        except Exception as e:  # noqa: BLE001
            results["email"] = f"{type(e).__name__}: {str(e)[:200]}"
    return results


def _dispatch(alerts: list[dict]) -> None:
    try:
        with session_scope() as db:
            for a in alerts:
                org = db.get(Organization, a["org_id"])
                cfg = config_of(org)
                if not cfg.get("enabled") or RANK.get(a["severity"], 0) < RANK.get(cfg.get("min_severity", "high"), 3):
                    continue
                key = f"{a['org_id']}|{a['message']}"
                with _RECENT_LOCK:
                    if time.monotonic() - _RECENT.get(key, -1e9) < DEDUPE_SECONDS:
                        continue
                    _RECENT[key] = time.monotonic()
                link = f"{get_settings().public_base_url.rstrip('/')}/{a['link']}" if a.get("link") else ""
                res = send(db, org, f"[H.E.D.E.S] {a['severity'].upper()}: {a['kind']}", a["message"] + (f"\n{link}" if link else ""))
                for channel, outcome in res.items():
                    if outcome != "sent":
                        log.warning("alert delivery via %s failed: %s", channel, outcome)
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
