"""Continue with Google: OAuth sign-in, token refresh, importing the provider's data sources, Graph-style paging."""
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.db import session_scope
from app.models import Connection
from app.services import engine, oauth, vault
from conftest import login


@pytest.fixture
def provider(monkeypatch):
    calls = []

    def post_token(provider, app, client_secret, data):
        calls.append({"provider": provider, "client_id": app["client_id"], "secret": client_secret, **data})
        if data["grant_type"] == "authorization_code":
            return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600, "scope": "openid email"}
        return {"access_token": "at-2", "expires_in": 3600}  # refresh: no new refresh token, the old one is kept

    monkeypatch.setattr(oauth, "_post_token", post_token)
    monkeypatch.setattr(oauth, "_get_json", lambda url, token: {"email": "admin@acme.example"})
    return calls


def _sign_in(server, client):
    r = client.post("/api/v1/oauth/google/start", json={})
    assert r.status_code == 200, r.text
    q = parse_qs(urlsplit(r.json()["url"]).query)
    return q, client.cookies.get("hedes_oauth")


def test_continue_with_google_connects_and_imports_its_sources(server, provider):
    admin, builder = login(server, "admin"), login(server, "builder")
    assert builder.put("/api/v1/oauth/providers/google", json={"client_id": "cid-123.apps", "client_secret": "s"}).status_code == 403
    assert builder.post("/api/v1/oauth/google/start", json={}).status_code == 409          # not set up yet
    assert admin.put("/api/v1/oauth/providers/google", json={"client_id": "cid-123.apps", "client_secret": "sec-xyz"}).json()["ok"]
    google = next(p for p in admin.get("/api/v1/oauth/providers").json() if p["id"] == "google")
    assert google["configured"] and google["redirect_uri"].endswith("/api/v1/oauth/callback")
    assert any(t["id"] == "google-workspace" for t in google["templates"])

    q, nonce = _sign_in(server, builder)
    assert q["client_id"] == ["cid-123.apps"] and q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    assert "https://www.googleapis.com/auth/admin.reports.audit.readonly" in q["scope"][0].split()   # the templates' scopes
    state = q["state"][0]

    # a callback from another browser (no nonce cookie) is refused: nobody can link their account into yours by a link
    stranger = httpx.Client(base_url=server, follow_redirects=False)
    r = stranger.get("/api/v1/oauth/callback", params={"state": state, "code": "c"})
    assert r.status_code == 303 and "/#/credentials/oauth-error/" in r.headers["location"] and "another%20browser" in r.headers["location"]
    q, nonce = _sign_in(server, builder)                                                       # the state is single-use: start again
    r = httpx.get(f"{server}/api/v1/oauth/callback", params={"state": q["state"][0], "code": "the-code"}, cookies={"hedes_oauth": nonce}, follow_redirects=False)
    assert r.headers["location"] == "/#/credentials/connected/google"
    exchange = provider[-1]
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "the-code" and exchange["code_verifier"] and exchange["secret"] == "sec-xyz"

    conns = {c["name"]: c for c in builder.get("/api/v1/connections").json()}
    assert conns["google"]["auth_type"] == "oauth2_authorization_code" and conns["google"]["hint"] == "admin@acme.example"
    assert "at-1" not in str(conns["google"])                                                  # tokens never leave the vault

    r = builder.post("/api/v1/oauth/google/import", json={"templates": ["google-workspace", "okta"]}).json()["results"]
    assert r[0]["ok"] and r[0]["integration"]["slug"] == "google-workspace" and r[1] == {"template": "okta", "ok": False, "error": "Not a template of this provider"}
    assert next(p for p in builder.get("/api/v1/oauth/providers").json() if p["id"] == "google")["templates"][0]["installed"]

    # the access token is refreshed (and saved) shortly before it expires; the refresh token is kept
    with session_scope() as db:
        c = db.query(Connection).filter_by(name="google").first()
        s = vault.read_secret(db, c.secret_ref, c.organization_id)
        vault.delete_secret(db, c.secret_ref)
        c.secret_ref = vault.store_secret(db, c.organization_id, {**s, "expires_at": int(time.time()) + 10})
    with session_scope() as db:
        c = db.query(Connection).filter_by(name="google").first()
        fresh = oauth.fresh_secret(db, c, vault.read_secret(db, c.secret_ref, c.organization_id))
        assert fresh["access_token"] == "at-2" and fresh["refresh_token"] == "rt-1"
        assert vault.read_secret(db, c.secret_ref, c.organization_id)["access_token"] == "at-2"
    assert provider[-1] == {"provider": "google", "client_id": "cid-123.apps", "secret": "sec-xyz", "grant_type": "refresh_token", "refresh_token": "rt-1"}


def test_signed_in_requests_use_the_access_token_and_follow_body_next_links():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        if "skiptoken" in str(request.url):
            return httpx.Response(200, json={"value": [{"id": "3"}]})
        return httpx.Response(200, json={"value": [{"id": "1"}, {"id": "2"}],
                                          "@odata.nextLink": "https://graph.example.com/v1.0/users?$top=2&$skiptoken=abc"})

    cfg = {"schema_version": "1.0", "integration": {"id": "graph", "name": "Graph", "version": "1.0.0", "transport": "http",
           "base_url": "https://graph.example.com", "authentication": {"type": "oauth2_authorization_code", "credential_ref": "microsoft"},
           "operations": [{"id": "users", "method": "GET", "path": "/v1.0/users", "input_schema": {"type": "object"},
                           "request": {"query": {"$top": "2"}},
                           "pagination": {"type": "link", "next_url_path": "$['@odata.nextLink']", "max_pages": 5},
                           "response": {"status_codes": [200], "items_path": "$.value"}}]}}
    res = engine.execute(cfg, "users", {}, credential={"access_token": "at-9"}, transport=httpx.MockTransport(handler), live=False, raw=True)
    assert res.status == "succeeded", res.error
    assert [x["id"] for x in res.output["items"]] == ["1", "2", "3"]
    assert seen[0][1] == "Bearer at-9" and "skiptoken=abc" in seen[1][0]
