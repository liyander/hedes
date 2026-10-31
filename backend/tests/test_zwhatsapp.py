"""WhatsApp alerts: setup, number verification, delivery with fix links, and the one-tap fix actions (Meta API mocked)."""
import re
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from conftest import login

SENT: list[dict] = []
FAIL: dict = {}


def _graph(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "graph.facebook.com" and req.headers["authorization"] == "Bearer wa-token-123456"
    if FAIL:
        return httpx.Response(400, json={"error": FAIL})
    if req.method == "GET":
        assert "display_phone_number" in req.url.params["fields"]
        return httpx.Response(200, json={"display_phone_number": "+1 555-010-0199", "verified_name": "Test Number", "id": "1065403522"})
    SENT.append({"path": req.url.path, **__import__("json").loads(req.content)})
    return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(SENT)}"}]})


@pytest.fixture(scope="module")
def wa(server, admin):
    from app.services import whatsapp
    whatsapp.TRANSPORT = httpx.MockTransport(_graph)
    assert admin.put("/api/v1/settings/whatsapp", json={"enabled": True}).status_code == 422  # nothing configured yet
    r = admin.put("/api/v1/settings/whatsapp", json={"enabled": True, "phone_number_id": "1065403522", "access_token": "wa-token-123456"})
    assert r.status_code == 200, r.text
    assert r.json()["configured"] and "wa-token-123456" not in r.text
    assert r.json()["display_phone"] == "+1 555-010-0199" and r.json()["check_error"] == ""  # saving looks the number up
    me = admin.get("/api/v1/me/whatsapp").json()
    assert me["business_phone"] == "+1 555-010-0199" and me["has_template"] is False
    yield whatsapp
    whatsapp.TRANSPORT = None


def _wait(n: int) -> list[dict]:
    for _ in range(100):
        if len(SENT) >= n:
            return SENT
        time.sleep(0.05)
    raise AssertionError(f"expected {n} WhatsApp messages, got {len(SENT)}")


def _verify(client, phone: str) -> None:
    before = len(SENT)
    r = client.post("/api/v1/me/whatsapp/code", json={"phone": phone})
    assert r.status_code == 200, r.text
    msg = _wait(before + 1)[-1]
    code = re.search(r"\b(\d{6})\b", msg["text"]["body"]).group(1)
    assert client.post("/api/v1/me/whatsapp/verify", json={"code": "000000" if code != "000000" else "111111"}).status_code == 422
    r = client.post("/api/v1/me/whatsapp/verify", json={"code": code})
    assert r.status_code == 200 and r.json()["verified"] and r.json()["enabled"]


def _alert(admin_client, kind: str, severity: str, integration_id: str | None, link: str = "") -> tuple[str, str]:
    from app.core.db import session_scope
    from app.services import audit
    org_id = admin_client.get("/api/v1/organizations/current").json()["id"]
    message = f"{kind} test {uuid.uuid4().hex[:8]}"
    with session_scope() as db:
        audit.alert(db, org_id, kind, message, severity, integration_id, link)
    return org_id, message


def owner_client(admin_client):
    return login(str(admin_client.base_url).rstrip("/"), "owner")


def _token(msg: dict) -> str:
    return re.search(r"/fix/([\w-]+)", msg["text"]["body"]).group(1)


def test_phone_numbers_are_normalised(wa):
    assert wa.normalize_phone("+44 7700 900123") == "447700900123"
    assert wa.normalize_phone("0044 (7700) 900-123") == "447700900123"
    for bad in ("07700 900123", "+1 23", "abc"):
        with pytest.raises(ValueError):
            wa.normalize_phone(bad)


def test_verification_then_alert_with_fix_link_and_actions(server, admin, builder, wa):
    _verify(admin, "+44 7700 900123")
    assert admin.post("/api/v1/me/whatsapp/code", json={"phone": "+44 7700 900123"}).status_code == 429  # resend throttle
    assert builder.get("/api/v1/me/whatsapp").status_code == 403  # admins only

    src = admin.get("/api/v1/sources").json()["sources"][0]
    n = len(SENT)
    _, message = _alert(admin, "source_failing", "high", src["integration_id"], "#/sources")
    msg = _wait(n + 1)[-1]
    assert msg["to"] == "447700900123" and message in msg["text"]["body"] and msg["path"].endswith("/1065403522/messages")
    token = _token(msg)

    # The link only works for its recipient, and the short /fix URL forwards into the app.
    assert builder.get(f"/api/v1/fix/{token}").status_code == 403
    assert httpx.get(f"{server}/fix/{token}", follow_redirects=False).headers["location"] == f"/#/fix/{token}"
    ctx = admin.get(f"/api/v1/fix/{token}").json()
    ids = {a["id"] for a in ctx["actions"]}
    assert {"retry", "pause", "ack", "mute", "open"} <= ids and ctx["alert"]["message"] == message

    r = admin.post(f"/api/v1/fix/{token}/pause", json={"source_id": src["id"]})
    assert r.status_code == 200 and r.json()["result"] == "paused" and "resume" in {a["id"] for a in r.json()["actions"]}
    assert admin.post(f"/api/v1/fix/{token}/resume", json={"source_id": src["id"]}).json()["result"] == "enabled"
    assert admin.post(f"/api/v1/fix/{token}/ack", json={}).json()["alert"]["acknowledged"] is True

    # Muted integrations stop sending until unmuted.
    assert admin.post(f"/api/v1/fix/{token}/mute", json={"minutes": 60}).json()["muted_until"]
    n = len(SENT)
    _alert(admin, "source_failing", "high", src["integration_id"])
    time.sleep(0.5)
    assert len(SENT) == n
    assert admin.post(f"/api/v1/fix/{token}/unmute", json={}).json()["muted_until"] is None
    assert admin.post(f"/api/v1/fix/{token}/bogus", json={}).status_code == 404
    assert any(e["action"] == "fix.pause" and e["metadata"]["via"] == "whatsapp-fix-link" for e in admin.get("/api/v1/audit").json())


