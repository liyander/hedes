"""Self-healing cases: list and inspect investigations, re-run one, or investigate a failed execution on demand."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.security import Principal, current_principal, require_human, require_role
from ..models import Execution, HealingCase, Integration
from ..services import healing
from .common import iso

router = APIRouter(prefix="/api/v1", tags=["self-healing"])


def case_view(c: HealingCase) -> dict:
    return {"id": c.id, "target": c.target, "error_class": c.error_class, "status": c.status, "root_cause": c.root_cause,
            "analysis": c.analysis, "attempts": c.attempts or [], "required": c.required or [], "endpoint": c.endpoint or {},
            "still_failing": c.still_failing, "occurrences": c.occurrences, "integration_id": c.integration_id, "workflow_id": c.workflow_id,
            "execution_id": c.execution_id, "approval_request_id": c.approval_request_id, "evidence": c.evidence or {}, "model": c.model,
            "notified_at": iso(c.notified_at), "created_at": iso(c.created_at), "updated_at": iso(c.updated_at),
            "report": healing.report_text(c)}


@router.get("/healing/cases")
def list_cases(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(HealingCase).where(HealingCase.organization_id == p.org_id).order_by(HealingCase.updated_at.desc()).limit(100)).all()
    return {"enabled": healing.enabled(db, p.org_id), "cases": [case_view(c) for c in rows]}


def _case(db: Session, p: Principal, cid: str) -> HealingCase:
    c = db.get(HealingCase, cid)
    if not c or c.organization_id != p.org_id:
        raise HTTPException(404, "Case not found")
    return c


@router.get("/healing/cases/{cid}")
def get_case(cid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return case_view(_case(db, p, cid))


@router.post("/healing/cases/{cid}/retry")
def retry_case(cid: str, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    """Investigate again, for example after an administrator fixed what was required."""
    c = _case(db, p, cid)
    c.status = "investigating"
    healing._queue(db, c)
    db.commit()
    return case_view(c)


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
