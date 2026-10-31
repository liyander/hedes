"""Slack as a channel to the AI assistant: linking, conversations, approval buttons, permissions."""
import pytest

from app.core.db import session_scope
from app.models import AssistantAction, Membership, Organization, User, Workflow
from app.services import slack_bot
from conftest import login
from test_zassistant import Stub


class FakeSlack:
    def __init__(self):
        self.calls = []

    def __call__(self, token, method, **body):
        self.calls.append({"token": token, "method": method, **body})
        if token == "xoxb-bad":
            raise slack_bot.SlackError("invalid_auth")
        if method == "auth.test":
            return {"ok": True, "team": "hedes", "user": "hedes_bot", "user_id": "UBOT"}
        return {"ok": True, "ts": "9.9"}

    def posts(self):
        return [c for c in self.calls if c["method"] == "chat.postMessage"]


@pytest.fixture(scope="module")
def ai(server):
    stub = Stub()
    owner = login(server, "owner")
    pr = owner.post("/api/v1/ai/providers", json={"name": "Slack test model", "provider_type": "openai_compatible", "base_url": stub.url,
                                                  "api_key": "test-key-123456", "model_id": "m", "purposes": ["general"]}).json()
    yield stub
    owner.delete(f"/api/v1/ai/providers/{pr['id']}")
    stub.srv.shutdown()


