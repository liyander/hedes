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
        if method == "conversations.info":
            return {"ok": True, "channel": {"id": body["channel"], "name": getattr(self, "names", {}).get(body["channel"], "")}}
        if method == "conversations.replies":
            return {"ok": True, "messages": [{"ts": body["ts"], "user": "UBOT", "text": getattr(self, "parent", "")}]}
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
    monkeypatch.setattr(slack_bot, "missing_scopes", lambda token: {} if token == "xoxb-good" else {"im:history": "receiving direct messages"})
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
    dm(org, "UADMIN", f"`link {code}`")                                                # Slack sends it formatted as code
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
    # first the model forgets a required input: no buttons are offered, it is told what is missing and asks instead
    ping = {"tool": "notify.send_message", "label": "Ping ops", "inputs": {"channel": "ops", "text": "Ping from H.E.D.E.S"}}
    ai.script = [{"reply": "I can create that workflow.", "tool_calls": [{"tool": "create_workflow", "args": {
                      "name": "Ops ping", "steps": [{"tool": "notify.send_message", "inputs": {"text": "Ping"}}]}}]},
                 {"reply": "Which channel should I ping?"}]
    dm(org, "UADMIN", "create a workflow that pings ops in Notify", ts="1.1", thread_ts="1.1")
    asked = slack.posts()[-1]
    assert asked["text"] == "Which channel should I ping?" and not asked.get("blocks")
    assert "NOT proposed" in ai.prompts[-1] and "missing the required input 'channel'" in ai.prompts[-1]

    ai.script = [{"reply": "I can create that workflow.", "tool_calls": [{"tool": "create_workflow", "why": "You asked for it",
                  "args": {"description": "Notify the ops channel in Notify when run", "name": "Ops ping", "steps": [ping]}}]}]
    dm(org, "UADMIN", "the ops channel", ts="1.2", thread_ts="1.1")
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
    assert slack_bot.mrkdwn("visit the [Workflows](/#/workflows) page").endswith("/#/workflows|Workflows> page")


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



def test_missing_scopes_are_reported(server, slack, monkeypatch):
    monkeypatch.setattr(slack_bot, "LAST_EVENT", {})
    admin = login(server, "admin")
    v = admin.put("/api/v1/settings/slack", json={"enabled": True, "bot_token": "xoxb-old-scopes", "app_token": "xapp-1"}).json()
    assert v["missing_scopes"] == {"im:history": "receiving direct messages"} and v["last_event"] is None
    v = admin.put("/api/v1/settings/slack", json={"enabled": False, "bot_token": "xoxb-good"}).json()
    assert v["missing_scopes"] == {}


def test_scopes_are_read_from_slacks_response_header(monkeypatch):
    import httpx
    monkeypatch.setattr(slack_bot.httpx, "post", lambda *a, **k: httpx.Response(200, json={"ok": True},
                        headers={"x-oauth-scopes": "chat:write,chat:write.public"}))
    assert set(slack_bot.NEEDED_SCOPES) - set(slack_bot.missing_scopes("xoxb-x")) == {"chat:write"}


def test_a_replaced_connection_cannot_overwrite_the_new_ones_status(monkeypatch):
    monkeypatch.setattr(slack_bot, "_BRIDGES", {})
    monkeypatch.setattr(slack_bot, "STATUS", {})
    old, new = slack_bot.Bridge("org-x", "fp-old"), slack_bot.Bridge("org-x", "fp-new")
    slack_bot._BRIDGES["org-x"] = new
    new._status("connected")
    old._status("stopped")                      # the replaced connection shutting down a moment later
    assert slack_bot.STATUS["org-x"]["state"] == "connected"


def test_the_bot_tokens_app_is_looked_up(monkeypatch):
    replies = {"auth.test": {"ok": True, "bot_id": "B1"}, "bots.info": {"ok": True, "bot": {"id": "B1", "app_id": "A_BOT"}}}
    monkeypatch.setattr(slack_bot, "call", lambda token, method, **b: replies[method])
    assert slack_bot.bot_app_id("xoxb-x") == "A_BOT"
    monkeypatch.setattr(slack_bot, "call", lambda token, method, **b: (_ for _ in ()).throw(slack_bot.SlackError("missing_scope")))
    assert slack_bot.bot_app_id("xoxb-x") == ""                        # without users:read the check is skipped, not fatal


