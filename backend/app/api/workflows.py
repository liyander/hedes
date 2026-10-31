from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import get_db
from ..core.security import Principal, current_principal, require_role
from ..models import Workflow, WorkflowRun, WorkflowStep, WorkflowVersion
from ..services import agents, audit, automation, workflows
from ..services.ai_gateway import Gateway, ProviderError
from .common import actor_of, iso, username

router = APIRouter(prefix="/api/v1", tags=["workflows"])


def _wf(db: Session, p: Principal, wid: str) -> Workflow:
    wf = db.get(Workflow, wid)
    if not wf or wf.organization_id != p.org_id:
        raise HTTPException(404, "Workflow not found")
    return wf


def _can_edit(p: Principal, wf: Workflow) -> None:
    if wf.created_by != p.id and not p.at_least("admin"):
        raise HTTPException(403, "Only the owner or an administrator can change this workflow")


def view(db: Session, wf: Workflow, full: bool = False) -> dict:
    v = db.get(WorkflowVersion, wf.current_version_id) if wf.current_version_id else None
    last = db.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == wf.id).order_by(WorkflowRun.started_at.desc())).first()
    out = {"id": wf.id, "name": wf.name, "description": wf.description, "status": wf.status, "owner": username(db, wf.created_by),
           "version": v.version if v else None, "trigger": (v.graph.get("trigger") if v else {}) or {},
           "nodes": len(v.graph.get("nodes") or []) if v else 0, "has_webhook_secret": bool(wf.webhook_secret_ref),
           "last_run": {"id": last.id, "status": last.status, "at": iso(last.started_at)} if last else None,
           "updated_at": iso(wf.updated_at)}
    if full and v:
        out["graph"] = v.graph
        out["webhook_url"] = f"{get_settings().public_base_url.rstrip('/')}/api/v1/hooks/{wf.id}"
        out["validation"] = workflows.validate(db, wf, v.graph)
        out["tools"] = workflows.tool_catalog(db, workflows.owner_actor(db, wf))
        out["events"] = {k: {"description": d, "fields": automation.FIELDS[k], "sample": automation.SAMPLES[k]} for k, d in automation.EVENTS.items()}
    return out


@router.get("/workflows")
def list_workflows(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(Workflow).where(Workflow.organization_id == p.org_id).order_by(Workflow.updated_at.desc())).all()
    return [view(db, w) for w in rows]


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    graph: dict


