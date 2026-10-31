"""Data-source ingestion: pull records through the generic engine and map them into one common event format.

A source is an operation carrying an ``ingest`` block. The platform handles everything around the mapping:
incremental fetches (a persisted high-water mark), de-duplication, per-record validation with a dead-letter store
(one bad record never fails a run), run history and health. Connectors stay pure configuration.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.db import session_scope, utcnow
from ..models import (Connection, DeadLetter, Execution, IngestRun, IngestSource, Integration, IntegrationVersion,
                      NormalizedDevice, NormalizedEvent, NormalizedUser)
from . import audit, cron, engine, jsonpath, monitoring, vault
from .integrations import org_policy

log = logging.getLogger("integra.ingest")

# The common formats. Every connector produces one of these shapes, whatever the upstream calls its fields.
ENTITY_FIELDS = {
    "event": ("event_id", "timestamp", "category", "action", "actor", "target", "outcome", "severity", "message"),
    "user": ("id", "name", "email", "status", "last_login"),
    "device": ("id", "hostname", "ip", "os", "owner", "last_seen"),
}
CANONICAL_FIELDS = ENTITY_FIELDS["event"]  # kept for callers that predate user/device entities
ENTITY_MODELS = {"event": NormalizedEvent, "user": NormalizedUser, "device": NormalizedDevice}
SEVERITIES = ("info", "low", "medium", "high", "critical")
OUTCOMES = ("success", "failure", "unknown")
DEFAULT_INTERVAL = 15
DEGRADED_REJECT_RATIO = 0.2
STALE_FACTOR = 3  # a source that has not succeeded for this many intervals is reported as stale
DEFAULT_FAILURE_ALERT_AFTER = 3

_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()


# ----------------------------------------------------------------------------------------- normalisation

def parse_timestamp(value: Any) -> datetime | None:
    """Accept ISO-8601 (with Z / offsets), dates, epoch seconds or milliseconds; return naive UTC."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()):
            n = float(value)
            if n > 1e11:  # milliseconds
                n /= 1000
            if not 0 < n < 4_102_444_800:  # year 2100
                return None
            return datetime.fromtimestamp(n, tz=timezone.utc).replace(tzinfo=None)
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T", 1))
            return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _text(v: Any, limit: int = 255) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, separators=(",", ":"), sort_keys=True)
    return str(v).strip()[:limit]


def _map_value(spec: dict, field: str, value: str) -> str:
    table = (spec.get("value_maps") or {}).get(field) or {}
    return table.get(value, table.get(value.lower(), table.get(value.upper(), value)))


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def extract_field(raw: Any, fspec: Any) -> Any:
    """Read one common-format field. ``fspec`` is a JSONPath string or {path|paths, sep, default, transform, thresholds}."""
    if isinstance(fspec, str):
        found, value = jsonpath.query(raw, fspec)
        return value if found else None
    value: Any = None
    if fspec.get("paths"):
        parts = []
        for path in fspec["paths"]:
            found, v = jsonpath.query(raw, path)
            if found and not _empty(v):
                parts.append(_text(v, 500))
        value = (fspec.get("sep", " ")).join(parts) if parts else None
    elif fspec.get("path"):
        found, value = jsonpath.query(raw, fspec["path"])
        value = value if found else None
    if _empty(value) and "default" in fspec:
        value = fspec["default"]
    transform = fspec.get("transform")
    if _empty(value) or not transform:
        return value
    if transform == "first":
        return value[0] if isinstance(value, list) and value else value
    if transform == "join_list":
        return ", ".join(_text(x, 200) for x in value) if isinstance(value, list) else value
    if transform in ("lower", "upper", "trim", "title"):
        t = _text(value, 1000)
        return {"lower": t.lower, "upper": t.upper, "trim": t.strip, "title": t.title}[transform]()
    if transform == "iso_date":
        ts = parse_timestamp(value)
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None
    if transform == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if transform == "thresholds":  # [[90, "critical"], [50, "medium"]]: first row whose minimum the number reaches
        try:
            n = float(value)
        except (TypeError, ValueError):
            return None
        for floor, label in sorted(fspec.get("thresholds") or [], key=lambda r: -float(r[0])):
            if n >= float(floor):
                return label
        return None
    return value