def test_channel_mentions_reach_the_model_with_their_id(server, ai, slack):
    admin = login(server, "admin")
    org = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    admin.put("/api/v1/settings/slack", json={"enabled": True, "bot_token": "xoxb-good", "app_token": "xapp-1-good"})
    code = admin.post("/api/v1/settings/slack/link-code").json()["code"]
    dm(org, "UADMIN", f"link {code}", ts="5.0")
    ai.script = [{"reply": "Noted."}]
    dm(org, "UADMIN", "post failures to <#C0CDMS322QM|failures>", ts="5.1")
    assert "#failures (existing Slack channel, ID C0CDMS322QM)" in ai.prompts[-1]
    # Slack often sends a mention without its name: the name is looked up, so the model never sees a bare ID
    slack.names = {"C0CDMS322QM": "failures"}
    ai.script = [{"reply": "I'll post alerts to C0CDMS322QM.", "tool_calls": [{"tool": "set_alert_channel", "args": {"channel": "C0CDMS322QM"}}]}]
    dm(org, "UADMIN", "send all failures to <#C0CDMS322QM|>", ts="5.2")
    assert "#failures (existing Slack channel, ID C0CDMS322QM)" in ai.prompts[-1]
    reply, card = slack.posts()[-2:]
    assert reply["text"].endswith("<#C0CDMS322QM>.")                                    # Slack renders it as #failures
    assert "<#C0CDMS322QM> (existing channel)" in card["blocks"][0]["text"]["text"]
    admin.put("/api/v1/settings/slack", json={"enabled": False})


def test_alerts_are_posted_to_the_alert_channel(server, slack):
    admin = login(server, "admin")
    assert admin.put("/api/v1/settings/slack", json={"enabled": True, "bot_token": "xoxb-good", "app_token": "xapp-1-good",
                                                     "alert_channel": "#failures"}).status_code == 422
    v = admin.put("/api/v1/settings/slack", json={"enabled": True, "alert_channel": "C0CDMS322QM"}).json()
    assert v["alert_channel"] == "C0CDMS322QM"
    with session_scope() as db:
        org = db.get(Organization, admin.get("/api/v1/auth/me").json()["organization"]["id"])
        assert slack_bot.post_alert(db, org, {"kind": "execution_failure", "severity": "high", "message": "github.list_issues failed: 401"}) == "sent"
    post = slack.posts()[-1]
    assert post["channel"] == "C0CDMS322QM" and "github.list_issues failed: 401" in post["text"]
    admin.put("/api/v1/settings/slack", json={"enabled": False, "alert_channel": ""})


def channel_msg(org_id, user, text, ts, thread_ts=None, parent_user=None):
    ev = {"type": "message", "channel_type": "channel", "channel": "CFAIL", "user": user, "text": text, "ts": ts}
    if thread_ts:
        ev.update(thread_ts=thread_ts, parent_user_id=parent_user)
    slack_bot.handle_envelope(org_id, {"type": "events_api", "envelope_id": "e", "payload": {"event": ev}})


