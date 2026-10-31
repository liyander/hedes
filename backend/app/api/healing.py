"""Self-healing cases: list and inspect investigations, re-run one, or investigate a failed execution on demand."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.db import get_db, utcnow
from ..core.security import Principal, current_principal, require_human, require_role
from ..models import ApprovalRequest, Execution, HealingCase, Integration, IntegrationVersion
from ..services import audit, healing
from ..services.ai_gateway import strip_wrappers
from .common import iso, like_pattern

router = APIRouter(prefix="/api/v1", tags=["self-healing"])


def _duration_s(c: HealingCase) -> int | None:
    end = c.resolved_at or (c.updated_at if c.status != "investigating" else None)
    return int((end - c.created_at).total_seconds()) if end and c.created_at else None


def case_view(c: HealingCase, db: Session | None = None, full: bool = False) -> dict:
    out = {"id": c.id, "target": c.target, "error_class": c.error_class, "status": c.status, "root_cause": c.root_cause,
           "analysis": strip_wrappers(c.analysis), "attempts": c.attempts or [], "required": c.required or [], "endpoint": c.endpoint or {},
           "still_failing": c.still_failing, "occurrences": c.occurrences, "integration_id": c.integration_id, "workflow_id": c.workflow_id,
           "execution_id": c.execution_id, "approval_request_id": c.approval_request_id, "evidence": c.evidence or {}, "model": c.model,
           "runs": c.runs or (1 if c.attempts or c.root_cause else 0), "resolved_at": iso(c.resolved_at), "duration_s": _duration_s(c),
           "fixes_tried": len(c.attempts or []), "fixes_worked": sum(1 for x in c.attempts or [] if x.get("ok")),
           "notified_at": iso(c.notified_at), "created_at": iso(c.created_at), "updated_at": iso(c.updated_at),
           "report": healing.report_text(c)}
    if full:
        out["log"] = c.log or []
        if db is not None:
            integ = db.get(Integration, c.integration_id) if c.integration_id else None
            out["integration"] = {"id": integ.id, "name": integ.name, "slug": integ.slug} if integ else None
            fix = db.get(IntegrationVersion, c.fix_version_id) if c.fix_version_id else None
            req = db.get(ApprovalRequest, c.approval_request_id) if c.approval_request_id else None
            out["fix"] = {"version_id": fix.id, "version": fix.version, "status": fix.status, "notes": fix.notes,
                          "decision": req.decision if req else None, "comment": req.comment if req else ""} if fix else None
            q = select(Execution).where(Execution.organization_id == c.organization_id, Execution.error_class == c.error_class)
            q = q.where(Execution.integration_id == c.integration_id) if c.integration_id else q.where(Execution.id == c.execution_id)
            out["executions"] = [{"id": e.id, "tool": e.tool_name, "status": e.status, "http_status": e.http_status, "error": e.error[:200],
                                  "created_at": iso(e.created_at)} for e in db.scalars(q.order_by(Execution.created_at.desc()).limit(10)).all()]
    return out


@router.get("/healing/cases")
def list_cases(p: Principal = Depends(current_principal), db: Session = Depends(get_db), status: str | None = None,
               q: str | None = Query(None, max_length=100), limit: int = Query(100, ge=1, le=500)):
    cond = [HealingCase.organization_id == p.org_id]
    if status:
        cond.append(HealingCase.status == status)
    if q:
        like = like_pattern(q)
        cond.append(or_(HealingCase.target.ilike(like, escape="\\"), HealingCase.error_class.ilike(like, escape="\\"), HealingCase.root_cause.ilike(like, escape="\\")))
    rows = db.scalars(select(HealingCase).where(*cond).order_by(HealingCase.updated_at.desc()).limit(limit)).all()
    counts = dict(db.execute(select(HealingCase.status, func.count()).where(HealingCase.organization_id == p.org_id)
                             .group_by(HealingCase.status)).all())
    total = sum(counts.values())
    return {"enabled": healing.enabled(db, p.org_id), "auto": healing.auto_enabled(db, p.org_id), "cases": [case_view(c) for c in rows],
            "summary": {"total": total, **{k: counts.get(k, 0) for k in ("investigating", "resolved", "awaiting_approval", "needs_admin")},
                        "fixed_rate": round(100 * counts.get("resolved", 0) / total) if total else None}}


def _case(db: Session, p: Principal, cid: str) -> HealingCase:
    c = db.get(HealingCase, cid)
    if not c or c.organization_id != p.org_id:
        raise HTTPException(404, "Case not found")
    return c


@router.get("/healing/cases/{cid}")
def get_case(cid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return case_view(_case(db, p, cid), db, full=True)


@router.post("/healing/cases/{cid}/retry")
def retry_case(cid: str, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    """Investigate again, for example after an administrator fixed what was required."""
    c = _case(db, p, cid)
    c.status = "investigating"
    healing._queue(db, c)
    db.commit()
    return case_view(c)


class ResolveIn(BaseModel):
    note: str = Field(default="", max_length=1000)


@router.post("/healing/cases/{cid}/resolve")
def resolve_case(cid: str, body: ResolveIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    """An administrator marks a session fixed (for example after rotating a credential or correcting the API by hand)."""
    c = _case(db, p, cid)
    if c.status == "resolved":
        raise HTTPException(409, "This session is already marked fixed")
    if c.status == "investigating":
        raise HTTPException(409, "Wait for the running investigation to finish")
    pending = c.status == "awaiting_approval"
    c.status, c.resolved_at, c.required, c.still_failing = "resolved", utcnow(), [], ""
    healing.log_event(c, "resolved_by_admin", f"{p.user.username} marked this fixed" + (f": {body.note.strip()}" if body.note.strip() else "")
                      + (" (the prepared fix is still waiting in Approvals)" if pending else ""), True)
    audit.record(db, p.org_id, p, "healing.resolve", c.target, case_id=c.id, note=body.note[:300])
    db.commit()
    return case_view(c, db, full=True)


@router.post("/healing/cases/{cid}/reopen")
def reopen_case(cid: str, body: ResolveIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    """Undo "mark as fixed": the session needs an administrator again."""
    c = _case(db, p, cid)
    if c.status != "resolved":
        raise HTTPException(409, "Only a fixed session can be reopened")
    c.status, c.resolved_at = "needs_admin", None
    healing.log_event(c, "reopened", f"{p.user.username} reopened this session" + (f": {body.note.strip()}" if body.note.strip() else ""), False)
    audit.record(db, p.org_id, p, "healing.reopen", c.target, case_id=c.id, note=body.note[:300])
    db.commit()
    return case_view(c, db, full=True)


@router.post("/executions/{eid}/heal")
def heal_execution(eid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    """Investigate one failed execution now, whether or not automatic self-healing is enabled."""
    ex = db.get(Execution, eid)
    if not ex or ex.organization_id != p.org_id:
        raise HTTPException(404, "Execution not found")
    if ex.status == "succeeded":
        raise HTTPException(409, "This execution succeeded; there is nothing to heal")
    integ = db.get(Integration, ex.integration_id) if ex.integration_id else None
    if not integ:
        raise HTTPException(409, "The integration of this execution no longer exists")
    c = healing.on_failure(db, ex, integ, force=True)
    db.commit()
    return case_view(c)
