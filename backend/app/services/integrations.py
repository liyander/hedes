"""Integration lifecycle: versions, validation, safe tests, review submission, approval, publication, rollback.

Invariants enforced here (not in prompts or the UI):
* A version is editable only while it is a draft; submitting freezes it and pins its config hash.
* Publication happens only through ``approve`` (or ``rollback`` to a hash that was approved before), by a human
  admin who is not the requester, after re-checking the hash, the security policy, tests and credentials.
* Only the published version's tools are executable.
"""
from __future__ import annotations

import json

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.db import utcnow
from ..models import (ApprovalEvent, ApprovalRequest, Connection, Integration, IntegrationVersion, Organization,
                      RepairProposal, TestRun, Tool)
from . import agents, audit, engine, security_review
from .validation import config_hash, normalize_config, validate_config


def org_policy(db: Session, org_id: str) -> dict:
    org = db.get(Organization, org_id)
    return dict(org.policy or {}) if org else {}


def connections_map(db: Session, org_id: str) -> dict[str, dict]:
    rows = db.scalars(select(Connection).where(Connection.organization_id == org_id)).all()
    return {c.name: {"auth_type": c.auth_type, "status": c.status, "id": c.id} for c in rows}


def get_integration(db: Session, org_id: str, integration_id: str) -> Integration:
    integ = db.get(Integration, integration_id)
    if not integ or integ.organization_id != org_id:
        raise HTTPException(404, "Integration not found")
    return integ


def get_version(db: Session, org_id: str, version_id: str) -> tuple[IntegrationVersion, Integration]:
    v = db.get(IntegrationVersion, version_id)
    if not v:
        raise HTTPException(404, "Version not found")
    return v, get_integration(db, org_id, v.integration_id)


def refresh_status(db: Session, integ: Integration) -> None:
    if integ.status in ("disabled", "archived"):
        return
    if integ.published_version_id:
        integ.status = "published"
        return
    latest = db.get(IntegrationVersion, integ.latest_version_id) if integ.latest_version_id else None
    integ.status = {"pending_approval": "pending_approval", "rejected": "revision",
                    "changes_requested": "revision"}.get(latest.status if latest else "", "draft")


def analyse(db: Session, version: IntegrationVersion, org_id: str) -> None:
    """Validation + deterministic security review, stored on the version."""
    version.validation = validate_config(version.config)
    version.security_report = security_review.review(version.config, org_policy(db, org_id), connections_map(db, org_id))


def _next_version_string(db: Session, integ: Integration, wanted: str, bump: str) -> str:
    existing = set(db.scalars(select(IntegrationVersion.version).where(IntegrationVersion.integration_id == integ.id)).all())
    if not existing:
        return wanted
    latest = max(existing, key=lambda s: tuple(int(x) for x in s.split(".")))
    major, minor, patch = (int(x) for x in latest.split("."))
    candidate = wanted if wanted not in existing and tuple(int(x) for x in wanted.split(".")) > (major, minor, patch) else \
        (f"{major}.{minor}.{patch + 1}" if bump == "patch" else f"{major}.{minor + 1}.0")
    while candidate in existing:
        major, minor, patch = (int(x) for x in candidate.split("."))
        candidate = f"{major}.{minor}.{patch + 1}"
    return candidate


def create_integration(db: Session, org_id: str, actor, config: dict, source: str, session_id: str | None = None,
                       notes: str = "") -> tuple[Integration, IntegrationVersion]:
    config = normalize_config(config)
    slug = config["integration"]["id"]
    integ = db.scalars(select(Integration).where(Integration.organization_id == org_id, Integration.slug == slug)).first()
    if not integ:
        integ = Integration(organization_id=org_id, slug=slug, name=config["integration"]["name"],
                            description=config["integration"].get("description", ""),
                            created_by=getattr(actor, "id", None), session_id=session_id)
        db.add(integ)
        db.flush()
        audit.record(db, org_id, actor, "integration.create", slug, source=source)
    v = create_version(db, integ, config, source, actor, notes)
    return integ, v


