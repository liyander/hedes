"""End-to-end: the judge demo, run against a live server with the bundled mock APIs and the offline AI model."""
import hashlib
import hmac
import json
import time

import httpx

from conftest import login


def wait(fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.25)
    raise AssertionError("timed out")


def test_full_lifecycle(server, builder, admin, owner):
    # 1. Conversation without documentation -> the AI asks a follow-up question.
    s = builder.post("/api/v1/ai/sessions", json={"message": "I want to connect another task-management service and create tasks automatically."}).json()
    assert not s["requirements"]["ready"] and s["requirements"]["missing"]

    # Malformed documentation -> clear error.
    r = builder.post(f"/api/v1/ai/sessions/{s['id']}/documents", json={"name": "bad.json", "content": "{nope"})
    assert r.status_code == 422

    # 2. Upload the never-seen TaskFlow OpenAPI document.
    spec = httpx.get(server + "/mock/taskflow/openapi.json").text
    s = builder.post(f"/api/v1/ai/sessions/{s['id']}/documents", json={"name": "taskflow.json", "content": spec}).json()
    assert s["requirements"]["ready"], s["requirements"]
    assert any(o["operation_id"] == "create_task" for o in s["requirements"]["operations"])

    # 3. Generate: discover -> config -> validate -> security -> mock tests -> submit.
    #    Nothing is written to the database until the user confirms the creation plan.
    assert builder.post(f"/api/v1/ai/sessions/{s['id']}/generate", json={}).status_code == 409
    plan = builder.get(f"/api/v1/ai/sessions/{s['id']}/plan").json()
    assert [i["slug"] for i in plan["items"]] == ["taskflow"] and "integrations" in plan["items"][0]["tables"]
    builder.post(f"/api/v1/ai/sessions/{s['id']}/generate", json={"confirm": plan["fingerprint"]})
    s = wait(lambda: (x := builder.get(f"/api/v1/ai/sessions/{s['id']}").json())["status"] in ("ready", "failed") and x)
    assert s["status"] == "ready", s["stages"]
    item = s["integrations"][0]
    assert item["slug"] == "taskflow" and item["version_status"] == "pending_approval"
    assert item["tests"]["failed"] == 0
    approval_id = item["approval_id"]

    # 4. A user cannot approve; approval needs the credential.
    assert builder.post(f"/api/v1/admin/approvals/{approval_id}/approve", json={}).status_code == 403
    r = admin.post(f"/api/v1/admin/approvals/{approval_id}/approve", json={})
    assert r.status_code == 409 and "taskflow" in r.json()["detail"]

    # 5. The user connects the credential (stored in the vault, never returned).
    c = builder.post("/api/v1/connections", json={"name": "taskflow", "auth_type": "bearer", "token": "taskflow-demo-token"}).json()
    assert "taskflow-demo-token" not in json.dumps(builder.get("/api/v1/connections").json())
    assert c["hint"].startswith("••••")

    # A draft cannot be edited once submitted.
    assert builder.put(f"/api/v1/versions/{item['version_id']}", json={"config": item["config"]}).status_code == 409

    r = admin.post(f"/api/v1/admin/approvals/{approval_id}/approve", json={"comment": "Looks good"})
    assert r.status_code == 200, r.text

    # 6. Execute the newly published tool through the generic engine.
    tools = {t["name"]: t for t in builder.get("/api/v1/tools").json()}
    tool = tools["taskflow.create_task"]
    ex = builder.post(f"/api/v1/tools/{tool['id']}/execute", json={"inputs": {"title": "Rotate keys", "assignee": "Alex"}}).json()
    assert ex["status"] == "succeeded", ex
    assert ex["output"]["assignee_user_name"] == "Alex"
    assert ex["request"]["headers"]["Authorization"] == "••••••"
    assert any(t["title"] == "Rotate keys" for t in builder.get("/api/v1/demo/state").json()["tasks"])

    # 7. Unauthorized role is denied.
    admin.patch(f"/api/v1/admin/tools/{tool['id']}", json={"allowed_roles": ["admin"]})
    assert builder.post(f"/api/v1/tools/{tool['id']}/execute", json={"inputs": {"title": "x"}}).status_code in (403, 404)
    admin.patch(f"/api/v1/admin/tools/{tool['id']}", json={"allowed_roles": ["user", "admin"]})

    # 8. Schema drift -> detection -> AI repair proposal -> admin review.
    admin.post("/api/v1/demo/drift", json={"enabled": True})
    for _ in range(2):
        ex = builder.post(f"/api/v1/tools/{tool['id']}/execute", json={"inputs": {"title": "After drift", "assignee": "Sam"}}).json()
        assert ex["error_class"] == "response_drift"
    rp = wait(lambda: next((r for r in builder.get("/api/v1/repairs").json() if r["status"] == "pending_approval"), None))
    assert rp["diagnosis"]["mapping_changes"] == {"assignee_user_name": "$.assignee.display_name"}

    # Rejecting the repair leaves the live version unchanged.
    integ_id = rp["integration_id"]
    live_before = builder.get(f"/api/v1/integrations/{integ_id}").json()["published_version"]
    assert admin.post(f"/api/v1/admin/approvals/{rp['approval_request_id']}/reject", json={"comment": ""}).status_code == 422
    admin.post(f"/api/v1/admin/approvals/{rp['approval_request_id']}/reject", json={"comment": "Not yet"})
    assert builder.get(f"/api/v1/integrations/{integ_id}").json()["published_version"] == live_before

    # A fresh failure proposes again; this time the admin approves and the tool works.
    for _ in range(2):
        builder.post(f"/api/v1/tools/{tool['id']}/execute", json={"inputs": {"title": "Again", "assignee": "Sam"}})
    rp = wait(lambda: next((r for r in builder.get("/api/v1/repairs").json() if r["status"] == "pending_approval"), None))
    assert admin.post(f"/api/v1/admin/approvals/{rp['approval_request_id']}/approve", json={}).status_code == 200
    new_tool = {t["name"]: t for t in builder.get("/api/v1/tools").json()}["taskflow.create_task"]
    ex = builder.post(f"/api/v1/tools/{new_tool['id']}/execute", json={"inputs": {"title": "Healed", "assignee": "Sam"}}).json()
    assert ex["status"] == "succeeded" and ex["output"]["assignee_user_name"] == "Sam"
    # The superseded tool is no longer executable.
    assert builder.post(f"/api/v1/tools/{tool['id']}/execute", json={"inputs": {"title": "x"}}).status_code == 403

    # 9. Workflow: signed webhook -> condition -> create task -> notify with the task id.
    graph = {"trigger": {"type": "webhook", "sample": {"title": "XSS", "labels": ["security"]}}, "nodes": [
        {"id": "task", "tool": "taskflow.create_task", "inputs": {"title": "${trigger.title}", "priority": "high"},
         "when": [{"left": "${trigger.labels}", "op": "contains", "right": "security"}]},
        {"id": "ping", "tool": "notify.send_message", "depends_on": ["task"],
         "inputs": {"channel": "security-team", "text": "Task ${steps.task.output.id} created: ${trigger.title}", "severity": "critical"}}]}
    wf = builder.post("/api/v1/workflows", json={"name": "Security triage", "graph": graph}).json()
    assert wf["validation"]["valid"], wf["validation"]
    dry = builder.post(f"/api/v1/workflows/{wf['id']}/execute", json={"dry_run": True}).json()
    assert dry["status"] == "succeeded" and all(st["status"] == "planned" for st in dry["steps"])
    act = builder.post(f"/api/v1/workflows/{wf['id']}/activate").json()
    secret = act["webhook_secret"]
    body = json.dumps({"title": "SQLi in login", "labels": ["security"]}).encode()
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    hdr = {"X-Integra-Timestamp": ts, "X-Integra-Signature": sig, "X-Integra-Delivery": "d-1", "Content-Type": "application/json"}
    assert httpx.post(f"{server}/api/v1/hooks/{wf['id']}", content=body, headers=hdr).status_code == 202
    assert httpx.post(f"{server}/api/v1/hooks/{wf['id']}", content=body, headers=hdr).status_code == 409  # replay
    assert httpx.post(f"{server}/api/v1/hooks/{wf['id']}", content=body, headers={**hdr, "X-Integra-Signature": "sha256=00", "X-Integra-Delivery": "d-2"}).status_code == 401
    run = wait(lambda: next((r for r in builder.get(f"/api/v1/workflows/{wf['id']}/runs").json() if r["trigger"] == "webhook" and r["status"] != "running"), None))
    assert run["status"] == "succeeded", run
    msgs = builder.get("/api/v1/demo/state").json()["messages"]
    assert any("SQLi in login" in m["text"] and "Task " in m["text"] for m in msgs)

    # 10. MCP: tools are invisible until an admin exposes them; calls run through the engine.
    token = builder.post("/api/v1/tokens", json={"name": "agent"}).json()["token"]
    mcp = httpx.Client(base_url=server, headers={"Authorization": f"Bearer {token}"})
    rpc = lambda method, params=None, i=1: mcp.post("/mcp", json={"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}).json()
    assert rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"] == "2025-06-18"
    assert rpc("tools/list")["result"]["tools"] == []
    admin.patch(f"/api/v1/admin/tools/{new_tool['id']}", json={"mcp_enabled": True})
    names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
    assert names == ["taskflow_create_task"]
    res = rpc("tools/call", {"name": "taskflow_create_task", "arguments": {"title": "From an agent"}})["result"]
    assert res["isError"] is False
    # API tokens can never approve.
    assert mcp.post(f"/api/v1/admin/approvals/{approval_id}/approve", json={}).status_code == 403

    # 11. Audit trail recorded the governance actions.
    actions = {a["action"] for a in admin.get("/api/v1/audit").json()}
    assert {"approval.approve", "integration.publish", "repair.propose", "approval.rejected", "workflow.run"} <= actions
    admin.post("/api/v1/demo/drift", json={"enabled": False})


def test_blocked_destination_and_unpublished_draft(builder):
    cfg = {"schema_version": "1.0", "integration": {"id": "metadata-probe", "name": "Probe", "version": "1.0.0", "transport": "http",
           "base_url": "http://169.254.169.254", "authentication": {"type": "none"},
           "operations": [{"id": "read", "method": "GET", "path": "/latest/meta-data", "input_schema": {"type": "object"}, "response": {}}]}}
    r = builder.post("/api/v1/integrations", json={"config": cfg}).json()
    assert r["version"]["risk"]["blocking"]
    iid = r["integration"]["id"]
    assert builder.post(f"/api/v1/integrations/{iid}/submit", json={}).status_code == 409
    assert "metadata-probe.read" not in {t["name"] for t in builder.get("/api/v1/tools").json()}


def test_ai_provider_fallback(owner, builder):
    # A broken provider is preferred for every task; the gateway must fall back to the built-in model.
    r = owner.post("/api/v1/ai/providers", json={"name": "Broken", "provider_type": "openai_compatible",
                                                 "base_url": "http://127.0.0.1:9/v1", "api_key": "k", "model_id": "nope"})
    assert r.status_code == 200
    s = builder.post("/api/v1/ai/sessions", json={"message": "Connect Slack and post a message to #alerts"}).json()
    assert s["requirements"]["ready"]
    assert "Built-in" in s["messages"][-1]["meta"]["model"]
    usage = owner.get("/api/v1/ai/usage").json()
    assert any(not x["success"] for x in usage["recent"])
    owner.delete(f"/api/v1/ai/providers/{r.json()['id']}")


def test_jira_requires_site_url(builder):
    s = builder.post("/api/v1/ai/sessions", json={"message": "Whenever a GitHub issue is labeled security, create a Jira ticket and notify our Slack channel #sec"}).json()
    req = s["requirements"]
    assert {x["slug"] for x in req["services"]} == {"github", "jira", "slack"}
    assert any("Jira" in q for q in req["missing"]) and not req["ready"]
    s = builder.post(f"/api/v1/ai/sessions/{s['id']}/messages", json={"content": "It's https://acme.atlassian.net, project SEC"}).json()
    assert s["requirements"]["ready"], s["requirements"]
    assert s["requirements"]["triggers"][0]["condition"] == "labels contains security"


def test_csrf_header_required_for_cookie_sessions(server):
    c = login(server, "builder")
    c.headers.pop("X-Requested-With")
    assert c.post("/api/v1/ai/sessions", json={}).status_code == 403
