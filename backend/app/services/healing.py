"""Self-healing: trace failures of integrations, data sources and workflows, find the root cause, try to fix it, and
report to the administrators by email when it cannot.

Enabled per organisation (policy ``self_healing``). Each failure opens - or adds to - one case per integration (or
workflow) and error class. The investigation:

1. collects evidence: recent failures, last success, the endpoint, the credential's state, the workflow step;
2. classifies the root cause from the error class and that evidence;
3. tries what is safe to try automatically: re-checking the service after a pause, re-testing a credential, or preparing
   a corrected configuration version (submitted for approval - production is never changed directly);
4. asks the AI gateway to explain the evidence in plain language;
5. if the problem is not resolved, raises a ``self_healing`` alert, which is emailed with the root cause, the endpoint,
   what was tried, what is still failing and what is required from an administrator.
"""
from __future__ import annotations

import copy
import difflib
import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from ..core.db import session_scope, utcnow
from ..models import (AISession, Execution, HealingCase, IngestSource, Integration, IntegrationVersion, Workflow, WorkflowRun,
                      WorkflowStep, WorkflowVersion)
from . import audit, catalog, engine, ingest, integrations, runtime
from .ai_gateway import Gateway, ProviderError

log = logging.getLogger("integra.healing")
OPEN = ("investigating", "awaiting_approval", "needs_admin")
REOPEN_AFTER = timedelta(hours=6)  # a recurring failure is added to the open case instead of starting a new one
TRANSIENT = {"upstream_error", "timeout", "network_error"}
CREDENTIAL = {"auth_invalid", "forbidden", "credential_missing"}
ENDPOINT = {"not_found", "request_rejected", "unexpected_status"}
_pause = time.sleep  # replaced in tests
PAUSE_SECONDS = 20


def enabled(db: Session, org_id: str) -> bool:
    return bool(integrations.org_policy(db, org_id).get("self_healing"))


def auto_enabled(db: Session, org_id: str) -> bool:
    """Automatic mode: non-critical fixes are published without waiting for an administrator."""
    p = integrations.org_policy(db, org_id)
    return bool(p.get("self_healing") and p.get("self_healing_auto"))


class _AutoReviewer:
    """The actor recorded when self-healing publishes a non-critical fix on its own."""
    id = None
    username = "self-healing (automatic)"


AUTO = _AutoReviewer()


def critical_reasons(old: dict, new: dict, version: IntegrationVersion) -> list[str]:
    """Why a prepared fix must wait for an administrator. Empty = safe to apply automatically."""
    from urllib.parse import urlsplit
    reasons: list[str] = []
    report = version.security_report or {}
    if report.get("blocking") or report.get("level") in ("high", "critical"):
        reasons.append(f"the security review rates it {report.get('level', 'blocking')}")
    o, n = old["integration"], new["integration"]
    if urlsplit(o.get("base_url", "")).netloc != urlsplit(n.get("base_url", "")).netloc:
        reasons.append("it changes the destination host")
    if o.get("authentication") != n.get("authentication"):
        reasons.append("it changes how the integration authenticates")
    if (o.get("default_headers") or {}) != (n.get("default_headers") or {}):
        reasons.append("it changes the headers sent with every request")
    old_ops = {op["id"]: op for op in o.get("operations") or []}
    new_ops = {op["id"]: op for op in n.get("operations") or []}
    if set(old_ops) != set(new_ops):
        reasons.append("it adds or removes operations")
    for op_id in set(old_ops) & set(new_ops):
        a, b = old_ops[op_id], new_ops[op_id]
        if a == b:
            continue
        if a.get("method") != b.get("method"):
            reasons.append(f"it changes the HTTP method of {op_id}")
        if b.get("method") == "DELETE" or b.get("safety") == "destructive" or a.get("safety") == "destructive":
            reasons.append(f"it changes {op_id}, which can delete data")
        if (a.get("input_schema") or {}) != (b.get("input_schema") or {}):
            reasons.append(f"it changes the inputs of {op_id}, which callers depend on")
    return reasons


