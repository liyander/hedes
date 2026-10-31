"""WhatsApp chatbot: signed inbound webhook, menus, details and actions from chat (Meta API mocked)."""
import hashlib
import hmac
import json
import re
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

SENT: list[dict] = []
SECRET = "meta-app-secret-123"
PNID = "2065403522"
ME = "447700900456"


def _graph(req: httpx.Request) -> httpx.Response:
    if req.method == "GET":
        return httpx.Response(200, json={"display_phone_number": "+1 555-010-0200", "verified_name": "Bot Test"})
    body = json.loads(req.content)
    if body.get("status") != "read":
        SENT.append(body)
    return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(SENT)}"}]})


@pytest.fixture(scope="module")
def bot(server, admin):
    from app.services import whatsapp
    whatsapp.TRANSPORT = httpx.MockTransport(_graph)
    base = {"enabled": True, "phone_number_id": PNID, "access_token": "bot-token-123456"}
    assert admin.put("/api/v1/settings/whatsapp", json={**base, "chat_enabled": True}).status_code == 422  # needs the app secret
    r = admin.put("/api/v1/settings/whatsapp", json={**base, "chat_enabled": True, "app_secret": SECRET})
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["chat_enabled"] and cfg["has_app_secret"] and cfg["verify_token"] and SECRET not in r.text
    admin.delete("/api/v1/me/whatsapp")
    n = len(SENT)
    assert admin.post("/api/v1/me/whatsapp/code", json={"phone": "+" + ME}).status_code == 200
    code = re.search(r"\b(\d{6})\b", _wait(n)[-1]["text"]["body"]).group(1)
    assert admin.post("/api/v1/me/whatsapp/verify", json={"code": code}).json()["verified"]
    admin.patch("/api/v1/me/whatsapp", json={"enabled": False})  # chat only: keep alert pushes out of these tests
    yield {**cfg, "server": server}
    admin.put("/api/v1/settings/whatsapp", json={"enabled": False, "phone_number_id": PNID})
    whatsapp.TRANSPORT = None


def _wait(n: int, timeout: float = 15) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        if len(SENT) > n:
            time.sleep(0.15)
            return SENT[n:]
        time.sleep(0.05)
    return []


