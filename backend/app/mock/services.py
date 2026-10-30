"""Bundled mock third-party APIs used for the demo and tests.

* TaskFlow - a task-management API the platform has never seen (bearer token auth, OpenAPI published).
  It has a "drift" switch that changes its response structure to demonstrate self-healing.
* Notify - a team-alert API (API-key header auth).

They are ordinary HTTP APIs; IntegraAI reaches them through the generic engine like any other service.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..core.config import EXAMPLES_DIR, get_settings

TASKFLOW_TOKEN = "taskflow-demo-token"
NOTIFY_KEY = "notify-demo-key"

router = APIRouter(prefix="/mock", include_in_schema=False)
_lock = threading.Lock()
STATE: dict = {"drift": False, "tasks": {}, "next_task": 1, "messages": [], "next_message": 1}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_out(t: dict) -> dict:
    out = {k: v for k, v in t.items() if k != "assignee_name"}
    # Drift changes the assignee object from {"user_name": ...} to {"display_name": ...}.
    out["assignee"] = {"display_name": t["assignee_name"]} if STATE["drift"] else {"user_name": t["assignee_name"]}
    return out


def _taskflow_auth(authorization: str | None) -> None:
    if authorization != f"Bearer {TASKFLOW_TOKEN}":
        raise HTTPException(401, "invalid or missing bearer token")


@router.get("/taskflow/openapi.json")
def taskflow_openapi():
    spec = json.loads((EXAMPLES_DIR / "mock-openapi.json").read_text())
    spec["servers"] = [{"url": get_settings().public_base_url.rstrip("/") + "/mock/taskflow"}]
    return spec


@router.get("/taskflow/tasks")
def list_tasks(status: str | None = None, page: int = 1, per_page: int = 50, authorization: str | None = Header(None)):
    _taskflow_auth(authorization)
    with _lock:
        rows = [t for t in STATE["tasks"].values() if not status or t["status"] == status]
    rows.sort(key=lambda t: -t["id"])
    start = max(0, (page - 1) * per_page)
    return {"items": [_task_out(t) for t in rows[start:start + per_page]], "page": page, "total": len(rows)}


@router.post("/taskflow/tasks", status_code=201)
async def create_task(request: Request, authorization: str | None = Header(None)):
    _taskflow_auth(authorization)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "body must be JSON")
    title = body.get("title") if isinstance(body, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(422, "title is required")
    if body.get("priority", "medium") not in ("low", "medium", "high"):
        raise HTTPException(422, "priority must be low, medium or high")
    with _lock:
        tid = STATE["next_task"]
        STATE["next_task"] += 1
        task = {"id": tid, "title": title[:200], "description": str(body.get("description") or ""),
                "status": "todo", "priority": body.get("priority", "medium"),
                "assignee_name": str(body.get("assignee") or "Unassigned"),
                "url": f"{get_settings().public_base_url.rstrip('/')}/mock/taskflow/tasks/{tid}", "created_at": _now()}
        STATE["tasks"][tid] = task
    return _task_out(task)


@router.get("/taskflow/tasks/{task_id}")
def get_task(task_id: int, authorization: str | None = Header(None)):
    _taskflow_auth(authorization)
    t = STATE["tasks"].get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return _task_out(t)


@router.patch("/taskflow/tasks/{task_id}")
async def update_task(task_id: int, request: Request, authorization: str | None = Header(None)):
    _taskflow_auth(authorization)
    body = await request.json()
    with _lock:
        t = STATE["tasks"].get(task_id)
        if not t:
            raise HTTPException(404, "task not found")
        for k in ("title", "status", "priority"):
            if k in body:
                t[k] = body[k]
    return _task_out(t)


@router.delete("/taskflow/tasks/{task_id}", status_code=204)
def delete_task(task_id: int, authorization: str | None = Header(None)):
    _taskflow_auth(authorization)
    with _lock:
        if STATE["tasks"].pop(task_id, None) is None:
            raise HTTPException(404, "task not found")
    return Response(status_code=204)


def _notify_auth(key: str | None) -> None:
    if key != NOTIFY_KEY:
        raise HTTPException(401, "invalid API key")


@router.post("/notify/messages", status_code=201)
async def send_message(request: Request, x_api_key: str | None = Header(None)):
    _notify_auth(x_api_key)
    body = await request.json()
    if not body.get("channel") or not body.get("text"):
        return JSONResponse({"error": "channel and text are required"}, status_code=422)
    with _lock:
        mid = STATE["next_message"]
        STATE["next_message"] += 1
        msg = {"id": f"msg_{mid}", "channel": str(body["channel"]), "text": str(body["text"])[:2000],
               "severity": body.get("severity", "info"), "delivered": True, "created_at": _now()}
        STATE["messages"].insert(0, msg)
        del STATE["messages"][200:]
    return msg


@router.get("/notify/messages")
def list_messages(channel: str | None = None, x_api_key: str | None = Header(None)):
    _notify_auth(x_api_key)
    return {"items": [m for m in STATE["messages"] if not channel or m["channel"] == channel][:50]}


def snapshot() -> dict:
    with _lock:
        return {"drift": STATE["drift"],
                "tasks": [_task_out(t) for t in sorted(STATE["tasks"].values(), key=lambda t: -t["id"])][:20],
                "messages": STATE["messages"][:20]}


def set_drift(enabled: bool) -> None:
    STATE["drift"] = bool(enabled)