def _auto_apply(db: Session, case: HealingCase, integ: Integration, live: IntegrationVersion) -> None:
    """Automatic mode: publish a prepared, tested, non-critical fix, verify it, and roll back if the service is worse."""
    fix = db.get(IntegrationVersion, case.fix_version_id) if case.fix_version_id else None
    if not fix or not case.approval_request_id:
        return
    reasons = critical_reasons(live.config, fix.config, fix)
    if (case.evidence or {}).get("unverified_fix"):
        reasons.append("the corrected endpoint could not be confirmed to exist")
    if reasons:
        log_event(case, "auto_skip", "Automatic mode is on, but this fix is critical, so it waits for an administrator: " + "; ".join(reasons))
        case.required = [f"Approve or reject version {fix.version} in Approvals (critical: {'; '.join(reasons)})."]
        return
    from fastapi import HTTPException
    try:
        integrations.approve(db, case.organization_id, case.approval_request_id, AUTO,
                             comment="Applied automatically by self-healing (non-critical change; it passed validation, the security policy and the safe tests)")
    except HTTPException as e:
        log_event(case, "auto_skip", f"Could not publish the fix automatically: {e.detail}", False)
        return
    log_event(case, "auto_applied", f"Published version {fix.version} automatically (non-critical: same host, authentication, methods and inputs)", True)
    ok, detail = _probe(db, integ, fix)
    fixed_op = next((o for o in fix.config["integration"]["operations"] if o["id"] == (case.evidence or {}).get("fixed_operation")), None)
    if ok is None and fixed_op:
        ok, detail = endpoint_exists(db, integ, fix.config, fixed_op)
    if ok is False:
        try:
            integrations.rollback(db, case.organization_id, integ, live.id, AUTO)
            log_event(case, "rolled_back", f"The check after publishing failed ({detail[:300]}), so version {live.version} was restored", False)
        except HTTPException as e:
            log_event(case, "rolled_back", f"The check after publishing failed ({detail[:300]}) and the previous version could not be restored: {e.detail}", False)
        case.status = "needs_admin"
        case.required = [f"The automatic fix (version {fix.version}) did not work and was rolled back. Investigate the integration by hand."]
        return
    log_event(case, "verified", f"Checked the service after publishing: {detail}" if ok else "There is no safe read-only call to verify with; the safe tests passed", ok)
    case.status, case.resolved_at, case.required, case.still_failing = "resolved", utcnow(), [], ""
    case.root_cause = case.root_cause + f" Fixed automatically with version {fix.version}."
    audit.alert(db, case.organization_id, "self_healed", f"{case.target}\nRoot cause: {case.root_cause}\nWhat was done:\n"
                + "\n".join(f"- {a['action']}" for a in case.attempts or []) + f"\n- Published version {fix.version} automatically and re-checked the service",
                "info", case.integration_id, f"#/healing/{case.id}")


# ----------------------------------------------------------------------------------------------- triggers

def _open_case(db: Session, org_id: str, error_class: str, integration_id: str | None = None, workflow_id: str | None = None) -> HealingCase | None:
    q = select(HealingCase).where(HealingCase.organization_id == org_id, HealingCase.error_class == error_class,
                                  HealingCase.status.in_(OPEN), HealingCase.updated_at >= utcnow() - REOPEN_AFTER)
    q = q.where(HealingCase.integration_id == integration_id) if integration_id else q.where(HealingCase.workflow_id == workflow_id)
    return db.scalars(q).first()


def _queue(db: Session, case: HealingCase) -> None:
    db.flush()
    db.info.setdefault("healing_cases", []).append(case.id)


def on_failure(db: Session, ex: Execution, integ: Integration, force: bool = False) -> HealingCase | None:
    """Called for every failed execution. Opens a case (investigated after the transaction commits)."""
    if ex.status == "succeeded" or not (force or enabled(db, ex.organization_id)):
        return None
    if ex.error_class == "input_invalid" and ex.actor_type == "user" and not force:
        return None  # someone typing wrong inputs in the Test Lab is not a system fault
    case = _open_case(db, ex.organization_id, ex.error_class, integration_id=integ.id)
    if case and not force:
        case.occurrences += 1
        case.evidence = {**(case.evidence or {}), "last_execution_id": ex.id, "last_error": ex.error[:500]}
        log_event(case, "recurred", f"The same failure happened again (occurrence {case.occurrences}): {ex.error[:300]}")
        # Look again when something changed (a new version went live) or the last look is stale; otherwise just count it.
        changed = (case.evidence or {}).get("version") != (db.get(IntegrationVersion, integ.published_version_id).version if integ.published_version_id else None)
        if case.status == "needs_admin" and (changed or case.updated_at < utcnow() - timedelta(minutes=10)):
            case.execution_id, case.endpoint = ex.id, _endpoint(ex)
            _queue(db, case)
        return case
    case = HealingCase(organization_id=ex.organization_id, integration_id=integ.id, execution_id=ex.id, target=ex.tool_name or integ.name,
                       error_class=ex.error_class or "unknown", endpoint=_endpoint(ex), log=[], runs=0)
    log_event(case, "opened", f"{ex.tool_name or integ.name} failed with {ex.error_class or 'an error'}"
              + (f" (HTTP {ex.http_status})" if ex.http_status else "") + f": {ex.error[:300]}" + (" - started by hand" if force else ""), False)
    db.add(case)
    _queue(db, case)
    return case


