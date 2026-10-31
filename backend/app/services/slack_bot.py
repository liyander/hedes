"""Slack as a channel to the AI assistant.

People talk to the assistant in a direct message with the H.E.D.E.S app or by @mentioning it in a channel. It is the
same assistant as the in-app console: the same tools, the same permissions (each person acts as their linked H.E.D.E.S
account) and the same rule that nothing changes until that person presses Approve.

Connection: Slack Socket Mode. H.E.D.E.S opens an outbound WebSocket to Slack, so no public URL or tunnel is needed and
every event arrives over an authenticated connection created with the app-level token.

Identity: a Slack user is linked to a H.E.D.E.S account with a one-time code generated in that person's signed-in
session (Settings > Slack). Unlinked or disabled accounts are refused. Secrets are never accepted in Slack.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import session_scope, utcnow
from ..core.security import Principal
from ..models import AssistantAction, AssistantMessage, AssistantThread, Membership, Organization, User
from . import audit, vault

log = logging.getLogger("integra.slack")
API = "https://slack.com/api/"
CODE_TTL = 600
_CODES: dict[str, tuple[str, str, float]] = {}  # sha256(code) -> (org_id, user_id, expires)
_LOCK = threading.Lock()
STATUS: dict[str, dict] = {}  # org_id -> {"state", "detail", "since"}
_BRIDGES: dict[str, "Bridge"] = {}
MANIFEST = {
    "display_information": {"name": "H.E.D.E.S", "description": "Talk to the H.E.D.E.S AI assistant"},
    "features": {"bot_user": {"display_name": "H.E.D.E.S", "always_online": True},
                 "app_home": {"messages_tab_enabled": True, "messages_tab_read_only_enabled": False}},
    "oauth_config": {"scopes": {"bot": ["app_mentions:read", "chat:write", "im:history", "im:read", "im:write"]}},
    "settings": {"event_subscriptions": {"bot_events": ["app_mention", "message.im"]}, "interactivity": {"is_enabled": True},
                 "socket_mode_enabled": True, "org_deploy_enabled": False, "token_rotation_enabled": False},
}
ACTION_LABELS = {"create_workflow": "Create a workflow", "run_workflow": "Run a workflow", "investigate_failure": "Investigate a failure"}


# ----------------------------------------------------------------------------------------------- configuration

def config_of(org: Organization | None) -> dict:
    return dict(((org.policy if org else None) or {}).get("slack_bot") or {})


def save_config(db: Session, org: Organization, cfg: dict) -> None:
    org.policy = {**(org.policy or {}), "slack_bot": cfg}


def tokens(db: Session, org: Organization) -> tuple[str, str]:
    cfg = config_of(org)
    bot = (vault.read_secret(db, cfg.get("bot_ref"), org.id) or {}).get("token", "") if cfg.get("bot_ref") else ""
    app = (vault.read_secret(db, cfg.get("app_ref"), org.id) or {}).get("token", "") if cfg.get("app_ref") else ""
    return bot, app


class SlackError(Exception):
    pass


def call(token: str, method: str, **body) -> dict:
    """One Slack Web API call. Raises SlackError with Slack's error code (Slack answers HTTP 200 for failures)."""
    try:
        r = httpx.post(API + method, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=20)
    except httpx.HTTPError as e:
        raise SlackError(f"cannot reach Slack: {type(e).__name__}")
    try:
        data = r.json()
    except ValueError:
        raise SlackError(f"HTTP {r.status_code} from Slack")
    if not data.get("ok"):
        raise SlackError(data.get("error") or f"HTTP {r.status_code}" + (f" (needed: {data['needed']})" if data.get("needed") else ""))
    return data


# ----------------------------------------------------------------------------------------------- linking people

