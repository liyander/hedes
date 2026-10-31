from datetime import timedelta
from types import SimpleNamespace

import httpx

from app.core.db import utcnow
from app.services import catalog, engine, ingest

NVD_404_PAGE = (b"<html><head><title>Web Services</title></head><body><h2>NVD Web Services Endpoint</h2><h3>0.3</h3>Available at: TBD"
                b"<ul><li>Initial public draft</li></ul><script>(function(){var b=a.contentDocument;})()</script></body></html>")


def test_error_details_are_readable():
    assert engine.error_detail(NVD_404_PAGE, {"Content-Type": "text/html"}) == \
        "HTML error page: NVD Web Services Endpoint 0.3 Available at: TBD Initial public draft"
    assert engine.error_detail(NVD_404_PAGE, {"message": "Date range cannot exceed 120 days."}) == "Date range cannot exceed 120 days."
    assert engine.error_detail(b'{"error":  "bad key"}', {"content-type": "application/json"}) == '{"error": "bad key"}'


def test_failed_execution_reports_the_reason_not_the_markup():
    cfg = catalog.build_from_template("nvd", {"severity": "CRITICAL"})
    t = httpx.MockTransport(lambda r: httpx.Response(404, content=NVD_404_PAGE, headers={"content-type": "text/html"}))
    r = engine.execute(cfg, "list_cves", {}, transport=t, live=False)
    assert r.error_class == "not_found" and "<" not in r.error and "script" not in r.error and r.output is None


def test_nvd_template_uses_the_real_endpoint_and_a_date_window():
    cfg = catalog.build_from_template("nvd", {"severity": "HIGH"})
    op = cfg["integration"]["operations"][0]
    assert op["path"] == "/rest/json/cves/2.0" and op["request"]["query"] == {"cvssV3Severity": "HIGH"}
    spec = op["ingest"]
    first = ingest._incremental_query(SimpleNamespace(watermark=""), spec)
    start = ingest.parse_timestamp(first["pubStartDate"])
    assert abs((utcnow() - start) - timedelta(days=30)) < timedelta(minutes=1) and "pubEndDate" in first     # recent, not 1999
    old = ingest._incremental_query(SimpleNamespace(watermark="2019-01-01T00:00:00"), spec)
    assert utcnow() - ingest.parse_timestamp(old["pubStartDate"]) <= timedelta(days=120, minutes=1)          # NVD's 120-day limit
    assert ingest._incremental_query(SimpleNamespace(watermark=""), {"incremental": {"param": "since"}}) == {}  # other sources unchanged