@router.post("/workflows")
def create_workflow(body: WorkflowIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    wf = Workflow(organization_id=p.org_id, name=body.name, description=body.description, created_by=p.id)
    db.add(wf)
    db.flush()
    wv = WorkflowVersion(workflow_id=wf.id, version=1, graph=body.graph, created_by=p.id)
    db.add(wv)
    db.flush()
    wf.current_version_id = wv.id
    audit.record(db, p.org_id, p, "workflow.create", wf.name)
    db.commit()
    return view(db, wf, True)


class PlanIn(BaseModel):
    description: str = Field(min_length=5, max_length=4000)


@router.post("/workflows/plan")
def plan(body: PlanIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    """Workflow Planning Agent: natural language -> graph over the tools the user may run."""
    tools = workflows.tool_catalog(db, actor_of(p))
    req = agents.requirements_from_tools(body.description, tools)
    gw = Gateway(db, p.org_id)
    try:
        graph, model = agents.plan_workflow(gw, req, tools)
    except ProviderError as e:
        raise HTTPException(503, str(e))
    return {"graph": graph, "model": model, "validation": workflows.validate_workflow_graph(graph, tools)}


@router.get("/workflows/{wid}")
def get_workflow(wid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return view(db, _wf(db, p, wid), True)


class GraphIn(BaseModel):
    graph: dict
    name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)


@router.put("/workflows/{wid}")
def add_step(wid: str, body: GraphIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    """Save a new graph version (the editor sends the whole graph: steps, dependencies, conditions)."""
    wf = _wf(db, p, wid)
    _can_edit(p, wf)
    if body.name:
        wf.name = body.name
    if body.description is not None:
        wf.description = body.description
    n = workflows.save_version(db, wf, body.graph, p.id)
    audit.record(db, p.org_id, p, "workflow.update", wf.name, version=n)
    db.commit()
    return view(db, wf, True)


@router.post("/workflows/{wid}/validate")
def validate_graph(wid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    wf = _wf(db, p, wid)
    return workflows.validate(db, wf, db.get(WorkflowVersion, wf.current_version_id).graph)


@router.post("/workflows/{wid}/activate")
def activate(wid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    wf = _wf(db, p, wid)
    _can_edit(p, wf)
    secret = workflows.activate(db, wf)
    audit.record(db, p.org_id, p, "workflow.activate", wf.name)
    db.commit()
    return {**view(db, wf, True), "webhook_secret": secret}


@router.post("/workflows/{wid}/stop")
def stop_workflow(wid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    wf = _wf(db, p, wid)
    _can_edit(p, wf)
    wf.status = "paused"
    audit.record(db, p.org_id, p, "workflow.pause", wf.name)
    db.commit()
    return view(db, wf, True)


@router.delete("/workflows/{wid}")
def delete_workflow(wid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    wf = _wf(db, p, wid)
    _can_edit(p, wf)
    wf.status = "paused"
    wf.name = wf.name if wf.name.startswith("[archived] ") else "[archived] " + wf.name
    audit.record(db, p.org_id, p, "workflow.archive", wf.name)
    db.commit()
    return {"ok": True}


class RunIn(BaseModel):
    payload: dict = {}
    dry_run: bool = False


@router.post("/workflows/{wid}/execute")
def execute_workflow(wid: str, body: RunIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    wf = _wf(db, p, wid)
    _can_edit(p, wf)
    graph = db.get(WorkflowVersion, wf.current_version_id).graph
    payload = body.payload or (graph.get("trigger") or {}).get("sample") or {}
    run = workflows.execute(db, wf, "manual", payload, dry_run=body.dry_run)
    return run_view(db, run, True)


def run_view(db: Session, r: WorkflowRun, full: bool = False) -> dict:
    out = {"id": r.id, "status": r.status, "trigger": r.trigger, "dry_run": r.dry_run, "error": r.error,
           "started_at": iso(r.started_at), "finished_at": iso(r.finished_at)}
    if full:
        steps = db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == r.id).order_by(WorkflowStep.started_at)).all()
        out["input"] = r.input
        out["steps"] = [{"node_id": s.node_id, "status": s.status, "execution_id": s.execution_id, "output": s.output,
                         "error": s.error, "at": iso(s.started_at)} for s in steps]
    return out


@router.get("/workflows/{wid}/runs")
def run_history(wid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    wf = _wf(db, p, wid)
    rows = db.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == wf.id).order_by(WorkflowRun.started_at.desc()).limit(50)).all()
    return [run_view(db, r, True) for r in rows]


# ------------------------------------------------------------------------------------ webhook trigger

hooks_router = APIRouter(tags=["workflows"])


@hooks_router.post("/api/v1/hooks/{wid}", status_code=202)
async def dispatch_event(wid: str, request: Request, db: Session = Depends(get_db)):
    """Inbound webhook. Signed with HMAC-SHA256 over '<timestamp>.<raw body>' using the workflow's secret."""
    body = await request.body()
    if len(body) > 256_000:
        raise HTTPException(413, "Payload too large")
    wf = db.get(Workflow, wid)
    if not wf:
        raise HTTPException(404, "Unknown workflow")
    delivery = workflows.verify_webhook(db, wf, body, request.headers.get("x-integra-timestamp"),
                                        request.headers.get("x-integra-signature"), request.headers.get("x-integra-delivery"))
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "Body must be JSON")
    workflows.run_async(wf.id, "webhook", payload if isinstance(payload, dict) else {"data": payload}, delivery)
    return {"accepted": True, "delivery_id": delivery}