def on_workflow_failure(db: Session, wf: Workflow, run: WorkflowRun) -> HealingCase | None:
    """Workflow failures that are not an integration's fault (those open their own case through the execution)."""
    if run.status != "failed" or run.dry_run or not enabled(db, wf.organization_id):
        return None
    steps = db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id, WorkflowStep.status == "failed")).all()
    if not steps or all(s.execution_id for s in steps):
        return None
    case = _open_case(db, wf.organization_id, "workflow_step", workflow_id=wf.id)
    if case:
        case.occurrences += 1
        log_event(case, "recurred", f"The workflow failed again (occurrence {case.occurrences}): {run.error[:300]}")
        return case
    case = HealingCase(organization_id=wf.organization_id, workflow_id=wf.id, target=f"workflow: {wf.name}", error_class="workflow_step",
                       evidence={"run_id": run.id}, log=[], runs=0)
    log_event(case, "opened", f"Workflow '{wf.name}' failed: {run.error[:300]}", False)
    db.add(case)
    _queue(db, case)
    return case


@event.listens_for(Session, "after_commit")
def _start_after_commit(session: Session) -> None:
    for case_id in session.info.pop("healing_cases", []):
        threading.Thread(target=investigate_by_id, args=(case_id,), name="self-healing", daemon=True).start()


@event.listens_for(Session, "after_rollback")
def _drop_after_rollback(session: Session) -> None:
    session.info.pop("healing_cases", None)


def investigate_by_id(case_id: str) -> None:
    try:
        with session_scope() as db:
            investigate(db, db.get(HealingCase, case_id))
    except Exception:  # background job: record, never crash the server
        log.exception("self-healing investigation failed")
        with session_scope() as db:
            case = db.get(HealingCase, case_id)
            if case:
                case.status = "needs_admin"
                case.root_cause = case.root_cause or "The automatic investigation itself failed (see the server log)."
                log_event(case, "error", "The automatic investigation itself failed (see the server log)", False)
                _report(db, case)


# ----------------------------------------------------------------------------------------------- investigation

def _endpoint(ex: Execution) -> dict:
    rp = ex.request_preview or {}
    return {"method": rp.get("method", ""), "url": rp.get("url", ""), "operation": ex.tool_name, "http_status": ex.http_status}


MAX_LOG = 300
STATUS_TEXT = {"resolved": "Fixed", "awaiting_approval": "A fix is waiting for an administrator's approval",
               "needs_admin": "Could not fix it automatically; an administrator is needed", "investigating": "Investigating"}


def log_event(case: HealingCase, stage: str, text: str, ok: bool | None = None) -> None:
    """Append to the case's timeline. Kept across re-investigations so the full history of a case stays visible."""
    entry = {"at": utcnow().isoformat() + "Z", "run": case.runs or 0, "stage": stage, "text": str(text)[:1000], "ok": ok}
    case.log = [*(case.log or []), entry][-MAX_LOG:]


def _try(case: HealingCase, action: str, result: str, ok: bool) -> None:
    case.attempts = [*(case.attempts or []), {"action": action, "result": result[:600], "ok": ok, "at": utcnow().isoformat() + "Z"}]
    log_event(case, "attempt", f"{action} - {result[:600]}", ok)


def _probe(db: Session, integ: Integration, version: IntegrationVersion) -> tuple[bool | None, str]:
    """A safe, read-only check of the live integration: a data source preview, or a GET operation without inputs.
    Returns (ok, detail); ok is None when nothing can be called safely."""
    src = db.scalars(select(IngestSource).where(IngestSource.integration_id == integ.id, IngestSource.enabled.is_(True))).first()
    if src:
        try:
            r = ingest.test_source(db, src)
        except ValueError as e:
            return False, str(e)
        return (True, f"the data source answered (HTTP {r.get('http_status')})") if r.get("ok") else \
            (False, f"{r.get('error_class')}: {r.get('error', '')[:300]}")
    config = copy.deepcopy(version.config)
    op = next((o for o in config["integration"]["operations"] if o["method"] == "GET" and not o["input_schema"].get("required")
               and "{" not in o["path"]), None)
    if not op:
        return None, "no read-only operation without inputs exists to check it safely"
    op.pop("pagination", None)
    cred = runtime.credential_for(db, integ.organization_id, config)
    if (config["integration"].get("authentication") or {}).get("type", "none") != "none" and cred is None:
        return False, "credential_missing: no active, unexpired credential is connected"
    r = engine.execute(config, op["id"], {}, credential=cred, allowed_domains=integrations.org_policy(db, integ.organization_id).get("allowed_domains") or [])
    return (True, f"{op['id']} answered HTTP {r.http_status}") if r.status == "succeeded" or r.error_class == "response_drift" else \
        (False, f"{r.error_class}: {r.error[:300]}")


