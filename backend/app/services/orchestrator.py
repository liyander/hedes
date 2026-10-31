"""AI Studio orchestration: a persisted state machine driving the agents.

COLLECT_REQUIREMENTS -> DISCOVER_API -> DESIGN_INTEGRATION -> GENERATE_CONFIGURATION -> VALIDATE_CONFIGURATION
-> SECURITY_EVALUATION -> RUN_SAFE_TESTS -> AWAIT_ADMIN_APPROVAL -> PUBLISHED

Each stage writes its status and output to ai_sessions.state, so a failed stage can be retried without
repeating the earlier ones. The pipeline can submit for review on the user's behalf, but approval is a
separate human action enforced by the approvals API.
"""
from __future__ import annotations

import logging
import threading
import traceback

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import new_id, session_scope, utcnow
from ..models import AIMessage, AISession, Integration, IntegrationVersion, Tool, User, Workflow, WorkflowVersion
from . import agents, audit, catalog, docs_search, integrations, openapi
from .ai_gateway import Gateway, ProviderError

log = logging.getLogger("integra.orchestrator")
STAGES = ["COLLECT_REQUIREMENTS", "DISCOVER_API", "DESIGN_INTEGRATION", "GENERATE_CONFIGURATION", "VALIDATE_CONFIGURATION",
          "RESEARCH_SETUP", "SECURITY_EVALUATION", "RUN_SAFE_TESTS", "AWAIT_ADMIN_APPROVAL", "PUBLISHED"]
_running: set[str] = set()
_lock = threading.Lock()


def _set(db: Session, s: AISession, stage: str, status: str, detail: str = "", **extra) -> None:
    state = dict(s.state or {})
    stages = dict(state.get("stages") or {})
    stages[stage] = {**stages.get(stage, {}), "status": status, "detail": detail, "at": utcnow().isoformat(), **extra}
    state["stages"] = stages
    s.state = state
    s.stage = stage
    db.commit()


def _out(s: AISession, key: str, value=None):
    state = dict(s.state or {})
    if value is None:
        return (state.get("outputs") or {}).get(key)
    outputs = dict(state.get("outputs") or {})
    outputs[key] = value
    state["outputs"] = outputs
    s.state = state


def _say(db: Session, s: AISession, text: str, **meta) -> None:
    db.add(AIMessage(session_id=s.id, role="assistant", content=text, meta=meta))
    db.commit()


def handle_message(db: Session, s: AISession, text: str) -> AIMessage:
    db.add(AIMessage(session_id=s.id, role="user", content=text[:8000]))
    db.commit()
    return refresh_requirements(db, s)


def _note_source(s: AISession, label: str, url: str, kind: str) -> None:
    state = dict(s.state or {})
    sources = [x for x in state.get("sources") or [] if x.get("url") != url or not url]
    sources.append({"label": label, "url": url, "kind": kind})
    state["sources"] = sources
    s.state = state


def _add_document(s: AISession, catalog: dict, name: str, url: str, origin: str, alias: str = "") -> None:
    docs = [d for d in (s.documents or []) if openapi.slugify(d["catalog"]["title"]) != openapi.slugify(catalog["title"])]
    docs.append({"id": new_id(), "name": name, "kind": "openapi", "catalog": catalog, "url": url, "origin": origin,
                 "aliases": [alias] if alias else []})
    s.documents = docs


