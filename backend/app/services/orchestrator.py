"""AI Studio orchestration: a persisted state machine driving the agents.

COLLECT_REQUIREMENTS -> DISCOVER_API -> DESIGN_INTEGRATION -> GENERATE_CONFIGURATION -> VALIDATE_CONFIGURATION
-> SECURITY_EVALUATION -> RUN_SAFE_TESTS -> AWAIT_ADMIN_APPROVAL -> PUBLISHED

Each stage writes its status and output to ai_sessions.state, so a failed stage can be retried without
repeating the earlier ones. The pipeline can submit for review on the user's behalf, but approval is a
separate human action enforced by the approvals API.
"""
from __future__ import annotations

import logging
import threading
import traceback

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import session_scope, utcnow
from ..models import AIMessage, AISession, Integration, IntegrationVersion, Tool, User, Workflow, WorkflowVersion
from . import agents, audit, integrations
from .ai_gateway import Gateway, ProviderError

log = logging.getLogger("integra.orchestrator")
STAGES = ["COLLECT_REQUIREMENTS", "DISCOVER_API", "DESIGN_INTEGRATION", "GENERATE_CONFIGURATION", "VALIDATE_CONFIGURATION",
          "SECURITY_EVALUATION", "RUN_SAFE_TESTS", "AWAIT_ADMIN_APPROVAL", "PUBLISHED"]
_running: set[str] = set()
_lock = threading.Lock()


def _set(db: Session, s: AISession, stage: str, status: str, detail: str = "", **extra) -> None:
    state = dict(s.state or {})
    stages = dict(state.get("stages") or {})
    stages[stage] = {**stages.get(stage, {}), "status": status, "detail": detail, "at": utcnow().isoformat(), **extra}
    state["stages"] = stages
    s.state = state
    s.stage = stage
    db.commit()


def _out(s: AISession, key: str, value=None):
    state = dict(s.state or {})
    if value is None:
        return (state.get("outputs") or {}).get(key)
    outputs = dict(state.get("outputs") or {})
    outputs[key] = value
    state["outputs"] = outputs
    s.state = state


def _say(db: Session, s: AISession, text: str, **meta) -> None:
    db.add(AIMessage(session_id=s.id, role="assistant", content=text, meta=meta))
    db.commit()


def handle_message(db: Session, s: AISession, text: str) -> AIMessage:
    db.add(AIMessage(session_id=s.id, role="user", content=text[:8000]))
    db.commit()
    return refresh_requirements(db, s)


def refresh_requirements(db: Session, s: AISession) -> AIMessage:
    msgs = [{"role": m.role, "content": m.content} for m in
            db.scalars(select(AIMessage).where(AIMessage.session_id == s.id).order_by(AIMessage.created_at)).all() if m.role != "system"]
    gw = Gateway(db, s.organization_id)
    try:
        req, model = agents.analyze_request(gw, msgs, s.documents or [], s.requirements or {})
    except ProviderError as e:
        _set(db, s, "COLLECT_REQUIREMENTS", "failed", str(e))
        msg = AIMessage(session_id=s.id, role="assistant", content=f"I couldn't reach any AI model: {e}", meta={"error": True})
        db.add(msg)
        db.commit()
        return msg
    s.requirements = req
    if s.title == "New integration" and req["services"]:
        s.title = req["summary"][:200]
    s.status = "collecting"
    _set(db, s, "COLLECT_REQUIREMENTS", "done" if req["ready"] else "waiting", req["summary"], model=model)
    msg = AIMessage(session_id=s.id, role="assistant", content=req["reply"], meta={"model": model, "ready": req["ready"]})
    db.add(msg)
    db.commit()
    return msg


def start(session_id: str, from_stage: str | None = None) -> None:
    with _lock:
        if session_id in _running:
            raise HTTPException(409, "Generation is already running for this session")
        _running.add(session_id)

    def work():
        try:
            with session_scope() as db:
                run_pipeline(db, db.get(AISession, session_id), from_stage)
        except Exception:
            log.exception("pipeline crashed")
            with session_scope() as db:
                s = db.get(AISession, session_id)
                s.status = "failed"
                _set(db, s, s.stage, "failed", "Internal error: " + traceback.format_exc(limit=1).strip().splitlines()[-1])
        finally:
            with _lock:
                _running.discard(session_id)
    threading.Thread(target=work, name=f"pipeline-{session_id[:8]}", daemon=True).start()


