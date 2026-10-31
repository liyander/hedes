"""Bundled mock third-party APIs used for the demo and tests.

* TaskFlow - a task-management API the platform has never seen (bearer token auth, OpenAPI published).
  It has a "drift" switch that changes its response structure to demonstrate self-healing.
* Notify - a team-alert API (API-key header auth).

They are ordinary HTTP APIs; H.E.D.E.S reaches them through the generic engine like any other service.
"""
from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..core.config import EXAMPLES_DIR, get_settings

TASKFLOW_TOKEN = "taskflow-demo-token"
NOTIFY_KEY = "notify-demo-key"
OKTA_TOKEN = "okta-demo-token"
AWS_ACCESS_KEY = "AKIADEMOCLOUDTRAIL"
AWS_SECRET_KEY = "demo/aws/secret/access/key"
AWS_REGION, AWS_SERVICE = "us-east-1", "cloudtrail"
FIREWALL_KEY = "fw-demo-key"

router = APIRouter(prefix="/mock", include_in_schema=False)
_lock = threading.Lock()
STATE: dict = {"drift": False, "tasks": {}, "next_task": 1, "messages": [], "next_message": 1,
               "feeds": {"okta": [], "aws": [], "firewall": []}, "firewall_flaky": False, "firewall_calls": 0}


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
        return {"drift": STATE["drift"], "firewall_flaky": STATE["firewall_flaky"],
                "feeds": {k: len(v) for k, v in STATE["feeds"].items()},
                "tasks": [_task_out(t) for t in sorted(STATE["tasks"].values(), key=lambda t: -t["id"])][:20],
                "messages": STATE["messages"][:20]}


def set_drift(enabled: bool) -> None:
    STATE["drift"] = bool(enabled)


# --------------------------------------------------------------------------------------------- data sources
# Three deliberately different "security data" APIs, so the same generic engine can be shown ingesting each of them:
#   Okta        SSWS token auth,       Link-header pagination,   ISO-8601 timestamps
#   CloudTrail  AWS SigV4 signing,     NextToken pagination,     epoch-seconds timestamps
#   Firewall    API-key header,        offset pagination,        epoch-millis timestamps, bad records, rate limiting

_USERS = ["ana@acme.io", "bo@acme.io", "chen@acme.io", "dee@acme.io", "eli@acme.io"]
_rng = random.Random(7)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _okta_event(i: int, ts: float) -> dict:
    kind = _rng.choice([("user.session.start", "SUCCESS", "INFO"), ("user.session.start", "FAILURE", "WARN"),
                        ("user.account.lock", "SUCCESS", "WARN"), ("user.mfa.factor.deactivate", "SUCCESS", "WARN"),
                        ("policy.rule.update", "SUCCESS", "INFO"), ("system.api_token.create", "SUCCESS", "WARN")])
    return {"uuid": f"okta-{i:05d}", "published": _iso(ts), "eventType": kind[0], "displayMessage": kind[0].replace(".", " "),
            "severity": kind[2], "outcome": {"result": kind[1]}, "actor": {"alternateId": _rng.choice(_USERS), "type": "User"},
            "target": [{"alternateId": _rng.choice(["Okta Dashboard", "Salesforce", "AWS Console"]), "type": "AppInstance"}],
            "client": {"ipAddress": f"203.0.113.{_rng.randint(2, 250)}"}}


def _aws_event(i: int, ts: float) -> dict:
    name = _rng.choice(["ConsoleLogin", "ConsoleLogin", "CreateAccessKey", "AttachUserPolicy", "PutBucketPolicy", "DeleteTrail",
                        "StopLogging", "DescribeInstances", "ListBuckets"])
    ev = {"EventId": f"aws-{i:05d}-" + "".join(_rng.choices("0123456789abcdef", k=8)), "EventTime": round(ts, 3), "EventName": name,
          "EventSource": "signin.amazonaws.com" if name == "ConsoleLogin" else "iam.amazonaws.com",
          "Username": _rng.choice(["alice", "bob", "ci-deployer", "root"]),
          "Resources": [{"ResourceType": "AWS::IAM::User", "ResourceName": _rng.choice(["alice", "ci-deployer", "prod-trail", "logs-bucket"])}]}
    if _rng.random() < 0.18:
        ev["ErrorCode"] = _rng.choice(["AccessDenied", "UnauthorizedOperation"])
    return ev


def _fw_event(i: int, ts: float) -> dict:
    action = _rng.choices(["allow", "deny", "drop"], [6, 3, 1])[0]
    return {"session_id": f"fw-{i:05d}", "ts": int(ts * 1000), "src_ip": f"10.0.{_rng.randint(0, 4)}.{_rng.randint(2, 250)}",
            "dst_ip": f"198.51.100.{_rng.randint(2, 250)}", "dst_port": _rng.choice([22, 443, 3389, 8080, 53]), "action": action,
            "rule": _rng.choice(["default-deny", "allow-web", "block-geo", "ids-signature-4412"]),
            "threat_level": 0 if action == "allow" else _rng.randint(1, 5)}