def _propose(db: Session, case: HealingCase, integ: Integration, live: IntegrationVersion, config: dict, notes: str) -> bool:
    """Create a corrected version, test it and submit it for approval. True when it is waiting for an administrator."""
    v = integrations.revise(db, integ, live, "self-healing", config, source="repair", notes=notes[:1000])
    if not v.validation.get("valid") or v.security_report.get("blocking"):
        _try(case, f"Prepared version {v.version} with the fix", "It did not pass validation or the security policy, so it was not submitted.", False)
        return False
    tr = integrations.run_tests(db, v, "self-healing")
    if tr.result != "passed":
        _try(case, f"Prepared version {v.version} with the fix", f"Its safe tests failed ({tr.report['passed']}/{tr.report['total']}), so it was not submitted.", False)
        return False
    req = integrations.submit_for_review(db, v, integ, "self-healing", kind="repair", requester_id=None)
    case.fix_version_id, case.approval_request_id = v.id, req.id
    _try(case, f"Prepared, tested and submitted version {v.version} with the fix", "Waiting for an administrator to approve it.", True)
    return True


def _documented_paths(db: Session, integ: Integration, method: str) -> list[str]:
    """Paths the API documents for this method: from the OpenAPI document the integration was built from, or its template."""
    paths: list[str] = []
    if integ.session_id:
        s = db.get(AISession, integ.session_id)
        for d in (s.documents if s else None) or []:
            paths += [o["path"] for o in d["catalog"]["operations"] if o["method"] == method]
    tpl = next((d for d in catalog._raw_templates().values() if d["integration"]["id"] == integ.slug), None)
    if tpl:
        paths += [o["path"] for o in tpl["integration"]["operations"] if o["method"] == method and "{{" not in o["path"]]
    return list(dict.fromkeys(paths))


def endpoint_exists(db: Session, integ: Integration, config: dict, op: dict) -> tuple[bool | None, str]:
    """Does this operation's URL exist? A harmless GET (no body) - a 404/410 means no, anything else (even 401/405) means yes.
    None when the path has parameters that cannot be filled in without real inputs."""
    import httpx

    from . import egress
    integ_cfg = config["integration"]
    if "{" in op["path"]:
        return None, "the path has parameters, so it cannot be checked without real inputs"
    url = integ_cfg["base_url"].rstrip("/") + op["path"]
    try:
        egress.check_url(url, integrations.org_policy(db, integ.organization_id).get("allowed_domains") or [])
    except egress.EgressDenied as e:
        return False, f"{url} is not allowed: {e}"
    req = engine.PreparedRequest("GET", url, {}, {k: str(v) for k, v in (integ_cfg.get("default_headers") or {}).items()})
    auth = integ_cfg.get("authentication") or {}
    if auth.get("type") in ("bearer", "api_key_header", "api_key_query", "basic", "ssws"):
        try:
            engine.apply_auth(req, auth, runtime.credential_for(db, integ.organization_id, config))
        except engine.EngineError:
            pass  # without the credential a real endpoint still answers 401/403, not 404
    try:
        r = httpx.get(url, params=req.query, headers=req.headers, timeout=10, follow_redirects=False)
    except httpx.HTTPError as e:
        return False, f"{url} could not be reached ({type(e).__name__})"
    if r.status_code in (404, 410):
        return False, f"{url} answers HTTP {r.status_code}"
    return True, f"{url} exists (HTTP {r.status_code} to a harmless GET)"


def _reference_configs(db: Session, integ: Integration) -> list[tuple[str, dict]]:
    """Known-good configurations for this service: the bundled catalogue entry or source template with the same id."""
    out = []
    ref = catalog.known_services().get(integ.slug)
    if ref:
        out.append(("the built-in reference configuration", ref))
    tpl = next((d for d in catalog._raw_templates().values() if d["integration"]["id"] == integ.slug), None)
    if tpl and "{{" not in tpl["integration"]["base_url"]:
        out.append(("its source template", {k: v for k, v in tpl.items() if k != "template"}))
    return out