def _find_documentation(db: Session, s: AISession, req: dict, latest: str) -> list[str]:
    """Import OpenAPI documents linked in the latest message, and search the public directory for services that have no
    documentation yet. Returns notes for the user. Each service is searched once per session."""
    notes: list[str] = []
    state = dict(s.state or {})
    searched = dict(state.get("doc_search") or {})
    for url in docs_search.spec_urls_in(latest)[:3]:
        if any(d.get("url") == url for d in s.documents or []):
            continue
        try:
            cat = docs_search.fetch_spec(url, latest)
        except docs_search.DocsError as e:
            notes.append(f"I couldn't use the document at {url}: {e}")
            continue
        _add_document(s, cat, url.rsplit("/", 1)[-1] or url, url, "link")
        _note_source(s, f"{cat['title']} OpenAPI document (link you provided)", url, "document")
        notes.append(f"I read the API documentation you linked: **{cat['title']}**, {len(cat['operations'])} operations.")
    for name in req.get("unknown_services") or []:
        if name.lower() in searched:
            continue
        try:
            hits = docs_search.search(name)
        except docs_search.DocsError as e:
            searched[name.lower()] = {"status": "error", "message": str(e)}
            notes.append(f"I couldn't search online for {name} documentation ({e}).")
            continue
        usable = [h for h in hits if h["openapi_version"].startswith("3") and h["spec_url"]]
        if not usable:
            status = "unsupported" if hits else "not_found"
            searched[name.lower()] = {"status": status, "hits": [h["key"] for h in hits]}
            notes.append(f"I searched the {docs_search.DIRECTORY_NAME} for {name}: " +
                         ("it only has a Swagger 2.0 document, which I can't read yet." if hits else "no API documentation was listed."))
            continue
        best = usable[0]
        try:
            cat = docs_search.fetch_spec(best["spec_url"], latest)
        except docs_search.DocsError as e:
            searched[name.lower()] = {"status": "error", "message": str(e)}
            notes.append(f"I found {name} in the {docs_search.DIRECTORY_NAME} but couldn't read it: {e}")
            continue
        _add_document(s, cat, f"{best['key']} (online)", best["spec_url"], "online", alias=name)
        _note_source(s, f"{best['title']} - OpenAPI document from the {docs_search.DIRECTORY_NAME}", best["spec_url"], "document")
        if best.get("docs_url"):
            _note_source(s, f"{best['title']} - original specification", best["docs_url"], "reference")
        searched[name.lower()] = {"status": "found", "url": best["spec_url"]}
        others = ", ".join(h["key"] for h in usable[1:4])
        notes.append(f"I found the **{best['title']}** API documentation online ({len(cat['operations'])} operations"
                     + (f", trimmed from {cat['trimmed_from']} to the most relevant" if cat.get("trimmed_from") else "")
                     + f") at {best['spec_url']}." + (f" Other matches: {others} - tell me if you meant one of those." if others else ""))
    state = dict(s.state or {})
    state["doc_search"] = searched
    s.state = state
    return notes


def _tailor_missing(s: AISession, req: dict) -> None:
    """Explain what was already tried for services whose documentation could not be found."""
    searched = (s.state or {}).get("doc_search") or {}
    out = []
    for q in req.get("missing") or []:
        name = next((n for n in req.get("unknown_services") or [] if f"documentation for {n}" in q), None)
        result = searched.get(name.lower()) if name else None
        if result and result["status"] != "found":
            q = (f"I couldn't find usable API documentation for {name} online"
                 + (" (only a Swagger 2.0 version is listed)" if result["status"] == "unsupported" else "")
                 + ". Paste a link to its OpenAPI/Swagger document or upload the file (JSON or YAML).")
        out.append(q)
    req["missing"] = out
    req["missing_help"] = [{"question": q, "how": catalog.how_to_find(q)} for q in out]
    req["ready"] = bool(req["services"]) and not out


def refresh_requirements(db: Session, s: AISession) -> AIMessage:
    msgs = [{"role": m.role, "content": m.content} for m in
            db.scalars(select(AIMessage).where(AIMessage.session_id == s.id).order_by(AIMessage.created_at)).all() if m.role != "system"]
    gw = Gateway(db, s.organization_id)
    latest = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    try:
        req, model = agents.analyze_request(gw, msgs, s.documents or [], s.requirements or {})
        notes = _find_documentation(db, s, req, latest)
        if any(n.startswith(("I found", "I read")) for n in notes):  # new documentation: re-read the request against it
            req, model = agents.analyze_request(gw, msgs, s.documents or [], s.requirements or {})
        _tailor_missing(s, req)
        if notes or req["missing"]:
            req["reply"] = "\n".join(notes + ([""] if notes else []) + [agents.compose_reply(req)])
        if gw.fallback_note():
            req["reply"] = gw.fallback_note() + "\n\n" + req["reply"]
    except ProviderError as e:
        _set(db, s, "COLLECT_REQUIREMENTS", "failed", str(e))
        msg = AIMessage(session_id=s.id, role="assistant", content=f"I couldn't reach any AI model: {e}", meta={"error": True})
        db.add(msg)
        db.commit()
        return msg
    s.requirements = req
    if s.title == "New integration" and req["services"]:
        s.title = req["summary"][:200]
    s.status = "collecting"
    _set(db, s, "COLLECT_REQUIREMENTS", "done" if req["ready"] else "waiting", req["summary"], model=model)
    msg = AIMessage(session_id=s.id, role="assistant", content=req["reply"], meta={"model": model, "ready": req["ready"]})
    db.add(msg)
    db.commit()
    return msg