def run_pipeline(db: Session, s: AISession, from_stage: str | None = None) -> None:
    req = s.requirements or {}
    if not req.get("ready"):
        raise HTTPException(409, "Requirements are incomplete - answer the open questions first")
    user = db.get(User, s.user_id)
    gw = Gateway(db, s.organization_id)
    start_at = STAGES.index(from_stage) if from_stage in STAGES else 1
    s.status = "running"
    db.commit()

    def skip(stage):
        return STAGES.index(stage) < start_at and (s.state.get("stages") or {}).get(stage, {}).get("status") == "done"

    # DISCOVER_API: bind every service to verified documentation (OpenAPI or catalog) or flag it.
    if not skip("DISCOVER_API"):
        _set(db, s, "DISCOVER_API", "running", "Reading API documentation")
        found = []
        for svc in req["services"]:
            ops = agents.service_ops(svc, s.documents or [])
            found.append({"service": svc["slug"], "name": svc["name"], "source": svc.get("source", "unknown"), "operations": len(ops)})
        _out(s, "discovery", found)
        unknown = [f["name"] for f in found if not f["operations"] and f["source"] != "model_knowledge"]
        if unknown:
            s.status = "failed"
            _set(db, s, "DISCOVER_API", "failed", "No documentation for " + ", ".join(unknown) + ". Upload an OpenAPI document.")
            return
        _set(db, s, "DISCOVER_API", "done", "; ".join(f"{f['name']}: {f['operations']} operations ({f['source'].replace('_', ' ')})" for f in found))

    # DESIGN_INTEGRATION + GENERATE_CONFIGURATION
    if not skip("GENERATE_CONFIGURATION"):
        _set(db, s, "DESIGN_INTEGRATION", "done", f"{len(req['services'])} integration(s), {len(req['operations'])} operation(s); "
             "credentials via connections: " + ", ".join(sv["slug"] for sv in req["services"]))
        _set(db, s, "GENERATE_CONFIGURATION", "running", "Generating configurations")
        created, notes, models = [], [], set()
        for svc in req["services"]:
            try:
                cfg, meta = agents.generate_config(gw, svc, req, s.documents or [])
            except (ValueError, ProviderError) as e:
                s.status = "failed"
                _set(db, s, "GENERATE_CONFIGURATION", "failed", f"{svc['name']}: {e}")
                return
            integ, v = integrations.create_integration(db, s.organization_id, user, cfg, "ai", s.id, meta.get("note", ""))
            created.append({"slug": integ.slug, "integration_id": integ.id, "version_id": v.id, "version": v.version})
            notes.append(f"{integ.name} {v.version}")
            models.add(meta.get("model", ""))
        _out(s, "integrations", created)
        db.commit()
        _set(db, s, "GENERATE_CONFIGURATION", "done", ", ".join(notes), model=", ".join(m for m in models if m))

    created = _out(s, "integrations") or []

    # VALIDATE_CONFIGURATION
    _set(db, s, "VALIDATE_CONFIGURATION", "running")
    invalid = []
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        integrations.analyse(db, v, s.organization_id)
        if not v.validation["valid"]:
            invalid.append(f"{c['slug']}: " + "; ".join(e["message"] for e in v.validation["errors"][:3]))
    db.commit()
    if invalid:
        s.status = "failed"
        _set(db, s, "VALIDATE_CONFIGURATION", "failed", " | ".join(invalid))
        return
    _set(db, s, "VALIDATE_CONFIGURATION", "done", "All configurations match the integration schema")

    # SECURITY_EVALUATION
    _set(db, s, "SECURITY_EVALUATION", "running")
    blocking, summaries, model = [], [], ""
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        report = dict(v.security_report)
        try:
            narrative, model = agents.explain_risks(gw, report)
            report["ai_summary"], report["ai_concerns"], report["ai_model"] = narrative["summary"], narrative.get("concerns", []), model
            v.security_report = report
        except ProviderError:
            pass
        summaries.append(f"{c['slug']}: risk {report['score']}/100 ({report['level']})")
        if report["blocking"]:
            blocking.append(c["slug"])
    db.commit()
    if blocking:
        s.status = "failed"
        _set(db, s, "SECURITY_EVALUATION", "failed", "Blocking findings in " + ", ".join(blocking) + ". Revise the request.")
        return
    _set(db, s, "SECURITY_EVALUATION", "done", "; ".join(summaries), model=model)

    # RUN_SAFE_TESTS (mocked transport - no live calls before approval)
    _set(db, s, "RUN_SAFE_TESTS", "running")
    failures, totals = [], []
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        tr = integrations.run_tests(db, v, user, s.documents or [])
        totals.append(f"{c['slug']}: {tr.report['passed']}/{tr.report['total']} passed")
        if tr.result != "passed":
            failures.append(c["slug"])
    db.commit()
    if failures:
        s.status = "failed"
        _set(db, s, "RUN_SAFE_TESTS", "failed", "; ".join(totals))
        return
    _set(db, s, "RUN_SAFE_TESTS", "done", "; ".join(totals))

    # AWAIT_ADMIN_APPROVAL: submit on the user's behalf (approval itself is a separate human decision).
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        integ = db.get(Integration, c["integration_id"])
        if v.status == "draft":
            req_row = integrations.submit_for_review(db, v, integ, user)
            c["approval_id"] = req_row.id
    _out(s, "integrations", created)
    db.commit()
    _set(db, s, "AWAIT_ADMIN_APPROVAL", "waiting", "Submitted to administrators for review")

    if req.get("triggers") and len(req["services"]) >= 1:
        _plan_workflow(db, s, gw, user, req)

    s.status = "ready"
    db.commit()
    lines = ["Done. I generated, validated, security-checked and tested:"]
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        lines.append(f"- **{c['slug']}** {c['version']} - risk {v.security_report['level']}, tests passed, submitted for approval")
    lines.append("\nNext: connect the credentials (they go to the vault, never to me), then an administrator reviews and publishes.")
    wf = _out(s, "workflow")
    if wf:
        lines.append(f"I also drafted a workflow, **{wf['name']}** - you can review it in Workflows once the integrations are published.")
    _say(db, s, "\n".join(lines), stage="AWAIT_ADMIN_APPROVAL")