def _ai_endpoint_guess(db: Session, case: HealingCase, integ: Integration, config: dict, op: dict) -> dict | None:
    schema = {"type": "object", "required": ["base_url", "path", "reason"], "properties": {
        "base_url": {"type": "string", "pattern": "^https://[^\\s]+$"}, "path": {"type": "string", "pattern": "^/[^\\s]*$"},
        "reason": {"type": "string", "maxLength": 400}}}
    prompt = (f"Service: {integ.name} ({integ.description[:300]})\nConfigured base URL: {config['integration']['base_url']}\n"
              f"Operation {op['id']}: {op['method']} {op['path']} - {op.get('description', '')}\nFailing request: {case.endpoint.get('method', '')} "
              f"{case.endpoint.get('url', '')} -> {case.error_class} (HTTP {case.endpoint.get('http_status') or '?'})\n"
              f"Response excerpt (untrusted): {(case.evidence or {}).get('error', '')[:300]}")
    system = ("You correct API endpoint URLs. Given a request that returned 404, give the most likely correct base URL and path for this "
              "operation of this public API, keeping the same service and host unless the host itself is clearly wrong. "
              "Only answer with an endpoint you know this API documents.")
    try:
        res = Gateway(db, case.organization_id).structured("repair", system, prompt, schema, local=lambda: None, allow_local=False)
    except ProviderError:
        return None
    return res.data if isinstance(res.data, dict) else None


def _fix_endpoint(db: Session, case: HealingCase, integ: Integration, live: IntegrationVersion, op_id: str) -> bool:
    """Find a corrected base URL / path, prove it exists with a harmless request, then prepare the fix.
    Candidates come from the API documentation, the built-in reference configuration, and finally the AI."""
    config = copy.deepcopy(live.config)
    op = next((o for o in config["integration"]["operations"] if o["id"] == op_id), None)
    if not op:
        return False
    current = (config["integration"]["base_url"].rstrip("/"), op["path"])
    candidates: list[tuple[str, str, str]] = []  # (base_url, path, where it came from)
    documented = _documented_paths(db, integ, op["method"])
    if documented:
        close = difflib.get_close_matches(op["path"], documented, n=1, cutoff=0.75)
        if close and close[0] != op["path"]:
            candidates.append((current[0], close[0], "the API documentation"))
    for label, ref in _reference_configs(db, integ):
        ref_op = next((o for o in ref["integration"]["operations"] if o["id"] == op_id), None)
        if ref_op:
            candidates.append((ref["integration"]["base_url"].rstrip("/"), ref_op["path"], label))
    guess = _ai_endpoint_guess(db, case, integ, config, op)
    if guess:
        candidates.append((guess["base_url"].rstrip("/"), guess["path"], f"the AI ({guess.get('reason', '')[:200]})"))
    seen = {current}
    tried = 0
    for base, path, source in candidates:
        if (base, path) in seen:
            continue
        seen.add((base, path))
        trial = copy.deepcopy(config)
        trial["integration"]["base_url"] = base
        trial_op = next(o for o in trial["integration"]["operations"] if o["id"] == op_id)
        trial_op["path"] = path
        ok, detail = endpoint_exists(db, integ, trial, trial_op)
        tried += 1
        _try(case, f"Checked {op['method']} {base}{path} suggested by {source}", detail, bool(ok))
        if ok:
            case.evidence = {**(case.evidence or {}), "fixed_operation": op_id, "unverified_fix": False}
            what = (f"base URL {current[0]} -> {base}" if base != current[0] else "") + (" and " if base != current[0] and path != op["path"] else "") + \
                   (f"path {op['path']} -> {path}" if path != op["path"] else "")
            return _propose(db, case, integ, live, trial, f"Self-healing: {op_id} called {case.endpoint.get('url', '')} which returned {case.error_class}; "
                                                          f"corrected the {what} (from {source.split(' (')[0]}), verified the endpoint exists.")
    documented_first = next(((b, p, src) for b, p, src in candidates if src == "the API documentation"), None)
    if documented_first:
        # The documentation is authoritative even when the endpoint cannot be confirmed from here (e.g. it rejects GET):
        # prepare it for an administrator, but never publish it automatically.
        base, path, _ = documented_first
        trial = copy.deepcopy(config)
        trial["integration"]["base_url"] = base
        next(o for o in trial["integration"]["operations"] if o["id"] == op_id)["path"] = path
        case.evidence = {**(case.evidence or {}), "fixed_operation": op_id, "unverified_fix": True}
        return _propose(db, case, integ, live, trial, f"Self-healing: {op_id} called {case.endpoint.get('url', '')} which returned {case.error_class}; "
                                                      f"the documented path is {path} (it could not be confirmed with a harmless request).")
    if not tried:
        _try(case, "Looked for a corrected endpoint (API documentation, built-in reference, AI)",
             "No different endpoint was found to try." + ("" if guess else " No AI provider was available to suggest one."), False)
    return False


