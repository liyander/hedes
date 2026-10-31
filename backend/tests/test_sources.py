"""Data-source ingestion: engine features (backoff, throttle, pagination, auth), normalisation and the live pipeline."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.services import engine, ingest
from app.services.validation import validate_config

SPEC = {"category": "identity", "fields": {"event_id": "$.id", "timestamp": "$.at", "actor": "$.who", "action": "$.what", "severity": "$.sev",
                                         "outcome": "$.res"},
        "value_maps": {"severity": {"WARN": "medium"}, "outcome": {"OK": "success", "NO": "failure"}}}


def cfg(auth=None, pagination=None, rate_limit=None, retry=None, base="https://api.example.com"):
    op = {"id": "pull", "method": "GET", "path": "/logs", "input_schema": {"type": "object", "properties": {}},
          "response": {"status_codes": [200], "items_path": "$.items"}}
    if pagination:
        op["pagination"] = pagination
    if retry:
        op["retry"] = retry
    integ = {"id": "src", "name": "Src", "version": "1.0.0", "transport": "http", "base_url": base,
             "authentication": auth or {"type": "none"}, "operations": [op]}
    if rate_limit:
        integ["rate_limit"] = {"requests_per_minute": rate_limit}
    return {"schema_version": "1.0", "integration": integ}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(engine, "_sleep", slept.append)
    return slept


# --------------------------------------------------------------------------------------- normalisation

def test_normalize_maps_values_and_coerces_timestamps():
    rec, why = ingest.normalize({"id": 7, "at": 1_700_000_000, "who": "ana", "what": "login", "sev": "WARN", "res": "NO"}, SPEC, "demo-okta")
    assert not why
    assert rec["event_id"] == "7" and rec["severity"] == "medium" and rec["outcome"] == "failure" and rec["category"] == "identity"
    assert rec["timestamp"] == datetime(2023, 11, 14, 22, 13, 20)
    for value in ("2024-05-01T10:00:00Z", "2024-05-01T12:00:00+02:00", 1714557600000, "1714557600"):
        assert ingest.parse_timestamp(value) == datetime(2024, 5, 1, 10, 0)


@pytest.mark.parametrize("raw,fragment", [
    ({"id": 1, "who": "a", "what": "x"}, "timestamp"),
    ({"id": 1, "at": "not a date", "what": "x"}, "timestamp"),
    ({"id": 1, "at": 1_700_000_000}, "action"),
    ("just a string", "expected an object"),
])
def test_normalize_rejects_bad_records_with_reasons(raw, fragment):
    rec, why = ingest.normalize(raw, SPEC, "x")
    assert rec is None and any(fragment in r for r in why)


def test_normalize_future_timestamp_rejected_and_missing_id_is_hashed():
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert ingest.normalize({"at": future, "what": "x"}, SPEC, "x")[0] is None
    a = ingest.normalize({"at": 1_700_000_000, "what": "x"}, SPEC, "x")[0]["event_id"]
    b = ingest.normalize({"at": 1_700_000_000, "what": "x"}, SPEC, "x")[0]["event_id"]
    assert a == b and a.startswith("h:")
    assert ingest.normalize({"id": "1", "at": 1_700_000_000, "what": "x", "sev": "weird"}, SPEC, "x")[0]["severity"] == "info"


# --------------------------------------------------------------------------------------- backoff & throttling

def test_retry_wait_honours_server_hints_with_jitter_and_cap(monkeypatch):
    monkeypatch.setattr(engine, "_random", lambda: 1.0)
    assert engine.retry_wait(3, 1.0, {}) == 4.0                              # 1 * 2^2, top of the jitter band
    monkeypatch.setattr(engine, "_random", lambda: 0.0)
    assert engine.retry_wait(3, 1.0, {}) == 2.0                              # bottom of the band
    assert engine.retry_wait(1, 1.0, {"Retry-After": "7"}) == 7.0            # the API's hint is a floor
    assert engine.retry_wait(1, 1.0, {"Retry-After": "999"}, max_wait=20) == 20.0
    assert engine.retry_wait(1, 1.0, {"X-RateLimit-Reset": str(int(time.time()) + 5)}) == pytest.approx(5, abs=1.5)
    http_date = (datetime.now(timezone.utc) + timedelta(seconds=9)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 6 <= engine.retry_wait(1, 0.1, {"Retry-After": http_date}) <= 10


def test_engine_retries_429_then_succeeds_and_reports_waits(no_sleep):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"items": [{"id": 1}]})

    res = engine.execute(cfg(retry={"max_attempts": 4, "backoff_seconds": 0.1}), "pull", {}, transport=httpx.MockTransport(handler), live=False, raw=True)
    assert res.status == "succeeded" and res.attempts == 3 and res.output["count"] == 1
    assert len(no_sleep) == 2 and all(w >= 2 for w in no_sleep) and res.diagnostics["retry_waits"] == [2.0, 2.0]


def test_engine_gives_up_with_rate_limited_class():
    res = engine.execute(cfg(retry={"max_attempts": 2}), "pull", {}, transport=httpx.MockTransport(lambda r: httpx.Response(429)), live=False)
    assert res.error_class == "rate_limited" and res.attempts == 2


def test_per_connector_throttle_spaces_requests(no_sleep):
    c = cfg(rate_limit=30, pagination={"type": "page", "size": 1, "max_pages": 3})
    c["integration"]["id"] = "throttle-test"
    pages = iter([{"items": [{"a": 1}]}, {"items": [{"a": 2}]}, {"items": [{"a": 3}]}])
    res = engine.execute(c, "pull", {}, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(pages))), live=False, raw=True)
    assert res.output["count"] == 3
    # 30 requests/minute = one slot every 2 s. Sleep is stubbed (time does not pass), so the 2nd and 3rd slots are 2 s and 4 s away.
    assert len(no_sleep) == 2 and no_sleep == pytest.approx([2.0, 4.0], abs=0.1) and res.diagnostics["throttled_seconds"] > 5.5


# --------------------------------------------------------------------------------------- pagination

def test_offset_pagination_stops_at_total():
    seen = []

    def handler(req):
        q = parse_qs(urlsplit(str(req.url)).query)
        off = int(q["offset"][0])
        seen.append(off)
        return httpx.Response(200, json={"total": 5, "items": [{"i": n} for n in range(off, min(off + 2, 5))]})

    c = cfg(pagination={"type": "offset", "offset_param": "offset", "size_param": "limit", "size": 2, "total_path": "$.total", "max_pages": 10})
    res = engine.execute(c, "pull", {}, transport=httpx.MockTransport(handler), live=False, raw=True)
    assert seen == [0, 2, 4] and res.output["count"] == 5


def test_link_header_pagination_ignores_foreign_hosts():
    def handler(req):
        if req.url.params.get("after") == "1":
            return httpx.Response(200, json={"items": [{"i": 2}]})
        return httpx.Response(200, json={"items": [{"i": 1}]}, headers={"Link": '<https://api.example.com/logs?after=1&limit=1>; rel="next"'})

    c = cfg(pagination={"type": "link", "max_pages": 5})
    assert engine.execute(c, "pull", {}, transport=httpx.MockTransport(handler), live=False, raw=True).output["count"] == 2
    evil = httpx.MockTransport(lambda r: httpx.Response(200, json={"items": [{"i": 1}]}, headers={"Link": '<https://evil.test/logs?after=1>; rel="next"'}))
    assert engine.execute(c, "pull", {}, transport=evil, live=False, raw=True).output["count"] == 1


def test_raw_mode_reports_missing_items_path_as_drift():
    res = engine.execute(cfg(), "pull", {}, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"rows": []})), live=False, raw=True)
    assert res.error_class == "response_drift"


# --------------------------------------------------------------------------------------- authentication

def test_ssws_auth_header_and_redaction():
    seen = {}
    c = cfg(auth={"type": "ssws", "credential_ref": "demo-okta"})

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"items": []})

    res = engine.execute(c, "pull", {}, credential={"token": "abc123"}, transport=httpx.MockTransport(handler), live=False, raw=True)
    assert seen["auth"] == "SSWS abc123" and res.request_preview["headers"]["Authorization"] == "••••••"


def test_sigv4_matches_the_aws_documented_vector():
    # From the AWS SigV4 test suite ("get-vanilla"): GET / with only host + x-amz-date signed.
    auth = engine.sigv4_authorization("GET", "example.amazonaws.com", "/", {}, b"", "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                                      "us-east-1", "service", "20150830T123600Z")
    assert auth.endswith("Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")
    assert "SignedHeaders=host;x-amz-date" in auth and "Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request" in auth


def test_sigv4_requests_are_signed_per_page_and_redacted():
    auths = []
    c = cfg(auth={"type": "aws_sigv4", "credential_ref": "aws", "region": "us-east-1", "service": "cloudtrail"},
            pagination={"type": "cursor", "cursor_param": "NextToken", "next_cursor_path": "$.next", "max_pages": 3})
    pages = iter([{"items": [{"a": 1}], "next": "t1"}, {"items": [{"a": 2}]}])

    def handler(req):
        auths.append((req.headers["authorization"], req.headers["x-amz-date"], dict(req.url.params)))
        return httpx.Response(200, json=next(pages))

    secret = {"access_key_id": "AKIA", "secret_access_key": "s3cret", "session_token": "tok"}
    res = engine.execute(c, "pull", {}, credential=secret, transport=httpx.MockTransport(handler), live=False, raw=True)
    assert res.output["count"] == 2 and len(auths) == 2 and auths[0][0] != auths[1][0]  # different query => different signature
    assert all(a[0].startswith("AWS4-HMAC-SHA256 Credential=AKIA/") for a in auths)
    assert "s3cret" not in json.dumps(res.request_preview) and "tok" not in json.dumps(res.request_preview["headers"])


def test_oauth2_client_credentials_fetches_and_caches_token():
    tokens = []

    def handler(req):
        if req.url.path == "/token":
            tokens.append(1)
            assert req.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        assert req.headers["authorization"] == "Bearer at-1"
        return httpx.Response(200, json={"items": [{"a": 1}]})

    c = cfg(auth={"type": "oauth2_client_credentials", "credential_ref": "x", "token_url": "https://api.example.com/token", "scopes": ["logs.read"]})
    secret = {"client_id": "cid-unique-1", "client_secret": "cs"}
    for _ in range(2):
        assert engine.execute(c, "pull", {}, credential=secret, transport=httpx.MockTransport(handler), live=False, raw=True).status == "succeeded"
    assert len(tokens) == 1
    bad = httpx.MockTransport(lambda r: httpx.Response(401))
    res = engine.execute(c, "pull", {}, credential={"client_id": "other", "client_secret": "x"}, transport=bad, live=False, raw=True)
    assert res.error_class == "auth_invalid"


def test_missing_credential_fields_for_new_auth_types():
    res = engine.execute(cfg(auth={"type": "aws_sigv4", "credential_ref": "a", "region": "us-east-1", "service": "s"}), "pull", {},
                         credential={"access_key_id": "only"}, live=False, raw=True)
    assert res.error_class == "credential_missing"


# --------------------------------------------------------------------------------------- validation

def test_ingest_config_validation():
    good = cfg()
    good["integration"]["operations"][0]["ingest"] = SPEC
    assert validate_config(good)["valid"]
    bad = json.loads(json.dumps(good))
    op = bad["integration"]["operations"][0]
    op["method"] = "POST"
    op["ingest"] = {"fields": {"event_id": "$.id"}}
    op["input_schema"] = {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}
    msgs = " | ".join(e["message"] for e in validate_config(bad)["errors"])
    assert "GET" in msgs and "timestamp" in msgs and "action" in msgs and "inputs cannot be required" in msgs
    sig = cfg(auth={"type": "aws_sigv4", "credential_ref": "a"})
    assert any("region" in e["message"] for e in validate_config(sig)["errors"])
    off = cfg(pagination={"type": "offset"})
    assert any("Offset" in e["message"] for e in validate_config(off)["errors"])


# --------------------------------------------------------------------------------------- live pipeline

def _sources(admin):
    return {s["slug"]: s for s in admin.get("/api/v1/sources").json()["sources"]}


def _run(admin, src):
    r = admin.post(f"/api/v1/sources/{src['id']}/run")
    assert r.status_code == 200, r.text
    return r.json()["run"]


def test_three_sources_ingest_into_one_common_format(server, admin):
    sources = _sources(admin)
    assert set(sources) == {"demo-okta", "demo-aws-cloudtrail", "demo-firewall"}
    assert {s["pagination"] for s in sources.values()} == {"link", "cursor", "offset"}
    assert {s["auth_type"] for s in sources.values()} == {"ssws", "aws_sigv4", "api_key_header"}
    assert all(s["health"] == "pending" for s in sources.values())

    first = {slug: _run(admin, s) for slug, s in sources.items()}
    assert first["demo-okta"]["stored"] == 48 and first["demo-okta"]["rejected"] == 0
    assert first["demo-aws-cloudtrail"]["stored"] == 34
    assert first["demo-firewall"]["stored"] == 60 and first["demo-firewall"]["rejected"] == 3 and first["demo-firewall"]["status"] == "partial"

    ev = admin.get("/api/v1/events", params={"limit": 200}).json()
    assert ev["total"] == 142 and ev["sources"] == {"demo-okta": 48, "demo-aws-cloudtrail": 34, "demo-firewall": 60}
    for e in ev["items"]:
        assert e["severity"] in ingest.SEVERITIES and e["outcome"] in ingest.OUTCOMES and e["timestamp"] and e["action"]
    for slug in ("demo-okta", "demo-aws-cloudtrail", "demo-firewall"):
        row = next(e for e in ev["items"] if e["source"] == slug)
        detail = admin.get(f"/api/v1/events/{row['id']}").json()
        assert detail["raw"] and detail["actor"] and detail["target"]  # the original record travels with the normalised one
    assert any(e["outcome"] == "failure" for e in ev["items"] if e["source"] == "demo-aws-cloudtrail")  # ErrorCode mapped via value_maps
    assert any(e["severity"] == "critical" for e in ev["items"] if e["source"] == "demo-firewall")

    health = _sources(admin)
    assert all(s["health"] in ("healthy", "degraded") for s in health.values())
    assert health["demo-firewall"]["totals"]["rejected"] == 3 and health["demo-firewall"]["dead_letters"] == 3
    assert health["demo-okta"]["watermark"]


def test_incremental_runs_only_fetch_new_records_and_deduplicate(server, admin):
    s = _sources(admin)["demo-okta"]
    before = admin.get("/api/v1/events", params={"source": "demo-okta"}).json()["total"]
    again = _run(admin, s)
    assert again["stored"] == 0 and again["fetched"] <= 3 and again["duplicates"] == again["fetched"]  # only the overlap window is re-read
    assert again["watermark_before"] and again["watermark_after"] >= again["watermark_before"]

    assert admin.post("/api/v1/demo/feeds", json={"okta": 5, "aws": 4, "firewall": 6}).status_code == 200
    runs = {slug: _run(admin, _sources(admin)[slug]) for slug in ("demo-okta", "demo-aws-cloudtrail", "demo-firewall")}
    assert runs["demo-okta"]["stored"] == 5 and runs["demo-aws-cloudtrail"]["stored"] == 4 and runs["demo-firewall"]["stored"] == 6
    assert admin.get("/api/v1/events", params={"source": "demo-okta"}).json()["total"] == before + 5
    # Full re-read after a reset: nothing is stored twice.
    assert admin.post(f"/api/v1/sources/{s['id']}/reset").status_code == 200
    full = _run(admin, _sources(admin)["demo-okta"])
    assert full["stored"] == 0 and full["duplicates"] == full["fetched"] > 40
    assert full["watermark_after"]                                        # the high-water mark is restored even though nothing was new


def test_dead_letters_are_parked_once_and_can_be_reprocessed_or_dismissed(server, admin):
    dl = admin.get("/api/v1/dead-letters").json()
    assert dl["total"] == 3
    reasons = " ".join(" ".join(d["reasons"]) for d in dl["items"])
    assert "timestamp" in reasons and "action" in reasons
    again = _run(admin, _sources(admin)["demo-firewall"])                      # incremental pull no longer sees the garbage...
    assert again["rejected"] == 0
    assert admin.post(f"/api/v1/sources/{_sources(admin)['demo-firewall']['id']}/reset").status_code == 200
    _run(admin, _sources(admin)["demo-firewall"])                              # ...and a full re-read does not park it twice
    assert admin.get("/api/v1/dead-letters").json()["total"] == 3

    first = dl["items"][0]
    r = admin.post(f"/api/v1/dead-letters/{first['id']}/reprocess").json()
    assert r["stored"] is False and r["reasons"]                         # still invalid with the current mapping
    assert admin.delete(f"/api/v1/dead-letters/{first['id']}").status_code == 200
    assert admin.get("/api/v1/dead-letters").json()["total"] == 2


def test_firewall_rate_limit_is_retried_transparently(server, admin):
    assert admin.post("/api/v1/demo/feeds", json={"firewall": 60, "firewall_flaky": True}).json()["firewall_flaky"] is True
    try:
        run = _run(admin, _sources(admin)["demo-firewall"])
        assert run["status"] in ("succeeded", "partial") and run["stored"] == 60, (run["error_class"], run["error"], run["fetched"], run["attempts"])
        pages = -(-run["fetched"] // 25)
        assert run["attempts"] > pages >= 3                              # at least one 429 was absorbed by a retry
        ex = next(e for e in admin.get("/api/v1/executions", params={"limit": 20}).json() if e["tool"] == "demo-firewall.list_traffic_logs")
        assert ex["status"] == "succeeded"
    finally:
        admin.post("/api/v1/demo/feeds", json={"firewall_flaky": False})


def test_auth_failures_surface_as_failing_health_with_guidance(server, admin):
    conns = {c["name"]: c for c in admin.get("/api/v1/connections").json()}
    r = admin.put(f"/api/v1/connections/{conns['demo-okta']['id']}/secret", json={"auth_type": "ssws", "token": "revoked-token-value"})
    assert r.status_code == 200
    src = _sources(admin)["demo-okta"]
    run = _run(admin, src)
    assert run["status"] == "failed" and run["error_class"] == "auth_invalid"
    bad = admin.get(f"/api/v1/sources/{src['id']}").json()
    assert bad["health"] == "failing" and bad["error_fix"] and bad["consecutive_failures"] >= 1
    assert any(a["kind"] == "auth_invalid" for a in admin.get("/api/v1/alerts").json())
    admin.put(f"/api/v1/connections/{conns['demo-okta']['id']}/secret", json={"auth_type": "ssws", "token": "okta-demo-token"})
    assert _run(admin, src)["status"] == "succeeded"
    assert _sources(admin)["demo-okta"]["health"] == "healthy"


def test_sigv4_rejects_a_wrong_secret(server, admin):
    conns = {c["name"]: c for c in admin.get("/api/v1/connections").json()}
    admin.put(f"/api/v1/connections/{conns['demo-aws-cloudtrail']['id']}/secret",
              json={"auth_type": "aws_sigv4", "access_key_id": "AKIADEMOCLOUDTRAIL", "secret_access_key": "wrong"})
    run = _run(admin, _sources(admin)["demo-aws-cloudtrail"])
    assert run["status"] == "failed" and run["error_class"] == "forbidden"
    admin.put(f"/api/v1/connections/{conns['demo-aws-cloudtrail']['id']}/secret",
              json={"auth_type": "aws_sigv4", "access_key_id": "AKIADEMOCLOUDTRAIL", "secret_access_key": "demo/aws/secret/access/key"})
    assert _run(admin, _sources(admin)["demo-aws-cloudtrail"])["status"] == "succeeded"


def test_expired_credentials_are_flagged_and_not_used(server, admin):
    c = next(c for c in admin.get("/api/v1/connections").json() if c["name"] == "demo-firewall")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert "expired" in admin.put(f"/api/v1/connections/{c['id']}/expiry", json={"expires_at": past}).json()["warnings"]
    src = _sources(admin)["demo-firewall"]
    assert src["credential"]["status"] == "expired"
    run = _run(admin, src)
    assert run["status"] == "failed" and run["error_class"] == "credential_missing" and "expired" in run["error"]
    soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    assert "expiring" in admin.put(f"/api/v1/connections/{c['id']}/expiry", json={"expires_at": soon}).json()["warnings"]
    assert admin.put(f"/api/v1/connections/{c['id']}/expiry", json={"expires_at": None}).json()["warnings"] == []
    assert _run(admin, src)["status"] in ("succeeded", "partial")


def test_credential_hygiene_alerts_and_source_controls(server, admin):
    from app.core.db import session_scope
    with session_scope() as db:
        assert ingest.credential_hygiene(db) == 0
    s = _sources(admin)["demo-firewall"]
    assert admin.patch(f"/api/v1/sources/{s['id']}", json={"enabled": False}).json()["health"] == "paused"
    assert admin.post(f"/api/v1/sources/{s['id']}/run").status_code == 409
    assert admin.patch(f"/api/v1/sources/{s['id']}", json={"enabled": True, "interval_minutes": 30}).json()["interval_minutes"] == 30


def test_builder_can_run_but_not_govern_sources(server, builder):
    s = next(iter(builder.get("/api/v1/sources").json()["sources"]))
    assert builder.patch(f"/api/v1/sources/{s['id']}", json={"enabled": False}).status_code == 403
    assert builder.post(f"/api/v1/sources/{s['id']}/reset").status_code == 403
    assert builder.post("/api/v1/demo/feeds", json={"okta": 1}).status_code == 403


def test_stale_running_state_from_a_crash_does_not_block_the_source(server, admin):
    from app.core.db import session_scope, utcnow
    from app.models import IngestSource
    sid = _sources(admin)["demo-okta"]["id"]
    with session_scope() as db:
        src = db.get(IngestSource, sid)
        src.state, src.next_run_at = "running", utcnow() - timedelta(minutes=1)  # as left by a process killed mid-run
    with session_scope() as db:
        assert sid in ingest.due_sources(db)
        assert ingest.health(db.get(IngestSource, sid))["status"] != "running"


def test_acknowledged_credential_alert_is_not_raised_again_the_same_day(server, admin):
    from app.core.db import session_scope, utcnow
    from app.models import Connection
    c = next(c for c in admin.get("/api/v1/connections").json() if c["name"] == "notify")
    with session_scope() as db:
        conn = db.get(Connection, c["id"])
        original, conn.rotated_at = conn.rotated_at, utcnow() - timedelta(days=200)
    try:
        with session_scope() as db:
            assert ingest.credential_hygiene(db) == 1
        alert = next(a for a in admin.get("/api/v1/alerts").json() if a["kind"] == "credential")
        assert admin.post(f"/api/v1/alerts/{alert['id']}/ack").status_code == 200
        with session_scope() as db:
            assert ingest.credential_hygiene(db) == 0
    finally:
        with session_scope() as db:
            db.get(Connection, c["id"]).rotated_at = original