def start(session_id: str, from_stage: str | None = None) -> None:
    with _lock:
        if session_id in _running:
            raise HTTPException(409, "Generation is already running for this session")
        _running.add(session_id)

    def work():
        try:
            with session_scope() as db:
                run_pipeline(db, db.get(AISession, session_id), from_stage)
        except Exception:
            log.exception("pipeline crashed")
            with session_scope() as db:
                s = db.get(AISession, session_id)
                s.status = "failed"
                _set(db, s, s.stage, "failed", "Internal error: " + traceback.format_exc(limit=1).strip().splitlines()[-1])
        finally:
            with _lock:
                _running.discard(session_id)
    threading.Thread(target=work, name=f"pipeline-{session_id[:8]}", daemon=True).start()


def run_pipeline(db: Session, s: AISession, from_stage: str | None = None) -> None:
    req = s.requirements or {}
    if not req.get("ready"):
        raise HTTPException(409, "Requirements are incomplete - answer the open questions first")
    user = db.get(User, s.user_id)
    gw = Gateway(db, s.organization_id)
    start_at = STAGES.index(from_stage) if from_stage in STAGES else 1
    s.status = "running"
    db.commit()

    def skip(stage):
        return STAGES.index(stage) < start_at and (s.state.get("stages") or {}).get(stage, {}).get("status") == "done"

    # DISCOVER_API: bind every service to verified documentation (OpenAPI or catalog) or flag it.
    if not skip("DISCOVER_API"):
        _set(db, s, "DISCOVER_API", "running", "Reading API documentation")
        found = []
        for svc in req["services"]:
            ops = agents.service_ops(svc, s.documents or [])
            found.append({"service": svc["slug"], "name": svc["name"], "source": svc.get("source", "unknown"), "operations": len(ops)})
        _out(s, "discovery", found)
        unknown = [f["name"] for f in found if not f["operations"] and f["source"] != "model_knowledge"]
        if unknown:
            s.status = "failed"
            _set(db, s, "DISCOVER_API", "failed", "No documentation for " + ", ".join(unknown) + ". Upload an OpenAPI document.")
            return
        _set(db, s, "DISCOVER_API", "done", "; ".join(f"{f['name']}: {f['operations']} operations ({f['source'].replace('_', ' ')})" for f in found))

    # DESIGN_INTEGRATION + GENERATE_CONFIGURATION
    if not skip("GENERATE_CONFIGURATION"):
        _set(db, s, "DESIGN_INTEGRATION", "done", f"{len(req['services'])} integration(s), {len(req['operations'])} operation(s); "
             "credentials via connections: " + ", ".join(sv["slug"] for sv in req["services"]))
        _set(db, s, "GENERATE_CONFIGURATION", "running", "Generating configurations")
        created, notes, models = [], [], set()
        for svc in req["services"]:
            try:
                cfg, meta = agents.generate_config(gw, svc, req, s.documents or [])
            except (ValueError, ProviderError) as e:
                s.status = "failed"
                _set(db, s, "GENERATE_CONFIGURATION", "failed", f"{svc['name']}: {e}")
                return
            integ, v = integrations.create_integration(db, s.organization_id, user, cfg, "ai", s.id, meta.get("note", ""))
            created.append({"slug": integ.slug, "integration_id": integ.id, "version_id": v.id, "version": v.version})
            notes.append(f"{integ.name} {v.version}")
            models.add(meta.get("model", ""))
        _out(s, "integrations", created)
        db.commit()
        _set(db, s, "GENERATE_CONFIGURATION", "done", ", ".join(notes), model=", ".join(m for m in models if m))

    created = _out(s, "integrations") or []

    # VALIDATE_CONFIGURATION
    _set(db, s, "VALIDATE_CONFIGURATION", "running")
    invalid = []
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        integrations.analyse(db, v, s.organization_id)
        if not v.validation["valid"]:
            invalid.append(f"{c['slug']}: " + "; ".join(e["message"] for e in v.validation["errors"][:3]))
    db.commit()
    if invalid:
        s.status = "failed"
        _set(db, s, "VALIDATE_CONFIGURATION", "failed", " | ".join(invalid))
        return
    _set(db, s, "VALIDATE_CONFIGURATION", "done", "All configurations match the integration schema")

    # RESEARCH_SETUP: what the person must do in each vendor's product, read from the vendor's own documentation.
    _set(db, s, "RESEARCH_SETUP", "running", "Reading the vendors' reference pages")
    guides, read_total = dict(_out(s, "setup_guides") or {}), 0
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        guide, model = agents.setup_guide(gw, c["slug"], v.config, s.documents or [])
        guides[c["slug"]] = {**guide, "model": model}
        for url in guide["sources"]:
            _note_source(s, f"{v.config['integration']['name']} - official reference page (setup requirements)", url, "reference")
        read_total += len(guide["sources"])
    _out(s, "setup_guides", guides)
    db.commit()
    _set(db, s, "RESEARCH_SETUP", "done", f"Setup checklist from {read_total} vendor documentation page(s)", model=model if created else "")

    # SECURITY_EVALUATION
    _set(db, s, "SECURITY_EVALUATION", "running")
    blocking, summaries, model = [], [], ""
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        report = dict(v.security_report)
        try:
            narrative, model = agents.explain_risks(gw, report)
            report["ai_summary"], report["ai_concerns"], report["ai_model"] = narrative["summary"], narrative.get("concerns", []), model
            v.security_report = report
        except ProviderError:
            pass
        summaries.append(f"{c['slug']}: risk {report['score']}/100 ({report['level']})")
        if report["blocking"]:
            blocking.append(c["slug"])
    db.commit()
    if blocking:
        s.status = "failed"
        _set(db, s, "SECURITY_EVALUATION", "failed", "Blocking findings in " + ", ".join(blocking) + ". Revise the request.")
        return
    _set(db, s, "SECURITY_EVALUATION", "done", "; ".join(summaries), model=model)

    # RUN_SAFE_TESTS (mocked transport - no live calls before approval)
    _set(db, s, "RUN_SAFE_TESTS", "running")
    failures, totals = [], []
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        tr = integrations.run_tests(db, v, user, s.documents or [])
        totals.append(f"{c['slug']}: {tr.report['passed']}/{tr.report['total']} passed")
        if tr.result != "passed":
            failures.append(c["slug"])
    db.commit()
    if failures:
        s.status = "failed"
        _set(db, s, "RUN_SAFE_TESTS", "failed", "; ".join(totals))
        return
    _set(db, s, "RUN_SAFE_TESTS", "done", "; ".join(totals))

    # AWAIT_ADMIN_APPROVAL: submit on the user's behalf (approval itself is a separate human decision).
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        integ = db.get(Integration, c["integration_id"])
        if v.status == "draft":
            req_row = integrations.submit_for_review(db, v, integ, user)
            c["approval_id"] = req_row.id
    _out(s, "integrations", created)
    db.commit()
    _set(db, s, "AWAIT_ADMIN_APPROVAL", "waiting", "Submitted to administrators for review")

    if req.get("triggers") and len(req["services"]) >= 1:
        _plan_workflow(db, s, gw, user, req)

    s.status = "ready"
    db.commit()
    lines = ([gw.fallback_note(), ""] if gw.fallback_note() else []) + ["Done. I generated, validated, security-checked and tested:"]
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        lines.append(f"- **{c['slug']}** {c['version']} - risk {v.security_report['level']}, tests passed, submitted for approval")
    lines.append("\nNext: connect the credentials (they go to the vault, never to me), then an administrator reviews and publishes.")
    for c in created:
        v = db.get(IntegrationVersion, c["version_id"])
        auth = v.config["integration"]["authentication"]
        if auth["type"] != "none":
            lines.append(f"- **{c['slug']}** needs a credential named `{auth.get('credential_ref')}`. Where to get it: {catalog.credential_help(c['slug'], auth['type'])}")
        guide = (_out(s, "setup_guides") or {}).get(c["slug"])
        if guide and (guide["permissions"] or guide["steps"] or guide["common_errors"]):
            lines.append(f"\n**Before {c['slug']} works, in {v.config['integration']['name']}:**")
            if guide["permissions"]:
                lines.append(f"- Required scopes/permissions: {', '.join(f'`{x}`' for x in guide['permissions'])}")
            lines += [f"- {x}" for x in guide["steps"] + guide.get("prerequisites", [])]
            if guide["common_errors"]:
                lines.append("- If it fails: " + "; ".join(f"`{e['code']}` - {e['fix']}" for e in guide["common_errors"][:5]))
    wf = _out(s, "workflow")
    if wf:
        lines.append(f"I also drafted a workflow, **{wf['name']}** - you can review it in Workflows once the integrations are published.")
    sources = sources_referred(db, s)
    if sources:
        lines.append("\n**Sources referred:**")
        lines += [f"- {x['label']}" + (f": {x['url']}" if x.get("url") else "") for x in sources]
    _say(db, s, "\n".join(lines), stage="AWAIT_ADMIN_APPROVAL")


