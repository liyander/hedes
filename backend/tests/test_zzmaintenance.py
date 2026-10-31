"""Runs last: purges the demo data the other tests rely on."""
from conftest import login

from app.core.db import session_scope
from app.maintenance import purge_demo_data


def test_purge_demo_data_keeps_real_work(server):
    admin = login(server, "admin")
    real = {"schema_version": "1.0", "integration": {"id": "my-real-api", "name": "My API", "version": "1.0.0", "transport": "http",
            "base_url": "https://api.example.com", "authentication": {"type": "none"},
            "operations": [{"id": "ping", "method": "GET", "path": "/ping", "input_schema": {"type": "object"}, "response": {}}]}}
    assert admin.post("/api/v1/integrations", json={"config": real}).status_code == 200
    before = {i["slug"] for i in admin.get("/api/v1/integrations").json()}
    assert {"notify", "github", "my-real-api"} <= before

    with session_scope() as db:
        summary = purge_demo_data(db)
    assert "notify" in summary["integrations"] and "my-real-api" not in summary["integrations"]

    after = {i["slug"] for i in admin.get("/api/v1/integrations").json()}
    assert after == before - set(summary["integrations"]) and "my-real-api" in after
    assert not any("/mock/" in (i["base_url"] or "") for i in admin.get("/api/v1/integrations").json())
    assert not {c["name"] for c in admin.get("/api/v1/connections").json()} & set(summary["credentials"])
    assert not set(admin.get("/api/v1/events").json()["sources"]) & set(summary["integrations"])  # real sources' events stay
    assert all(t["name"].split(".")[0] not in summary["integrations"] for t in admin.get("/api/v1/tools").json())
    assert admin.get("/api/v1/ai/providers").status_code == 200 and admin.get("/api/v1/auth/me").status_code == 200
    assert any(a["action"] == "maintenance.purge_demo" for a in admin.get("/api/v1/audit").json())
    with session_scope() as db:
        assert purge_demo_data(db)["integrations"] == []        # idempotent
