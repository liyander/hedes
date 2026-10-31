from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.security import Principal, current_principal, require_human, require_role
from ..models import ApprovalEvent, ApprovalRequest, Integration, IntegrationVersion, RepairProposal
from ..services import integrations, security_review
from .common import iso, username
from .integrations import compare_versions

router = APIRouter(prefix="/api/v1", tags=["approvals"])


def _row(db: Session, a: ApprovalRequest) -> dict:
    i = db.get(Integration, a.integration_id)
    v = db.get(IntegrationVersion, a.version_id)
    return {"id": a.id, "integration_id": i.id, "integration": i.name, "slug": i.slug, "version_id": v.id, "version": v.version,
            "decision": a.decision, "kind": a.kind, "risk": a.risk, "comment": a.comment,
            "requester": username(db, a.requester_id, "AI repair agent"), "requester_id": a.requester_id,
            "reviewer": username(db, a.reviewer_id), "created_at": iso(a.created_at), "decided_at": iso(a.decided_at)}


@router.get("/admin/approvals")
def pending_reviews(status: str = "pending", p: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)):
    q = select(ApprovalRequest).where(ApprovalRequest.organization_id == p.org_id)
    if status != "all":
        q = q.where(ApprovalRequest.decision == status)
    return [_row(db, a) for a in db.scalars(q.order_by(ApprovalRequest.created_at.desc()).limit(200)).all()]


@router.get("/approvals/mine")
def my_requests(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    q = select(ApprovalRequest).where(ApprovalRequest.organization_id == p.org_id, ApprovalRequest.requester_id == p.id)
    return [_row(db, a) for a in db.scalars(q.order_by(ApprovalRequest.created_at.desc()).limit(100)).all()]


@router.get("/admin/approvals/{rid}")
def review_details(rid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    a = db.get(ApprovalRequest, rid)
    if not a or a.organization_id != p.org_id or (not p.at_least("admin") and a.requester_id != p.id):
        raise HTTPException(404, "Approval request not found")
    v = db.get(IntegrationVersion, a.version_id)
    i = db.get(Integration, a.integration_id)
    report = security_review.review(v.config, integrations.org_policy(db, p.org_id), integrations.connections_map(db, p.org_id))
    for k in ("ai_summary", "ai_concerns", "ai_model"):
        if k in (v.security_report or {}):
            report[k] = v.security_report[k]
    tr = integrations.latest_test(db, v)
    diff = None
    if i.published_version_id and i.published_version_id != v.id:
        diff = compare_versions(i.id, i.published_version_id, v.id, p, db)
    repair = db.scalars(select(RepairProposal).where(RepairProposal.approval_request_id == a.id)).first()
    events = db.scalars(select(ApprovalEvent).where(ApprovalEvent.request_id == a.id).order_by(ApprovalEvent.created_at)).all()
    blockers = integrations.approval_blockers(db, a, v, i, p.id) if a.decision == "pending" else []
    return {**_row(db, a), "config": v.config, "config_hash": a.config_hash, "hash_matches": v.config_hash == a.config_hash,
            "security": report, "tests": tr.report if tr else None, "test_result": tr.result if tr else None,
            "diff": diff, "repair": {"diagnosis": repair.diagnosis, "status": repair.status} if repair else None,
            "blockers": blockers, "can_decide": p.at_least("admin") and p.via == "session" and a.decision == "pending",
            "events": [{"action": e.action, "actor": username(db, e.actor_id, "AI agent"), "comment": e.comment, "at": iso(e.created_at)} for e in events]}


class DecisionIn(BaseModel):
    comment: str = Field(default="", max_length=4000)


@router.post("/admin/approvals/{rid}/approve")
def approve(rid: str, body: DecisionIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    a = integrations.approve(db, p.org_id, rid, p, body.comment)
    db.commit()
    return _row(db, a)


@router.post("/admin/approvals/{rid}/reject")
def reject(rid: str, body: DecisionIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    a = integrations.decide_negative(db, p.org_id, rid, p, "rejected", body.comment)
    db.commit()
    return _row(db, a)


@router.post("/admin/approvals/{rid}/changes")
def request_changes(rid: str, body: DecisionIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    a = integrations.decide_negative(db, p.org_id, rid, p, "changes_requested", body.comment)
    db.commit()
    return _row(db, a)
