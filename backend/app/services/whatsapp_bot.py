"""WhatsApp chatbot: browse and operate H.A.D.E.S from a chat with the business number.

Only verified administrator numbers (see services/whatsapp.py) get answers; other senders are ignored. Each reply is a
WhatsApp interactive list or set of buttons whose ids are the next command, so the whole platform is reachable by tapping:

  menu -> Integrations / Data sources / Alerts / Approvals / Workflows / Ask AI
  int:<id>  details and config summary (never credentials)      src:<id>  retry, pause, resume
  al:<id>   acknowledge, mute                                     ap:<id>   approve (two taps, bound to the config hash), reject
  wf:<id>   status and recent runs                                any other text -> the read-only AI assistant

Actions run as that administrator, with their role, and are audited with via=whatsapp-chat. Approving from chat can be
turned off in Settings (chat_approvals); then the bot sends the signed fix link instead.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import session_scope, utcnow
from ..core.security import ROLE_RANK, Principal
from ..models import (Alert, ApprovalRequest, AssistantThread, Execution, IngestSource, Integration, IntegrationVersion, Membership,
                      Organization, RepairProposal, User, WhatsAppRecipient, Workflow, WorkflowRun)
from . import assistant, audit, ingest, integrations, whatsapp

log = logging.getLogger("integra.whatsapp.bot")
PAGE = 9  # list rows per page; the 10th is "More"
MENU_WORDS = {"hi", "hello", "hey", "menu", "start", "help", "?", "home"}
KEYWORDS = {"integrations": "int:list", "sources": "src:list", "data sources": "src:list", "alerts": "al:list",
            "approvals": "ap:list", "reviews": "ap:list", "workflows": "wf:list"}
THREAD_TITLE = "WhatsApp chat"

_seen: dict[str, float] = {}      # message ids already handled (Meta retries webhooks)
_pending: dict[str, dict] = {}    # phone -> {"reject": request_id, "until": monotonic}
_rate: dict[str, list[float]] = {}
_lock = threading.Lock()


# ---------- entry point ----------

def enqueue(org_id: str, message: dict) -> None:
    mid = message.get("id", "")
    with _lock:
        now = time.monotonic()
        for k in [k for k, t in _seen.items() if now - t > 3600]:
            _seen.pop(k, None)
        if mid in _seen:
            return
        _seen[mid] = now
    threading.Thread(target=_handle, args=(org_id, message), name="whatsapp-bot", daemon=True).start()


def _throttled(phone: str) -> bool:
    with _lock:
        now = time.monotonic()
        hits = [t for t in _rate.get(phone, []) if now - t < 60] + [now]
        _rate[phone] = hits
        return len(hits) > 30


def _sender(db: Session, org_id: str, phone: str) -> tuple[WhatsAppRecipient, Principal] | None:
    row = db.execute(select(WhatsAppRecipient, User, Membership.role).join(User, User.id == WhatsAppRecipient.user_id).join(
        Membership, (Membership.user_id == WhatsAppRecipient.user_id) & (Membership.organization_id == WhatsAppRecipient.organization_id)).where(
        WhatsAppRecipient.organization_id == org_id, WhatsAppRecipient.phone == phone, WhatsAppRecipient.verified_at.isnot(None),
        User.status == "active")).first()
    if not row or ROLE_RANK.get(row[2], 0) < ROLE_RANK["admin"]:
        return None
    return row[0], Principal(row[1], org_id, row[2], "whatsapp")


def _command(phone: str, message: dict) -> tuple[str, str]:
    """Returns (command id, free text)."""
    kind = message.get("type")
    if kind == "interactive":
        inter = message.get("interactive") or {}
        reply = inter.get("list_reply") or inter.get("button_reply") or {}
        return reply.get("id", "menu"), ""
    if kind == "button":  # quick-reply button on a template
        return (message.get("button") or {}).get("payload") or "menu", ""
    text = ((message.get("text") or {}).get("body") or "").strip()
    with _lock:
        pending = _pending.pop(phone, None)
    if pending and pending["until"] > time.monotonic() and text.lower() != "cancel":
        return f"ap:reason:{pending['reject']}", text
    low = text.lower().strip(" !.")
    if not text or low in MENU_WORDS or low == "cancel":
        return "menu", ""
    if low in KEYWORDS:
        return KEYWORDS[low], ""
    return "ai", text


def _handle(org_id: str, message: dict) -> None:
    phone = message.get("from", "")
    try:
        with session_scope() as db:
            org = db.get(Organization, org_id)
            if not org or not whatsapp.config_of(org).get("chat_enabled"):
                return
            who = _sender(db, org_id, phone)
            if not who:
                log.info("WhatsApp chat: ignored a message from an unregistered number %s", whatsapp.mask_phone(phone))
                return
            whatsapp.mark_read(db, org, message.get("id", ""))
            if _throttled(phone):
                return
            cmd, text = _command(phone, message)
            bot = Bot(db, org, who[1], phone)
            try:
                bot.run(cmd, text)
            except HTTPException as e:
                db.rollback()
                whatsapp.send_buttons(db, org, phone, f"⚠️ {e.detail}", [("menu", "Menu")])
    except Exception:  # background job: log, never crash the server
        log.exception("WhatsApp chat message failed")


# ---------- the bot ----------

def _when(dt) -> str:
    if not dt:
        return "never"
    s = (utcnow() - dt).total_seconds()
    return "just now" if s < 60 else f"{int(s // 60)} min ago" if s < 3600 else f"{int(s // 3600)} h ago" if s < 86400 else dt.strftime("%d %b")


class Bot:
    def __init__(self, db: Session, org: Organization, p: Principal, phone: str):
        self.db, self.org, self.p, self.phone = db, org, p, phone

    # sending
    def text(self, body: str):
        whatsapp.send_text(self.db, self.org, self.phone, body)

    def buttons(self, body: str, buttons: list[tuple[str, str]]):
        whatsapp.send_buttons(self.db, self.org, self.phone, body, buttons + ([("menu", "Menu")] if len(buttons) < 3 else []))

    def pick(self, body: str, button: str, rows: list[dict], cmd: str, offset: int, title: str):
        if not rows:
            return self.buttons(body + "\n\nNothing here.", [])
        page = rows[offset:offset + PAGE]
        if len(rows) > offset + PAGE:
            page.append({"id": f"{cmd}:{offset + PAGE}", "title": "More…", "description": f"{len(rows) - offset - PAGE} more"})
        else:
            page.append({"id": "menu", "title": "Menu", "description": "Back to the main menu"})
        whatsapp.send_list(self.db, self.org, self.phone, body, button, page, title)

    def audit(self, action: str, target: str, **meta):
        audit.record(self.db, self.p.org_id, self.p, f"chat.{action}", target, via="whatsapp-chat", **meta)

    # routing
    def run(self, cmd: str, text: str):
        parts = cmd.split(":")
        head, arg = parts[0], parts[1] if len(parts) > 1 else ""
        rest = parts[2:]
        handlers = {"menu": self.menu, "int": self.integration, "src": self.source, "al": self.alert, "ap": self.approval,
                    "wf": self.workflow, "ai": lambda *_: self.ask(text)}
        if head not in handlers:
            return self.menu()
        if head in ("menu", "ai"):
            return handlers[head]()
        return handlers[head](arg, rest, text)

    def _q(self, model, *where, order=None, limit: int | None = None):
        stmt = select(model).where(model.organization_id == self.p.org_id, *where)
        if order is not None:
            stmt = stmt.order_by(order)
        return self.db.scalars(stmt.limit(limit) if limit else stmt)

    def _count(self, model, *where) -> int:
        return self.db.scalar(select(func.count()).select_from(model).where(model.organization_id == self.p.org_id, *where)) or 0

    def menu(self):
        open_alerts = self._count(Alert, Alert.acknowledged.is_(False))
        pending = self._count(ApprovalRequest, ApprovalRequest.decision == "pending")
        failing = self._count(IngestSource, IngestSource.state.in_(("failing", "degraded")))
        rows = [{"id": "int:list", "title": "Integrations", "description": f"{self._count(Integration)} in total"},
                {"id": "src:list", "title": "Data sources", "description": f"{failing} failing or degraded" if failing else "all healthy"},
                {"id": "al:list", "title": "Alerts", "description": f"{open_alerts} open"},
                {"id": "ap:list", "title": "Approvals", "description": f"{pending} waiting for review"},
                {"id": "wf:list", "title": "Workflows", "description": f"{self._count(Workflow)} in total"},
                {"id": "ai", "title": "Ask AI", "description": "Or just type a question at any time"}]
        whatsapp.send_list(self.db, self.org, self.phone,
                           f"*H.A.D.E.S* · {self.org.name}\nHi {self.p.user.name or self.p.user.username}. What do you want to see?",
                           "Open menu", rows, "Main menu")

    # integrations
    def integration(self, arg: str, rest: list[str], _text: str = ""):
        if arg in ("list", ""):
            items = self._q(Integration).all()
            items.sort(key=lambda i: (i.status != "published", i.name.lower()))
            rows = [{"id": f"int:{i.id}", "title": i.name, "description": f"{i.status.replace('_', ' ')} · {i.slug}"} for i in items]
            return self.pick(f"*Integrations* ({len(items)})", "Choose", rows, "int:list", int(rest[0]) if rest else 0, "Integrations")
        integ = self.db.get(Integration, arg)
        if not integ or integ.organization_id != self.p.org_id:
            raise HTTPException(404, "Integration not found")
        live = self.db.get(IntegrationVersion, integ.published_version_id) if integ.published_version_id else None
        latest = self.db.get(IntegrationVersion, integ.latest_version_id) if integ.latest_version_id else None
        v = live or latest
        cfg = (v.config if v else {}).get("integration", {})
        auth = cfg.get("authentication") or {}
        since = utcnow() - timedelta(hours=24)
        runs = self._count(Execution, Execution.integration_id == integ.id, Execution.created_at >= since)
        failed = self._count(Execution, Execution.integration_id == integ.id, Execution.created_at >= since, Execution.status != "succeeded")
        last_err = self._q(Execution, Execution.integration_id == integ.id, Execution.status != "succeeded", order=Execution.created_at.desc()).first()
        tr = integrations.latest_test(self.db, v) if v else None
        ops = "\n".join(f"  • {o['id']}: {o['method']} {o['path']}" for o in (cfg.get("operations") or [])[:12]) or "  none"
        body = (f"*{integ.name}* ({integ.slug})\nStatus: {integ.status.replace('_', ' ')}\n"
                f"Published: {live.version if live else '-'} · latest: {latest.version if latest else '-'}\n"
                f"Base URL: {cfg.get('base_url', '-')}\nAuth: {auth.get('type', 'none')}" + (f" (credential '{auth['credential_ref']}')" if auth.get("credential_ref") else "") +
                f"\nTests: {tr.result if tr else 'not run'}\nLast 24 h: {runs} runs, {failed} failed"
                + (f"\nLast error: [{last_err.error_class}] {last_err.error[:200]} ({_when(last_err.created_at)})" if last_err else "")
                + f"\n\nOperations:\n{ops}")
        buttons = []
        if self._count(IngestSource, IngestSource.integration_id == integ.id):
            buttons.append((f"src:of:{integ.id}", "Data sources"))
        req = self._q(ApprovalRequest, ApprovalRequest.integration_id == integ.id, ApprovalRequest.decision == "pending").first()
        if req:
            buttons.append((f"ap:{req.id}", "Pending review"))
        self.buttons(body, buttons)

    # data sources
    def _source_rows(self, sources: list[IngestSource]) -> list[dict]:
        names = {i.id: i.name for i in self._q(Integration).all()}
        return [{"id": f"src:{s.id}", "title": names.get(s.integration_id, "?"),
                 "description": f"{s.operation_id} · {'paused' if not s.enabled else s.state.replace('_', ' ')}"} for s in sources]

    def source(self, arg: str, rest: list[str], _text: str = ""):
        if arg in ("list", "of", ""):
            ingest.sync_sources(self.db, self.p.org_id)  # newly published ingest operations become sources
            q = [IngestSource.integration_id == rest[0]] if arg == "of" else []
            items = self._q(IngestSource, *q).all()
            rank = {"failing": 0, "degraded": 1, "running": 2, "never_run": 3, "ok": 4}
            items.sort(key=lambda s: (rank.get(s.state, 5), s.operation_id))
            offset = int(rest[0]) if arg == "list" and rest else 0
            return self.pick(f"*Data sources* ({len(items)})", "Choose", self._source_rows(items), "src:list", offset, "Data sources")
        if arg in ("retry", "pause", "resume"):
            return self._source_action(arg, rest[0] if rest else "")
        src = self._src(arg)
        integ = self.db.get(Integration, src.integration_id)
        h = ingest.health(src)
        body = (f"*{integ.name if integ else '?'}* · {src.operation_id}\nHealth: {h['status']} ({h['reason']})\n"
                f"Enabled: {'yes' if src.enabled else 'no (paused)'}\nSchedule: {'cron ' + src.cron if src.cron else f'every {src.interval_minutes} min'}\n"
                f"Last run: {_when(src.last_run_at)} · last success: {_when(src.last_success_at)}\n"
                f"Stored {src.total_stored} · duplicates {src.total_duplicates} · rejected {src.total_rejected}"
                + (f"\nFailed {src.consecutive_failures} in a row" if src.consecutive_failures else "")
                + (f"\nLast error [{src.last_error_class}]: {src.last_error[:300]}" if src.last_error else ""))
        btns = [(f"src:retry:{src.id}", "Retry now"), (f"src:pause:{src.id}", "Pause")] if src.enabled else [(f"src:resume:{src.id}", "Resume")]
        self.buttons(body, btns)

    def _src(self, sid: str) -> IngestSource:
        src = self.db.get(IngestSource, sid)
        if not src or src.organization_id != self.p.org_id:
            raise HTTPException(404, "Data source not found")
        return src

    def _source_action(self, action: str, sid: str):
        src = self._src(sid)
        target = f"{src.integration_id}.{src.operation_id}"
        if action == "retry":
            if not src.enabled:
                raise HTTPException(409, "This source is paused. Resume it first.")
            if not ingest.try_acquire(src.id):
                raise HTTPException(409, "A run is already in progress")
            try:
                run = ingest.run_source(self.db, src, "manual")
            except ValueError as e:
                raise HTTPException(409, str(e))
            finally:
                ingest.release(src.id)
            self.audit("retry", target, status=run.status)
            self.db.commit()
            msg = f"Run {run.status}: fetched {run.fetched}, stored {run.stored}, rejected {run.rejected}" + (f"\n{run.error[:300]}" if run.error else "")
        else:
            src.enabled = action == "resume"
            if src.enabled:
                src.next_run_at, src.consecutive_failures = utcnow(), 0
            self.audit(action, target)
            self.db.commit()
            msg = "Resumed; it runs on its next schedule." if src.enabled else "Paused. It will not run until resumed."
        self.buttons(f"✅ {msg}", [(f"src:{src.id}", "Show source")])

    # alerts
    def alert(self, arg: str, rest: list[str], _text: str = ""):
        if arg in ("list", ""):
            items = self._q(Alert, Alert.acknowledged.is_(False), order=Alert.created_at.desc(), limit=60).all()
            rows = [{"id": f"al:{a.id}", "title": f"{a.severity} · {a.kind}", "description": a.message} for a in items]
            return self.pick(f"*Open alerts* ({len(items)})", "Choose", rows, "al:list", int(rest[0]) if rest else 0, "Alerts")
        if arg == "ack":
            a = self._alert(rest[0] if rest else "")
            a.acknowledged = True
            self.audit("ack", a.kind, alert_id=a.id)
            self.db.commit()
            return self.buttons("✅ Acknowledged.", [("al:list", "Open alerts")])
        if arg == "mute":
            a = self._alert(rest[0] if rest else "")
            minutes = int(rest[1]) if len(rest) > 1 else 1440
            if not a.integration_id:
                raise HTTPException(409, "This alert is not about an integration")
            whatsapp.set_mute(self.org, a.integration_id, minutes)
            self.audit("mute", a.integration_id, minutes=minutes)
            self.db.commit()
            return self.buttons(f"🔕 Alerts for this integration are muted for {minutes // 60} h (they still show in the app).", [("al:list", "Open alerts")])
        a = self._alert(arg)
        integ = self.db.get(Integration, a.integration_id) if a.integration_id else None
        body = f"*{a.severity.upper()} · {a.kind.replace('_', ' ')}* ({_when(a.created_at)})\n{a.message}"
        btns = [] if a.acknowledged else [(f"al:ack:{a.id}", "Acknowledge")]
        if integ and integ.organization_id == self.p.org_id:
            btns.append((f"al:mute:{a.id}:1440", "Mute 24 h"))
            if len(btns) < 3:
                btns.append((f"int:{integ.id}", "Integration"))
        self.buttons(body, btns)

    def _alert(self, aid: str) -> Alert:
        a = self.db.get(Alert, aid)
        if not a or a.organization_id != self.p.org_id:
            raise HTTPException(404, "Alert not found")
        return a

    # approvals
    def approval(self, arg: str, rest: list[str], text: str = ""):
        if arg in ("list", ""):
            items = self._q(ApprovalRequest, ApprovalRequest.decision == "pending", order=ApprovalRequest.created_at.desc()).all()
            names = {i.id: i.name for i in self._q(Integration).all()}
            versions = {v.id: v.version for v in self.db.scalars(select(IntegrationVersion).where(
                IntegrationVersion.id.in_([r.version_id for r in items] or [""]))).all()}
            rows = [{"id": f"ap:{r.id}", "title": names.get(r.integration_id, "?"),
                     "description": f"{versions.get(r.version_id, '')} · {r.kind} · risk {(r.risk or {}).get('level', 'n/a')}"} for r in items]
            return self.pick(f"*Waiting for review* ({len(items)})", "Choose", rows, "ap:list", int(rest[0]) if rest else 0, "Approvals")
        if arg == "ok":
            return self._confirm(rest[0] if rest else "")
        if arg == "yes":
            return self._approve(rest[0] if rest else "", rest[1] if len(rest) > 1 else "")
        if arg == "no":
            req, *_ = self._request(rest[0] if rest else "")
            with _lock:
                _pending[self.phone] = {"reject": req.id, "until": time.monotonic() + 600}
            return self.text("Reply with the reason for rejecting (the requester sees it), or 'cancel'.")
        if arg == "reason":
            req, v, integ = self._request(rest[0] if rest else "")
            integrations.decide_negative(self.db, self.p.org_id, req.id, self.p, "rejected", text)
            self.audit("reject", f"{integ.slug}@{v.version}", request_id=req.id)
            self.db.commit()
            return self.buttons(f"❌ Rejected {integ.name} {v.version}. The live version is unchanged.", [("ap:list", "Approvals")])
        req, v, integ = self._request(arg)
        rp = self.db.scalars(select(RepairProposal).where(RepairProposal.approval_request_id == req.id)).first()
        tr = integrations.latest_test(self.db, v)
        blockers = integrations.approval_blockers(self.db, req, v, integ, self.p.id)
        changes = (rp.diagnosis or {}).get("mapping_changes") if rp else None
        summary = (rp.diagnosis or {}).get("summary") if rp else v.notes
        body = (f"*{integ.name} {v.version}* ({req.kind})\nRisk: {(req.risk or {}).get('level', 'n/a')} · tests: {tr.result if tr else 'not run'}\n"
                f"Config hash: {req.config_hash[:12]}" + (f"\n\n{summary[:500]}" if summary else "")
                + ("\n\nChanges:\n" + "\n".join(f"  • {k} → {val}" for k, val in list(changes.items())[:8]) if changes else "")
                + ("\n\n⛔ Cannot be approved yet:\n" + "\n".join(f"  • {b}" for b in blockers) if blockers else ""))
        btns = [] if blockers else [(f"ap:ok:{req.id}", "Approve")]
        self.buttons(body, btns + [(f"ap:no:{req.id}", "Reject")])

    def _request(self, rid: str) -> tuple[ApprovalRequest, IntegrationVersion, Integration]:
        req = self.db.get(ApprovalRequest, rid)
        if not req or req.organization_id != self.p.org_id:
            raise HTTPException(404, "Approval request not found")
        if req.decision != "pending":
            raise HTTPException(409, f"This request is already {req.decision.replace('_', ' ')}")
        return req, self.db.get(IntegrationVersion, req.version_id), self.db.get(Integration, req.integration_id)

    def _chat_approvals(self, req: ApprovalRequest) -> bool:
        if whatsapp.config_of(self.org).get("chat_approvals", True) is not False:
            return True
        link = whatsapp.issue_link(self.db, self.p.org_id, self.p.id, {"id": None, "integration_id": req.integration_id,
                                                                         "link": f"#/approvals/{req.id}"})
        self.db.commit()
        self.buttons(f"Approving from chat is turned off. Review and approve here (link valid {whatsapp.LINK_HOURS} h):\n{whatsapp.fix_url(link)}", [])
        return False

    def _confirm(self, rid: str):
        req, v, integ = self._request(rid)
        if not self._chat_approvals(req):
            return
        self.buttons(f"Publish *{integ.name} {v.version}* now?\nThis replaces the live version. Config hash {req.config_hash[:12]}.",
                     [(f"ap:yes:{req.id}:{req.config_hash[:12]}", "Confirm approve"), ("menu", "Cancel")])

    def _approve(self, rid: str, hash12: str):
        req, v, integ = self._request(rid)
        if not self._chat_approvals(req):
            return
        if not hash12 or req.config_hash[:12] != hash12:
            raise HTTPException(409, "The configuration changed since you looked at it. Open the request again.")
        integrations.approve(self.db, self.p.org_id, req.id, self.p, "Approved from WhatsApp chat")
        self.audit("approve", f"{integ.slug}@{v.version}", request_id=req.id, config_hash=req.config_hash)
        self.db.commit()
        self.buttons(f"✅ {integ.name} {v.version} is approved and published.", [(f"int:{integ.id}", "Integration")])

    # workflows
    def workflow(self, arg: str, rest: list[str], _text: str = ""):
        if arg in ("list", ""):
            items = self._q(Workflow, order=Workflow.name).all()
            rows = [{"id": f"wf:{w.id}", "title": w.name, "description": f"{w.status} · {w.description}"} for w in items]
            return self.pick(f"*Workflows* ({len(items)})", "Choose", rows, "wf:list", int(rest[0]) if rest else 0, "Workflows")
        w = self.db.get(Workflow, arg)
        if not w or w.organization_id != self.p.org_id:
            raise HTTPException(404, "Workflow not found")
        runs = self._q(WorkflowRun, WorkflowRun.workflow_id == w.id, order=WorkflowRun.started_at.desc(), limit=5).all()
        lines = "\n".join(f"  • {_when(r.started_at)}: {r.status} ({r.trigger})" + (f" {r.error[:80]}" if r.error else "") for r in runs) or "  none yet"
        self.buttons(f"*{w.name}*\nStatus: {w.status}\n{w.description[:400]}\n\nRecent runs:\n{lines}", [("wf:list", "Workflows")])

    # AI
    def ask(self, text: str):
        if not text:
            return self.text("Type your question, e.g. “why is the Okta source failing?”")
        if not assistant.configured(self.db, self.p.org_id):
            return self.buttons("No AI provider is configured, so I can only show the menus. Add one under AI Providers.", [])
        thread = self.db.scalars(select(AssistantThread).where(AssistantThread.user_id == self.p.id, AssistantThread.organization_id == self.p.org_id,
                                                               AssistantThread.title == THREAD_TITLE)).first()
        if not thread:
            thread = AssistantThread(organization_id=self.p.org_id, user_id=self.p.id, title=THREAD_TITLE)
            self.db.add(thread)
            self.db.commit()
        msg = assistant.chat(self.db, self.p, thread, text, [], "whatsapp")
        reply = msg.content.replace("**", "*")
        proposed = len([a for a in thread_actions(self.db, msg.id)])
        if proposed:
            reply += (f"\n\n(The assistant proposed {proposed} change{'s' if proposed > 1 else ''}. Changes suggested by the AI are never run from chat; "
                      f"review them in the app: {get_settings().public_base_url.rstrip('/')} → Ask AI → {THREAD_TITLE}.)")
        self.text(reply)


def thread_actions(db: Session, message_id: str):
    from ..models import AssistantAction
    return db.scalars(select(AssistantAction).where(AssistantAction.message_id == message_id)).all()