def normalize(raw: Any, spec: dict, source: str) -> tuple[dict | None, list[str]]:
    """Map one raw record to the common format of ``spec['entity']``. Returns (record, []) or (None, reasons)."""
    if not isinstance(raw, dict):
        return None, [f"record is a {type(raw).__name__}, expected an object"]
    entity = spec.get("entity") or "event"
    names = ENTITY_FIELDS[entity]
    out: dict[str, Any] = {}
    for name in names:
        fspec = (spec.get("fields") or {}).get(name)
        value = extract_field(raw, fspec) if fspec is not None else None
        if _empty(value):
            default = (spec.get("defaults") or {}).get(name) or (spec.get("category") if name == "category" else None)
            value = default if default is not None else None
        out[name] = value
    if entity == "event":
        return _normalize_event(raw, out, spec)
    return _normalize_entity(raw, out, spec, entity)


def _normalize_event(raw: dict, out: dict, spec: dict) -> tuple[dict | None, list[str]]:
    reasons: list[str] = []
    ts = parse_timestamp(out["timestamp"])
    if ts is None:
        reasons.append(f"timestamp {out['timestamp']!r} is missing or not a recognised date/epoch")
    elif ts > utcnow() + timedelta(days=1):
        reasons.append(f"timestamp {ts.isoformat()} is in the future")
    action = _text(_map_value(spec, "action", _text(out["action"], 200)), 200)
    if not action:
        reasons.append("action is missing")
    if reasons:
        return None, reasons
    severity = _map_value(spec, "severity", _text(out["severity"], 32)).lower()
    if severity not in SEVERITIES:
        severity = "info"
    outcome = _map_value(spec, "outcome", _text(out["outcome"], 32)).lower()
    if outcome not in OUTCOMES:
        outcome = "unknown"
    event_id = _text(out["event_id"], 200) or "h:" + hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:32]
    return {"event_id": event_id, "timestamp": ts, "category": _map_value(spec, "category", _text(out["category"], 32)).lower(),
            "action": action, "actor": _text(out["actor"]), "target": _text(out["target"]), "outcome": outcome,
            "severity": severity, "message": _text(out["message"], 1000)}, []


def _normalize_entity(raw: dict, out: dict, spec: dict, entity: str) -> tuple[dict | None, list[str]]:
    ident = _text(out["id"], 200)
    if not ident:
        return None, ["id is missing"]
    rec: dict[str, Any] = {"external_id": ident}
    reasons: list[str] = []
    for name in ENTITY_FIELDS[entity][1:]:
        v = _text(_map_value(spec, name, _text(out[name], 255)), 255) if not _empty(out[name]) else ""
        if name in ("last_login", "last_seen"):
            ts = parse_timestamp(out[name])
            if ts is None and v:
                reasons.append(f"{name} {v!r} is not a recognised date/epoch")
            rec[name] = ts
        elif name == "email":
            rec[name] = v.lower()
            if v and "@" not in v:
                reasons.append(f"email {v!r} is not an email address")
        elif name == "ip":
            if v:
                try:
                    ipaddress.ip_address(v)
                except ValueError:
                    reasons.append(f"ip {v!r} is not a valid address")
            rec[name] = v
        else:
            rec[name] = v
    return (None, reasons) if reasons else (rec, [])


def _format_watermark(ts: datetime, fmt: str) -> str:
    if fmt == "epoch_seconds":
        return str(int(ts.replace(tzinfo=timezone.utc).timestamp()))
    if fmt == "epoch_millis":
        return str(int(ts.replace(tzinfo=timezone.utc).timestamp() * 1000))
    if fmt == "iso8601_millis":
        return ts.strftime("%Y-%m-%dT%H:%M:%S.000")
    if fmt == "date":
        return ts.strftime("%Y-%m-%d")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------------------- sources

def ingest_ops(config: dict) -> list[dict]:
    return [op for op in config["integration"]["operations"] if op.get("ingest")]


def sync_sources(db: Session, org_id: str) -> list[IngestSource]:
    """Make sure every ``ingest`` operation of a published integration has a source row (and drop vanished ones)."""
    rows = {(s.integration_id, s.operation_id): s for s in db.scalars(select(IngestSource).where(IngestSource.organization_id == org_id)).all()}
    live: set[tuple[str, str]] = set()
    for integ in db.scalars(select(Integration).where(Integration.organization_id == org_id, Integration.status == "published")).all():
        version = db.get(IntegrationVersion, integ.published_version_id) if integ.published_version_id else None
        if not version:
            continue
        for op in ingest_ops(version.config):
            key = (integ.id, op["id"])
            live.add(key)
            sched = op["ingest"].get("schedule") or {}
            interval = int(sched.get("interval_minutes") or DEFAULT_INTERVAL)
            if key not in rows:
                src = IngestSource(organization_id=org_id, integration_id=integ.id, operation_id=op["id"], interval_minutes=interval,
                                   cron=sched.get("cron"), next_run_at=utcnow())
                db.add(src)
                rows[key] = src
    for key, src in rows.items():
        if key not in live and src.enabled:
            src.enabled = False  # integration unpublished or operation removed: keep the history, stop running
            src.state = "never_run" if not src.last_run_at else src.state
    db.flush()
    return [s for k, s in rows.items() if k in live]


