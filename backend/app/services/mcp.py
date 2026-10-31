"""Dynamic MCP gateway (JSON-RPC over Streamable HTTP, request/response mode).

Approved tools that an admin has exposed to agents (``mcp_enabled``) and that the caller's role may run are
listed as MCP tools. ``tools/call`` goes through the same authorisation and generic engine as every other
execution - there is no service-specific MCP code.
"""
from __future__ import annotations

import json
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Tool
from . import runtime

SUPPORTED_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]
SERVER_INFO = {"name": "hedes", "version": "1.0.0"}


def mcp_name(tool: Tool) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", tool.name.replace(".", "_"))[:64]


def convert_to_mcp_schema(tool: Tool) -> dict:
    schema = dict(tool.input_schema or {"type": "object"})
    schema.setdefault("type", "object")
    return {"name": mcp_name(tool), "title": tool.name, "description": tool.description or tool.name, "inputSchema": schema,
            "annotations": {"readOnlyHint": tool.method == "GET", "destructiveHint": tool.method == "DELETE"}}


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(db: Session, actor: runtime.Actor, msg: dict) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
    method, id_, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if id_ is None:  # notification (e.g. notifications/initialized) - no response
        return None
    if method == "initialize":
        wanted = params.get("protocolVersion")
        return _result(id_, {"protocolVersion": wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[-1],
                             "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                             "instructions": "Tools are approved H.A.D.E.S integrations. Every call is authorised and audited."})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [convert_to_mcp_schema(t) for t in runtime.visible_tools(db, actor, mcp_only=True)]})
    if method == "tools/call":
        name = params.get("name")
        tool = next((t for t in runtime.visible_tools(db, actor, mcp_only=True) if mcp_name(t) == name), None)
        if not tool:
            return _error(id_, -32602, f"Unknown or unauthorised tool: {name}")
        try:
            ex = runtime.execute_tool(db, actor, tool, params.get("arguments") or {})
        except HTTPException as e:
            return _result(id_, {"content": [{"type": "text", "text": str(e.detail)}], "isError": True})
        payload = ex.output if ex.status == "succeeded" else {"error_class": ex.error_class, "error": ex.error}
        return _result(id_, {"content": [{"type": "text", "text": json.dumps(payload, default=str)[:20000]}],
                             "structuredContent": payload if isinstance(payload, dict) else {"value": payload},
                             "isError": ex.status != "succeeded"})
    return _error(id_, -32601, f"Method not found: {method}")
