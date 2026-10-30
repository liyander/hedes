from __future__ import annotations

import json

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.security import Principal, current_principal, require_human, require_role
from ..models import ApprovalRequest, Integration, IntegrationVersion, RepairProposal, TestRun, Tool
from ..services import audit, catalog, engine, integrations, openapi
from ..services.validation import normalize_config
from .common import iso, username

router = APIRouter(prefix="/api/v1", tags=["integrations"])


def _parse_config(config: dict | None, text: str | None) -> dict:
    if config is not None:
        return config
    if not text:
        raise HTTPException(422, "Provide config (object) or config_text (YAML/JSON)")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise HTTPException(422, f"Not valid YAML/JSON: {str(e)[:200]}")
    if not isinstance(data, dict):
        raise HTTPException(422, "The configuration must be an object")
    return data


def version_summary(db: Session, v: IntegrationVersion) -> dict:
    tr = integrations.latest_test(db, v)
    return {"id": v.id, "number": v.number, "version": v.version, "status": v.status, "source": v.source, "config_hash": v.config_hash,
            "valid": v.validation.get("valid"), "risk": {k: v.security_report.get(k) for k in ("score", "level", "blocking")},
            "tests": {"result": tr.result, "passed": tr.report["passed"], "total": tr.report["total"]} if tr else None,
            "created_by": username(db, v.created_by, "AI agent" if v.source == "repair" else "system"), "created_at": iso(v.created_at),
            "notes": v.notes}


def integration_summary(db: Session, i: Integration) -> dict:
    latest = db.get(IntegrationVersion, i.latest_version_id) if i.latest_version_id else None
    live = db.get(IntegrationVersion, i.published_version_id) if i.published_version_id else None
    cfg = (live or latest).config if (live or latest) else {}
    integ = cfg.get("integration", {})
    return {"id": i.id, "slug": i.slug, "name": i.name, "description": i.description, "status": i.status,
            "base_url": integ.get("base_url"), "auth_type": (integ.get("authentication") or {}).get("type"),
            "operations": len(integ.get("operations") or []),
            "published_version": live.version if live else None,
            "latest_version": latest.version if latest else None, "latest_status": latest.status if latest else None,
            "risk": (latest.security_report or {}).get("level") if latest else None,
            "updated_at": iso(i.updated_at), "created_by": username(db, i.created_by)}


@router.get("/integrations")
def list_integrations(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(Integration).where(Integration.organization_id == p.org_id, Integration.status != "archived")
                      .order_by(Integration.updated_at.desc())).all()
    return [integration_summary(db, i) for i in rows]


class ConfigIn(BaseModel):
    config: dict | None = None
    config_text: str | None = Field(default=None, max_length=500_000)


@router.post("/integrations")
def create_draft(body: ConfigIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    cfg = _parse_config(body.config, body.config_text)
    slug = ((cfg.get("integration") or {}).get("id") or "")
    if not isinstance(slug, str) or not slug:
        raise HTTPException(422, "integration.id is required")
    integ, v = integrations.create_integration(db, p.org_id, p, cfg, "manual")
    db.commit()
    return {"integration": integration_summary(db, integ), "version": version_summary(db, v), "validation": v.validation}


class ImportIn(BaseModel):
    content: str = Field(min_length=2, max_length=1_000_000)
    operations: list[str] | None = None
    slug: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,62}$")
    base_url: str | None = None
    preview: bool = False


