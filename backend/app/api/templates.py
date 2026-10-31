"""Real data-source templates (set up from a form) and the step-by-step guide for any integration."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.security import Principal, current_principal, require_role
from ..models import ApprovalRequest, Connection, Execution, IngestRun, IngestSource, Integration, IntegrationVersion
from ..services import audit, catalog, integrations
from .api_helpers import integration_summary_for, version_summary_for

router = APIRouter(prefix="/api/v1", tags=["templates & guide"])


@router.get("/source-templates")
def list_templates(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    existing = {i.slug for i in db.scalars(select(Integration).where(Integration.organization_id == p.org_id)).all()}
    conns = {c.name: c for c in db.scalars(select(Connection).where(Connection.organization_id == p.org_id)).all()}
    out = []
    for t in catalog.source_templates():
        cred = t.get("credential")
        out.append({**t, "installed": t["id"] in existing,
                    "credential_state": None if not cred else ("connected" if conns.get(cred["name"]) and conns[cred["name"]].status == "active" else "missing")})
    return out


class TemplateIn(BaseModel):
    variables: dict[str, str] = Field(default_factory=dict)


@router.post("/source-templates/{tid}/create")
def create_from_template(tid: str, body: TemplateIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    try:
        cfg = catalog.build_from_template(tid, body.variables)
    except catalog.TemplateError as e:
        raise HTTPException(422, str(e))
    integ, v = integrations.create_integration(db, p.org_id, p, cfg, "template", notes=f"Created from the {tid} template")
    tests = None
    if v.validation.get("valid"):
        tr = integrations.run_tests(db, v, p)
        tests = {"result": tr.result, "passed": tr.report["passed"], "total": tr.report["total"]}
    audit.record(db, p.org_id, p, "template.create", tid, integration=integ.slug)
    db.commit()
    return {"integration": integration_summary_for(db, integ), "version": version_summary_for(db, v), "validation": v.validation, "tests": tests}


def _step(key: str, title: str, detail: str, done: bool, href: str = "", action: str = "", blocked: str = "") -> dict:
    return {"key": key, "title": title, "detail": detail, "done": done, "href": href, "action": action, "blocked": blocked}


@router.get("/integrations/{iid}/guide")
def guide(iid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """The path from idea to a running integration, with the state of every step for this integration."""
    integ = integrations.get_integration(db, p.org_id, iid)
    latest = db.get(IntegrationVersion, integ.latest_version_id) if integ.latest_version_id else None
    live = db.get(IntegrationVersion, integ.published_version_id) if integ.published_version_id else None
    v = live or latest
    cfg = v.config["integration"] if v else {}
    auth = cfg.get("authentication") or {}
    conn = db.scalars(select(Connection).where(Connection.organization_id == p.org_id, Connection.name == auth.get("credential_ref"))).first() if auth.get("credential_ref") else None
    tr = integrations.latest_test(db, v) if v else None
    pending = db.scalars(select(ApprovalRequest).where(ApprovalRequest.integration_id == integ.id, ApprovalRequest.decision == "pending")).first()
    has_ingest = any(o.get("ingest") for o in cfg.get("operations") or [])
    src = db.scalars(select(IngestSource).where(IngestSource.integration_id == integ.id)).first()
    ran = bool(db.scalar(select(func.count()).select_from(Execution).where(Execution.integration_id == integ.id, Execution.status == "succeeded")))
    needs_cred = auth.get("type", "none") != "none"
    steps = [
        _step("describe", "Describe it to the AI (or start from a template)", "Tell AI Studio what to connect, upload API documentation, or pick a ready-made source template. The AI drafts a declarative configuration - no code.",
              bool(v), "#/studio", "Open AI Studio"),
        _step("validate", "Validate the configuration", "The platform checks the configuration against the schema and security policy and shows clear errors before anything runs.",
              bool(v and v.validation.get("valid")), f"#/integrations/{integ.id}", "Review validation"),
        _step("credential", "Connect the credential", "Secrets go straight into the encrypted vault (or read from an environment variable with env:NAME). The configuration only names the credential." if needs_cred else "This integration needs no credential.",
              (not needs_cred) or bool(conn and conn.status == "active"), "#/credentials", "Open Credentials"),
        _step("test", "Run the safe tests", "Schema-derived tests exercise the configuration without calling the real service. They must pass for this exact configuration.",
              bool(tr and tr.result == "passed"), f"#/integrations/{integ.id}", "Run tests"),
        _step("submit", "Submit for review", "Freezes the version and sends it to the administrators; any of them can approve it.",
              bool(live or pending or (latest and latest.status in ("pending_approval", "published"))), f"#/integrations/{integ.id}", "Submit"),
        _step("approve", "Administrator approves and publishes", "An admin reviews exactly what it can do and where it connects, then publishes that exact version. The AI can never approve.",
              bool(live), "#/approvals", "Open Approvals", blocked="" if (pending or live) else "Submit it first"),
        _step("run", "Run it" if not has_ingest else "Run it and watch the data arrive",
              ("Use Data Sources to test, run now and see health, or let the schedule run." if has_ingest else "Run a tool from the Tool Registry, Test Lab or a workflow."),
              bool(ran), "#/sources" if has_ingest else "#/tools", "Open Data Sources" if has_ingest else "Open Tool Registry", blocked="" if live else "Needs approval first"),
    ]
    current = next((i for i, s in enumerate(steps) if not s["done"]), len(steps))
    for i, s in enumerate(steps):
        s["state"] = "done" if s["done"] else "current" if i == current else "todo"
    return {"integration": integ.name, "slug": integ.slug, "steps": steps, "current": current, "source_id": src.id if src else None}
