"""Self-healing: diagnose drift, prepare a patched version, regression-test it, and submit it for admin review.

The repair agent never edits a published version; it creates a new draft that goes through normal approval.
"""
from __future__ import annotations

import copy
import logging
import threading

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..core.db import session_scope
from ..models import Execution, Integration, IntegrationVersion, RepairProposal, Tool
from . import agents, audit, integrations
from .ai_gateway import Gateway, ProviderError
from .monitoring import open_repair

log = logging.getLogger("integra.repairs")
REPAIR_ACTOR = "ai-repair-agent"


def create_repair(db: Session, org_id: str, execution_id: str) -> RepairProposal:
    ex = db.get(Execution, execution_id)
    if not ex or ex.organization_id != org_id:
        raise HTTPException(404, "Execution not found")
    if ex.error_class != "response_drift":
        raise HTTPException(409, "Automatic repairs are prepared for response-structure drift. Use 'Explain' for other failures.")
    integ = db.get(Integration, ex.integration_id)
    base = db.get(IntegrationVersion, ex.version_id)
    tool = db.get(Tool, ex.tool_id) if ex.tool_id else None
    if not integ or not base or not tool:
        raise HTTPException(404, "The failing integration version no longer exists")
    existing = open_repair(db, integ.id)
    if existing:
        return existing
    missing = ex.diagnostics.get("missing_fields") or []
    shape = ex.diagnostics.get("response_shape") or {}
    rp = RepairProposal(organization_id=org_id, integration_id=integ.id, base_version_id=base.id, error_class=ex.error_class,
                        diagnosis={"execution_id": ex.id, "operation": tool.operation_id, "missing_fields": missing})
    db.add(rp)
    db.flush()
    try:
        proposal, model = agents.propose_repair(Gateway(db, org_id), base.config, tool.operation_id, ex.error_class, missing, shape)
    except ProviderError as e:
        rp.status, rp.diagnosis = "failed", {**rp.diagnosis, "summary": f"No AI model could analyse the failure: {e}"}
        return rp
    rp.diagnosis = {**rp.diagnosis, "summary": proposal["diagnosis"], "mapping_changes": proposal["mapping_changes"],
                    "confidence": proposal["confidence"], "model": model}
    op_cfg = next(o for o in base.config["integration"]["operations"] if o["id"] == tool.operation_id)
    valid_paths = set(shape)
    changes = {k: v for k, v in (proposal["mapping_changes"] or {}).items()
               if k in (op_cfg["response"].get("mapping") or {}) and _path_exists(v, valid_paths, op_cfg["response"].get("items_path"))}
    if not changes:
        rp.status = "failed"
        audit.alert(db, org_id, "repair", f"{integ.name}: drift detected but no safe mapping change could be found. Manual review needed.",
                    "high", integ.id, "#/monitoring")
        return rp

    config = copy.deepcopy(base.config)
    op = next(o for o in config["integration"]["operations"] if o["id"] == tool.operation_id)
    op["response"]["mapping"].update(changes)
    v = integrations.revise(db, integ, base, REPAIR_ACTOR, config, source="repair", notes=proposal["diagnosis"][:1000])
    rp.proposed_version_id = v.id
    if not v.validation.get("valid") or v.security_report.get("blocking"):
        rp.status = "failed"
        return rp
    cases = agents.generate_tests(v.config, [])
    cases.append({"operation": tool.operation_id, "name": f"{tool.operation_id}: regression against the observed (drifted) response",
                  "inputs": next(c["inputs"] for c in cases if c["operation"] == tool.operation_id),
                  "mock_status": (op["response"].get("status_codes") or [200])[0],
                  "mock_response": agents.sample_from_shape(shape), "expect": "succeeded"})
    tr = integrations.run_tests(db, v, REPAIR_ACTOR, cases=cases)
    if tr.result != "passed":
        rp.status = "failed"
        rp.diagnosis = {**rp.diagnosis, "summary": rp.diagnosis["summary"] + " Regression tests failed; the patch was not submitted."}
        return rp
    req = integrations.submit_for_review(db, v, integ, REPAIR_ACTOR, kind="repair", requester_id=None)
    rp.approval_request_id = req.id
    rp.status = "pending_approval"
    audit.record(db, org_id, REPAIR_ACTOR, "repair.propose", f"{integ.slug}@{v.version}", base_version=base.version, changes=changes)
    audit.alert(db, org_id, "repair", f"{integ.name}: a repair ({base.version} → {v.version}) is ready for admin review.",
                "medium", integ.id, f"#/approvals/{req.id}")
    return rp


def _path_exists(path: str, shape_paths: set[str], items_path: str | None) -> bool:
    if items_path:
        root = "$[0]" if items_path == "$" else items_path + "[0]"
        path = root + path[1:]
    return path in shape_paths


def start_async(org_id: str, execution_id: str) -> None:
    def work():
        try:
            with session_scope() as db:
                create_repair(db, org_id, execution_id)
        except Exception:  # background job: log, never crash the server
            log.exception("automatic repair failed")
    threading.Thread(target=work, name="repair", daemon=True).start()
