"""Execution monitoring: alerts, drift detection and platform metrics."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.db import utcnow
from ..models import (Alert, ApprovalRequest, Execution, Integration, RepairProposal, Tool, UsageRecord, Workflow,
                      WorkflowRun)
from . import audit
from .engine import ERROR_GUIDE
from .integrations import org_policy


def _recent_alert(db: Session, org_id: str, kind: str, integration_id: str | None, minutes: int = 60,
                  include_acknowledged: bool = False) -> bool:
    since = utcnow() - timedelta(minutes=minutes)
    q = select(Alert).where(Alert.organization_id == org_id, Alert.kind == kind, Alert.integration_id == integration_id,
                            Alert.created_at >= since)
    if not include_acknowledged:
        q = q.where(Alert.acknowledged.is_(False))
    return db.scalars(q).first() is not None


def open_repair(db: Session, integration_id: str) -> RepairProposal | None:
    return db.scalars(select(RepairProposal).where(RepairProposal.integration_id == integration_id,
                                                   RepairProposal.status.in_(("proposed", "pending_approval")))).first()


def after_execution(db: Session, ex: Execution, integ: Integration) -> bool:
    """Raise alerts for failures. Returns True when an automatic repair proposal should be prepared."""
    if ex.status == "succeeded":
        return False
    from . import automation, healing
    automation.emit(db, ex.organization_id, "failure", {
        "source": "data_source" if ex.actor_type == "ingest" else "tool", "tool": ex.tool_name, "integration": integ.name,
        "integration_id": integ.id, "error_class": ex.error_class, "http_status": ex.http_status, "error": (ex.error or "")[:1000],
        "workflow": "", "execution_id": ex.id, "actor_type": ex.actor_type, "link": f"#/monitoring/execution/{ex.id}"})
    healing.on_failure(db, ex, integ)  # no-op unless self-healing is enabled; investigates after this transaction commits
    cls = ex.error_class
    if cls in ("auth_invalid", "forbidden", "credential_missing") and not _recent_alert(db, ex.organization_id, cls, integ.id):
        audit.alert(db, ex.organization_id, cls, f"{integ.name}: {ERROR_GUIDE[cls][0]} {ERROR_GUIDE[cls][1]}", "high", integ.id, "#/credentials")
    if cls == "upstream_error" and not _recent_alert(db, ex.organization_id, cls, integ.id):
        audit.alert(db, ex.organization_id, cls, f"{integ.name}: the external service is failing (HTTP {ex.http_status}).", "medium", integ.id, "#/monitoring")
    if cls in ("not_found", "request_rejected") and not _recent_alert(db, ex.organization_id, cls, integ.id):
        audit.alert(db, ex.organization_id, cls, f"{integ.name}: {ERROR_GUIDE[cls][0]}", "medium", integ.id, f"#/monitoring/execution/{ex.id}")
    if cls != "response_drift":
        return False
    if not _recent_alert(db, ex.organization_id, "drift", integ.id):
        audit.alert(db, ex.organization_id, "drift", f"{integ.name}: response structure changed for {ex.tool_name} - "
                    f"{', '.join(ex.diagnostics.get('missing_fields') or [])}", "high", integ.id, f"#/monitoring/execution/{ex.id}")
    threshold = int(org_policy(db, ex.organization_id).get("repair_after_failures", 2))
    since = utcnow() - timedelta(hours=1)
    count = db.scalar(select(func.count()).select_from(Execution).where(
        Execution.version_id == ex.version_id, Execution.error_class == "response_drift", Execution.created_at >= since)) or 0
    return count >= threshold and open_repair(db, integ.id) is None


def metrics(db: Session, org_id: str, hours: int = 24) -> dict:
    since = utcnow() - timedelta(hours=hours)
    rows = db.scalars(select(Execution).where(Execution.organization_id == org_id, Execution.created_at >= since)).all()
    total = len(rows)
    ok = sum(1 for r in rows if r.status == "succeeded")
    durations = sorted(r.duration_ms for r in rows)

    def pct(p):
        return durations[min(len(durations) - 1, int(len(durations) * p))] if durations else 0

    by_tool: dict[str, dict] = {}
    by_error: dict[str, int] = {}
    buckets = [{"t": (since + timedelta(hours=i)).isoformat(), "ok": 0, "failed": 0} for i in range(hours)]
    for r in rows:
        t = by_tool.setdefault(r.tool_name, {"tool": r.tool_name, "total": 0, "failed": 0, "avg_ms": 0})
        t["total"] += 1
        t["avg_ms"] += r.duration_ms
        if r.status != "succeeded":
            t["failed"] += 1
            by_error[r.error_class or "unknown"] = by_error.get(r.error_class or "unknown", 0) + 1
        idx = min(hours - 1, max(0, int((r.created_at - since).total_seconds() // 3600)))
        buckets[idx]["ok" if r.status == "succeeded" else "failed"] += 1
    for t in by_tool.values():
        t["avg_ms"] = int(t["avg_ms"] / t["total"]) if t["total"] else 0
        t["success_rate"] = round(100 * (t["total"] - t["failed"]) / t["total"], 1) if t["total"] else None
    month = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ai_cost = db.scalar(select(func.coalesce(func.sum(UsageRecord.estimated_cost), 0.0)).where(
        UsageRecord.organization_id == org_id, UsageRecord.created_at >= month)) or 0.0
    ai_calls = db.scalar(select(func.count()).select_from(UsageRecord).where(
        UsageRecord.organization_id == org_id, UsageRecord.created_at >= since)) or 0
    integ_counts = dict(db.execute(select(Integration.status, func.count()).where(Integration.organization_id == org_id)
                                   .group_by(Integration.status)).all())
    return {
        "window_hours": hours,
        "executions": total,
        "succeeded": ok,
        "failed": total - ok,
        "success_rate": round(100 * ok / total, 1) if total else None,
        "p50_ms": pct(0.5),
        "p95_ms": pct(0.95),
        "by_tool": sorted(by_tool.values(), key=lambda t: -t["total"]),
        "by_error": [{"error_class": k, "count": v, "meaning": ERROR_GUIDE.get(k, ("", ""))[0]} for k, v in sorted(by_error.items(), key=lambda kv: -kv[1])],
        "timeline": buckets,
        "integrations": integ_counts,
        "tools": db.scalar(select(func.count()).select_from(Tool).where(Tool.organization_id == org_id, Tool.enabled.is_(True))) or 0,
        "pending_approvals": db.scalar(select(func.count()).select_from(ApprovalRequest).where(
            ApprovalRequest.organization_id == org_id, ApprovalRequest.decision == "pending")) or 0,
        "open_repairs": db.scalar(select(func.count()).select_from(RepairProposal).where(
            RepairProposal.organization_id == org_id, RepairProposal.status.in_(("proposed", "pending_approval")))) or 0,
        "workflows_active": db.scalar(select(func.count()).select_from(Workflow).where(Workflow.organization_id == org_id, Workflow.status == "active")) or 0,
        "workflow_runs": db.scalar(select(func.count()).select_from(WorkflowRun).where(WorkflowRun.organization_id == org_id, WorkflowRun.started_at >= since)) or 0,
        "ai_cost_month": round(float(ai_cost), 4),
        "ai_calls": ai_calls,
    }
