"""Data sources: per-connection health, run history, the normalised event store and the dead-letter queue."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..core.db import get_db, utcnow
from ..core.security import Principal, current_principal, require_human, require_role
from ..models import (Connection, DeadLetter, IngestRun, IngestSource, Integration, IntegrationVersion, NormalizedDevice, NormalizedEvent,
                      NormalizedUser)
from ..services import audit, cron, ingest
from ..services.engine import ERROR_GUIDE
from .common import iso

router = APIRouter(prefix="/api/v1", tags=["data sources"])


def _credential_view(db: Session, org_id: str, config: dict) -> dict | None:
    auth = config["integration"].get("authentication") or {}
    if auth.get("type", "none") == "none":
        return None
    conn = db.scalars(select(Connection).where(Connection.organization_id == org_id, Connection.name == auth.get("credential_ref"))).first()
    if not conn:
        return {"name": auth.get("credential_ref"), "status": "missing"}
    now = utcnow()
    since = conn.rotated_at or conn.created_at
    return {"name": conn.name, "status": "expired" if conn.expires_at and conn.expires_at <= now else conn.status, "auth_type": conn.auth_type,
            "hint": conn.hint, "expires_at": iso(conn.expires_at), "age_days": (now - since).days, "last_used_at": iso(conn.last_used_at)}


def source_view(db: Session, src: IngestSource, detail: bool = False) -> dict:
    integ = db.get(Integration, src.integration_id)
    version = db.get(IntegrationVersion, integ.published_version_id) if integ and integ.published_version_id else None
    op = next((o for o in version.config["integration"]["operations"] if o["id"] == src.operation_id), {}) if version else {}
    since = utcnow() - timedelta(hours=24)
    runs24 = db.execute(select(IngestRun.status, func.count(), func.coalesce(func.sum(IngestRun.stored), 0)).where(
        IngestRun.source_id == src.id, IngestRun.started_at >= since).group_by(IngestRun.status)).all()
    recent = db.scalars(select(IngestRun).where(IngestRun.source_id == src.id).order_by(IngestRun.started_at.desc()).limit(12)).all()
    h = ingest.health(src)
    out = {
        "id": src.id, "integration_id": src.integration_id, "integration": integ.name if integ else "?", "slug": integ.slug if integ else "",
        "operation_id": src.operation_id, "description": op.get("description", ""), "category": (op.get("ingest") or {}).get("category", ""),
        "pagination": (op.get("pagination") or {}).get("type", "none"), "auth_type": ((version.config["integration"].get("authentication") or {}).get("type") if version else None),
        "entity": (op.get("ingest") or {}).get("entity", "event"), "cron": src.cron or "",
        "enabled": src.enabled, "interval_minutes": src.interval_minutes, "running": ingest.is_running(src.id),
        "health": h["status"], "health_reason": h["reason"], "state": src.state,
        "last_run_at": iso(src.last_run_at), "last_success_at": iso(src.last_success_at), "next_run_at": iso(src.next_run_at) if src.enabled else None,
        "watermark": src.watermark, "consecutive_failures": src.consecutive_failures,
        "last_error_class": src.last_error_class, "last_error": src.last_error,
        "error_meaning": ERROR_GUIDE.get(src.last_error_class, ("", ""))[0], "error_fix": ERROR_GUIDE.get(src.last_error_class, ("", ""))[1],
        "totals": {"updated": int(db.scalar(select(func.coalesce(func.sum(IngestRun.updated), 0)).where(IngestRun.source_id == src.id)) or 0), "stored": src.total_stored, "duplicates": src.total_duplicates, "rejected": src.total_rejected},
        "runs_24h": {status: n for status, n, _ in runs24}, "stored_24h": int(sum(stored for _, _, stored in runs24)),
        "dead_letters": db.scalar(select(func.count()).select_from(DeadLetter).where(DeadLetter.source_id == src.id)) or 0,
        "recent": [r.status for r in reversed(recent)],
        "credential": _credential_view(db, src.organization_id, version.config) if version else None,
        "rate_limit": ((version.config["integration"].get("rate_limit") or {}).get("requests_per_minute") if version else None),
    }
    if detail:
        out["runs"] = [run_view(r) for r in db.scalars(select(IngestRun).where(IngestRun.source_id == src.id)
                                                       .order_by(IngestRun.started_at.desc()).limit(25)).all()]
        out["fields"] = (op.get("ingest") or {}).get("fields", {})
    return out


def run_view(r: IngestRun) -> dict:
    return {"id": r.id, "trigger": r.trigger, "status": r.status, "fetched": r.fetched, "stored": r.stored, "duplicates": r.duplicates,
            "updated": r.updated or 0, "rejected": r.rejected, "attempts": r.attempts, "http_status": r.http_status, "error_class": r.error_class, "error": r.error,
            "duration_ms": r.duration_ms, "watermark_before": r.watermark_before, "watermark_after": r.watermark_after,
            "started_at": iso(r.started_at), "finished_at": iso(r.finished_at)}


def _owned(db: Session, p: Principal, sid: str) -> IngestSource:
    src = db.get(IngestSource, sid)
    if not src or src.organization_id != p.org_id:
        raise HTTPException(404, "Data source not found")
    return src


@router.get("/sources")
def list_sources(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = ingest.sync_sources(db, p.org_id)
    db.commit()
    views = [source_view(db, s) for s in sorted(rows, key=lambda s: s.created_at)]
    order = {"failing": 0, "stale": 1, "degraded": 2, "running": 3, "pending": 4, "paused": 5, "healthy": 6}
    summary = {k: sum(1 for v in views if v["health"] == k) for k in order}
    return {"sources": sorted(views, key=lambda v: (order.get(v["health"], 9), v["integration"])), "summary": summary,
            "events_total": db.scalar(select(func.count()).select_from(NormalizedEvent).where(NormalizedEvent.organization_id == p.org_id)) or 0,
            "common_format": list(ingest.CANONICAL_FIELDS)}


@router.get("/sources/{sid}")
def get_source(sid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return source_view(db, _owned(db, p, sid), detail=True)


@router.post("/sources/{sid}/run")
async def run_now(sid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    src = _owned(db, p, sid)
    if not src.enabled:
        raise HTTPException(409, "This source is paused")
    if not ingest.try_acquire(src.id):
        raise HTTPException(409, "A run is already in progress")
    try:
        run = await run_in_threadpool(ingest.run_source, db, src, "manual")
    except ValueError as e:
        raise HTTPException(409, str(e))
    finally:
        ingest.release(src.id)
    audit.record(db, p.org_id, p, "source.run", f"{src.integration_id}.{src.operation_id}", status=run.status, stored=run.stored)
    db.commit()
    return {"run": run_view(run), "source": source_view(db, src)}


class SourcePatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    cron: str | None = Field(default=None, max_length=64)  # "" switches back to the interval


@router.patch("/sources/{sid}")
def update_source(sid: str, body: SourcePatch, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    src = _owned(db, p, sid)
    changes = body.model_dump(exclude_none=True)
    if "cron" in changes:
        changes["cron"] = changes["cron"].strip()
        if changes["cron"]:
            try:
                cron.parse(changes["cron"])
            except cron.CronError as e:
                raise HTTPException(422, f"Invalid cron expression: {e}")
        changes["cron"] = changes["cron"] or None
    for k, v in changes.items():
        setattr(src, k, v)
    if "cron" in changes or "interval_minutes" in changes:
        src.next_run_at = ingest._next_run(src)
    if changes.get("enabled"):
        src.next_run_at, src.consecutive_failures = utcnow(), 0
    audit.record(db, p.org_id, p, "source.update", f"{src.integration_id}.{src.operation_id}", **changes)
    db.commit()
    return source_view(db, src)


@router.post("/sources/{sid}/test")
async def test_source(sid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    """Fetch one page and show how it would be normalised. Nothing is stored and the schedule is untouched."""
    src = _owned(db, p, sid)
    try:
        return await run_in_threadpool(ingest.test_source, db, src)
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/sources/{sid}/reset")
def reset_source(sid: str, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    """Forget the high-water mark so the next run re-reads the full history (de-duplication keeps events unique)."""
    src = _owned(db, p, sid)
    src.watermark, src.next_run_at = "", utcnow()
    audit.record(db, p.org_id, p, "source.reset", f"{src.integration_id}.{src.operation_id}")
    db.commit()
    return source_view(db, src)


def event_view(e: NormalizedEvent, full: bool = False) -> dict:
    out = {"id": e.id, "source": e.source, "event_id": e.event_id, "fetched_at": iso(e.ingested_at),
           "type": e.category or e.action, "description": e.message or e.action,  # names used by the common Event/Alert schema
           "timestamp": iso(e.timestamp), "category": e.category, "action": e.action,
           "actor": e.actor, "target": e.target, "outcome": e.outcome, "severity": e.severity, "message": e.message}
    if full:
        out["raw"] = e.raw
    return out


@router.get("/events")
def list_events(source: str | None = None, severity: str | None = None, outcome: str | None = None, q: str | None = Query(None, max_length=100),
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), p: Principal = Depends(current_principal),
                db: Session = Depends(get_db)):
    cond = [NormalizedEvent.organization_id == p.org_id]
    if source:
        cond.append(NormalizedEvent.source == source)
    if severity:
        cond.append(NormalizedEvent.severity == severity)
    if outcome:
        cond.append(NormalizedEvent.outcome == outcome)
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        cond.append(or_(NormalizedEvent.action.ilike(like), NormalizedEvent.actor.ilike(like), NormalizedEvent.target.ilike(like),
                        NormalizedEvent.message.ilike(like)))
    total = db.scalar(select(func.count()).select_from(NormalizedEvent).where(*cond)) or 0
    rows = db.scalars(select(NormalizedEvent).where(*cond).order_by(NormalizedEvent.timestamp.desc()).limit(limit).offset(offset)).all()
    by_source = db.execute(select(NormalizedEvent.source, func.count()).where(NormalizedEvent.organization_id == p.org_id)
                           .group_by(NormalizedEvent.source)).all()
    return {"total": total, "items": [event_view(e) for e in rows], "sources": {s: n for s, n in by_source}}


@router.get("/events/{eid}")
def get_event(eid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    e = db.get(NormalizedEvent, eid)
    if not e or e.organization_id != p.org_id:
        raise HTTPException(404, "Event not found")
    return event_view(e, full=True)


@router.get("/dead-letters")
def list_dead_letters(source_id: str | None = None, limit: int = Query(50, ge=1, le=200), p: Principal = Depends(current_principal),
                      db: Session = Depends(get_db)):
    cond = [DeadLetter.organization_id == p.org_id] + ([DeadLetter.source_id == source_id] if source_id else [])
    rows = db.scalars(select(DeadLetter).where(*cond).order_by(DeadLetter.created_at.desc()).limit(limit)).all()
    names = {s.id: s for s in db.scalars(select(IngestSource).where(IngestSource.organization_id == p.org_id)).all()}
    total = db.scalar(select(func.count()).select_from(DeadLetter).where(*cond)) or 0
    return {"total": total, "items": [{"id": d.id, "source_id": d.source_id, "source": (db.get(Integration, names[d.source_id].integration_id).slug
                                       if d.source_id in names else "?"), "reasons": d.reasons, "raw": d.raw, "created_at": iso(d.created_at)} for d in rows]}


def _dead_letter(db: Session, p: Principal, did: str) -> DeadLetter:
    d = db.get(DeadLetter, did)
    if not d or d.organization_id != p.org_id:
        raise HTTPException(404, "Record not found")
    return d


@router.post("/dead-letters/{did}/reprocess")
def reprocess(did: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    d = _dead_letter(db, p, did)
    try:
        ok, reasons = ingest.reprocess_dead_letter(db, d)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return {"stored": ok, "reasons": reasons}


@router.delete("/dead-letters/{did}")
def dismiss(did: str, p: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)):
    d = _dead_letter(db, p, did)
    src = db.get(IngestSource, d.source_id)
    if src:
        src.total_rejected = max(0, src.total_rejected - 1)
    db.delete(d)
    audit.record(db, p.org_id, p, "deadletter.dismiss", did)
    db.commit()
    return {"ok": True}


ENTITY_VIEWS = {"user": (NormalizedUser, ("name", "email", "status")), "device": (NormalizedDevice, ("hostname", "ip", "os", "owner"))}


def entity_view(row, kind: str) -> dict:
    out = {"id": row.id, "external_id": row.external_id, "source": row.source, "fetched_at": iso(row.fetched_at), "first_seen_at": iso(row.first_seen_at)}
    for f in ENTITY_VIEWS[kind][1]:
        out[f] = getattr(row, f)
    out["last_login" if kind == "user" else "last_seen"] = iso(row.last_login if kind == "user" else row.last_seen)
    return out


@router.get("/entities/{kind}")
def list_entities(kind: str, source: str | None = None, q: str | None = Query(None, max_length=100), status: str | None = None,
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), p: Principal = Depends(current_principal),
                  db: Session = Depends(get_db)):
    """The common User and Device tables: one row per account/device across every source."""
    if kind not in ENTITY_VIEWS:
        raise HTTPException(404, "Unknown entity; use user or device")
    model, fields = ENTITY_VIEWS[kind]
    cond = [model.organization_id == p.org_id]
    if source:
        cond.append(model.source == source)
    if status and kind == "user":
        cond.append(model.status == status)
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        cond.append(or_(*[getattr(model, f).ilike(like) for f in fields]))
    total = db.scalar(select(func.count()).select_from(model).where(*cond)) or 0
    order = model.last_login if kind == "user" else model.last_seen
    rows = db.scalars(select(model).where(*cond).order_by(order.desc().nullslast(), model.fetched_at.desc()).limit(limit).offset(offset)).all()
    by_source = db.execute(select(model.source, func.count()).where(model.organization_id == p.org_id).group_by(model.source)).all()
    return {"total": total, "items": [entity_view(r, kind) for r in rows], "sources": {s: n for s, n in by_source}}