def _config_for(db: Session, src: IngestSource) -> tuple[Integration, IntegrationVersion, dict]:
    integ = db.get(Integration, src.integration_id)
    version = db.get(IntegrationVersion, integ.published_version_id) if integ and integ.published_version_id else None
    if not integ or not version or integ.status != "published":
        raise ValueError("The integration is not published")
    op = next((o for o in version.config["integration"]["operations"] if o["id"] == src.operation_id and o.get("ingest")), None)
    if not op:
        raise ValueError(f"Operation {src.operation_id!r} no longer has an ingest block")
    return integ, version, op


def _credential(db: Session, org_id: str, config: dict) -> tuple[dict | None, Connection | None]:
    auth = config["integration"].get("authentication") or {}
    if auth.get("type", "none") == "none":
        return None, None
    conn = db.scalars(select(Connection).where(Connection.organization_id == org_id, Connection.name == auth.get("credential_ref"))).first()
    if not conn or conn.status != "active" or conn.auth_type != auth["type"]:
        return None, conn
    if conn.expires_at and conn.expires_at <= utcnow():
        return None, conn
    return vault.read_secret(db, conn.secret_ref, org_id), conn


def _next_run(src: IngestSource) -> datetime:
    """Cron when configured; otherwise the interval, with exponential backoff (capped at 1h) while a source is failing."""
    if src.cron:
        try:
            return cron.next_after(src.cron, utcnow())
        except cron.CronError:
            pass
    delay = src.interval_minutes if src.consecutive_failures == 0 else min(60, src.interval_minutes * 2 ** min(src.consecutive_failures, 4))
    return utcnow() + timedelta(minutes=delay)


def _incremental_query(src: IngestSource, spec: dict) -> dict:
    inc = spec.get("incremental")
    if not inc or not src.watermark:
        return {}
    fmt = inc.get("format", "iso8601")
    since = (parse_timestamp(src.watermark) or utcnow()) - timedelta(seconds=int(inc.get("overlap_seconds", 60)))  # overlap is absorbed by de-duplication
    query = {inc["param"]: _format_watermark(since, fmt)}
    if inc.get("end_param"):
        query[inc["end_param"]] = _format_watermark(utcnow(), fmt)
    return query


def _prefetch(db: Session, model, org_id: str, integration_id: str, ids: list[str]) -> dict:
    found: dict = {}
    key = model.event_id if model is NormalizedEvent else model.external_id
    for i in range(0, len(ids), 500):
        for row in db.scalars(select(model).where(model.organization_id == org_id, model.integration_id == integration_id,
                                                  key.in_(ids[i:i + 500]))).all():
            found[row.event_id if model is NormalizedEvent else row.external_id] = row
    return found


def _store(db: Session, src: IngestSource, integ: Integration, entity: str, records: list[tuple[dict, dict]], run: IngestRun) -> datetime | None:
    """Insert new records; for users/devices also update changed ones in place. Returns the newest event timestamp."""
    model = ENTITY_MODELS[entity]
    key = "event_id" if entity == "event" else "external_id"
    existing = _prefetch(db, model, src.organization_id, integ.id, [r[key] for r, _ in records])
    newest: datetime | None = None
    now = utcnow()
    for rec, item in records:
        row = existing.get(rec[key])
        if entity == "event" and (newest is None or rec["timestamp"] > newest):
            newest = rec["timestamp"]  # also for duplicates: after a reset the mark must come back even when nothing is new
        if row is None:
            existing[rec[key]] = model(organization_id=src.organization_id, integration_id=integ.id, source_id=src.id, source=integ.slug,
                                       raw=_bounded(item), **rec)
            db.add(existing[rec[key]])
            run.stored += 1
        elif entity == "event":
            run.duplicates += 1
        else:
            changed = {k: v for k, v in rec.items() if k != key and getattr(row, k) != v}
            row.fetched_at = now
            if changed:
                for k, v in changed.items():
                    setattr(row, k, v)
                row.raw = _bounded(item)
                run.updated = (run.updated or 0) + 1
            else:
                run.duplicates += 1
    return newest