def _plan_workflow(db: Session, s: AISession, gw: Gateway, user, req: dict) -> None:
    tools: dict[str, dict] = {}
    for c in _out(s, "integrations") or []:
        v = db.get(IntegrationVersion, c["version_id"])
        for op in v.config["integration"]["operations"]:
            resp = op.get("response") or {}
            tools[f"{c['slug']}.{op['id']}"] = {"input_schema": op["input_schema"],
                                                 "output_fields": ["items", "count"] if resp.get("items_path") else list((resp.get("mapping") or {}).keys())}
    for t in db.scalars(select(Tool).where(Tool.organization_id == s.organization_id, Tool.enabled.is_(True))).all():
        tools.setdefault(t.name, {"input_schema": t.input_schema, "output_fields": t.output_fields})
    try:
        graph, model = agents.plan_workflow(gw, req, tools)
    except ProviderError:
        return
    wf = Workflow(organization_id=s.organization_id, name=req["summary"][:160], description=(req.get("triggers") or [{}])[0].get("event", ""),
                  created_by=user.id)
    db.add(wf)
    db.flush()
    wv = WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, created_by=user.id)
    db.add(wv)
    db.flush()
    wf.current_version_id = wv.id
    _out(s, "workflow", {"id": wf.id, "name": wf.name, "model": model})
    audit.record(db, s.organization_id, user, "workflow.create", wf.name, source="ai")
    db.commit()


