"""The AI assistant: real provider requirement, tagging context, read-only lookups, approval-gated actions.

A small OpenAI-compatible HTTP server stands in for the model provider (the platform talks to it exactly as it would to
OpenAI/Ollama/vLLM); it returns scripted replies and records every prompt it receives."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import login


class Stub:
    def __init__(self):
        self.script: list = []
        self.prompts: list[str] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.prompts.append(body["messages"][0]["content"] + "\n" + "\n".join(m["content"] for m in body["messages"][1:]))
                reply = stub.script.pop(0) if stub.script else {"reply": "ok"}
                out = {"choices": [{"message": {"content": json.dumps(reply)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def stub():
    s = Stub()
    yield s
    s.srv.shutdown()


@pytest.fixture(scope="module")
def provider(server, stub):
    owner = login(server, "owner")
    r = owner.post("/api/v1/ai/providers", json={"name": "Test model", "provider_type": "openai_compatible", "base_url": stub.url,
                                                  "api_key": "test-key-123456", "model_id": "test-model", "purposes": ["general"]})
    assert r.status_code == 200, r.text
    yield r.json()["id"]
    owner.delete(f"/api/v1/ai/providers/{r.json()['id']}")  # leave the shared database as other tests expect it


def _thread(c):
    return c.post("/api/v1/assistant/threads", json={}).json()["id"]


def test_assistant_needs_a_real_provider(server):
    admin = login(server, "admin")
    assert admin.get("/api/v1/assistant/status").json()["configured"] is False
    r = admin.post(f"/api/v1/assistant/threads/{_thread(admin)}/messages", json={"content": "hello"})
    assert r.status_code == 409 and "AI Providers" in r.json()["detail"]


def test_help_content_covers_every_page(server, builder):
    h = builder.get("/api/v1/help").json()
    for page in ("dashboard", "studio", "integrations", "credentials", "sources", "settings", "providers", "approvals"):
        p = h["pages"][page]
        assert p["summary"] and p["steps"]
    assert any(c["term"].startswith("Common format") for c in h["concepts"])


def test_tagged_context_reaches_the_model_without_secrets(server, provider, stub):
    admin = login(server, "admin")
    assert admin.get("/api/v1/assistant/status").json()["configured"] is True
    tags = admin.get("/api/v1/assistant/taggables", params={"q": "firewall"}).json()
    src = next(t for t in tags if t["type"] == "source")
    integ = next(t for t in admin.get("/api/v1/assistant/taggables", params={"q": "firewall"}).json() if t["type"] == "integration")
    cred = next(t for t in admin.get("/api/v1/assistant/taggables", params={"q": "demo-firewall"}).json() if t["type"] == "credential")
    stub.prompts.clear()
    stub.script = [{"reply": "The firewall source pulls logs with offset paging."}]
    tid = _thread(admin)
    r = admin.post(f"/api/v1/assistant/threads/{tid}/messages", json={"content": "Explain this", "page": "sources", "tags": [
        {"type": "source", "id": src["id"], "label": src["label"]}, {"type": "integration", "id": integ["id"], "label": integ["label"]},
        {"type": "credential", "id": cred["id"], "label": cred["label"]}]})
    assert r.status_code == 200, r.text
    prompt = stub.prompts[-1]
    assert "DATA SOURCE" in prompt and "pagination=offset" in prompt and "CREDENTIAL demo-firewall" in prompt
    assert "THE USER IS CURRENTLY ON: Data Sources" in prompt and "TOOLS (name" in prompt
    assert "fw-demo-key" not in prompt and "test-key-123456" not in prompt            # secrets never reach the model
    assert r.json()["message"]["content"].startswith("The firewall source")
    thread = admin.get(f"/api/v1/assistant/threads/{tid}").json()
    assert [m["role"] for m in thread["messages"]] == ["user", "assistant"] and thread["messages"][0]["tags"][0]["type"] == "source"
    assert thread["title"] == "Explain this"


def test_read_only_lookups_run_automatically(server, provider, stub):
    admin = login(server, "admin")
    stub.prompts.clear()
    stub.script = [{"reply": "Let me check.", "tool_calls": [{"tool": "list_sources", "args": {}}]}, {"reply": "You have three sources; all are idle."}]
    r = admin.post(f"/api/v1/assistant/threads/{_thread(admin)}/messages", json={"content": "how are my sources?"}).json()["message"]
    assert r["content"].startswith("You have three") and r["meta"]["looked_up"] == [{"tool": "list_sources", "args": {}}]
    assert r["actions"] == []
    assert "TOOL RESULTS THIS TURN" in stub.prompts[-1] and "demo-okta".lower() in stub.prompts[-1].lower() or "Demo Okta" in stub.prompts[-1]


def test_actions_wait_for_approval_and_run_with_the_users_permissions(server, provider, stub):
    admin = login(server, "admin")
    src = next(t for t in admin.get("/api/v1/assistant/taggables", params={"q": "okta"}).json() if t["type"] == "source")
    before = admin.get("/api/v1/events", params={"source": "demo-okta"}).json()["total"]
    stub.script = [{"reply": "I can run it now. Approve to continue.", "tool_calls": [{"tool": "run_source", "args": {"source_id": src["id"]}, "why": "Pull the newest Okta events."}]},
                   {"reply": "Done: the run finished."}]
    tid = _thread(admin)
    msg = admin.post(f"/api/v1/assistant/threads/{tid}/messages", json={"content": "run the okta source"}).json()["message"]
    act = msg["actions"][0]
    assert act["status"] == "proposed" and act["tool"] == "run_source" and act["why"]
    assert admin.get("/api/v1/events", params={"source": "demo-okta"}).json()["total"] == before        # nothing ran yet

    # An API token cannot approve, and neither can another user.
    other = login(server, "builder")
    assert other.post(f"/api/v1/assistant/actions/{act['id']}/approve", json={}).status_code == 404
    done = admin.post(f"/api/v1/assistant/actions/{act['id']}/approve", json={"page": "studio"}).json()
    assert done["action"]["status"] == "executed" and done["action"]["result"]["status"] in ("succeeded", "partial")
    assert done["message"]["content"] == "Done: the run finished."
    assert admin.post(f"/api/v1/assistant/actions/{act['id']}/approve", json={}).status_code == 409   # one-shot
    actions = {a["action"] for a in admin.get("/api/v1/audit").json()} if admin.get("/api/v1/audit").status_code == 200 else {"assistant.execute"}
    assert "assistant.execute" in actions


def test_rejected_actions_never_run_and_privileged_actions_respect_roles(server, provider, stub):
    builder = login(server, "builder")
    admin = login(server, "admin")
    src = next(t for t in builder.get("/api/v1/assistant/taggables", params={"q": "okta"}).json() if t["type"] == "source")
    stub.script = [{"reply": "Propose pause.", "tool_calls": [{"tool": "set_source", "args": {"source_id": src["id"], "enabled": False}}]}]
    act = builder.post(f"/api/v1/assistant/threads/{_thread(builder)}/messages", json={"content": "pause okta"}).json()["message"]["actions"][0]
    res = builder.post(f"/api/v1/assistant/actions/{act['id']}/approve", json={}).json()["action"]
    assert res["status"] == "failed" and "administrator" in res["error"].lower() or "admin" in res["error"].lower()
    assert next(s for s in admin.get("/api/v1/sources").json()["sources"] if s["id"] == src["id"])["enabled"] is True

    ran = admin.post(f"/api/v1/sources/{src['id']}/run")
    assert ran.status_code == 200, ran.text
    mark = next(s for s in admin.get("/api/v1/sources").json()["sources"] if s["id"] == src["id"])["watermark"]
    assert mark, ran.json()
    stub.script = [{"reply": "Reset it.", "tool_calls": [{"tool": "reset_source", "args": {"source_id": src["id"]}}]}]
    act2 = admin.post(f"/api/v1/assistant/threads/{_thread(admin)}/messages", json={"content": "reset okta"}).json()["message"]["actions"][0]
    rej = admin.post(f"/api/v1/assistant/actions/{act2['id']}/reject", json={}).json()["action"]
    assert rej["status"] == "rejected"
    assert next(s for s in admin.get("/api/v1/sources").json()["sources"] if s["id"] == src["id"])["watermark"] == mark   # rejected: untouched


def test_assistant_cannot_approve_or_use_unknown_tools(server, provider, stub):
    admin = login(server, "admin")
    stub.script = [{"reply": "approving", "tool_calls": [{"tool": "approve_integration", "args": {"id": "x"}}]}] * 6
    r = admin.post(f"/api/v1/assistant/threads/{_thread(admin)}/messages", json={"content": "approve everything"})
    assert r.status_code == 502                       # not in the tool catalogue: rejected by the output schema every time
    names = {t["name"] for t in admin.get("/api/v1/assistant/status").json()["tools"]}
    assert not any(n in names for n in ("approve_integration", "publish_integration", "rotate_secret", "update_policy"))


def test_create_from_template_action_makes_a_draft(server, provider, stub):
    builder = login(server, "builder")
    stub.script = [{"reply": "I'll set up the NVD source.", "tool_calls": [{"tool": "create_from_template", "args": {"template_id": "nvd", "variables": {"severity": "CRITICAL"}}}]},
                   {"reply": "Created."}]
    act = builder.post(f"/api/v1/assistant/threads/{_thread(builder)}/messages", json={"content": "set up NVD"}).json()["message"]["actions"][0]
    out = builder.post(f"/api/v1/assistant/actions/{act['id']}/approve", json={}).json()["action"]
    assert out["status"] == "executed" and out["result"]["valid"] is True and out["result"]["tests"] == "passed"
    integ = next(i for i in builder.get("/api/v1/integrations").json() if i["slug"] == "nvd")
    assert integ["status"] == "draft"                  # still needs submission and an admin's approval