def _relax_rate_limit(db: Session, case: HealingCase, integ: Integration, live: IntegrationVersion, op_id: str) -> bool:
    config = copy.deepcopy(live.config)
    rpm = int((config["integration"].get("rate_limit") or {}).get("requests_per_minute") or 60)
    config["integration"]["rate_limit"] = {"requests_per_minute": max(1, rpm // 2)}
    for op in config["integration"]["operations"]:
        if op["id"] == op_id:
            op["retry"] = {**(op.get("retry") or {}), "max_attempts": 4, "backoff_seconds": max(2, (op.get("retry") or {}).get("backoff_seconds", 0.5))}
    return _propose(db, case, integ, live, config, f"Self-healing: {op_id} is being rate-limited; send at most {max(1, rpm // 2)} requests per minute "
                                                   "and retry with a longer backoff.")


def _credential_requirements(integ: Integration, live: IntegrationVersion, error_class: str) -> list[str]:
    auth = live.config["integration"].get("authentication") or {}
    ref, kind = auth.get("credential_ref"), auth.get("type")
    if error_class == "forbidden":
        scopes = ", ".join(auth.get("scopes") or []) or "the permissions this operation needs"
        return [f"Give the credential '{ref}' access to {scopes} (the API answered 403 Forbidden), or connect a credential that has it.",
                catalog.credential_help(integ.slug, kind)]
    return [f"Rotate or reconnect the credential '{ref}' ({kind}) in Credentials, then press Test. The current one is invalid, expired or missing.",
            catalog.credential_help(integ.slug, kind)]


def investigate(db: Session, case: HealingCase) -> None:
    case.status, case.attempts, case.required = "investigating", [], []
    case.runs = (case.runs or 0) + 1
    log_event(case, "started", f"Investigation #{case.runs} started")
    db.commit()
    if case.workflow_id and not case.integration_id:
        _investigate_workflow(db, case)
        return _finish(db, case)
    integ = db.get(Integration, case.integration_id)
    live = db.get(IntegrationVersion, integ.published_version_id) if integ and integ.published_version_id else None
    ex = db.get(Execution, case.execution_id) if case.execution_id else None
    if not integ or not live:
        case.root_cause = "The integration is no longer published."
        case.required = ["Publish a version of the integration again, or remove the workflows and sources that use it."]
        case.status = "needs_admin"
        return _finish(db, case)
    op_id = (ex.tool_name.split(".", 1)[1] if ex and "." in ex.tool_name else "")
    since = utcnow() - timedelta(hours=24)
    case.evidence = {**(case.evidence or {}),
                     "failures_24h": db.scalar(select(func.count()).select_from(Execution).where(
                         Execution.integration_id == integ.id, Execution.status == "failed", Execution.created_at >= since)) or 0,
                     "last_success": str(db.scalar(select(func.max(Execution.created_at)).where(
                         Execution.integration_id == integ.id, Execution.status == "succeeded")) or "never"),
                     "error": (ex.error if ex else "")[:500], "http_status": ex.http_status if ex else None,
                     "version": live.version, "base_url": live.config["integration"]["base_url"]}
    if ex:
        step = db.scalars(select(WorkflowStep).where(WorkflowStep.execution_id == ex.id)).first()
        if step:
            run = db.get(WorkflowRun, step.run_id)
            wf = db.get(Workflow, run.workflow_id) if run else None
            case.evidence["workflow"] = f"{wf.name if wf else '?'} (step {step.node_id})"
            case.workflow_id = wf.id if wf else None
    cls = case.error_class
    meaning, fix = engine.ERROR_GUIDE.get(cls, ("The operation failed.", ""))
    case.root_cause = meaning
    log_event(case, "evidence", f"Collected evidence: {case.evidence['failures_24h']} failure(s) in 24 h, last success {case.evidence['last_success']}, "
              f"version {live.version}" + (f", in workflow {case.evidence['workflow']}" if case.evidence.get("workflow") else ""))

    if cls in TRANSIENT:
        _pause(PAUSE_SECONDS)
        ok, detail = _probe(db, integ, live)
        _try(case, f"Waited {PAUSE_SECONDS} s and checked the service again", detail if ok is not None else "Could not re-check: " + detail, bool(ok))
        if ok:
            case.root_cause = f"A temporary failure at {case.evidence['base_url']} ({meaning.rstrip('.')}). The service answers normally again."
            case.status = "resolved"
        else:
            case.root_cause = f"{meaning} The service at {case.evidence['base_url']} is still failing."
            case.required = ["Check the service's status page or contact its provider; the outage is on their side.",
                             "If the service moved, update the base URL with a new revision of the integration."]
    elif cls == "rate_limited":
        ok, detail = _probe(db, integ, live)
        _try(case, "Checked whether the rate limit has cleared", detail if ok is not None else "Could not re-check: " + detail, bool(ok))
        case.root_cause = "The API is rejecting requests because too many were sent (HTTP 429)."
        if _relax_rate_limit(db, case, integ, live, op_id):
            case.status = "awaiting_approval"
            case.required = ["Approve the prepared version in Approvals so requests are slowed down.",
                             "If it keeps happening, raise the API quota with the provider."]
        else:
            case.required = ["Lower the request rate or raise the API quota with the provider."]
    elif cls == "response_drift":
        case.root_cause = "The API changed the structure of its responses; fields the integration maps are missing."
        if ex and ex.tool_id:
            from . import repairs
            try:
                rp = repairs.create_repair(db, case.organization_id, ex.id)
            except Exception as e:  # noqa: BLE001 - recorded as an attempt
                rp, err = None, str(getattr(e, "detail", e))
            if rp and rp.status == "pending_approval":
                case.fix_version_id, case.approval_request_id = rp.proposed_version_id, rp.approval_request_id
                _try(case, "Compared the expected fields with the response the API now sends and remapped them",
                     (rp.diagnosis or {}).get("summary", "")[:400] + " Submitted for approval.", True)
                case.status = "awaiting_approval"
                case.required = ["Approve the repaired version in Approvals."]
            else:
                _try(case, "Tried to remap the missing fields to the new response structure",
                     (rp.diagnosis or {}).get("summary", "") if rp else err, False)
        if case.status != "awaiting_approval":
            case.required = ["Check what the API now returns (Monitoring > the failed execution > observed response structure) and update the "
                             "output mapping in a new revision."]
    elif cls in ENDPOINT:
        case.root_cause = f"{meaning} Request: {case.endpoint.get('method', '')} {case.endpoint.get('url', '')}."
        if op_id and _fix_endpoint(db, case, integ, live, op_id):
            case.status = "awaiting_approval"
            case.required = ["Approve the corrected version in Approvals."]
        else:
            case.required = ["Check the API documentation for the current endpoint and parameters of this operation, then create a revision "
                             "(or upload the latest OpenAPI document in AI Studio so it can be compared automatically)."]
    elif cls in CREDENTIAL:
        ok, detail = _probe(db, integ, live)
        _try(case, "Re-tested the connection with the stored credential", detail if ok is not None else "Could not re-check: " + detail, bool(ok))
        if ok:
            case.root_cause = "The credential works again (it was probably rotated or the provider recovered)."
            case.status = "resolved"
        else:
            case.root_cause = f"{meaning} Secrets can only be changed by a person, never by the AI."
            case.required = _credential_requirements(integ, live, cls)
    elif cls == "egress_denied":
        case.root_cause = f"The destination {case.evidence['base_url']} is blocked by the organisation's network policy."
        case.required = [f"If {case.evidence['base_url']} is legitimate, add its domain to Allowed API destinations in Settings; otherwise correct the base URL."]
    elif cls == "input_invalid":
        case.root_cause = f"The operation was called with inputs that do not match its schema: {(ex.error if ex else '')[:300]}"
        where = case.evidence.get("workflow")
        case.required = [f"Fix the inputs of {where} in Workflows." if where else "Fix the inputs passed to this operation."]
    else:
        case.required = [fix or "Review the failed execution in Monitoring."]

    if case.status == "awaiting_approval" and auto_enabled(db, case.organization_id):
        _auto_apply(db, case, integ, live)
    if case.status == "investigating":
        case.status = "needs_admin"
    if case.status != "resolved":
        ok, detail = _probe(db, integ, live)
        case.still_failing = (case.evidence.get("error") or "") if ok is not True else ""
        if ok is True and case.status == "needs_admin" and cls not in ENDPOINT | {"input_invalid", "response_drift"}:
            case.status, case.root_cause = "resolved", case.root_cause + " A final check succeeded."
    _finish(db, case)


def _investigate_workflow(db: Session, case: HealingCase) -> None:
    wf = db.get(Workflow, case.workflow_id)
    run = db.get(WorkflowRun, (case.evidence or {}).get("run_id")) if (case.evidence or {}).get("run_id") else None
    steps = db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id, WorkflowStep.status == "failed")).all() if run else []
    problems = [f"step {s.node_id}: {s.error[:200]}" for s in steps if not s.execution_id]
    case.root_cause = "Workflow steps failed before calling any API: " + "; ".join(problems) if problems else (run.error if run else "Unknown")
    from .workflows import owner_actor
    try:
        available = {t.name for t in runtime.visible_tools(db, owner_actor(db, wf))} if wf else set()
    except Exception:  # noqa: BLE001 - the owner left the organisation
        available = set()
    v = db.get(WorkflowVersion, wf.current_version_id) if wf else None
    missing = [n["tool"] for n in ((v.graph if v else {}) or {}).get("nodes", []) if n["tool"] not in available]
    _try(case, "Checked that every tool the workflow uses is published and enabled",
         ("Not available: " + ", ".join(missing)) if missing else "All tools are available.", not missing)
    case.required = ([f"Publish or re-enable {t} (or change the step to another tool)." for t in missing]
                     or [f"Open the workflow '{wf.name if wf else ''}' and fix the failing steps' inputs or conditions."])
    case.still_failing = "; ".join(problems)[:1000]
    case.status = "needs_admin"


