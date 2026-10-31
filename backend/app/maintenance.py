"""Maintenance commands.

    python -m app.maintenance purge-demo     back up the database, then remove demo / mock data
                                             (PostgreSQL: back it up yourself, then add --without-backup)

``purge-demo`` removes integrations that call the bundled mock APIs (``/mock/``) or that are untouched platform seeds
(every version has source ``seed``), together with everything that hangs off them: versions, tools, approvals, tests,
executions, repairs, data sources with their runs / events / users / devices / dead letters, alerts, workflows that only
use their tools, and credentials no remaining integration uses. Accounts, organisation settings, AI providers, the audit
log and anything a person created or edited are kept.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .core.config import get_settings
from .core.db import engine, init_db, session_scope, utcnow
from .models import (Alert, ApprovalEvent, ApprovalRequest, Connection, DeadLetter, Execution, IngestRun, IngestSource,
                     Integration, IntegrationVersion, NormalizedDevice, NormalizedEvent, NormalizedUser, RepairProposal,
                     TestRun, Tool, Workflow, WorkflowRun, WorkflowStep, WorkflowVersion)
from .services import audit, vault


def _is_demo(integ: Integration, versions: list[IntegrationVersion]) -> bool:
    """An untouched platform seed, or an integration whose current configuration (the published one, else the latest) still
    calls the bundled mock APIs. One built against a mock and later pointed at a real API is real work and is kept."""
    if versions and all(v.source == "seed" for v in versions):
        return True
    current = next((v for v in versions if v.id == (integ.published_version_id or integ.latest_version_id)), None)
    return current is not None and "/mock/" in str(current.config.get("integration", {}).get("base_url", ""))


def purge_demo_data(db: Session) -> dict:
    integs = db.scalars(select(Integration)).all()
    by_integ: dict[str, list[IntegrationVersion]] = {}
    for v in db.scalars(select(IntegrationVersion)).all():
        by_integ.setdefault(v.integration_id, []).append(v)
    demo = [i for i in integs if _is_demo(i, by_integ.get(i.id, []))]
    if not demo:
        return {"integrations": [], "credentials": [], "workflows": 0, "events": 0}
    ids = [i.id for i in demo]
    slugs = {i.slug for i in demo}
    version_ids = [v.id for i in ids for v in by_integ.get(i, [])]
    cred_refs = {(v.config["integration"].get("authentication") or {}).get("credential_ref") for i in ids for v in by_integ.get(i, [])} - {None}
    kept_refs = {(v.config["integration"].get("authentication") or {}).get("credential_ref")
                 for i in integs if i.id not in ids for v in by_integ.get(i.id, [])}
    source_ids = db.scalars(select(IngestSource.id).where(IngestSource.integration_id.in_(ids))).all()
    request_ids = db.scalars(select(ApprovalRequest.id).where(ApprovalRequest.integration_id.in_(ids))).all()

    workflows = []
    for wf in db.scalars(select(Workflow)).all():
        tools = [n.get("tool", "") for v in db.scalars(select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf.id)).all()
                 for n in (v.graph or {}).get("nodes") or []]
        if tools and all(t.split(".", 1)[0] in slugs for t in tools):
            workflows.append(wf)
    wf_ids = [w.id for w in workflows]
    run_ids = db.scalars(select(WorkflowRun.id).where(WorkflowRun.workflow_id.in_(wf_ids))).all()

    conns = [c for c in db.scalars(select(Connection)).all() if c.name in cred_refs and c.name not in kept_refs]
    events = len(db.scalars(select(NormalizedEvent.id).where(NormalizedEvent.source_id.in_(source_ids))).all())

    db.execute(delete(WorkflowStep).where(WorkflowStep.run_id.in_(run_ids)))
    db.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(run_ids)))
    db.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id.in_(wf_ids)))
    db.execute(delete(Workflow).where(Workflow.id.in_(wf_ids)))
    db.execute(delete(Execution).where(or_(Execution.integration_id.in_(ids), *[Execution.tool_name.startswith(s + ".") for s in slugs])))
    db.execute(delete(RepairProposal).where(RepairProposal.integration_id.in_(ids)))
    db.execute(delete(ApprovalEvent).where(ApprovalEvent.request_id.in_(request_ids)))
    db.execute(delete(ApprovalRequest).where(ApprovalRequest.id.in_(request_ids)))
    db.execute(delete(TestRun).where(TestRun.version_id.in_(version_ids)))
    db.execute(delete(Tool).where(Tool.integration_id.in_(ids)))
    for model in (DeadLetter, NormalizedEvent, NormalizedUser, NormalizedDevice, IngestRun):
        db.execute(delete(model).where(model.source_id.in_(source_ids)))
    db.execute(delete(IngestSource).where(IngestSource.id.in_(source_ids)))
    db.execute(delete(Alert).where(Alert.integration_id.in_(ids + [c.id for c in conns])))
    for c in conns:
        vault.delete_secret(db, c.secret_ref)
        db.delete(c)
    db.execute(delete(IntegrationVersion).where(IntegrationVersion.id.in_(version_ids)))
    db.execute(delete(Integration).where(Integration.id.in_(ids)))
    summary = {"integrations": sorted(slugs), "credentials": sorted(c.name for c in conns), "workflows": len(wf_ids), "events": events}
    for org_id in {i.organization_id for i in demo}:
        audit.record(db, org_id, "maintenance", "maintenance.purge_demo", "demo data", **summary)
    return summary


def close_interrupted_runs(db: Session, older_than: timedelta = timedelta(hours=1)) -> int:
    """Runs still marked running long after they started were cut off by a restart: mark them failed so they stop showing
    as running for ever. Only old ones - another server sharing the database may be running recent ones right now."""
    cutoff, n = utcnow() - older_than, 0
    for model in (IngestRun, WorkflowRun):
        for run in db.scalars(select(model).where(model.status == "running", model.started_at < cutoff)).all():
            run.status, run.error, run.finished_at = "failed", "Interrupted: the server stopped during this run", utcnow()
            n += 1
    return n


def backup_sqlite() -> Path | None:
    """Consistent copy of the SQLite database (safe while the server is running). None for other databases."""
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        return None
    src = Path(url[len("sqlite:///"):])
    dest = src.with_name(f"{src.stem}.before-purge-{datetime.now():%Y%m%d-%H%M%S}{src.suffix}")
    with sqlite3.connect(src) as a, sqlite3.connect(dest) as b:
        a.backup(b)
    return dest


def main(argv: list[str]) -> int:
    if argv[:1] != ["purge-demo"]:
        print(__doc__)
        return 2
    if get_settings().demo_mode:
        print("DEMO_MODE is on: demo data would be seeded again on the next start. Run without DEMO_MODE.")
        return 1
    init_db()
    backup = backup_sqlite()
    if backup:
        print(f"Backup written to {backup}")
    elif "--without-backup" not in argv[1:]:
        print("This database is not SQLite, so no backup can be made here. Back it up first (pg_dump, or a Neon branch),\n"
              "then run again with:  python -m app.maintenance purge-demo --without-backup")
        return 1
    with session_scope() as db:
        summary = purge_demo_data(db)
    engine.dispose()
    if not summary["integrations"]:
        print("No demo or mock data found.")
    else:
        print(f"Removed integrations: {', '.join(summary['integrations'])}")
        print(f"Removed credentials:  {', '.join(summary['credentials']) or '-'}")
        print(f"Removed workflows: {summary['workflows']}, events: {summary['events']} (plus their runs, tools, approvals and alerts)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
