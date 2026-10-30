"""Authorised execution of published tools (used by the REST API, workflows and the MCP gateway)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Connection, Execution, Integration, IntegrationVersion, Tool
from . import audit, engine, monitoring, repairs, vault
from .integrations import org_policy

_CONCURRENCY = threading.BoundedSemaphore(16)
_RATE: dict[str, deque] = defaultdict(deque)
_RATE_LOCK = threading.Lock()
RATE_LIMIT_PER_MINUTE = 60


@dataclass
class Actor:
    user_id: str
    role: str
    org_id: str
    kind: str = "user"  # user | workflow | mcp
    label: str = ""

    @property
    def id(self) -> str:
        return self.user_id

    @property
    def username(self) -> str:
        return self.label or self.kind


def _rate_limit(key: str) -> None:
    now = time.monotonic()
    with _RATE_LOCK:
        q = _RATE[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MINUTE:
            raise HTTPException(429, "Execution rate limit reached (60 per minute). Try again shortly.")
        q.append(now)


def visible_tools(db: Session, actor: Actor, mcp_only: bool = False) -> list[Tool]:
    q = (select(Tool).join(Integration, Tool.integration_id == Integration.id)
         .where(Tool.organization_id == actor.org_id, Tool.enabled.is_(True), Integration.status == "published",
                Tool.integration_version_id == Integration.published_version_id))
    tools = [t for t in db.scalars(q.order_by(Tool.name)).all() if actor.role in (t.allowed_roles or [])]
    return [t for t in tools if t.mcp_enabled] if mcp_only else tools


def resolve_tool(db: Session, actor: Actor, name_or_id: str) -> Tool:
    tool = db.get(Tool, name_or_id)
    if not tool:
        tool = db.scalars(select(Tool).where(Tool.organization_id == actor.org_id, Tool.name == name_or_id, Tool.enabled.is_(True))).first()
    if not tool or tool.organization_id != actor.org_id:
        raise HTTPException(404, "Tool not found")
    return tool


def authorize(db: Session, actor: Actor, tool: Tool) -> tuple[Integration, IntegrationVersion]:
    integ = db.get(Integration, tool.integration_id)
    if not tool.enabled or not integ or integ.status != "published" or integ.published_version_id != tool.integration_version_id:
        raise HTTPException(403, "This tool is not published or has been disabled")
    if actor.role not in (tool.allowed_roles or []):
        raise HTTPException(403, "Your role is not permitted to run this tool")
    if actor.kind == "mcp" and not tool.mcp_enabled:
        raise HTTPException(403, "This tool is not exposed to AI agents (MCP)")
    return integ, db.get(IntegrationVersion, tool.integration_version_id)


def credential_for(db: Session, org_id: str, config: dict) -> dict | None:
    auth = config["integration"].get("authentication") or {}
    if auth.get("type", "none") == "none":
        return None
    conn = db.scalars(select(Connection).where(Connection.organization_id == org_id, Connection.name == auth.get("credential_ref"))).first()
    if not conn or conn.status != "active" or conn.auth_type != auth["type"]:
        return None
    return vault.read_secret(db, conn.secret_ref, org_id)


def execute_tool(db: Session, actor: Actor, tool: Tool, inputs: dict) -> Execution:
    integ, version = authorize(db, actor, tool)
    _rate_limit(actor.user_id)
    config = version.config
    auth_type = (config["integration"].get("authentication") or {}).get("type", "none")
    credential = credential_for(db, actor.org_id, config)
    if auth_type != "none" and credential is None:
        result = engine.EngineResult(status="failed", error_class="credential_missing",
                                     error=f"No active connection named '{config['integration']['authentication'].get('credential_ref')}'")
    else:
        if not _CONCURRENCY.acquire(timeout=30):
            raise HTTPException(503, "Too many concurrent executions")
        try:
            result = engine.execute(config, tool.operation_id, inputs or {}, credential=credential,
                                    allowed_domains=org_policy(db, actor.org_id).get("allowed_domains") or [])
        finally:
            _CONCURRENCY.release()
    ex = Execution(organization_id=actor.org_id, tool_id=tool.id, tool_name=tool.name, integration_id=integ.id,
                   version_id=version.id, actor_id=actor.user_id, actor_type=actor.kind, status=result.status,
                   http_status=result.http_status, duration_ms=result.duration_ms, attempts=result.attempts,
                   error_class=result.error_class, error=result.error[:2000], request_preview=result.request_preview,
                   output=result.output if isinstance(result.output, dict) else {"value": result.output},
                   diagnostics=result.diagnostics)
    db.add(ex)
    db.flush()
    audit.record(db, actor.org_id, actor, "tool.execute", tool.name, execution_id=ex.id, status=ex.status, actor_type=actor.kind)
    needs_repair = monitoring.after_execution(db, ex, integ)
    db.commit()
    if needs_repair:
        repairs.start_async(actor.org_id, ex.id)
    return ex