def create_version(db: Session, integ: Integration, config: dict, source: str, actor, notes: str = "",
                   bump: str = "minor") -> IntegrationVersion:
    config = normalize_config(config)
    if not isinstance(config.get("integration"), dict):
        raise HTTPException(422, "Configuration must contain an 'integration' object")
    config["integration"]["id"] = integ.slug
    wanted = str(config["integration"].get("version") or "1.0.0")
    if not all(p.isdigit() for p in wanted.split(".")) or wanted.count(".") != 2:
        wanted = "1.0.0"
    config["integration"]["version"] = _next_version_string(db, integ, wanted, bump)
    number = (db.scalar(select(func.max(IntegrationVersion.number)).where(IntegrationVersion.integration_id == integ.id)) or 0) + 1
    v = IntegrationVersion(integration_id=integ.id, number=number, version=config["integration"]["version"],
                           config=config, config_hash=config_hash(config), source=source, notes=notes,
                           created_by=getattr(actor, "id", None))
    db.add(v)
    db.flush()
    analyse(db, v, integ.organization_id)
    integ.latest_version_id = v.id
    integ.name = config["integration"].get("name") or integ.name
    integ.description = config["integration"].get("description", integ.description)
    refresh_status(db, integ)
    audit.record(db, integ.organization_id, actor, "version.create", f"{integ.slug}@{v.version}", source=source)
    return v


def update_draft(db: Session, version: IntegrationVersion, integ: Integration, config: dict, actor) -> IntegrationVersion:
    if version.status != "draft":
        raise HTTPException(409, "Only draft versions can be edited. Create a new revision instead.")
    config = normalize_config(config)
    config.setdefault("integration", {})["id"] = integ.slug
    config["integration"]["version"] = version.version
    version.config = config
    version.config_hash = config_hash(config)
    analyse(db, version, integ.organization_id)
    audit.record(db, integ.organization_id, actor, "version.update", f"{integ.slug}@{version.version}")
    return version


# ----------------------------------------------------------------------------------------- testing

def _mock_transport(status: int, body, captured: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"method": request.method, "url": str(request.url)})
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def run_mock_tests(config: dict, cases: list[dict]) -> dict:
    results = []
    for case in cases:
        captured: list = []
        op = next((o for o in config["integration"]["operations"] if o["id"] == case["operation"]), None)
        if not op:
            results.append({**case, "passed": False, "actual": "unknown operation"})
            continue
        res = engine.execute(config, case["operation"], case.get("inputs") or {}, credential={"token": "test", "api_key": "test", "username": "test", "password": "test", "client_id": "test",
                                                  "client_secret": "test", "access_key_id": "test", "secret_access_key": "test"},
                             transport=_mock_transport(case["mock_status"], case.get("mock_response"), captured), live=False)
        actual = "succeeded" if res.status == "succeeded" else res.error_class
        passed = actual == case["expect"]
        if passed and case["expect"] == "succeeded" and captured and captured[0]["method"] != op["method"]:
            passed, actual = False, f"sent {captured[0]['method']} instead of {op['method']}"
        results.append({"operation": case["operation"], "name": case["name"], "expect": case["expect"], "actual": actual,
                        "passed": passed, "inputs": case.get("inputs"), "output": res.output, "error": res.error,
                        "request": res.request_preview, "mock_status": case["mock_status"], "duration_ms": res.duration_ms})
    passed = sum(1 for r in results if r["passed"])
    return {"total": len(results), "passed": passed, "failed": len(results) - passed, "cases": results}


def run_tests(db: Session, version: IntegrationVersion, actor, documents: list[dict] | None = None,
              cases: list[dict] | None = None) -> TestRun:
    if not version.validation.get("valid"):
        raise HTTPException(409, "Fix validation errors before testing")
    cases = cases or agents.generate_tests(version.config, documents or [])
    report = run_mock_tests(version.config, cases)
    tr = TestRun(version_id=version.id, config_hash=version.config_hash, kind="mock",
                 result="passed" if report["failed"] == 0 and report["total"] else "failed", report=report,
                 executed_by=getattr(actor, "id", None))
    db.add(tr)
    db.flush()
    return tr


def latest_test(db: Session, version: IntegrationVersion) -> TestRun | None:
    return db.scalars(select(TestRun).where(TestRun.version_id == version.id, TestRun.config_hash == version.config_hash)
                      .order_by(TestRun.executed_at.desc())).first()


# ----------------------------------------------------------------------------------------- review