def run_source(db: Session, src: IngestSource, trigger: str = "schedule", transport=None) -> IngestRun:
    """One ingestion pass. Never raises for upstream problems: the outcome is recorded on the run and the source."""
    integ, version, op = _config_for(db, src)
    spec = op["ingest"]
    entity = spec.get("entity") or "event"
    config = version.config
    run = IngestRun(organization_id=src.organization_id, source_id=src.id, trigger=trigger, watermark_before=src.watermark, updated=0)
    db.add(run)
    src.state = "running"
    db.commit()

    credential, conn = _credential(db, src.organization_id, config)
    auth_type = (config["integration"].get("authentication") or {}).get("type", "none")
    if auth_type != "none" and credential is None:
        why = "expired" if conn and conn.expires_at and conn.expires_at <= utcnow() else "missing or inactive"
        result = engine.EngineResult(status="failed", error_class="credential_missing",
                                     error=f"The connection '{config['integration']['authentication'].get('credential_ref')}' is {why}.")
    else:
        result = engine.execute(config, src.operation_id, {}, credential=credential, transport=transport, raw=True,
                                query_overrides=_incremental_query(src, spec),
                                allowed_domains=org_policy(db, src.organization_id).get("allowed_domains") or [])
        if conn:
            conn.last_used_at = utcnow()

    run.attempts, run.http_status, run.duration_ms = result.attempts, result.http_status, result.duration_ms
    newest = parse_timestamp(src.watermark)
    complete = result.status == "succeeded"
    if result.status in ("succeeded", "partial"):
        items = result.output["items"]
        run.fetched = len(items)
        records: list[tuple[dict, dict]] = []
        parked: set[str] = set()
        for item in items:
            rec, reasons = normalize(item, spec, integ.slug)
            if rec is None:
                fp = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()
                if fp in parked or db.scalar(select(func.count()).select_from(DeadLetter).where(
                        DeadLetter.source_id == src.id, DeadLetter.fingerprint == fp)):
                    run.duplicates += 1
                    continue
                parked.add(fp)
                run.rejected += 1
                db.add(DeadLetter(organization_id=src.organization_id, source_id=src.id, run_id=run.id, reasons=reasons[:5],
                                  raw=_bounded(item), fingerprint=fp))
            else:
                records.append((rec, item))
        stored_newest = _store(db, src, integ, entity, records, run)
        if stored_newest and (newest is None or stored_newest > newest):
            newest = stored_newest
        db.flush()
        if complete:
            run.status = "partial" if run.rejected else "succeeded"
            if newest:
                src.watermark = newest.strftime("%Y-%m-%dT%H:%M:%S")
            src.last_success_at = utcnow()
            src.consecutive_failures = 0
            src.last_error, src.last_error_class = "", ""
            ratio = run.rejected / run.fetched if run.fetched else 0
            src.state = "degraded" if run.rejected and ratio >= DEGRADED_REJECT_RATIO else "ok"
        else:
            # Some pages arrived before the failure: keep them, but do not move the high-water mark past data we may have missed.
            run.status, run.error_class, run.error = "partial", result.error_class, result.error[:2000]
            src.consecutive_failures += 1
            src.last_error_class, src.last_error = result.error_class, result.error[:2000]
            src.state = "degraded"
    else:
        run.status, run.error_class, run.error = "failed", result.error_class, result.error[:2000]
        src.consecutive_failures += 1
        src.last_error_class, src.last_error = result.error_class, result.error[:2000]
        src.state = "failing"
    run.watermark_after = src.watermark
    run.finished_at = utcnow()
    src.last_run_at = run.finished_at
    src.total_stored += run.stored
    src.total_duplicates += run.duplicates
    src.total_rejected += run.rejected
    src.next_run_at = _next_run(src)

    # Mirror the pass into Executions so monitoring, alerts and repair proposals keep working for sources too.
    ex = Execution(organization_id=src.organization_id, integration_id=integ.id, version_id=version.id, tool_name=f"{integ.slug}.{src.operation_id}",
                   actor_type="ingest", status="succeeded" if complete else "failed", http_status=result.http_status, duration_ms=result.duration_ms,
                   attempts=result.attempts, error_class=result.error_class, error=result.error[:2000],
                   request_preview=result.request_preview, diagnostics=result.diagnostics,
                   output={"fetched": run.fetched, "stored": run.stored, "updated": run.updated, "duplicates": run.duplicates, "rejected": run.rejected})
    db.add(ex)
    db.flush()
    monitoring.after_execution(db, ex, integ)
    threshold = int(org_policy(db, src.organization_id).get("source_failure_alert_after", DEFAULT_FAILURE_ALERT_AFTER))
    if not complete and src.consecutive_failures == threshold:
        audit.alert(db, src.organization_id, "source_failing",
                    f"{integ.name} has failed {threshold} runs in a row ({src.last_error_class or 'error'}). {engine.ERROR_GUIDE.get(src.last_error_class, ('', ''))[1]}".strip(),
                    "high", integ.id, "#/sources")
    if run.status == "partial" and src.state == "degraded" and complete and not monitoring._recent_alert(db, src.organization_id, "bad_data", integ.id):
        audit.alert(db, src.organization_id, "bad_data", f"{integ.name}: {run.rejected} of {run.fetched} records were rejected (see Data Sources → Rejected).",
                    "medium", integ.id, "#/sources")
    db.commit()
    return run