def _seed_feeds() -> None:
    now = time.time()
    feeds = STATE["feeds"]
    for i in range(48):
        feeds["okta"].append(_okta_event(i + 1, now - (48 - i) * 420))
    for i in range(34):
        feeds["aws"].append(_aws_event(i + 1, now - (34 - i) * 600))
    for i in range(60):
        feeds["firewall"].append(_fw_event(i + 1, now - (60 - i) * 300))
    # Real exports contain garbage: records the platform must park instead of failing the whole run.
    bad = [{"session_id": "fw-bad-1", "src_ip": "10.0.0.9", "action": "deny"},                       # no timestamp
           {"session_id": "fw-bad-2", "ts": "yesterday-ish", "src_ip": "10.0.0.7", "action": "drop"},   # unparseable timestamp
           {"session_id": "fw-bad-3", "ts": int(now * 1000), "src_ip": "10.0.0.8"}]                     # no action
    feeds["firewall"][10:10] = bad[:1]
    feeds["firewall"][30:30] = bad[1:2]
    feeds["firewall"][50:50] = bad[2:]


with _lock:
    _seed_feeds()


def emit(counts: dict) -> dict:
    """Append fresh records to the bundled feeds (so incremental ingestion has something new to find)."""
    made = {}
    with _lock:
        for name, fn, key in (("okta", _okta_event, "okta"), ("aws", _aws_event, "aws"), ("firewall", _fw_event, "firewall")):
            n = max(0, min(int(counts.get(name) or 0), 500))
            feed = STATE["feeds"][key]
            base = len(feed) + 1000
            for k in range(n):
                feed.append(fn(base + k, time.time() - (n - k) * 2))
            made[name] = n
    return made


def set_firewall_flaky(enabled: bool) -> None:
    STATE["firewall_flaky"] = bool(enabled)
    STATE["firewall_calls"] = 0


@router.get("/okta/api/v1/logs")
def okta_logs(request: Request, since: str | None = None, limit: int = 20, after: int = 0, authorization: str | None = Header(None)):
    if authorization != f"SSWS {OKTA_TOKEN}":
        return JSONResponse({"errorCode": "E0000011", "errorSummary": "Invalid token provided"}, status_code=401)
    limit = max(1, min(limit, 100))
    with _lock:
        rows = list(STATE["feeds"]["okta"])
    if since:
        rows = [r for r in rows if r["published"] >= since]
    rows.sort(key=lambda r: r["published"])
    page = rows[after:after + limit]
    headers = {}
    if after + limit < len(rows):
        headers["Link"] = f'<{request.url.include_query_params(after=after + limit, limit=limit)}>; rel="next"'
    return JSONResponse(page, headers=headers)


@router.get("/aws/cloudtrail/events")
async def aws_cloudtrail(request: Request, StartTime: float | None = None, MaxResults: int = 10, NextToken: str | None = None):
    from ..services import engine
    auth = request.headers.get("authorization", "")
    amz_date = request.headers.get("x-amz-date", "")
    try:
        cred_part = auth.split("Credential=")[1].split(",")[0]
        access_key, date, region, service, _ = cred_part.split("/")
        age = abs(datetime.now(timezone.utc) - datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
        expected = engine.sigv4_authorization(
            request.method, request.headers.get("host", ""), request.url.path, dict(request.query_params), await request.body(),
            AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, AWS_SERVICE, amz_date, request.headers.get("x-amz-security-token"))
        valid = (access_key == AWS_ACCESS_KEY and (region, service) == (AWS_REGION, AWS_SERVICE) and age < timedelta(minutes=15)
                 and hmac_equal(expected, auth))
    except (IndexError, ValueError):
        valid = False
    if not valid:
        return JSONResponse({"__type": "InvalidSignatureException", "message": "The request signature we calculated does not match"}, status_code=403)
    size = max(1, min(MaxResults, 50))
    with _lock:
        rows = [r for r in STATE["feeds"]["aws"] if StartTime is None or r["EventTime"] >= StartTime]
    rows.sort(key=lambda r: r["EventTime"])
    start = int(NextToken.split(":")[1]) if NextToken and NextToken.startswith("tok:") and NextToken.split(":")[1].isdigit() else 0
    body = {"Events": rows[start:start + size]}
    if start + size < len(rows):
        body["NextToken"] = f"tok:{start + size}"
    return body


def hmac_equal(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


@router.get("/firewall/api/logs")
def firewall_logs(since: int | None = None, offset: int = 0, limit: int = 25, x_api_key: str | None = Header(None)):
    if x_api_key != FIREWALL_KEY:
        return JSONResponse({"error": "invalid api key"}, status_code=401)
    with _lock:
        STATE["firewall_calls"] += 1
        flaky = STATE["firewall_flaky"] and STATE["firewall_calls"] % 3 == 0
        rows = list(STATE["feeds"]["firewall"])
    if flaky:
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "1"})
    if since is not None:  # incremental pull: only well-formed newer records (the garbage was already delivered once)
        rows = [r for r in rows if isinstance(r.get("ts"), int) and r["ts"] >= since]
    limit = max(1, min(limit, 100))
    return {"total": len(rows), "logs": rows[offset:offset + limit]}
