"""Switching the active AI provider: it is tried first for every task, optionally to the exclusion of the others."""
from conftest import login


def _first(c, task="assistant"):
    return next(t["candidates"] for t in c.get("/api/v1/ai/routing").json()["tasks"] if t["task"] == task)


def test_switch_active_provider(server, builder):
    owner, admin = login(server, "owner"), login(server, "admin")
    ids = []
    for name, priority in (("Alpha", 10), ("Beta", 20)):
        r = owner.post("/api/v1/ai/providers", json={"name": name, "provider_type": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
                                                     "api_key": "k-123456789", "model_id": f"{name.lower()}-model"})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        mid = r.json()["models"][0]["id"]
        owner.put(f"/api/v1/ai/models/{mid}", json={"model_id": f"{name.lower()}-model", "priority": priority})
        ids.append(pid)
    alpha, beta = ids
    try:
        assert _first(admin)[0].startswith("Alpha")                                   # automatic: lowest priority wins
        r = admin.put("/api/v1/ai/active-provider", json={"provider_id": beta})
        assert r.status_code == 200 and r.json() == {"active_provider": beta, "active_only": False}
        cands = _first(admin)
        assert cands[0].startswith("Beta") and any(c.startswith("Alpha") for c in cands)   # others stay as fallback
        assert admin.get("/api/v1/ai/routing").json()["active_provider"] == beta

        admin.put("/api/v1/ai/active-provider", json={"provider_id": beta, "only": True})
        cands = _first(admin)
        assert cands[0].startswith("Beta") and not any(c.startswith("Alpha") for c in cands)
        assert "Built-in" in cands[-1]                                                     # last resort is kept

        owner.patch(f"/api/v1/ai/providers/{beta}", json={"status": "disabled"})        # disabled: back to automatic
        assert _first(admin)[0].startswith("Alpha")
        assert admin.put("/api/v1/ai/active-provider", json={"provider_id": beta}).status_code == 409
        assert admin.put("/api/v1/ai/active-provider", json={"provider_id": "missing"}).status_code == 404
        assert builder.put("/api/v1/ai/active-provider", json={"provider_id": alpha}).status_code == 403
    finally:
        admin.put("/api/v1/ai/active-provider", json={"provider_id": ""})
        for pid in ids:
            owner.delete(f"/api/v1/ai/providers/{pid}")
    assert admin.get("/api/v1/ai/routing").json()["active_provider"] == ""