def new_link_code(org_id: str, user_id: str) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    now = time.time()
    with _LOCK:
        for k, (_, _, exp) in list(_CODES.items()):
            if exp < now or _CODES[k][1] == user_id:
                _CODES.pop(k, None)
        _CODES[hashlib.sha256(f"{org_id}:{code}".encode()).hexdigest()] = (org_id, user_id, now + CODE_TTL)
    return code


def redeem_link_code(db: Session, org: Organization, code: str, slack_user: str) -> User | None:
    key = hashlib.sha256(f"{org.id}:{code}".encode()).hexdigest()
    with _LOCK:
        entry = _CODES.pop(key, None)
    if not entry or entry[2] < time.time():
        return None
    user = db.get(User, entry[1])
    cfg = config_of(org)
    cfg["links"] = {**(cfg.get("links") or {}), slack_user: user.id}
    save_config(db, org, cfg)
    audit.record(db, org.id, user, "slack.link", user.username, slack_user=slack_user)
    return user


def principal_for(db: Session, org: Organization, slack_user: str) -> Principal | None:
    user_id = (config_of(org).get("links") or {}).get(slack_user)
    user = db.get(User, user_id) if user_id else None
    m = db.scalars(select(Membership).where(Membership.user_id == user_id, Membership.organization_id == org.id)).first() if user else None
    if not user or user.status != "active" or not m or m.role not in ("admin", "owner"):
        return None  # Slack access is for administrators; a link stops working if the person is disabled or demoted
    return Principal(user, org.id, m.role, "slack")


# ----------------------------------------------------------------------------------------------- formatting