@pytest.fixture
def slack(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setattr(slack_bot, "call", fake)
    monkeypatch.setattr(slack_bot, "sync_bridges", lambda: None)  # no real WebSocket to Slack in tests
    return fake


def dm(org_id, user, text, ts="1.1", thread_ts=None):
    ev = {"type": "message", "channel_type": "im", "channel": "D1", "user": user, "text": text, "ts": ts}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    slack_bot.handle_envelope(org_id, {"type": "events_api", "envelope_id": "e", "payload": {"event": ev}})


def click(org_id, user, action_id, act_id):
    slack_bot.handle_envelope(org_id, {"type": "interactive", "payload": {
        "type": "block_actions", "user": {"id": user}, "actions": [{"action_id": action_id, "value": act_id}],
        "container": {"channel_id": "D1", "message_ts": "2.2"}, "message": {"ts": "2.2", "thread_ts": "1.1"}}})


def test_slack_channel_end_to_end(server, ai, slack):
    admin, builder = login(server, "admin"), login(server, "builder")
    org = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    assert admin.put("/api/v1/settings/slack", json={"enabled": True, "bot_token": "xoxp-user-token", "app_token": "xapp-1"}).status_code == 422
    assert admin.put("/api/v1/settings/slack", json={"enabled": True, "bot_token": "xoxb-bad", "app_token": "xapp-1"}).status_code == 422
    v = admin.put("/api/v1/settings/slack", json={"enabled": True, "bot_token": "xoxb-good", "app_token": "xapp-1-good"}).json()
    assert v["enabled"] and v["team"] == "hedes" and v["bot_name"] == "hedes_bot"
    assert builder.post("/api/v1/settings/slack/link-code").status_code == 403          # Slack access is for administrators

    dm(org, "UADMIN", "hello")
    assert "I don't know you yet" in slack.posts()[-1]["text"]
    code = admin.post("/api/v1/settings/slack/link-code").json()["code"]
    dm(org, "UADMIN", "link 000000" if code != "000000" else "link 111111")
    assert "invalid or expired" in slack.posts()[-1]["text"]
    dm(org, "UADMIN", f"link {code}")
    assert "Linked to H.E.D.E.S account *admin*" in slack.posts()[-1]["text"]
    assert any(l["username"] == "admin" for l in admin.get("/api/v1/settings/slack").json()["links"])

    # a question: the model looks up failures (read-only, runs automatically) and answers in the thread
    ai.script = [{"reply": "", "tool_calls": [{"tool": "list_failures", "args": {"limit": 3}}]},
                 {"reply": "Two runs failed: **notify.send_message** returned 401 - rotate the credential."}]
    dm(org, "UADMIN", "what failed recently?", ts="1.1")
    reply = slack.posts()[-1]
    assert reply["channel"] == "D1" and reply["thread_ts"] == "1.1" and reply["text"].startswith("Two runs failed: *notify.send_message*")
    assert "list_failures" in ai.prompts[-1] or "TOOL RESULTS" in ai.prompts[-1]

    # a change: the model proposes creating a workflow; nothing happens until the person approves in Slack
    before = 0
    with session_scope() as db:
        before = db.query(Workflow).filter_by(organization_id=org).count()
    ai.script = [{"reply": "I can create that workflow.", "tool_calls": [{"tool": "create_workflow", "why": "You asked for it",
                  "args": {"description": "Notify the ops channel in Notify when run", "name": "Ops ping"}}]}]
    dm(org, "UADMIN", "create a workflow that pings ops in Notify", ts="1.1", thread_ts="1.1")
    buttons = slack.posts()[-1]
    assert buttons["blocks"][1]["elements"][0]["action_id"] == "hedes_approve"
    act_id = buttons["blocks"][1]["elements"][0]["value"]
    with session_scope() as db:
        assert db.get(AssistantAction, act_id).status == "proposed" and db.query(Workflow).filter_by(organization_id=org).count() == before

    click(org, "USTRANGER", "hedes_approve", act_id)                                   # someone else cannot approve it
    assert "Only the person who asked" in slack.posts()[-1]["text"]
    ai.script = []
    click(org, "UADMIN", "hedes_approve", act_id)
    update = next(c for c in reversed(slack.calls) if c["method"] == "chat.update")
    assert "Done" in update["blocks"][0]["text"]["text"]
    with session_scope() as db:
        assert db.get(AssistantAction, act_id).status == "executed"
        assert db.query(Workflow).filter_by(organization_id=org, name="Ops ping").count() == 1

    # a person who is no longer an administrator loses Slack access
    with session_scope() as db:
        m = db.query(Membership).join(User, User.id == Membership.user_id).filter(User.username == "admin").one()
        m.role = "user"
    try:
        dm(org, "UADMIN", "hello again", ts="3.3")
        assert "I don't know you yet" in slack.posts()[-1]["text"]
    finally:
        with session_scope() as db:
            m = db.query(Membership).join(User, User.id == Membership.user_id).filter(User.username == "admin").one()
            m.role = "admin"
    admin.put("/api/v1/settings/slack", json={"enabled": False})


def test_markdown_becomes_slack_mrkdwn():
    out = slack_bot.mrkdwn("**Done**: see [the docs](https://x.io) and #/workflows/abc")
    assert out.startswith("*Done*: see <https://x.io|the docs> and <") and "/#/workflows/abc|open in H.E.D.E.S>" in out


def test_socket_mode_acknowledges_envelopes_and_reconnects(monkeypatch):
    import json
    import threading
    import time

    from websockets.sync.server import serve

    acks, connections, handled = [], [], []

    def slack_side(ws):
        connections.append(1)
        ws.send(json.dumps({"type": "hello"}))
        ws.send(json.dumps({"type": "events_api", "envelope_id": f"env-{len(connections)}",
                            "payload": {"event": {"type": "message", "channel_type": "im", "text": "hi"}}}))
        acks.append(json.loads(ws.recv(timeout=5))["envelope_id"])
        ws.send(json.dumps({"type": "disconnect", "reason": "refresh_requested"}))
        try:
            ws.recv(timeout=2)
        except Exception:
            pass

    server = serve(slack_side, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(slack_bot, "tokens", lambda db, org: ("xoxb-x", "xapp-x"))
    monkeypatch.setattr(slack_bot, "call", lambda token, method, **b: {"ok": True, "url": f"ws://127.0.0.1:{port}"})
    monkeypatch.setattr(slack_bot, "handle_envelope", lambda org_id, env: handled.append(env["envelope_id"]))
    with session_scope() as db:
        org_id = db.query(Organization).first().id
    bridge = slack_bot.Bridge(org_id, "fp")
    monkeypatch.setattr(bridge.stop, "wait", lambda t=None: bridge.stop.is_set())  # reconnect immediately in the test
    bridge.start()
    try:
        for _ in range(100):
            if len(acks) >= 2 and len(handled) >= 2:
                break
            time.sleep(0.05)
        assert acks[:2] == ["env-1", "env-2"] and handled[:2] == ["env-1", "env-2"] and len(connections) >= 2
    finally:
        bridge.stop.set()
        server.shutdown()