def test_source(db: Session, src: IngestSource, transport=None, sample: int = 8) -> dict:
    """Fetch ONE page and show how it would be normalised, without storing anything (the 'test connection' for a source)."""
    integ, version, op = _config_for(db, src)
    spec = op["ingest"]
    probe = copy.deepcopy(version.config)
    next(o for o in probe["integration"]["operations"] if o["id"] == src.operation_id).pop("pagination", None)
    credential, conn = _credential(db, src.organization_id, probe)
    auth_type = (probe["integration"].get("authentication") or {}).get("type", "none")
    if auth_type != "none" and credential is None:
        return {"ok": False, "error_class": "credential_missing", "error": "No active, unexpired credential is connected.", "sample": [], "rejected": []}
    result = engine.execute(probe, src.operation_id, {}, credential=credential, transport=transport, raw=True,
                            query_overrides=_incremental_query(src, spec),
                            allowed_domains=org_policy(db, src.organization_id).get("allowed_domains") or [])
    if result.status != "succeeded":
        return {"ok": False, "error_class": result.error_class, "error": result.error, "http_status": result.http_status, "sample": [], "rejected": []}
    good, bad = [], []
    for item in result.output["items"][:200]:
        rec, reasons = normalize(item, spec, integ.slug)
        if rec is None:
            bad.append({"reasons": reasons, "raw": _bounded(item, 1500)})
        elif len(good) < sample:
            good.append({k: (v.isoformat() + "Z" if isinstance(v, datetime) else v) for k, v in rec.items()})
    return {"ok": True, "http_status": result.http_status, "fetched": result.output["count"], "entity": spec.get("entity") or "event",
            "sample": good, "rejected": bad[:5], "rejected_count": len(bad), "duration_ms": result.duration_ms}


def _bounded(item: Any, limit: int = 8000) -> dict:
    """Raw records are stored for audit/debugging but never unbounded."""
    if not isinstance(item, dict):
        item = {"value": item}
    blob = json.dumps(item, default=str)
    return item if len(blob) <= limit else {"_truncated": True, "preview": blob[:limit]}


def run_source_by_id(source_id: str, trigger: str = "schedule") -> None:
    with _RUNNING_LOCK:
        if source_id in _RUNNING:
            return
        _RUNNING.add(source_id)
    try:
        with session_scope() as db:
            src = db.get(IngestSource, source_id)
            if src:
                try:
                    run_source(db, src, trigger)
                except ValueError as e:
                    src.enabled, src.state, src.last_error = False, "failing", str(e)
    except Exception:
        log.exception("ingestion run failed")
        with session_scope() as db:
            src = db.get(IngestSource, source_id)
            if src:
                src.state, src.consecutive_failures = "failing", src.consecutive_failures + 1
                src.last_error, src.last_error_class = "Internal error while ingesting; see server log", "internal"
                src.next_run_at = _next_run(src)
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(source_id)


def try_acquire(source_id: str) -> bool:
    with _RUNNING_LOCK:
        if source_id in _RUNNING:
            return False
        _RUNNING.add(source_id)
        return True