def test_replies_under_an_alert_discuss_it_and_apply_the_fix(server, ai, slack):
    admin = login(server, "admin")
    org = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    admin.put("/api/v1/settings/slack", json={"enabled": True, "bot_token": "xoxb-good", "app_token": "xapp-1-good"})
    dm(org, "UADMIN", f"link {admin.post('/api/v1/settings/slack/link-code').json()['code']}", ts="6.0")
    graph = {"trigger": {"type": "manual"}, "nodes": [{"id": "ping", "tool": "notify.send_message", "inputs": {"channel": "ops"}}]}
    wf = admin.post("/api/v1/workflows", json={"name": "Broken ping", "graph": graph}).json()
    assert not wf["validation"]["valid"]                                             # text is missing

    posted = len(slack.posts())
    channel_msg(org, "UADMIN", "unrelated chatter", ts="7.0")                       # channel talk is not for the bot
    channel_msg(org, "UADMIN", "a reply under someone else's message", ts="7.2", thread_ts="7.1", parent_user="UOTHER")
    assert len(slack.posts()) == posted

    slack.parent = "H.E.D.E.S Alert: workflow Broken ping failed: inputs: 'text' is a required property"
    ai.script = [{"reply": "Broken ping is missing its text.", "tool_calls": [{"tool": "update_workflow", "args": {
        "workflow_id": wf["id"], "steps": {"ping": {"inputs": {"text": "Ping from H.E.D.E.S"}}}}}]}]
    channel_msg(org, "UADMIN", "fix this using the text Ping from H.E.D.E.S", ts="8.1", thread_ts="8.0", parent_user="UBOT")
    assert "Broken ping failed" in ai.prompts[-1] and "untrusted_document" in ai.prompts[-1]
    reply, card = slack.posts()[-2:]
    assert reply["channel"] == "CFAIL" and reply["thread_ts"] == "8.0" and card["blocks"][1]["elements"][0]["action_id"] == "hedes_approve"

    ai.script = []
    slack_bot.handle_envelope(org, {"type": "interactive", "payload": {
        "type": "block_actions", "user": {"id": "UADMIN"}, "actions": [{"action_id": "hedes_approve", "value": card["blocks"][1]["elements"][0]["value"]}],
        "container": {"channel_id": "CFAIL", "message_ts": "8.2"}, "message": {"ts": "8.2", "thread_ts": "8.0"}}})
    after = admin.get(f"/api/v1/workflows/{wf['id']}").json()
    assert after["graph"]["nodes"][0]["inputs"] == {"channel": "ops", "text": "Ping from H.E.D.E.S"} and after["validation"]["valid"]

    ai.script = [{"reply": "It is fixed now."}]
    channel_msg(org, "UADMIN", "thanks, is it valid now?", ts="8.3", thread_ts="8.0", parent_user="UBOT")  # the thread continues
    assert slack.posts()[-1]["text"] == "It is fixed now."

    # "run and test it": a real run whose result (each step and what it returned) comes back to the model
    def approve(card, ts):
        slack_bot.handle_envelope(org, {"type": "interactive", "payload": {
            "type": "block_actions", "user": {"id": "UADMIN"}, "actions": [{"action_id": "hedes_approve", "value": card["blocks"][1]["elements"][0]["value"]}],
            "container": {"channel_id": "CFAIL", "message_ts": ts}, "message": {"ts": ts, "thread_ts": "8.0"}}})
    ai.script = [{"reply": "Running it now.", "tool_calls": [{"tool": "run_workflow", "args": {"workflow_id": wf["id"], "dry_run": False}}]}]
    channel_msg(org, "UADMIN", "run and test it", ts="8.4", thread_ts="8.0", parent_user="UBOT")
    ai.script = [{"reply": "The run succeeded: step ping delivered the message."}]
    approve(slack.posts()[-1], "8.5")
    # earlier tests may have broken the Notify integration on purpose, so the run's outcome varies: what matters is
    # that the model sees each step, what it sent and what came back
    assert '"dry_run": false' in ai.prompts[-1] and '"node": "ping"' in ai.prompts[-1] and '"text": "Ping from H.E.D.E.S"' in ai.prompts[-1]
    assert slack.posts()[-1]["text"] == "The run succeeded: step ping delivered the message."

    ai.script = [{"reply": "Activating it.", "tool_calls": [{"tool": "set_workflow_status", "args": {"workflow_id": wf["id"], "active": True}}]}]
    channel_msg(org, "UADMIN", "activate it", ts="8.6", thread_ts="8.0", parent_user="UBOT")
    ai.script = [{"reply": "It is active."}]
    approve(slack.posts()[-1], "8.7")
    assert admin.get(f"/api/v1/workflows/{wf['id']}").json()["status"] == "active"
    admin.post(f"/api/v1/workflows/{wf['id']}/stop")
    admin.put("/api/v1/settings/slack", json={"enabled": False})