def test_below_threshold_alerts_are_not_sent(admin, wa):
    n = len(SENT)
    _alert(admin, "upstream_error", "medium", None)
    time.sleep(0.5)
    assert len(SENT) == n


def test_pending_approval_is_sent_and_approved_from_the_fix_link(admin, builder, wa):
    doc = yaml.safe_load((Path(__file__).resolve().parents[2] / "examples" / "sources" / "cisa-kev.yaml").read_text())
    doc.pop("template")
    doc["integration"].update(id="wa-fix-kev", name="WhatsApp fix KEV")
    iid = builder.post("/api/v1/integrations", json={"config": doc}).json()["integration"]["id"]
    assert builder.post(f"/api/v1/integrations/{iid}/tests", json={}).json()["result"] == "passed"
    n = len(SENT)
    rid = builder.post(f"/api/v1/integrations/{iid}/submit", json={}).json()["approval_id"]
    msg = _wait(n + 1)[-1]  # severity "info", but the recipient asked for every pending approval
    token = _token(msg)
    ctx = admin.get(f"/api/v1/fix/{token}").json()
    assert ctx["approval"]["id"] == rid and ctx["approval"]["blockers"] == []
    r = admin.post(f"/api/v1/fix/{token}/approve", json={})
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["decision"] == "approved"
    assert admin.get(f"/api/v1/admin/approvals/{rid}").json()["decision"] == "approved"
    assert admin.post(f"/api/v1/fix/{token}/approve", json={}).status_code == 409


def test_template_messages_and_readable_meta_errors(admin, wa):
    r = admin.put("/api/v1/settings/whatsapp", json={"enabled": True, "phone_number_id": "1065403522", "alert_template": "hedes_alert",
                                                     "template_lang": "en_US", "template_has_button": True})
    assert r.status_code == 200 and r.json()["token_hint"]  # the stored token was kept
    n = len(SENT)
    assert admin.post("/api/v1/me/whatsapp/test").status_code == 200
    t = _wait(n + 1)[-1]["template"]
    assert t["name"] == "hedes_alert" and t["language"]["code"] == "en_US"
    body, button = t["components"]
    assert len(body["parameters"]) == 3 and "\n" not in body["parameters"][1]["text"]
    token = button["parameters"][0]["text"]
    assert body["parameters"][2]["text"].endswith(f"/fix/{token}")
    assert admin.get(f"/api/v1/fix/{token}").json()["test"] is True

    # With an alert template, verification codes go through it too (plain text would be dropped outside the 24-hour window).
    n = len(SENT)
    r = owner_client(admin).post("/api/v1/me/whatsapp/code", json={"phone": "+49 151 23456789"})
    assert r.status_code == 200 and r.json()["sent_via"] == "template"
    t = _wait(n + 1)[-1]["template"]
    assert t["name"] == "hedes_alert" and re.search(r"\b\d{6}\b", t["components"][0]["parameters"][1]["text"])
    assert t["components"][1]["parameters"][0]["text"] == "settings"

    FAIL.update(code=131047, message="Re-engagement message")
    try:
        r = admin.post("/api/v1/me/whatsapp/test")
        assert r.status_code == 502 and "24 hours" in r.json()["detail"]
        FAIL.update(code=190, message="Invalid OAuth access token")
        r = admin.post("/api/v1/settings/whatsapp/check")
        assert r.status_code == 502 and "access token" in r.json()["detail"]
    finally:
        FAIL.clear()
    assert admin.post("/api/v1/settings/whatsapp/check").json()["verified_name"] == "Test Number"
    admin.put("/api/v1/settings/whatsapp", json={"enabled": True, "phone_number_id": "1065403522"})


def test_expired_link_and_removing_the_number(admin, wa):
    from app.core.db import session_scope, utcnow
    from app.models import FixLink
    n = len(SENT)
    assert admin.post("/api/v1/me/whatsapp/test").status_code == 200
    token = _token(_wait(n + 1)[-1])
    with session_scope() as db:
        for link in db.query(FixLink).all():
            link.expires_at = utcnow()
    assert admin.get(f"/api/v1/fix/{token}").status_code == 410
    assert admin.delete("/api/v1/me/whatsapp").json()["verified"] is False
    other = login(admin.base_url.__str__().rstrip("/"), "owner")
    assert other.get("/api/v1/me/whatsapp").json()["org_ready"] is True