def release(source_id: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING.discard(source_id)


def reprocess_dead_letter(db: Session, dl: DeadLetter) -> tuple[bool, list[str]]:
    """Re-run normalisation on a parked record with the currently published mapping. True when it was stored."""
    src = db.get(IngestSource, dl.source_id)
    integ, _, op = _config_for(db, src)
    entity = op["ingest"].get("entity") or "event"
    rec, reasons = normalize(dl.raw, op["ingest"], integ.slug)
    if rec is None:
        dl.reasons = reasons[:5]
        return False, reasons
    run = IngestRun(organization_id=dl.organization_id, source_id=src.id, updated=0)  # scratch counters only; never added to the session
    _store(db, src, integ, entity, [(rec, dl.raw)], run)
    src.total_stored += run.stored
    src.total_rejected = max(0, src.total_rejected - 1)
    db.delete(dl)
    return True, []


def is_running(source_id: str) -> bool:
    with _RUNNING_LOCK:
        return source_id in _RUNNING


# ----------------------------------------------------------------------------------------- health view

def health(src: IngestSource, now: datetime | None = None) -> dict:
    """One word + reason for the 'is this connection OK?' question."""
    now = now or utcnow()
    if not src.enabled:
        return {"status": "paused", "reason": "Paused"}
    if is_running(src.id):  # in-memory: a persisted "running" left by a crashed process must not stick
        return {"status": "running", "reason": "Running now"}
    if not src.last_run_at:
        return {"status": "pending", "reason": "Has not run yet"}
    if src.state == "failing":
        ctx = engine.ERROR_GUIDE.get(src.last_error_class, ("", ""))[0]
        return {"status": "failing", "reason": ctx or src.last_error[:120]}
    stale_after = timedelta(minutes=src.interval_minutes * STALE_FACTOR)
    if src.last_success_at and now - src.last_success_at > stale_after:
        return {"status": "stale", "reason": f"No successful run for over {src.interval_minutes * STALE_FACTOR} minutes"}
    if src.state == "degraded":
        return {"status": "degraded", "reason": "A large share of records failed validation"}
    return {"status": "healthy", "reason": "Last run succeeded"}


def credential_hygiene(db: Session) -> int:
    """Raise alerts for expiring/expired and long-unrotated credentials. Returns the number of alerts raised."""
    raised = 0
    now = utcnow()
    for conn in db.scalars(select(Connection).where(Connection.status == "active")).all():
        policy = org_policy(db, conn.organization_id)
        rotation_days = int(policy.get("credential_rotation_days", 90))
        msg = None
        severity = "medium"
        if conn.expires_at and conn.expires_at <= now:
            msg, severity = f"Credential '{conn.name}' has expired. Rotate it to resume the integrations that use it.", "high"
        elif conn.expires_at and conn.expires_at - now <= timedelta(days=7):
            msg = f"Credential '{conn.name}' expires on {conn.expires_at:%Y-%m-%d}. Rotate it before then."
        elif rotation_days and now - (conn.rotated_at or conn.created_at) > timedelta(days=rotation_days):
            msg = f"Credential '{conn.name}' has not been rotated in over {rotation_days} days."
        # Alert.integration_id doubles as the subject id so each credential is de-duplicated on its own.
        # Acknowledged alerts count too, so acknowledging one silences it for the day instead of re-raising it next hour.
        if msg and not monitoring._recent_alert(db, conn.organization_id, "credential", conn.id, minutes=60 * 24, include_acknowledged=True):
            audit.alert(db, conn.organization_id, "credential", msg, severity, conn.id, "#/credentials")
            raised += 1
    return raised


# ----------------------------------------------------------------------------------------- scheduler

def due_sources(db: Session) -> list[str]:
    """Enabled sources whose next run is due. Uses the in-memory run registry, not the persisted state, so a source
    left as "running" by a crashed or restarted process is picked up again."""
    due: list[str] = []
    for org_id in set(db.scalars(select(Integration.organization_id).distinct()).all()):
        for src in sync_sources(db, org_id):
            if src.enabled and not is_running(src.id) and (src.next_run_at is None or src.next_run_at <= utcnow()):
                due.append(src.id)
    return due


def scheduler_loop(stop: threading.Event) -> None:
    ticks = 0
    while not stop.wait(15):
        try:
            with session_scope() as db:
                due = due_sources(db)
                if ticks % 240 == 0:  # roughly hourly
                    credential_hygiene(db)
            for sid in due:
                threading.Thread(target=run_source_by_id, args=(sid, "schedule"), name="ingest", daemon=True).start()
        except Exception:
            log.exception("ingest scheduler tick failed")
        ticks += 1