def creation_plan(db: Session, s: AISession) -> dict:
    """What generating will write to the database, shown to the user for confirmation before anything is created.
    ``fingerprint`` identifies this exact plan, so a confirmation cannot be replayed after the requirements change."""
    import hashlib
    import json
    req = s.requirements or {}
    items: list[dict] = []
    for svc in req.get("services") or []:
        existing = db.scalars(select(Integration).where(Integration.organization_id == s.organization_id, Integration.slug == svc["slug"])).first()
        ops = [o.get("operation_id") or o["action"] for o in req.get("operations") or [] if o["service"] == svc["slug"]]
        try:
            draft, _ = agents._draft_for_service(svc, req, s.documents or [])
        except Exception:  # noqa: BLE001 - the plan is informational; generation reports real errors
            draft = None
        auth = ((draft or {}).get("integration") or {}).get("authentication") or {}
        items.append({
            "service": svc["name"], "slug": svc["slug"],
            "record": "new version of the existing integration" if existing else "new integration",
            "tables": ["integration_versions", "test_runs", "approval_requests"] + ([] if existing else ["integrations"]),
            "operations": ops, "auth_type": auth.get("type", "unknown"), "credential_ref": auth.get("credential_ref"),
            "credential_help": catalog.credential_help(svc["slug"], auth.get("type")),
        })
    plan = {"items": items, "workflow": bool(req.get("triggers")),
            "notes": ["Nothing becomes executable yet: tools are created only after an administrator approves the exact version.",
                      "No credentials are created; you connect them yourself in Credentials."]}
    if plan["workflow"]:
        plan["notes"].insert(0, "A draft workflow (workflows, workflow_versions) is created for the trigger you described; it stays inactive.")
    plan["fingerprint"] = hashlib.sha256(json.dumps([items, plan["workflow"]], sort_keys=True, default=str).encode()).hexdigest()[:16]
    return plan