def _plan_workflow(db: Session, s: AISession, gw: Gateway, user, req: dict) -> None:
    tools: dict[str, dict] = {}
    for c in _out(s, "integrations") or []:
        v = db.get(IntegrationVersion, c["version_id"])
        for op in v.config["integration"]["operations"]:
            resp = op.get("response") or {}
            tools[f"{c['slug']}.{op['id']}"] = {"input_schema": op["input_schema"],
                                                 "output_fields": ["items", "count"] if resp.get("items_path") else list((resp.get("mapping") or {}).keys())}
    for t in db.scalars(select(Tool).where(Tool.organization_id == s.organization_id, Tool.enabled.is_(True))).all():
        tools.setdefault(t.name, {"input_schema": t.input_schema, "output_fields": t.output_fields})
    try:
        graph, model = agents.plan_workflow(gw, req, tools)
    except ProviderError:
        return
    wf = Workflow(organization_id=s.organization_id, name=req["summary"][:160], description=(req.get("triggers") or [{}])[0].get("event", ""),
                  created_by=user.id)
    db.add(wf)
    db.flush()
    wv = WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, created_by=user.id)
    db.add(wv)
    db.flush()
    wf.current_version_id = wv.id
    _out(s, "workflow", {"id": wf.id, "name": wf.name, "model": model})
    audit.record(db, s.organization_id, user, "workflow.create", wf.name, source="ai")
    db.commit()


def session_view(db: Session, s: AISession) -> dict:
    """Session state for the UI, with live statuses of the generated integrations."""
    outputs = (s.state or {}).get("outputs") or {}
    items = []
    conns = integrations.connections_map(db, s.organization_id)
    for c in outputs.get("integrations") or []:
        integ = db.get(Integration, c["integration_id"])
        v = db.get(IntegrationVersion, c["version_id"])
        if not integ or not v:
            continue
        tr = integrations.latest_test(db, v)
        items.append({"integration_id": integ.id, "slug": integ.slug, "name": integ.name, "status": integ.status,
                      "version_id": v.id, "version": v.version, "version_status": v.status, "config": v.config,
                      "validation": v.validation, "security": v.security_report,
                      "tests": tr.report if tr else None, "approval_id": c.get("approval_id"),
                      "credential_ref": v.config["integration"]["authentication"].get("credential_ref"),
                      "auth_type": v.config["integration"]["authentication"]["type"],
                      "credential_connected": (conns.get(v.config["integration"]["authentication"].get("credential_ref")) or {}).get("status") == "active"})
    stages = dict((s.state or {}).get("stages") or {})
    if items and all(i["version_status"] == "published" for i in items):
        stages["AWAIT_ADMIN_APPROVAL"] = {**stages.get("AWAIT_ADMIN_APPROVAL", {}), "status": "done", "detail": "Approved"}
        stages["PUBLISHED"] = {"status": "done", "detail": "Live in the tool registry"}
    elif any(i["version_status"] in ("rejected", "changes_requested") for i in items):
        stages["AWAIT_ADMIN_APPROVAL"] = {**stages.get("AWAIT_ADMIN_APPROVAL", {}), "status": "failed",
                                          "detail": "An administrator asked for changes - describe the fix and generate again"}
    msgs = db.scalars(select(AIMessage).where(AIMessage.session_id == s.id).order_by(AIMessage.created_at)).all()
    with _lock:
        running = s.id in _running
    return {"id": s.id, "title": s.title, "status": "running" if running else s.status, "stage": s.stage,
            "requirements": s.requirements, "stages": stages, "stage_order": STAGES,
            "documents": [{"id": d["id"], "name": d["name"], "title": d["catalog"]["title"],
                           "operations": len(d["catalog"]["operations"]), "base_url": d["catalog"].get("base_url")} for d in s.documents or []],
            "integrations": items, "workflow": outputs.get("workflow"),
            "messages": [{"id": m.id, "role": m.role, "content": m.content, "meta": m.meta, "created_at": m.created_at.isoformat()} for m in msgs],
            "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat() if s.updated_at else None}