@router.post("/integrations/import/openapi")
def import_openapi(body: ImportIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    try:
        cat = openapi.parse_openapi(body.content)
    except openapi.OpenAPIError as e:
        raise HTTPException(422, str(e))
    if body.preview:
        return {"catalog": {k: cat[k] for k in ("title", "version", "description", "base_url", "auth")},
                "operations": [{"id": o["id"], "method": o["method"], "path": o["path"], "summary": o["summary"]} for o in cat["operations"]]}
    slug = body.slug or openapi.slugify(cat["title"])
    try:
        cfg = openapi.catalog_to_config(cat, body.operations, slug, cat["title"], body.base_url)
    except openapi.OpenAPIError as e:
        raise HTTPException(422, str(e))
    integ, v = integrations.create_integration(db, p.org_id, p, cfg, "openapi")
    if v.validation.get("valid"):
        integrations.run_tests(db, v, p, [{"id": "upload", "name": "openapi", "catalog": cat}])
    db.commit()
    return {"integration": integration_summary(db, integ), "version": version_summary(db, v), "validation": v.validation}


@router.get("/integrations/{iid}")
def get_integration(iid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    i = integrations.get_integration(db, p.org_id, iid)
    versions = db.scalars(select(IntegrationVersion).where(IntegrationVersion.integration_id == i.id).order_by(IntegrationVersion.number.desc())).all()
    tools = db.scalars(select(Tool).where(Tool.integration_id == i.id, Tool.integration_version_id == i.published_version_id)).all()
    approvals = db.scalars(select(ApprovalRequest).where(ApprovalRequest.integration_id == i.id).order_by(ApprovalRequest.created_at.desc())).all()
    repairs = db.scalars(select(RepairProposal).where(RepairProposal.integration_id == i.id).order_by(RepairProposal.created_at.desc()).limit(10)).all()
    return {**integration_summary(db, i),
            "versions": [version_summary(db, v) for v in versions],
            "tools": [{"id": t.id, "name": t.name, "method": t.method, "description": t.description, "enabled": t.enabled,
                       "mcp_enabled": t.mcp_enabled, "allowed_roles": t.allowed_roles} for t in tools],
            "approvals": [{"id": a.id, "version_id": a.version_id, "decision": a.decision, "kind": a.kind, "comment": a.comment,
                           "requester": username(db, a.requester_id, "AI repair agent"), "reviewer": username(db, a.reviewer_id),
                           "created_at": iso(a.created_at), "decided_at": iso(a.decided_at)} for a in approvals],
            "repairs": [{"id": r.id, "status": r.status, "diagnosis": r.diagnosis, "created_at": iso(r.created_at)} for r in repairs]}


@router.get("/integrations/{iid}/versions")
def version_history(iid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    i = integrations.get_integration(db, p.org_id, iid)
    rows = db.scalars(select(IntegrationVersion).where(IntegrationVersion.integration_id == i.id).order_by(IntegrationVersion.number.desc())).all()
    return [version_summary(db, v) for v in rows]


@router.get("/versions")
def all_versions(p: Principal = Depends(current_principal), db: Session = Depends(get_db), limit: int = Query(100, le=500)):
    rows = db.execute(select(IntegrationVersion, Integration).join(Integration, IntegrationVersion.integration_id == Integration.id)
                      .where(Integration.organization_id == p.org_id).order_by(IntegrationVersion.created_at.desc()).limit(limit)).all()
    return [{**version_summary(db, v), "integration_id": i.id, "integration": i.name, "slug": i.slug,
             "is_live": i.published_version_id == v.id} for v, i in rows]


@router.get("/versions/{vid}")
def get_version(vid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    v, i = integrations.get_version(db, p.org_id, vid)
    tests = db.scalars(select(TestRun).where(TestRun.version_id == v.id).order_by(TestRun.executed_at.desc()).limit(5)).all()
    return {**version_summary(db, v), "integration_id": i.id, "integration": i.name, "slug": i.slug, "config": v.config,
            "validation": v.validation, "security": v.security_report, "is_live": i.published_version_id == v.id,
            "test_runs": [{"id": t.id, "result": t.result, "report": t.report, "current": t.config_hash == v.config_hash,
                           "executed_at": iso(t.executed_at)} for t in tests]}


@router.put("/versions/{vid}")
def update_version(vid: str, body: ConfigIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    v, i = integrations.get_version(db, p.org_id, vid)
    integrations.update_draft(db, v, i, _parse_config(body.config, body.config_text), p)
    db.commit()
    return {"version": version_summary(db, v), "validation": v.validation, "security": v.security_report}


class VersionRef(BaseModel):
    version_id: str | None = None


def _target(db: Session, p: Principal, iid: str, version_id: str | None) -> tuple[IntegrationVersion, Integration]:
    i = integrations.get_integration(db, p.org_id, iid)
    v = db.get(IntegrationVersion, version_id or i.latest_version_id)
    if not v or v.integration_id != i.id:
        raise HTTPException(404, "Version not found")
    return v, i


@router.post("/integrations/{iid}/validate")
def validate_schema(iid: str, body: VersionRef, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    v, i = _target(db, p, iid, body.version_id)
    integrations.analyse(db, v, p.org_id)
    db.commit()
    return {"validation": v.validation, "security": v.security_report}


@router.post("/integrations/{iid}/tests")
def run_tests(iid: str, body: VersionRef, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    v, i = _target(db, p, iid, body.version_id)
    tr = integrations.run_tests(db, v, p)
    audit.record(db, p.org_id, p, "version.test", f"{i.slug}@{v.version}", result=tr.result)
    db.commit()
    return {"id": tr.id, "result": tr.result, "report": tr.report}


class PreviewIn(BaseModel):
    version_id: str | None = None
    operation: str
    inputs: dict = {}


@router.post("/integrations/{iid}/preview")
def render_preview(iid: str, body: PreviewIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    v, i = _target(db, p, iid, body.version_id)
    try:
        return engine.preview(v.config, body.operation, body.inputs)
    except engine.EngineError as e:
        return {"ok": False, "errors": [str(e)]}


@router.post("/integrations/{iid}/submit")
def submit_for_review(iid: str, body: VersionRef, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    v, i = _target(db, p, iid, body.version_id)
    req = integrations.submit_for_review(db, v, i, p)
    db.commit()
    return {"approval_id": req.id, "status": v.status}


class ReviseIn(BaseModel):
    version_id: str | None = None
    config: dict | None = None
    config_text: str | None = None


@router.post("/integrations/{iid}/revise")
def revise(iid: str, body: ReviseIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    base, i = _target(db, p, iid, body.version_id)
    cfg = _parse_config(body.config, body.config_text) if (body.config or body.config_text) else None
    v = integrations.revise(db, i, base, p, cfg)
    db.commit()
    return version_summary(db, v)


@router.post("/admin/integrations/{iid}/disable")
def disable_tool(iid: str, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    i = integrations.get_integration(db, p.org_id, iid)
    i.status = "disabled"
    audit.record(db, p.org_id, p, "integration.disable", i.slug)
    db.commit()
    return integration_summary(db, i)


@router.post("/admin/integrations/{iid}/enable")
def enable_integration(iid: str, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    i = integrations.get_integration(db, p.org_id, iid)
    if i.status != "disabled":
        raise HTTPException(409, "Integration is not disabled")
    i.status = "draft"
    integrations.refresh_status(db, i)
    audit.record(db, p.org_id, p, "integration.enable", i.slug)
    db.commit()
    return integration_summary(db, i)


class RollbackIn(BaseModel):
    version_id: str


@router.post("/integrations/{iid}/rollback")
def rollback_version(iid: str, body: RollbackIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    i = integrations.get_integration(db, p.org_id, iid)
    v = integrations.rollback(db, p.org_id, i, body.version_id, p)
    db.commit()
    return version_summary(db, v)


def _flatten(value, prefix="") -> dict:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
        return out
    if isinstance(value, list) and value and all(isinstance(x, dict) and "id" in x for x in value):
        out = {}
        for x in value:
            out.update(_flatten(x, f"{prefix}[{x['id']}]"))
        return out
    return {prefix: value}


@router.get("/integrations/{iid}/compare")
def compare_versions(iid: str, a: str, b: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    va, _ = _target(db, p, iid, a)
    vb, _ = _target(db, p, iid, b)
    fa, fb = _flatten(va.config), _flatten(vb.config)
    changes = []
    for k in sorted(set(fa) | set(fb)):
        if k not in fa:
            changes.append({"path": k, "change": "added", "after": fb[k]})
        elif k not in fb:
            changes.append({"path": k, "change": "removed", "before": fa[k]})
        elif json.dumps(fa[k], sort_keys=True) != json.dumps(fb[k], sort_keys=True):
            changes.append({"path": k, "change": "changed", "before": fa[k], "after": fb[k]})
    return {"a": va.version, "b": vb.version, "changes": changes}


@router.get("/catalog/examples")
def examples(p: Principal = Depends(current_principal)):
    return [{"slug": k, "name": v["integration"]["name"], "description": v["integration"].get("description", ""),
             "config": normalize_config(v)} for k, v in catalog.known_services().items()]
