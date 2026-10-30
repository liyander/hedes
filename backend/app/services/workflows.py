"""Workflow engine: chains published tools as a DAG with conditions, parallel layers and output references."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.db import session_scope, utcnow
from ..models import Membership, WebhookDelivery, Workflow, WorkflowRun, WorkflowStep, WorkflowVersion
from . import audit, engine, runtime, templates, vault
from .validation import topo_layers, validate_workflow_graph

log = logging.getLogger("integra.workflows")
SIGNATURE_TOLERANCE_S = 300


def owner_actor(db: Session, wf: Workflow) -> runtime.Actor:
    m = db.scalars(select(Membership).where(Membership.user_id == wf.created_by, Membership.organization_id == wf.organization_id)).first()
    if not m:
        raise HTTPException(409, "The workflow owner is no longer a member of this organisation")
    return runtime.Actor(m.user_id, m.role, wf.organization_id, "workflow", f"workflow:{wf.name[:40]}")


def tool_catalog(db: Session, actor: runtime.Actor) -> dict[str, dict]:
    return {t.name: {"input_schema": t.input_schema, "output_fields": t.output_fields, "description": t.description,
                     "method": t.method, "id": t.id}
            for t in runtime.visible_tools(db, actor)}


def validate(db: Session, wf: Workflow, graph: dict) -> dict:
    return validate_workflow_graph(graph, tool_catalog(db, owner_actor(db, wf)))


def _cond(c: dict, ctx: dict) -> bool:
    left = templates.render(c.get("left", ""), ctx)
    right = c.get("right")
    if isinstance(right, str):
        right = templates.render(right, ctx)
    op = c["op"]
    if op == "exists":
        return left is not templates.MISSING and left not in ("", [], {})
    if op == "not_exists":
        return left is templates.MISSING or left in ("", [], {})
    if left is templates.MISSING:
        return False
    if op == "eq":
        return str(left).lower() == str(right).lower()
    if op == "neq":
        return str(left).lower() != str(right).lower()
    if op in ("contains", "not_contains"):
        hay = [str(x).lower() for x in left] if isinstance(left, list) else str(left).lower()
        found = str(right).lower() in hay
        return found if op == "contains" else not found
    if op == "in":
        return str(left).lower() in [str(x).lower() for x in (right if isinstance(right, list) else str(right).split(","))]
    try:
        return float(left) > float(right) if op == "gt" else float(left) < float(right)
    except (TypeError, ValueError):
        return False


def _run_node(org_id: str, actor: runtime.Actor, node: dict, inputs: dict, dry_run: bool) -> dict:
    with session_scope() as db:
        tool = runtime.resolve_tool(db, actor, node["tool"])
        _, version = runtime.authorize(db, actor, tool)
        if dry_run:
            pv = engine.preview(version.config, tool.operation_id, inputs)
            # Later steps reference this step's outputs; give them visible placeholders instead of real values.
            placeholders = {f: f"<{node['id']}.{f}>" for f in tool.output_fields}
            return {"status": "planned" if pv["ok"] else "failed", "output": placeholders, "preview": pv,
                    "error": "; ".join(pv.get("errors", []))}
        ex = runtime.execute_tool(db, actor, tool, inputs)
        return {"status": ex.status, "output": ex.output, "error": ex.error, "execution_id": ex.id}


def execute(db: Session, wf: Workflow, trigger: str, payload: dict, dry_run: bool = False,
            delivery_id: str | None = None) -> WorkflowRun:
    version = db.get(WorkflowVersion, wf.current_version_id)
    if not version:
        raise HTTPException(409, "Workflow has no version")
    actor = owner_actor(db, wf)
    graph = version.graph
    check = validate_workflow_graph(graph, tool_catalog(db, actor))
    if not check["valid"]:
        raise HTTPException(409, "Workflow is invalid: " + "; ".join(e["message"] for e in check["errors"][:3]))
    if not dry_run and check["warnings"]:
        raise HTTPException(409, "; ".join(w["message"] for w in check["warnings"][:3]))
    run = WorkflowRun(organization_id=wf.organization_id, workflow_id=wf.id, workflow_version_id=version.id, status="running",
                      trigger=trigger, dry_run=dry_run, input=payload or {}, delivery_id=delivery_id)
    db.add(run)
    db.commit()
    nodes = {n["id"]: n for n in graph["nodes"]}
    ctx: dict = {"trigger": payload or {}, "steps": {}}
    status: dict[str, str] = {}
    stopped = ""
    for layer in topo_layers(graph):
        todo = []
        for nid in layer:
            node = nodes[nid]
            deps = node.get("depends_on") or []
            if stopped:
                status[nid] = "cancelled"
            elif any(status.get(d) not in ("succeeded", "planned") for d in deps):
                status[nid] = "skipped"
            elif not all(_cond(c, ctx) for c in node.get("when") or []):
                status[nid] = "skipped"
            else:
                todo.append(nid)
                continue
            db.add(WorkflowStep(run_id=run.id, node_id=nid, status=status[nid], finished_at=utcnow(),
                                error="Condition not met or dependency did not complete" if status[nid] == "skipped" else "Run stopped"))
        rendered = {nid: templates.render(nodes[nid].get("inputs") or {}, ctx) for nid in todo}
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(todo)))) as pool:
            futures = {nid: pool.submit(_run_node, wf.organization_id, actor, nodes[nid], rendered[nid], dry_run) for nid in todo}
            for nid, fut in futures.items():
                started = utcnow()
                try:
                    res = fut.result()
                except HTTPException as e:
                    res = {"status": "failed", "output": {}, "error": str(e.detail)}
                status[nid] = res["status"]
                ctx["steps"][nid] = {"output": res.get("output") or {}, "status": res["status"]}
                db.add(WorkflowStep(run_id=run.id, node_id=nid, status=res["status"], execution_id=res.get("execution_id"),
                                    output={"inputs": rendered[nid], "result": res.get("preview") or res.get("output")}, error=res.get("error", "")[:2000],
                                    started_at=started, finished_at=utcnow()))
                if res["status"] == "failed" and nodes[nid].get("on_failure", "stop") == "stop":
                    stopped = nid
        db.commit()
    run.status = "failed" if stopped or any(s == "failed" for s in status.values()) else "succeeded"
    run.error = f"Step {stopped} failed" if stopped else ""
    run.finished_at = utcnow()
    db.commit()
    audit.record(db, wf.organization_id, actor, "workflow.run", wf.name, run_id=run.id, status=run.status, trigger=trigger, dry_run=dry_run)
    db.commit()
    return run


def run_async(workflow_id: str, trigger: str, payload: dict, delivery_id: str | None = None) -> None:
    def work():
        try:
            with session_scope() as db:
                execute(db, db.get(Workflow, workflow_id), trigger, payload, delivery_id=delivery_id)
        except Exception:
            log.exception("workflow run failed")
    threading.Thread(target=work, name="workflow", daemon=True).start()


# ----------------------------------------------------------------------------------- webhooks

def ensure_webhook_secret(db: Session, wf: Workflow) -> str | None:
    """Create the signing secret on first activation; returns it once (never retrievable afterwards)."""
    if wf.webhook_secret_ref:
        return None
    secret = "whsec_" + secrets.token_urlsafe(24)
    wf.webhook_secret_ref = vault.store_secret(db, wf.organization_id, {"secret": secret})
    return secret


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_webhook(db: Session, wf: Workflow, body: bytes, timestamp: str | None, signature: str | None,
                   delivery_id: str | None) -> str:
    if wf.status != "active":
        raise HTTPException(409, "Workflow is not active")
    secret = (vault.read_secret(db, wf.webhook_secret_ref, wf.organization_id) or {}).get("secret")
    if not secret or not timestamp or not signature:
        raise HTTPException(401, "Missing webhook signature")
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        raise HTTPException(401, "Bad timestamp")
    if age > SIGNATURE_TOLERANCE_S:
        raise HTTPException(401, "Webhook timestamp outside the 5 minute window")
    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        raise HTTPException(401, "Invalid webhook signature")
    did = (delivery_id or signature)[:128]
    try:
        db.add(WebhookDelivery(workflow_id=wf.id, delivery_id=did))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Duplicate delivery (replay) ignored")
    return did


# ----------------------------------------------------------------------------------- scheduler

def scheduler_loop(stop: threading.Event) -> None:
    while not stop.wait(30):
        try:
            with session_scope() as db:
                for wf in db.scalars(select(Workflow).where(Workflow.status == "active")).all():
                    v = db.get(WorkflowVersion, wf.current_version_id)
                    trig = (v.graph if v else {}).get("trigger") or {}
                    if trig.get("type") != "schedule":
                        continue
                    due = wf.last_scheduled_at is None or wf.last_scheduled_at + timedelta(minutes=int(trig.get("interval_minutes", 60))) <= utcnow()
                    if due:
                        wf.last_scheduled_at = utcnow()
                        db.commit()
                        run_async(wf.id, "schedule", {"scheduled_at": utcnow().isoformat()})
        except Exception:
            log.exception("scheduler tick failed")