def mrkdwn(text: str) -> str:
    """The assistant writes Markdown; Slack uses mrkdwn (*bold*, <url|text>)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    base = get_settings().public_base_url.rstrip("/")
    return re.sub(r"(?<![\w/])#/([\w/-]+)", lambda m: f"<{base}/#/{m.group(1)}|open in H.E.D.E.S>", text)


def _args_text(args: dict) -> str:
    parts = [f"{k}: {json.dumps(v) if not isinstance(v, str) else v}" for k, v in (args or {}).items()]
    return "; ".join(parts)[:600]


def action_blocks(act: AssistantAction) -> list[dict]:
    from .assistant import TOOLS
    label = ACTION_LABELS.get(act.tool, act.tool.replace("_", " ").capitalize())
    desc = TOOLS.get(act.tool, (False, "user", (), ""))[3]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":hand: *Approval needed - {label}*\n{act.why or desc}\n`{_args_text(act.args)}`"}},
        {"type": "actions", "block_id": f"hedes_{act.id}", "elements": [
            {"type": "button", "action_id": "hedes_approve", "value": act.id, "style": "primary", "text": {"type": "plain_text", "text": "Approve"}},
            {"type": "button", "action_id": "hedes_reject", "value": act.id, "style": "danger", "text": {"type": "plain_text", "text": "Reject"}}]},
    ]


# ----------------------------------------------------------------------------------------------- conversations

def _thread_for(db: Session, p: Principal, ref: str, text: str) -> AssistantThread | None:
    t = db.scalars(select(AssistantThread).where(AssistantThread.organization_id == p.org_id, AssistantThread.external_ref == ref)).first()
    if t and t.user_id != p.id:
        return None  # someone else's conversation in a shared channel thread: they start their own
    if not t:
        t = AssistantThread(organization_id=p.org_id, user_id=p.id, title=("Slack: " + text.strip().split("\n")[0])[:200], external_ref=ref)
        db.add(t)
        db.commit()
    return t


def _post_reply(token: str, db: Session, msg: AssistantMessage, channel: str, thread_ts: str) -> None:
    call(token, "chat.postMessage", channel=channel, thread_ts=thread_ts, text=mrkdwn(msg.content)[:39000], unfurl_links=False)
    if (msg.meta or {}).get("credential_requests"):
        base = get_settings().public_base_url.rstrip("/")
        call(token, "chat.postMessage", channel=channel, thread_ts=thread_ts,
             text=f":lock: Credentials are never accepted in Slack. Connect them securely in H.E.D.E.S: <{base}/#/credentials|Credentials>.")
    for act in db.scalars(select(AssistantAction).where(AssistantAction.message_id == msg.id, AssistantAction.status == "proposed")).all():
        call(token, "chat.postMessage", channel=channel, thread_ts=thread_ts, text=f"Approval needed: {act.tool}", blocks=action_blocks(act))


def handle_message(db: Session, org: Organization, token: str, event: dict) -> None:
    from fastapi import HTTPException

    from . import assistant
    channel, user = event.get("channel", ""), event.get("user", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text") or "").strip()
    if not text:
        return
    link = re.fullmatch(r"link\s+(\d{6})", text, re.I)
    if link:
        u = redeem_link_code(db, org, link.group(1), user)
        db.commit()
        call(token, "chat.postMessage", channel=channel, thread_ts=thread_ts,
             text=(f":white_check_mark: Linked to H.E.D.E.S account *{u.username}*. Ask me anything about your integrations, data sources, "
                   "workflows or failures." if u else ":x: That code is invalid or expired. Generate a new one in H.E.D.E.S > Settings > Slack."))
        return
    p = principal_for(db, org, user)
    if not p:
        call(token, "chat.postMessage", channel=channel, thread_ts=thread_ts,
             text="I don't know you yet. In H.E.D.E.S open *Settings > Slack* and press *Link my Slack account*, then send me `link <code>`.")
        return
    thread = _thread_for(db, p, f"slack:{channel}:{thread_ts}", text)
    if thread is None:
        call(token, "chat.postMessage", channel=channel, thread_ts=thread_ts, text="This thread belongs to someone else's conversation - mention me in a new message.")
        return
    try:
        msg = assistant.chat(db, p, thread, text, [], None)
    except HTTPException as e:
        call(token, "chat.postMessage", channel=channel, thread_ts=thread_ts, text=f":warning: {e.detail}")
        return
    _post_reply(token, db, msg, channel, thread_ts)


def handle_action(db: Session, org: Organization, token: str, payload: dict) -> None:
    from . import assistant
    act_info = (payload.get("actions") or [{}])[0]
    if act_info.get("action_id") not in ("hedes_approve", "hedes_reject"):
        return
    channel = (payload.get("container") or {}).get("channel_id") or (payload.get("channel") or {}).get("id", "")
    message_ts = (payload.get("container") or {}).get("message_ts") or (payload.get("message") or {}).get("ts", "")
    thread_ts = (payload.get("message") or {}).get("thread_ts") or message_ts
    p = principal_for(db, org, (payload.get("user") or {}).get("id", ""))
    act = db.get(AssistantAction, act_info.get("value", ""))
    if not p or not act or act.organization_id != org.id or act.user_id != p.id:
        call(token, "chat.postMessage", channel=channel, thread_ts=thread_ts,
             text=":no_entry: Only the person who asked for this action can approve or reject it.")
        return
    approve = act_info["action_id"] == "hedes_approve"
    act = assistant.decide(db, p, act, approve)
    outcome = {"executed": ":white_check_mark: Done", "failed": f":x: Failed: {act.error}", "rejected": ":no_entry_sign: Rejected"}.get(act.status, act.status)
    call(token, "chat.update", channel=channel, ts=message_ts, text=f"{act.tool}: {act.status}",
         blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"*{ACTION_LABELS.get(act.tool, act.tool)}* - {outcome}\n`{_args_text(act.args)}`"}}])
    thread = db.get(AssistantThread, act.thread_id)
    msg = assistant.follow_up(db, p, thread, act, None)
    if msg:
        _post_reply(token, db, msg, channel, thread_ts)


def handle_envelope(org_id: str, envelope: dict) -> None:
    """Process one Socket Mode envelope (already acknowledged). Runs in its own thread."""
    try:
        with session_scope() as db:
            org = db.get(Organization, org_id)
            token, _ = tokens(db, org)
            cfg = config_of(org)
            payload = envelope.get("payload") or {}
            if envelope.get("type") == "events_api":
                ev = payload.get("event") or {}
                if ev.get("bot_id") or ev.get("subtype") or ev.get("user") == cfg.get("bot_user_id"):
                    return
                if ev.get("type") == "app_mention" or (ev.get("type") == "message" and ev.get("channel_type") == "im"):
                    handle_message(db, org, token, ev)
            elif envelope.get("type") == "interactive" and payload.get("type") == "block_actions":
                handle_action(db, org, token, payload)
    except SlackError as e:
        log.warning("slack call failed: %s", e)
        STATUS[org_id] = {**STATUS.get(org_id, {}), "detail": f"Last Slack error: {e}"}
    except Exception:  # background worker: never take the connection down
        log.exception("slack event handling failed")


# ----------------------------------------------------------------------------------------------- Socket Mode connection

class Bridge(threading.Thread):
    def __init__(self, org_id: str, fingerprint: str):
        super().__init__(name=f"slack-{org_id[:8]}", daemon=True)
        self.org_id, self.fingerprint = org_id, fingerprint
        self.stop = threading.Event()

    def _status(self, state: str, detail: str = "") -> None:
        STATUS[self.org_id] = {"state": state, "detail": detail, "since": utcnow().isoformat()}

    def run(self) -> None:
        from websockets.sync.client import connect
        delay = 2
        while not self.stop.is_set():
            try:
                with session_scope() as db:
                    _, app_token = tokens(db, db.get(Organization, self.org_id))
                self._status("connecting")
                url = call(app_token, "apps.connections.open")["url"]
                with connect(url, open_timeout=15, close_timeout=2) as ws:
                    self._status("connected")
                    delay = 2
                    while not self.stop.is_set():
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        env = json.loads(raw)
                        if env.get("envelope_id"):
                            ws.send(json.dumps({"envelope_id": env["envelope_id"]}))  # acknowledge within Slack's 3 seconds
                        if env.get("type") == "disconnect":
                            break
                        if env.get("type") in ("events_api", "interactive"):
                            threading.Thread(target=handle_envelope, args=(self.org_id, env), daemon=True).start()
            except SlackError as e:
                self._status("error", f"Slack refused the app-level token: {e}")
            except Exception as e:  # network drops, Slack restarts: reconnect with backoff
                self._status("error", f"{type(e).__name__}: {str(e)[:200]}")
            if not self.stop.wait(delay):
                delay = min(delay * 2, 60)
        self._status("stopped")


def _fingerprint(cfg: dict) -> str:
    return f"{cfg.get('bot_ref')}|{cfg.get('app_ref')}"


def sync_bridges() -> None:
    """Start, restart or stop each organisation's connection to match its Slack settings."""
    with session_scope() as db:
        wanted = {o.id: _fingerprint(config_of(o)) for o in db.scalars(select(Organization)).all()
                  if config_of(o).get("enabled") and config_of(o).get("bot_ref") and config_of(o).get("app_ref")}
    for org_id, bridge in list(_BRIDGES.items()):
        if wanted.get(org_id) != bridge.fingerprint or not bridge.is_alive():
            bridge.stop.set()
            _BRIDGES.pop(org_id, None)
            if org_id not in wanted:
                STATUS[org_id] = {"state": "stopped", "detail": "", "since": utcnow().isoformat()}
    for org_id, fp in wanted.items():
        if org_id not in _BRIDGES:
            _BRIDGES[org_id] = Bridge(org_id, fp)
            _BRIDGES[org_id].start()


def supervisor(stop: threading.Event) -> None:
    while True:
        try:
            sync_bridges()
        except Exception:
            log.exception("slack supervisor tick failed")
        if stop.wait(15):
            for b in _BRIDGES.values():
                b.stop.set()
            return
