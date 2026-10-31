"""Security regressions: credentials only reach approved destinations, nobody can replace another person's credential,
the egress allow-list and sign-in throttling cannot be side-stepped, a password reset ends sessions, Slack codes can't be guessed."""
import copy
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from app.core.db import session_scope
from app.models import Integration, IntegrationVersion, Organization, User
from app.services import egress, healing, oauth, runtime, slack_bot
from conftest import login


def test_connection_test_never_sends_the_secret_to_an_unapproved_draft(server):
    builder = login(server, "builder")
    c = builder.post("/api/v1/connections", json={"name": "draft-probe", "auth_type": "bearer", "token": "secret-value-123"}).json()
    draft = {"schema_version": "1.0", "integration": {"id": "draft-probe-api", "name": "Draft probe", "version": "1.0.0", "transport": "http",
             "base_url": "https://collector.example.com", "authentication": {"type": "bearer", "credential_ref": "draft-probe"},
             "operations": [{"id": "ping", "method": "GET", "path": "/ping", "input_schema": {"type": "object"}, "response": {}}]}}
    assert builder.post("/api/v1/integrations", json={"config": draft}).status_code == 200
    r = builder.post(f"/api/v1/connections/{c['id']}/test").json()
    assert r["ok"] is None and "published" in r["message"] and "request" not in r      # nothing was sent anywhere


def test_endpoint_check_sends_the_credential_only_to_the_published_host(server, monkeypatch):
    sent = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: sent.append((url, kw.get("headers") or {})) or httpx.Response(401))
    monkeypatch.setattr(egress, "check_url", lambda url, allowed=None: None)    # the candidate host does not resolve offline
    monkeypatch.setattr(runtime, "credential_for", lambda db, org_id, config: {"api_key": "live-key-456"})
    with session_scope() as db:
        integ = db.scalars(select(Integration).where(Integration.slug == "notify")).first()
        live = db.get(IntegrationVersion, integ.published_version_id)
        op = {"id": "probe", "method": "GET", "path": "/messages"}
        moved = copy.deepcopy(live.config)
        moved["integration"]["base_url"] = "https://notify.attacker.example"  # e.g. suggested by the AI from an untrusted response
        assert healing.endpoint_exists(db, integ, moved, op)[0] is True
        assert healing.endpoint_exists(db, integ, live.config, op)[0] is True
    (other_url, other_headers), (_, same_headers) = sent
    assert other_url.startswith("https://notify.attacker.example") and "live-key-456" not in str(other_headers)
    assert "live-key-456" in str(same_headers)                                  # the published host gets it, as every real call does


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setattr(oauth, "_post_token", lambda provider, app, secret, data: {"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    monkeypatch.setattr(oauth, "_get_json", lambda url, token: {"email": "someone@acme.example"})


def _connect(server, client, name):
    r = client.post("/api/v1/oauth/google/start", json={"connection_name": name})
    assert r.status_code == 200, r.text
    q = parse_qs(urlsplit(r.json()["url"]).query)
    return httpx.get(f"{server}/api/v1/oauth/callback", params={"state": q["state"][0], "code": "c"},
                     cookies={"hedes_oauth": client.cookies.get("hedes_oauth")}, follow_redirects=False).headers["location"]


def test_signing_in_cannot_replace_someone_elses_credential(server, google):
    admin, builder = login(server, "admin"), login(server, "builder")
    assert admin.put("/api/v1/oauth/providers/google", json={"client_id": "cid-123.apps", "client_secret": "sec-xyz"}).json()["ok"]
    assert _connect(server, admin, "corp-google") == "/#/credentials/connected/corp-google"
    r = builder.post("/api/v1/oauth/google/start", json={"connection_name": "corp-google"})
    assert r.status_code == 409 and "someone else" in r.json()["detail"]
    assert _connect(server, builder, "my-google") == "/#/credentials/connected/my-google"    # a name of your own: fine
    assert _connect(server, admin, "my-google") == "/#/credentials/connected/my-google"      # an administrator may replace it


def test_allow_list_cannot_be_left_with_dot_segments(server):
    with pytest.raises(egress.EgressDenied):
        egress.static_check(f"{server}/mock/../api/v1/health")                  # httpx would send this to /api/v1/health
    egress.static_check(f"{server}/mock/notify/messages")                       # the allow-listed mock APIs still work


def test_sign_in_throttle_ignores_spacing_and_case(server):
    for i in range(8):
        r = httpx.post(f"{server}/api/v1/auth/login", json={"username": " " * i + "Ghost-User", "password": "wrong-password"})
        assert r.status_code == 401
    assert httpx.post(f"{server}/api/v1/auth/login", json={"username": "ghost-user ", "password": "wrong-password"}).status_code == 429


def test_password_reset_signs_the_account_out_everywhere(server):
    admin = login(server, "admin")
    m = admin.post("/api/v1/admin/users", json={"username": "resetme", "email": "resetme@x.io", "role": "user", "password": "first-password-1"}).json()
    old = httpx.Client(base_url=server, headers={"X-Requested-With": "integra"})
    assert old.post("/api/v1/auth/login", json={"username": "resetme", "password": "first-password-1"}).status_code == 200
    time.sleep(1.1)                                                              # session times have one-second resolution
    assert admin.post(f"/api/v1/admin/users/{m['id']}/password", json={"password": "second-password-2"}).status_code == 200
    assert old.get("/api/v1/auth/me").status_code == 401
    assert old.post("/api/v1/auth/refresh").status_code == 401                  # and it cannot be renewed
    new = httpx.Client(base_url=server, headers={"X-Requested-With": "integra"})
    assert new.post("/api/v1/auth/login", json={"username": "resetme", "password": "second-password-2"}).status_code == 200
    assert new.get("/api/v1/auth/me").json()["username"] == "resetme"


def test_slack_link_codes_cannot_be_guessed(server):
    with session_scope() as db:
        org = db.scalars(select(Organization)).first()
        admin_id = db.scalars(select(User.id).where(User.username == "admin")).first()
        code = slack_bot.new_link_code(org.id, admin_id)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(slack_bot.LINK_MAX_FAILS):
            assert slack_bot.redeem_link_code(db, org, wrong, "UGUESSER") is None
        assert slack_bot.redeem_link_code(db, org, code, "UGUESSER") is None    # locked out, even with the right code
        assert sum(1 for v in slack_bot._CODES.values() if v[1] == admin_id) == 1  # which stays valid for its owner