def _post(server: str, message: dict, sign: bool = True, secret: str = SECRET) -> httpx.Response:
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": PNID, "display_phone_number": "15550100200"},
        "messages": [message]}}]}]}).encode()
    headers = {"content-type": "application/json"}
    if sign:
        headers["x-hub-signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return httpx.post(f"{server}/api/v1/whatsapp/webhook", content=body, headers=headers)


def say(bot, text: str = "", tap: str = "", sender: str = ME) -> dict | None:
    msg = {"from": sender, "id": f"wamid.in.{uuid.uuid4().hex}", "timestamp": "1"}
    if tap:
        msg.update(type="interactive", interactive={"type": "list_reply" if ":list" in tap or tap == "menu" else "button_reply",
                                                    "list_reply" if ":list" in tap or tap == "menu" else "button_reply": {"id": tap, "title": "x"}})
    else:
        msg.update(type="text", text={"body": text})
    n = len(SENT)
    assert _post(bot["server"], msg).status_code == 200
    out = _wait(n)
    return out[-1] if out else None


def body_of(m: dict) -> str:
    return m["interactive"]["body"]["text"] if m["type"] == "interactive" else m["text"]["body"]


def ids_of(m: dict) -> list[str]:
    a = m["interactive"]["action"]
    return [r["id"] for s in a.get("sections", []) for r in s["rows"]] + [b["reply"]["id"] for b in a.get("buttons", [])]


def test_webhook_verification_and_signature(bot):
    s = bot["server"]
    ok = httpx.get(f"{s}/api/v1/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": bot["verify_token"], "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert httpx.get(f"{s}/api/v1/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"}).status_code == 403
    msg = {"from": ME, "id": "wamid.unsigned", "type": "text", "text": {"body": "hi"}}
    assert _post(s, msg, sign=False).status_code == 403
    assert _post(s, msg, secret="wrong").status_code == 403


def test_menu_and_unknown_senders(bot):
    m = say(bot, "Hi")
    assert m["to"] == ME and m["interactive"]["type"] == "list"
    assert {"int:list", "src:list", "al:list", "ap:list", "wf:list", "ai"} <= set(ids_of(m))
    assert all(len(r["title"]) <= 24 for s in m["interactive"]["action"]["sections"] for r in s["rows"])
    assert say(bot, "hi", sender="15550009999") is None  # not a verified admin: ignored

    # Meta retries: the same message id is handled once.
    msg = {"from": ME, "id": "wamid.dup", "type": "text", "text": {"body": "menu"}}
    n = len(SENT)
    _post(bot["server"], msg)
    _post(bot["server"], msg)
    time.sleep(1)
    assert len(SENT) == n + 1


def test_browse_integrations_and_operate_sources(bot, admin):
    m = say(bot, tap="int:list")
    first = next(i for i in ids_of(m) if i.startswith("int:") and i != "int:list")
    detail = say(bot, tap=first)
    assert "Base URL" in body_of(detail) and "Operations" in body_of(detail)
    assert "token" not in body_of(detail).lower() or "credential" in body_of(detail).lower()

    m = say(bot, "sources")
    src = next(i for i in ids_of(m) if re.fullmatch(r"src:[0-9a-f-]{36}", i))
    d = say(bot, tap=src)
    assert "Health" in body_of(d) and f"src:pause:{src[4:]}" in ids_of(d)
    assert "Paused" in body_of(say(bot, tap=f"src:pause:{src[4:]}"))
    assert admin.get(f"/api/v1/sources/{src[4:]}").json()["enabled"] is False
    assert "Resumed" in body_of(say(bot, tap=f"src:resume:{src[4:]}"))
    assert any(e["action"] == "chat.pause" and e["metadata"]["via"] == "whatsapp-chat" for e in admin.get("/api/v1/audit").json())


def _pending(builder, slug: str) -> str:
    doc = yaml.safe_load((Path(__file__).resolve().parents[2] / "examples" / "sources" / "cisa-kev.yaml").read_text())
    doc.pop("template")
    doc["integration"].update(id=slug, name=slug.replace("-", " ").title())
    iid = builder.post("/api/v1/integrations", json={"config": doc}).json()["integration"]["id"]
    assert builder.post(f"/api/v1/integrations/{iid}/tests", json={}).json()["result"] == "passed"
    return builder.post(f"/api/v1/integrations/{iid}/submit", json={}).json()["approval_id"]


def test_approve_and_reject_from_chat(bot, admin, builder):
    rid = _pending(builder, "chat-approve-kev")
    assert f"ap:{rid}" in ids_of(say(bot, "approvals"))
    d = say(bot, tap=f"ap:{rid}")
    assert "Config hash" in body_of(d) and f"ap:ok:{rid}" in ids_of(d)
    confirm = say(bot, tap=f"ap:ok:{rid}")
    yes = next(i for i in ids_of(confirm) if i.startswith("ap:yes:"))
    assert "changed" in body_of(say(bot, tap=f"ap:yes:{rid}:000000000000"))  # wrong hash: refused
    assert "approved and published" in body_of(say(bot, tap=yes))
    assert admin.get(f"/api/v1/admin/approvals/{rid}").json()["decision"] == "approved"
    assert "already approved" in body_of(say(bot, tap=yes))

    rid2 = _pending(builder, "chat-reject-kev")
    assert "reason" in body_of(say(bot, tap=f"ap:no:{rid2}"))
    assert "Rejected" in body_of(say(bot, "Wrong base URL"))
    r = admin.get(f"/api/v1/admin/approvals/{rid2}").json()
    assert r["decision"] == "rejected" and r["comment"] == "Wrong base URL"


def test_chat_approvals_can_be_turned_off(bot, admin, builder):
    admin.put("/api/v1/settings/whatsapp", json={"enabled": True, "phone_number_id": PNID, "chat_enabled": True, "chat_approvals": False})
    try:
        rid = _pending(builder, "chat-off-kev")
        m = say(bot, tap=f"ap:ok:{rid}")
        assert "turned off" in body_of(m) and "/fix/" in body_of(m)
        assert admin.get(f"/api/v1/admin/approvals/{rid}").json()["decision"] == "pending"
    finally:
        admin.put("/api/v1/settings/whatsapp", json={"enabled": True, "phone_number_id": PNID, "chat_enabled": True, "chat_approvals": True})


def test_alerts_and_free_text(bot, admin):
    from app.core.db import session_scope
    from app.services import audit
    org_id = admin.get("/api/v1/organizations/current").json()["id"]
    with session_scope() as db:
        audit.alert(db, org_id, "upstream_error", f"chat alert {uuid.uuid4().hex[:6]}", "medium", None)
    m = say(bot, "alerts")
    aid = next(i for i in ids_of(m) if re.fullmatch(r"al:[0-9a-f-]{36}", i))
    d = say(bot, tap=aid)
    assert f"al:ack:{aid[3:]}" in ids_of(d)
    assert "Acknowledged" in body_of(say(bot, tap=f"al:ack:{aid[3:]}"))
    # No real AI provider in the test environment: free text explains that instead of failing.
    assert "No AI provider" in body_of(say(bot, "why is okta failing?"))