def _finish(db: Session, case: HealingCase) -> None:
    log_event(case, "diagnosis", f"Root cause: {case.root_cause}")
    case.analysis, case.model = _explain(db, case)
    if case.analysis and case.analysis != case.root_cause:
        log_event(case, "analysis", f"AI analysis ({case.model or 'model'}): {case.analysis}")
    if case.status == "resolved":
        case.resolved_at = utcnow()
    log_event(case, "outcome", STATUS_TEXT.get(case.status, case.status) + (f". Required: {'; '.join(case.required)}" if case.required and case.status != "resolved" else ""),
              case.status == "resolved" if case.status != "awaiting_approval" else None)
    audit.record(db, case.organization_id, "self-healing", "healing.case", case.target, status=case.status, error_class=case.error_class,
                 attempts=len(case.attempts or []))
    if case.status in ("needs_admin", "awaiting_approval"):
        _report(db, case)
    db.commit()


def _explain(db: Session, case: HealingCase) -> tuple[str, str]:
    evidence = "\n".join([f"Target: {case.target}", f"Error class: {case.error_class}",
                          f"Endpoint: {case.endpoint.get('method', '')} {case.endpoint.get('url', '')}",
                          f"Evidence: {case.evidence}", f"Root cause (rules): {case.root_cause}",
                          "Attempts: " + "; ".join(f"{a['action']} -> {a['result']}" for a in case.attempts or [])])
    system = ("You are the H.E.D.E.S self-healing analyst. In at most 4 sentences, explain the most likely root cause of this failure "
              "and why the attempted fixes did or did not work. Be specific; do not invent facts beyond the evidence.")
    try:
        res = Gateway(db, case.organization_id).text("error_explanation", system, [{"role": "user", "content": evidence}],
                                                     local=lambda: case.root_cause)
        return res.text.strip()[:2000], res.model
    except ProviderError:
        return case.root_cause, ""