def submit_for_review(db: Session, version: IntegrationVersion, integ: Integration, actor, kind: str = "",
                      requester_id: str | None = None) -> ApprovalRequest:
    if version.status != "draft":
        raise HTTPException(409, f"Version is {version.status}; only drafts can be submitted")
    analyse(db, version, integ.organization_id)
    if not version.validation.get("valid"):
        raise HTTPException(409, "The configuration has validation errors")
    if version.security_report.get("blocking"):
        raise HTTPException(409, "Blocking security findings must be resolved before review")
    policy = org_policy(db, integ.organization_id)
    tr = latest_test(db, version)
    if policy.get("require_tests", True) and (not tr or tr.result != "passed"):
        raise HTTPException(409, "Run the safe tests for this exact configuration (and make them pass) before submitting")
    for old in db.scalars(select(ApprovalRequest).where(ApprovalRequest.integration_id == integ.id, ApprovalRequest.decision == "pending")).all():
        old.decision = "superseded"
        old_v = db.get(IntegrationVersion, old.version_id)
        if old_v and old_v.status == "pending_approval":
            old_v.status = "superseded"
    version.status = "pending_approval"
    req = ApprovalRequest(organization_id=integ.organization_id, integration_id=integ.id, version_id=version.id,
                          config_hash=version.config_hash, requester_id=requester_id if requester_id is not None else getattr(actor, "id", None),
                          kind=kind or ("update" if integ.published_version_id else "new"),
                          risk={k: version.security_report.get(k) for k in ("score", "level", "capabilities", "destinations")})
    db.add(req)
    db.flush()
    db.add(ApprovalEvent(request_id=req.id, actor_id=getattr(actor, "id", None), action="submitted"))
    refresh_status(db, integ)
    audit.record(db, integ.organization_id, actor, "approval.submit", f"{integ.slug}@{version.version}", request_id=req.id, config_hash=version.config_hash)
    audit.alert(db, integ.organization_id, "approval", f"{integ.name} {version.version} is waiting for review", "info", integ.id, f"#/approvals/{req.id}")
    return req


def _pending(db: Session, org_id: str, request_id: str) -> tuple[ApprovalRequest, IntegrationVersion, Integration]:
    req = db.get(ApprovalRequest, request_id)
    if not req or req.organization_id != org_id:
        raise HTTPException(404, "Approval request not found")
    if req.decision != "pending":
        raise HTTPException(409, f"This request was already {req.decision}")
    v = db.get(IntegrationVersion, req.version_id)
    integ = db.get(Integration, req.integration_id)
    return req, v, integ


def approval_blockers(db: Session, req: ApprovalRequest, v: IntegrationVersion, integ: Integration, reviewer_id: str | None) -> list[str]:
    problems = []
    if reviewer_id and req.requester_id == reviewer_id:
        problems.append("You submitted this version; a different administrator must review it.")
    if config_hash(v.config) != req.config_hash or v.config_hash != req.config_hash:
        problems.append("The configuration changed after it was submitted. It must be resubmitted.")
    report = security_review.review(v.config, org_policy(db, integ.organization_id), connections_map(db, integ.organization_id))
    if report["blocking"]:
        problems.append("Blocking security findings: " + "; ".join(f["title"] for f in report["findings"] if f["blocking"]))
    if not report["checks"]["schema_valid"]:
        problems.append("The configuration no longer validates.")
    auth = v.config["integration"]["authentication"]
    if auth["type"] != "none" and report["checks"].get("credential_connected") is not True:
        problems.append(f"Connect an active {auth['type']} credential named '{auth.get('credential_ref')}' in Credentials before approving.")
    tr = latest_test(db, v)
    if org_policy(db, integ.organization_id).get("require_tests", True) and (not tr or tr.result != "passed"):
        problems.append("No passing test run exists for this exact configuration.")
    return problems


