"""Setup checklists researched from vendor documentation, and visible fallback when the AI provider fails."""
import json

import pytest

from app.core.db import session_scope
from app.models import Alert
from app.services import agents, catalog, docs_search, openapi
from app.services.ai_gateway import AIResult, Gateway
from conftest import login

SLACK_PAGE = """<html><head><title>chat.postMessage</title><script>var x=1</script></head><body><nav>menu</nav>
<h1>chat.postMessage</h1><p>Scopes</p><p>Bot token: chat:write</p><p>User token: chat:write</p>
<h2>Errors</h2><p>missing_scope</p><p>The token used is not granted the specific scope permissions required.</p>
<p>not_in_channel</p><p>Cannot post user messages to a channel they are not in.</p></body></html>"""


@pytest.fixture
def pages(monkeypatch):
    fetched = []

    def fake_get(url, limit):
        fetched.append(url)
        if url == "https://api.slack.com/methods/chat.postMessage":
            return SLACK_PAGE.encode()
        raise docs_search.DocsError(f"HTTP 404 from {url}")

    monkeypatch.setattr(docs_search, "_get", fake_get)
    return fetched


def test_openapi_keeps_each_operations_scopes_and_reference_page():
    spec = {"openapi": "3.0.0", "info": {"title": "X"}, "externalDocs": {"url": "https://x.dev/docs"},
            "security": [{"oauth": ["read:all"]}],
            "paths": {"/msg": {"post": {"operationId": "send", "security": [{"oauth": ["chat:write", "chat:write.public"]}],
                                        "externalDocs": {"url": "https://x.dev/send"}, "responses": {"200": {"description": "ok"}}},
                               "get": {"operationId": "list", "responses": {"200": {"description": "ok"}}}}}}
    cat = openapi.parse_openapi(json.dumps(spec))
    ops = {o["id"]: o for o in cat["operations"]}
    assert ops["send"]["scopes"] == ["chat:write", "chat:write.public"] and ops["send"]["docs_url"] == "https://x.dev/send"
    assert ops["list"]["scopes"] == ["read:all"] and cat["docs_url"] == "https://x.dev/docs"


def test_setup_excerpt_keeps_what_matters():
    text = "\n".join(["intro text", "Scopes", "Bot token: chat:write", "filler line", "not_in_channel",
                      "Cannot post to a channel they are not in."])
    ex = docs_search.setup_excerpt(text)
    assert "chat:write" in ex and "not_in_channel" in ex and "Cannot post" in ex


def test_slack_checklist_from_the_vendor_page(server, pages):
    org = login(server, "admin").get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        guide, model = agents.setup_guide(Gateway(db, org), "slack", catalog.known_services()["slack"], [])
    assert guide["sources"] == ["https://api.slack.com/methods/chat.postMessage"]
    assert any("conversations.list" in u for u in guide["unread"])                 # an unreadable page is reported, not hidden
    assert "chat:write" in guide["permissions"] and "xoxb-" in " ".join(guide["steps"]) and "/invite" in " ".join(guide["steps"])
    assert {"missing_scope", "not_in_channel"} <= {e["code"] for e in guide["common_errors"]}


def test_model_output_cannot_drop_spec_scopes_or_cite_unread_pages(server, pages, monkeypatch):
    org = login(server, "admin").get("/api/v1/auth/me").json()["organization"]["id"]

    def fake_structured(self, task, system, prompt, schema, local, validate_extra=None):
        assert "<untrusted_document" in prompt and "chat:write" in prompt   # the vendor page reached the model, marked untrusted
        return AIResult(data={"credential": "Bot token", "permissions": ["channels:read"], "steps": ["Install the app"],
                              "common_errors": [], "sources": ["https://evil.example/made-up"]}, model="stub")

    monkeypatch.setattr(Gateway, "structured", fake_structured)
    doc = {"id": "d", "name": "slack", "catalog": {"title": "Slack", "operations": [
        {"id": "post_message", "method": "POST", "path": "/chat.postMessage", "scopes": ["chat:write"], "docs_url": ""}]}}
    with session_scope() as db:
        guide, model = agents.setup_guide(Gateway(db, org), "slack", catalog.known_services()["slack"], [doc])
    assert model == "stub" and guide["permissions"][0] == "chat:write" and "channels:read" in guide["permissions"]
    assert guide["sources"] == ["https://api.slack.com/methods/chat.postMessage"]      # the invented source was dropped


def test_failed_ai_provider_is_never_silent(server):
    owner = login(server, "owner")
    pr = owner.post("/api/v1/ai/providers", json={"name": "QuotaGone", "provider_type": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
                                                  "api_key": "k", "model_id": "m", "purposes": ["general"]}).json()
    org = owner.get("/api/v1/auth/me").json()["organization"]["id"]
    try:
        with session_scope() as db:
            gw = Gateway(db, org)
            gw.structured("requirements", "s", "p", {"type": "object"}, local=lambda: {})
            assert gw.fallbacks and "QuotaGone" in gw.fallback_note() and "built-in rules" in gw.fallback_note()
            assert db.query(Alert).filter_by(organization_id=org, kind="ai_provider").count() >= 1
        b = login(server, "builder")
        s = b.post("/api/v1/ai/sessions", json={"message": "Post a message to our Slack channel #deploys when I ask."}).json()
        assert s["messages"][-1]["content"].startswith("**Note:** your AI provider (QuotaGone")
    finally:
        owner.delete(f"/api/v1/ai/providers/{pr['id']}")


def test_build_pipeline_researches_setup(server):
    import time
    b = login(server, "builder")
    s = b.post("/api/v1/ai/sessions", json={"message": "Post a message to our Slack channel #deploys when I ask."}).json()
    assert s["requirements"]["ready"], s["requirements"]
    plan = b.get(f"/api/v1/ai/sessions/{s['id']}/plan").json()
    b.post(f"/api/v1/ai/sessions/{s['id']}/generate", json={"confirm": plan["fingerprint"]})
    for _ in range(80):
        s = b.get(f"/api/v1/ai/sessions/{s['id']}").json()
        if s["status"] in ("ready", "failed"):
            break
        time.sleep(0.25)
    assert s["stages"]["RESEARCH_SETUP"]["status"] == "done"
    item = s["integrations"][0]
    assert item["setup"] and "chat:write" in item["setup"]["permissions"]
    assert "Before slack works" in s["messages"][-1]["content"] and "missing_scope" in s["messages"][-1]["content"]