def report_text(case: HealingCase) -> str:
    lines = [f"Self-healing {'prepared a fix that needs approval' if case.status == 'awaiting_approval' else 'could not fix this on its own'}: {case.target}"]
    if case.endpoint.get("url"):
        lines.append(f"Endpoint: {case.endpoint.get('method', '')} {case.endpoint['url']}" +
                     (f" (HTTP {case.endpoint['http_status']})" if case.endpoint.get("http_status") else ""))
    lines.append(f"Root cause: {case.root_cause}")
    if case.analysis and case.analysis != case.root_cause:
        lines.append(f"Analysis: {case.analysis}")
    if case.attempts:
        lines.append("What I tried:")
        lines += [f"- {a['action']}: {a['result']}" for a in case.attempts]
    if case.still_failing:
        lines.append(f"Still failing: {case.still_failing}")
    if case.required:
        lines.append("Required from an administrator:")
        lines += [f"- {r}" for r in case.required]
    if case.occurrences > 1:
        lines.append(f"Seen {case.occurrences} times.")
    return "\n".join(lines)


def _report(db: Session, case: HealingCase) -> None:
    audit.alert(db, case.organization_id, "self_healing", report_text(case), "high", case.integration_id, f"#/healing/{case.id}")
    case.notified_at = utcnow()
    log_event(case, "notified", "Administrators were alerted (and emailed when alert delivery is configured)")


def on_fix_decision(db: Session, approval_request_id: str, decision: str, reviewer_label: str) -> None:
    """Close the loop when an administrator decides on a fix that self-healing prepared."""
    for case in db.scalars(select(HealingCase).where(HealingCase.approval_request_id == approval_request_id)).all():
        if decision == "approved":
            case.status, case.resolved_at = "resolved", utcnow()
            log_event(case, "fix_decision", f"{reviewer_label} approved the prepared fix; it is now live", True)
        else:
            case.status = "needs_admin"
            log_event(case, "fix_decision", f"{reviewer_label} {decision.replace('_', ' ')} the prepared fix; the failure still needs attention", False)
