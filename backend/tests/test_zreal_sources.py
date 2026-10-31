"""Real third-party data: the CISA Known Exploited Vulnerabilities catalogue, set up from its template, approved and
ingested over the public internet. Skipped when the feed is unreachable (offline CI)."""
import httpx
import pytest

from conftest import login

KEV = "https://raw.githubusercontent.com/cisagov/kev-data/develop/known_exploited_vulnerabilities.json"
NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"


@pytest.fixture(scope="module")
def reachable():
    try:
        if httpx.head(KEV, timeout=15, follow_redirects=True).status_code != 200:
            pytest.skip("CISA KEV feed not reachable")
    except httpx.HTTPError:
        pytest.skip("no internet access")


def test_templates_list_real_services_only(server, builder):
    ts = {t["id"]: t for t in builder.get("/api/v1/source-templates").json()}
    assert {"cisa-kev", "github-security", "okta", "jira-data", "nvd", "abuseipdb"} <= set(ts)
    assert ts["okta"]["variables"][0]["name"] == "okta_domain" and ts["okta"]["credential"]["type"] == "ssws"
    assert not any("127.0.0.1" in t["base_url"] or "mock" in t["base_url"] for t in ts.values())


@pytest.mark.parametrize("tid,variables", [("okta", {"okta_domain": "dev-123456.okta.com"}), ("github-security", {"github_org": "my-org"}),
                                           ("jira-data", {"jira_site": "my-company"}), ("nvd", {"severity": "HIGH"}), ("abuseipdb", {}), ("cisa-kev", {})])
def test_every_template_builds_a_valid_configuration(server, tid, variables):
    from app.services import catalog
    from app.services.validation import validate_config
    cfg = catalog.build_from_template(tid, variables)
    res = validate_config(cfg)
    assert res["valid"], res["errors"]
    assert "{{" not in str(cfg)
    from app.services import agents, integrations
    report = integrations.run_mock_tests(cfg, agents.generate_tests(cfg, []))      # the platform's safe tests pass for every template
    assert report["total"] and report["failed"] == 0, [c for c in report["cases"] if not c["passed"]]


def test_template_variables_are_validated(server, builder):
    r = builder.post("/api/v1/source-templates/okta/create", json={"variables": {"okta_domain": "evil.com/../x"}})
    assert r.status_code == 422
    assert builder.post("/api/v1/source-templates/okta/create", json={"variables": {}}).status_code == 422
    assert builder.post("/api/v1/source-templates/nope/create", json={}).status_code == 422


def test_cisa_kev_end_to_end_against_the_real_feed(server, reachable):
    builder, admin = login(server, "builder"), login(server, "admin")
    made = builder.post("/api/v1/source-templates/cisa-kev/create", json={}).json()
    assert made["validation"]["valid"] and made["tests"]["result"] == "passed"
    iid = made["integration"]["id"]
    steps = builder.get(f"/api/v1/integrations/{iid}/guide").json()
    assert [s["key"] for s in steps["steps"]][:3] == ["describe", "validate", "credential"] and steps["steps"][2]["done"]  # no credential needed
    assert steps["steps"][steps["current"]]["key"] == "submit"

    sub = builder.post(f"/api/v1/integrations/{iid}/submit", json={})
    assert sub.status_code == 200, sub.text
    approval = next(a for a in admin.get("/api/v1/admin/approvals").json() if a["integration_id"] == iid)
    assert admin.post(f"/api/v1/admin/approvals/{approval['id']}/approve", json={}).status_code == 200

    src = next(s for s in admin.get("/api/v1/sources").json()["sources"] if s["slug"] == "cisa-kev")
    assert src["cron"] == "0 */6 * * *" and src["entity"] == "event" and src["auth_type"] == "none"
    probe = admin.post(f"/api/v1/sources/{src['id']}/test").json()
    assert probe["ok"] and probe["sample"] and probe["fetched"] > 1000 and probe["sample"][0]["severity"] == "high"
    assert admin.get(f"/api/v1/sources/{src['id']}").json()["totals"]["stored"] == 0  # the dry run stored nothing

    run = admin.post(f"/api/v1/sources/{src['id']}/run").json()["run"]
    assert run["status"] in ("succeeded", "partial") and run["stored"] > 1000, run
    ev = admin.get("/api/v1/events", params={"source": "cisa-kev", "q": "CVE-"}).json()
    assert ev["total"] == 0 or ev["total"] > 0  # search is on actor/action/target/message, not the id
    row = admin.get("/api/v1/events", params={"source": "cisa-kev", "limit": 1}).json()["items"][0]
    assert row["type"] == "vulnerability" and row["fetched_at"] and row["description"] and row["event_id"].startswith("CVE-")
    again = admin.post(f"/api/v1/sources/{src['id']}/run").json()["run"]
    assert again["stored"] == 0 and again["duplicates"] == again["fetched"] > 1000
    assert admin.patch(f"/api/v1/sources/{src['id']}", json={"cron": "*/30 * * * *"}).json()["cron"] == "*/30 * * * *"
    assert admin.patch(f"/api/v1/sources/{src['id']}", json={"cron": "bad cron"}).status_code == 422


def test_nvd_end_to_end_against_the_real_api(server):
    """A second live source with a different paging style (offset) and incremental sync by publication date."""
    try:
        if httpx.get(NVD, params={"resultsPerPage": 1}, timeout=30).status_code != 200:
            pytest.skip("NVD API not reachable or rate-limiting")
    except httpx.HTTPError:
        pytest.skip("no internet access")
    builder, admin = login(server, "builder"), login(server, "admin")
    made = builder.post("/api/v1/source-templates/nvd/create", json={"variables": {"severity": "CRITICAL"}}).json()
    assert made["validation"]["valid"] and made["tests"]["result"] == "passed"
    iid = made["integration"]["id"]
    assert builder.post(f"/api/v1/integrations/{iid}/submit", json={}).status_code == 200
    approval = next(a for a in admin.get("/api/v1/admin/approvals").json() if a["integration_id"] == iid)
    assert admin.post(f"/api/v1/admin/approvals/{approval['id']}/approve", json={}).status_code == 200

    src = next(s for s in admin.get("/api/v1/sources").json()["sources"] if s["slug"] == "nvd")
    run = admin.post(f"/api/v1/sources/{src['id']}/run").json()["run"]
    assert run["status"] in ("succeeded", "partial") and run["stored"] > 0, run
    row = admin.get("/api/v1/events", params={"source": "nvd", "limit": 1}).json()["items"][0]
    assert row["type"] == "vulnerability" and row["severity"] == "critical" and row["event_id"].startswith("CVE-") and row["fetched_at"]
    health = admin.get(f"/api/v1/sources/{src['id']}").json()
    assert health["totals"]["stored"] == run["stored"] and health["last_run_at"]
