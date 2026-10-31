"""Automatic workflow triggers: H.E.D.E.S events (a failure, an alert, a new record) start the active workflows whose
trigger is `automatic`, listens for that event and whose conditions all hold.

Events are collected on the database session and dispatched only after the transaction commits, so a rolled-back
change never starts a workflow. Two guards stop loops and floods: a failure caused by an automatic run never starts
another automatic run, and the same event starts a given workflow at most once per DEDUPE_SECONDS.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

log = logging.getLogger("hedes.automation")

EVENTS = {
    "failure": "A tool call, data-source run or workflow run fails",
    "alert": "H.E.D.E.S raises an alert (credentials, failing sources, self-healing, approvals, drift)",
    "record": "A data source stores a new event record",
}
# Fields each event carries, shown in the editor so conditions and inputs can reference ${trigger.<field>}.
FIELDS = {
    "failure": ["source", "tool", "integration", "error_class", "http_status", "error", "workflow", "link"],
    "alert": ["kind", "severity", "message", "link"],
    "record": ["source", "title", "severity", "category", "event_type", "timestamp", "description"],
}
SAMPLES = {
    "failure": {"source": "tool", "tool": "github.list_issues", "integration": "GitHub", "error_class": "auth_invalid", "http_status": 401,
                "error": "Bad credentials", "workflow": "", "link": "#/monitoring"},
    "alert": {"kind": "auth_invalid", "severity": "high", "message": "GitHub: the credential was rejected.", "link": "#/credentials"},
    "record": {"source": "nvd", "title": "CVE-2026-1234 in OpenSSL", "severity": "critical", "category": "vulnerability",
               "event_type": "cve", "timestamp": "2026-10-31T12:00:00+00:00", "description": "Remote code execution"},
}
AUTOMATIC_ACTOR = "automation"  # Execution.actor_type of tool calls made by automatic runs
DEDUPE_SECONDS = 300
MAX_RUNS_PER_COMMIT = 50
_RECENT: dict[str, float] = {}
_LOCK = threading.Lock()


def emit(db: Session, org_id: str, event: str, payload: dict) -> None:
    """Queue an event; it is delivered after the surrounding transaction commits."""
    if event == "failure" and payload.get("actor_type") == AUTOMATIC_ACTOR:
        return  # an automatic run's own failure must not start automatic runs (no loops)
    clean = json.loads(json.dumps(payload, default=str))
    db.info.setdefault("workflow_events", []).append((org_id, event, clean))


def matches(trigger: dict, event: str, payload: dict) -> bool:
    from .workflows import _cond
    if trigger.get("type") != "automatic" or trigger.get("event") != event:
        return False
    return all(_cond(c, {"trigger": payload, "steps": {}}) for c in trigger.get("conditions") or [])


def _first_seen(key: str) -> bool:
    now = time.monotonic()
    with _LOCK:
        if now - _RECENT.get(key, -1e9) < DEDUPE_SECONDS:
            return False
        _RECENT[key] = now
        if len(_RECENT) > 5000:
            for k in [k for k, t in _RECENT.items() if now - t > DEDUPE_SECONDS]:
                _RECENT.pop(k, None)
        return True


def _dispatch(events: list[tuple[str, str, dict]]) -> None:
    from sqlalchemy import select

    from ..core.db import session_scope
    from ..models import Workflow, WorkflowVersion
    from . import workflows
    try:
        with session_scope() as db:
            todo: list[tuple[str, dict]] = []
            per_wf: dict[str, int] = {}
            for org_id in {e[0] for e in events}:
                active = [(wf, db.get(WorkflowVersion, wf.current_version_id)) for wf in
                          db.scalars(select(Workflow).where(Workflow.organization_id == org_id, Workflow.status == "active")).all()]
                for wf, v in active:
                    trig = ((v.graph if v else {}) or {}).get("trigger") or {}
                    for _, event, payload in (e for e in events if e[0] == org_id):
                        if not matches(trig, event, payload) or per_wf.get(wf.id, 0) >= MAX_RUNS_PER_COMMIT:
                            continue
                        if _first_seen(f"{wf.id}|{event}|{json.dumps(payload, sort_keys=True)}"):
                            per_wf[wf.id] = per_wf.get(wf.id, 0) + 1
                            todo.append((wf.id, {**payload, "event": event}))
            for wf_id, payload in todo:  # one after another: a burst of records must not open hundreds of threads
                try:
                    workflows.execute(db, db.get(Workflow, wf_id), "automatic", payload)
                except Exception as e:  # noqa: BLE001 - an invalid workflow must not stop the others
                    db.rollback()
                    log.warning("automatic run of workflow %s failed to start: %s", wf_id, e)
    except Exception:
        log.exception("automatic workflow dispatch failed")


@sa_event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending = session.info.pop("workflow_events", None)
    if pending:
        threading.Thread(target=_dispatch, args=(pending,), name="automation", daemon=True).start()


@sa_event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("workflow_events", None)