def sources_referred(db: Session, s: AISession) -> list[dict]:
    """Every source the build drew on: documents (uploaded, linked, found online), built-in references, model knowledge."""
    out = list((s.state or {}).get("sources") or [])
    seen = {x.get("url") or x["label"] for x in out}

    def add(label: str, url: str, kind: str) -> None:
        if (url or label) not in seen:
            seen.add(url or label)
            out.append({"label": label, "url": url, "kind": kind})

    for d in s.documents or []:
        if d.get("origin") in (None, "upload"):
            add(f"{d['catalog']['title']} OpenAPI document (uploaded file {d['name']})", "", "document")
    for svc in (s.requirements or {}).get("services") or []:
        if svc.get("source") == "catalog":
            add(f"{svc['name']} - built-in reference configuration (examples/{svc['slug']}.yaml)", "", "reference")
            guide = catalog.SERVICE_GUIDES.get(svc["slug"])
            if guide:
                add(f"{svc['name']} official API documentation", guide["docs"], "reference")
        elif svc.get("source") == "model_knowledge":
            add(f"{svc['name']} - AI model knowledge (not verified against documentation)", "", "model")
    return out


def session_view(db: Session, s: AISession) -> dict:
    """Session state for the UI, with live statuses of the generated integrations."""
    outputs = (s.state or {}).get("outputs") or {}
    items = []
    conns = integrations.connections_map(db, s.organization_id)
    for c in outputs.get("integrations") or []:
        integ = db.get(Integration, c["integration_id"])
        v = db.get(IntegrationVersion, c["version_id"])
        if not integ or not v:
            continue
        tr = integrations.latest_test(db, v)
        items.append({"integration_id": integ.id, "slug": integ.slug, "name": integ.name, "status": integ.status,
                      "version_id": v.id, "version": v.version, "version_status": v.status, "config": v.config,
                      "validation": v.validation, "security": v.security_report,
                      "tests": tr.report if tr else None, "approval_id": c.get("approval_id"),
                      "credential_ref": v.config["integration"]["authentication"].get("credential_ref"),
                      "auth_type": v.config["integration"]["authentication"]["type"],
                      "credential_connected": (conns.get(v.config["integration"]["authentication"].get("credential_ref")) or {}).get("status") == "active",
                      "credential_help": catalog.credential_help(integ.slug, v.config["integration"]["authentication"]["type"]),
                      "setup": (outputs.get("setup_guides") or {}).get(integ.slug)})
    stages = dict((s.state or {}).get("stages") or {})
    if items and all(i["version_status"] == "published" for i in items):
        stages["AWAIT_ADMIN_APPROVAL"] = {**stages.get("AWAIT_ADMIN_APPROVAL", {}), "status": "done", "detail": "Approved"}
        stages["PUBLISHED"] = {"status": "done", "detail": "Live in the tool registry"}
    elif any(i["version_status"] in ("rejected", "changes_requested") for i in items):
        stages["AWAIT_ADMIN_APPROVAL"] = {**stages.get("AWAIT_ADMIN_APPROVAL", {}), "status": "failed",
                                          "detail": "An administrator asked for changes - describe the fix and generate again"}
    msgs = db.scalars(select(AIMessage).where(AIMessage.session_id == s.id).order_by(AIMessage.created_at)).all()
    with _lock:
        running = s.id in _running
    return {"id": s.id, "title": s.title, "status": "running" if running else s.status, "stage": s.stage,
            "requirements": s.requirements, "stages": stages, "stage_order": STAGES,
            "documents": [{"id": d["id"], "name": d["name"], "title": d["catalog"]["title"],
                           "operations": len(d["catalog"]["operations"]), "base_url": d["catalog"].get("base_url")} for d in s.documents or []],
            "integrations": items, "workflow": outputs.get("workflow"), "sources": sources_referred(db, s),
            "messages": [{"id": m.id, "role": m.role, "content": m.content, "meta": m.meta, "created_at": m.created_at.isoformat()} for m in msgs],
            "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat() if s.updated_at else None}