def _register_tools(db: Session, integ: Integration, v: IntegrationVersion) -> None:
    previous = {t.name: t for t in db.scalars(select(Tool).where(Tool.integration_id == integ.id, Tool.enabled.is_(True))).all()}
    for t in previous.values():
        t.enabled = False
    for op in v.config["integration"]["operations"]:
        name = f"{integ.slug}.{op['id']}"
        old = previous.get(name)
        resp = op.get("response") or {}
        outputs = ["items", "count"] if resp.get("items_path") else list((resp.get("mapping") or {}).keys())
        db.add(Tool(organization_id=integ.organization_id, integration_id=integ.id, integration_version_id=v.id, name=name,
                    operation_id=op["id"], description=op.get("description", ""), method=op["method"],
                    input_schema=op["input_schema"], output_fields=outputs,
                    allowed_roles=old.allowed_roles if old else ["user", "admin", "owner"],
                    mcp_enabled=old.mcp_enabled if old else False))


def _publish(db: Session, integ: Integration, v: IntegrationVersion) -> None:
    if integ.published_version_id and integ.published_version_id != v.id:
        prev = db.get(IntegrationVersion, integ.published_version_id)
        if prev:
            prev.status = "superseded"
    v.status = "published"
    integ.published_version_id = v.id
    integ.status = "published"
    _register_tools(db, integ, v)


def approve(db: Session, org_id: str, request_id: str, reviewer, comment: str = "") -> ApprovalRequest:
    req, v, integ = _pending(db, org_id, request_id)
    problems = approval_blockers(db, req, v, integ, reviewer.id)
    if problems:
        raise HTTPException(409, " ".join(problems))
    _publish(db, integ, v)
    req.decision, req.reviewer_id, req.comment, req.decided_at = "approved", reviewer.id, comment, utcnow()
    db.add(ApprovalEvent(request_id=req.id, actor_id=reviewer.id, action="approved", comment=comment))
    rp = db.scalars(select(RepairProposal).where(RepairProposal.approval_request_id == req.id)).first()
    if rp:
        rp.status = "approved"
    audit.record(db, org_id, reviewer, "approval.approve", f"{integ.slug}@{v.version}", request_id=req.id, config_hash=req.config_hash)
    audit.record(db, org_id, reviewer, "integration.publish", f"{integ.slug}@{v.version}", config_hash=req.config_hash)
    return req


def decide_negative(db: Session, org_id: str, request_id: str, reviewer, decision: str, comment: str) -> ApprovalRequest:
    if not comment.strip():
        raise HTTPException(422, "Explain the decision so the requester can act on it")
    req, v, integ = _pending(db, org_id, request_id)
    if req.requester_id == reviewer.id:
        raise HTTPException(403, "You cannot review your own submission")
    req.decision, req.reviewer_id, req.comment, req.decided_at = decision, reviewer.id, comment, utcnow()
    v.status = decision  # rejected | changes_requested
    db.add(ApprovalEvent(request_id=req.id, actor_id=reviewer.id, action=decision, comment=comment))
    rp = db.scalars(select(RepairProposal).where(RepairProposal.approval_request_id == req.id)).first()
    if rp:
        rp.status = "rejected"
    refresh_status(db, integ)
    audit.record(db, org_id, reviewer, f"approval.{decision}", f"{integ.slug}@{v.version}", request_id=req.id, comment=comment)
    return req


def revise(db: Session, integ: Integration, base: IntegrationVersion, actor, config: dict | None = None,
           source: str = "manual", notes: str = "") -> IntegrationVersion:
    return create_version(db, integ, json.loads(json.dumps(config or base.config)), source, actor,
                          notes or f"Revision of {base.version}", bump="patch")


def rollback(db: Session, org_id: str, integ: Integration, version_id: str, admin) -> IntegrationVersion:
    v = db.get(IntegrationVersion, version_id)
    if not v or v.integration_id != integ.id:
        raise HTTPException(404, "Version not found")
    approved = db.scalars(select(ApprovalRequest).where(ApprovalRequest.version_id == v.id, ApprovalRequest.decision == "approved",
                                                       ApprovalRequest.config_hash == config_hash(v.config))).first()
    if not approved or v.status != "superseded":
        raise HTTPException(409, "Only a previously approved, superseded version can be restored directly. Otherwise create a revision and submit it.")
    report = security_review.review(v.config, org_policy(db, org_id), connections_map(db, org_id))
    if report["blocking"]:
        raise HTTPException(409, "That version no longer passes the security policy")
    _publish(db, integ, v)
    audit.record(db, org_id, admin, "integration.rollback", f"{integ.slug}@{v.version}", config_hash=v.config_hash)
    return v
