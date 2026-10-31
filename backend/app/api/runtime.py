"""Tool registry, execution, monitoring, repairs, alerts, audit and the MCP endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..core.config import get_settings
from ..core.db import get_db
from ..core.security import Principal, current_principal, require_human, require_role
from ..mock import services as mock
from ..models import Alert, AuditLog, Execution, Integration, RepairProposal, Tool
from ..services import agents, audit, mcp, monitoring, repairs, runtime
from ..services.ai_gateway import Gateway, ProviderError
from ..services.engine import ERROR_GUIDE
from .common import actor_of, iso, username

router = APIRouter(prefix="/api/v1", tags=["tools & monitoring"])


def tool_view(t: Tool, integ: Integration | None = None) -> dict:
    return {"id": t.id, "name": t.name, "operation_id": t.operation_id, "description": t.description, "method": t.method,
            "input_schema": t.input_schema, "output_fields": t.output_fields, "enabled": t.enabled, "allowed_roles": t.allowed_roles,
            "mcp_enabled": t.mcp_enabled, "mcp_name": mcp.mcp_name(t), "integration_id": t.integration_id,
            "integration": integ.name if integ else None, "version_id": t.integration_version_id}


@router.get("/tools")
def list_tools(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    out = []
    for t in runtime.visible_tools(db, actor_of(p)):
        out.append(tool_view(t, db.get(Integration, t.integration_id)))
    return out


@router.get("/admin/tools")
def all_tools(p: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)):
    rows = db.execute(select(Tool, Integration).join(Integration, Tool.integration_id == Integration.id)
                      .where(Tool.organization_id == p.org_id, Tool.integration_version_id == Integration.published_version_id)
                      .order_by(Tool.name)).all()
    return [{**tool_view(t, i), "integration_status": i.status} for t, i in rows]


@router.get("/tools/{tid}/schema")
def get_tool(tid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    t = runtime.resolve_tool(db, actor_of(p), tid)
    runtime.authorize(db, actor_of(p), t)
    return {**tool_view(t), "mcp": mcp.convert_to_mcp_schema(t)}


class ToolSettings(BaseModel):
    enabled: bool | None = None
    mcp_enabled: bool | None = None
    allowed_roles: list[str] | None = None


@router.patch("/admin/tools/{tid}")
def update_tool(tid: str, body: ToolSettings, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    t = db.get(Tool, tid)
    if not t or t.organization_id != p.org_id:
        raise HTTPException(404, "Tool not found")
    changes = body.model_dump(exclude_none=True)
    if "allowed_roles" in changes:
        roles = [r for r in changes["allowed_roles"] if r in ("user", "admin", "owner")]
        changes["allowed_roles"] = sorted(set(roles) | {"owner"})
    for k, v in changes.items():
        setattr(t, k, v)
    audit.record(db, p.org_id, p, "tool.settings", t.name, **changes)
    db.commit()
    return tool_view(t)


class ExecuteIn(BaseModel):
    inputs: dict = {}


def execution_view(db: Session, e: Execution, full: bool = False) -> dict:
    out = {"id": e.id, "tool": e.tool_name, "tool_id": e.tool_id, "integration_id": e.integration_id, "status": e.status,
           "http_status": e.http_status, "duration_ms": e.duration_ms, "attempts": e.attempts, "error_class": e.error_class,
           "error": e.error, "actor": username(db, e.actor_id), "actor_type": e.actor_type, "created_at": iso(e.created_at)}
    if full:
        out.update(request=e.request_preview, output=e.output, diagnostics=e.diagnostics,
                   guide=ERROR_GUIDE.get(e.error_class) if e.error_class else None)
    return out


@router.post("/tools/{tid}/execute")
def execute(tid: str, body: ExecuteIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    actor = actor_of(p)
    t = runtime.resolve_tool(db, actor, tid)
    ex = runtime.execute_tool(db, actor, t, body.inputs)
    return execution_view(db, ex, True)


@router.get("/executions")
def list_executions(p: Principal = Depends(current_principal), db: Session = Depends(get_db), status: str | None = None,
                    tool: str | None = None, integration_id: str | None = None, limit: int = Query(100, le=500)):
    q = select(Execution).where(Execution.organization_id == p.org_id)
    if not p.at_least("admin"):
        q = q.where(Execution.actor_id == p.id)
    if status:
        q = q.where(Execution.status == status)
    if tool:
        q = q.where(Execution.tool_name == tool)
    if integration_id:
        q = q.where(Execution.integration_id == integration_id)
    return [execution_view(db, e) for e in db.scalars(q.order_by(Execution.created_at.desc()).limit(limit)).all()]


def _execution(db: Session, p: Principal, eid: str) -> Execution:
    e = db.get(Execution, eid)
    if not e or e.organization_id != p.org_id or (not p.at_least("admin") and e.actor_id != p.id):
        raise HTTPException(404, "Execution not found")
    return e


@router.get("/executions/{eid}")
def get_execution(eid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return execution_view(db, _execution(db, p, eid), True)


@router.get("/executions/{eid}/logs")
def get_logs(eid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    e = _execution(db, p, eid)
    lines = [f"{iso(e.created_at)} execute {e.tool_name} as {username(db, e.actor_id)} ({e.actor_type})",
             f"request {e.request_preview.get('method', '')} {e.request_preview.get('url', '')} (credentials redacted)",
             f"attempts={e.attempts} duration={e.duration_ms}ms http={e.http_status or '-'}",
             f"result {e.status}" + (f" [{e.error_class}] {e.error}" if e.error_class else "")]
    if e.diagnostics.get("missing_fields"):
        lines.append("missing mapped fields: " + ", ".join(e.diagnostics["missing_fields"]))
    return {"lines": lines}


@router.post("/executions/{eid}/explain")
def explain(eid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    e = _execution(db, p, eid)
    if e.status == "succeeded":
        return {"explanation": "This execution succeeded.", "model": ""}
    guide = ERROR_GUIDE.get(e.error_class, ("The execution failed.", "Review the logs."))
    try:
        text, model = agents.explain_error(Gateway(db, p.org_id), e.tool_name, e.error_class, e.error, guide)
    except ProviderError:
        text, model = f"{guide[0]} {guide[1]}", "built-in guidance"
    return {"explanation": text, "model": model, "can_repair": e.error_class == "response_drift"}


@router.post("/executions/{eid}/repair")
def request_repair(eid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    e = _execution(db, p, eid)
    rp = repairs.create_repair(db, p.org_id, e.id)
    db.commit()
    return repair_view(db, rp)


# ------------------------------------------------------------------------------------ monitoring

@router.get("/monitoring/metrics")
def calculate_metrics(hours: int = Query(24, ge=1, le=168), p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return monitoring.metrics(db, p.org_id, hours)


@router.get("/monitoring/failures")
def failures(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return list_executions(p, db, status="failed", limit=50)


def repair_view(db: Session, r: RepairProposal) -> dict:
    i = db.get(Integration, r.integration_id)
    return {"id": r.id, "integration_id": r.integration_id, "integration": i.name if i else "", "status": r.status,
            "error_class": r.error_class, "diagnosis": r.diagnosis, "approval_request_id": r.approval_request_id,
            "proposed_version_id": r.proposed_version_id, "created_at": iso(r.created_at)}


@router.get("/repairs")
def list_repairs(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(RepairProposal).where(RepairProposal.organization_id == p.org_id).order_by(RepairProposal.created_at.desc()).limit(50)).all()
    return [repair_view(db, r) for r in rows]


@router.get("/alerts")
def list_alerts(p: Principal = Depends(current_principal), db: Session = Depends(get_db), include_acknowledged: bool = False):
    q = select(Alert).where(Alert.organization_id == p.org_id)
    if not include_acknowledged:
        q = q.where(Alert.acknowledged.is_(False))
    rows = db.scalars(q.order_by(Alert.created_at.desc()).limit(50)).all()
    return [{"id": a.id, "kind": a.kind, "severity": a.severity, "message": a.message, "link": a.link,
             "acknowledged": a.acknowledged, "created_at": iso(a.created_at)} for a in rows]


@router.post("/alerts/{aid}/ack")
def ack_alert(aid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    a = db.get(Alert, aid)
    if not a or a.organization_id != p.org_id:
        raise HTTPException(404, "Alert not found")
    a.acknowledged = True
    db.commit()
    return {"ok": True}


@router.post("/alerts/ack-all")
def ack_all(p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    for a in db.scalars(select(Alert).where(Alert.organization_id == p.org_id, Alert.acknowledged.is_(False))).all():
        a.acknowledged = True
    db.commit()
    return {"ok": True}


@router.get("/audit")
def list_changes(p: Principal = Depends(current_principal), db: Session = Depends(get_db), action: str | None = None,
                 limit: int = Query(200, le=1000)):
    q = select(AuditLog).where(AuditLog.organization_id == p.org_id)
    if not p.at_least("admin"):
        q = q.where(AuditLog.actor_id == p.id)
    if action:
        q = q.where(AuditLog.action.startswith(action))
    return [{"id": a.id, "actor": a.actor_label, "action": a.action, "target": a.target, "metadata": a.meta, "at": iso(a.created_at)}
            for a in db.scalars(q.order_by(AuditLog.created_at.desc()).limit(limit)).all()]


# ------------------------------------------------------------------------------------ MCP

mcp_router = APIRouter(tags=["MCP gateway"])


@mcp_router.post("/mcp")
async def mcp_endpoint(request: Request, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    actor = actor_of(p, "mcp")
    if isinstance(payload, list):
        out = [r for r in [await run_in_threadpool(mcp.handle, db, actor, m) for m in payload] if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    res = await run_in_threadpool(mcp.handle, db, actor, payload)
    return JSONResponse(res) if res is not None else Response(status_code=202)


@mcp_router.get("/mcp")
def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})


# ------------------------------------------------------------------------------------ demo controls

class DriftIn(BaseModel):
    enabled: bool = Field(...)


def _demo_only() -> None:
    if not get_settings().demo_mode:
        raise HTTPException(404, "Demo controls are disabled (DEMO_MODE is off)")


@router.get("/demo/state")
def demo_state(p: Principal = Depends(current_principal)):
    return {"demo": True, **mock.snapshot()} if get_settings().demo_mode else {"demo": False}


class FeedIn(BaseModel):
    okta: int = Field(0, ge=0, le=500)
    aws: int = Field(0, ge=0, le=500)
    firewall: int = Field(0, ge=0, le=500)
    firewall_flaky: bool | None = None


@router.post("/demo/feeds")
def demo_feeds(body: FeedIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db), _=Depends(_demo_only)):
    """Add fresh records to the bundled Okta / CloudTrail / firewall mocks and optionally make the firewall rate-limit."""
    made = mock.emit(body.model_dump())
    if body.firewall_flaky is not None:
        mock.set_firewall_flaky(body.firewall_flaky)
    audit.record(db, p.org_id, p, "demo.feeds", "mock data sources", emitted=made, firewall_flaky=body.firewall_flaky)
    db.commit()
    return mock.snapshot()


@router.post("/demo/drift")
def demo_drift(body: DriftIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db), _=Depends(_demo_only)):
    mock.set_drift(body.enabled)
    audit.record(db, p.org_id, p, "demo.drift", "TaskFlow mock API", enabled=body.enabled)
    db.commit()
    return mock.snapshot()
