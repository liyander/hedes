"""Generic configuration-driven HTTP execution engine.

Executes any operation described by an integration configuration. It contains no service-specific code
and no database access: callers hand it a published configuration, validated inputs and (optionally) a
decrypted credential. ``transport`` lets tests and mock runs swap the network for canned responses.
"""
from __future__ import annotations

import base64
import hashlib
import html
import hmac
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
from jsonschema import Draft202012Validator

from . import egress, jsonpath, templates

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 507, 508, 509, 520, 521, 522, 523, 524}  # 5xx except 501/505 (never transient)
IDEMPOTENT_METHODS = {"GET", "PUT", "DELETE"}
DEFAULT_MAX_WAIT = 10.0
_sleep = time.sleep      # swapped in tests so backoff does not slow the suite
_random = random.random

ERROR_GUIDE = {
    "input_invalid": ("The inputs do not match the operation's input schema.", "Fix the inputs and try again."),
    "credential_missing": ("No usable credential is connected for this integration.", "Create or re-activate the connection in Credentials."),
    "egress_denied": ("The destination was blocked by the network egress policy.", "Only approved public HTTPS destinations can be called."),
    "auth_invalid": ("HTTP 401: the API rejected the credentials (invalid or expired).", "Update the token in Credentials and re-test the connection."),
    "forbidden": ("HTTP 403: the credential lacks the required permission or scope.", "Grant the missing scope to the token, or use a different account."),
    "not_found": ("HTTP 404: the endpoint or resource does not exist.", "Check the identifiers in the inputs; if it persists the endpoint may have moved - request a repair."),
    "rate_limited": ("HTTP 429: the API is rate-limiting requests.", "Retried within policy; slow down or raise the API quota."),
    "upstream_error": ("HTTP 5xx: the external service failed.", "Usually transient. Retried within policy for idempotent operations."),
    "request_rejected": ("HTTP 4xx: the API rejected the request body or parameters.", "The API's request schema may have changed - request a repair."),
    "unexpected_status": ("The API returned a status code the configuration does not expect.", "Review the operation's expected status codes."),
    "timeout": ("The request timed out.", "The service may be slow or unreachable; try again later."),
    "network_error": ("The service could not be reached.", "Check that the base URL is correct and the service is up."),
    "response_invalid": ("The response was not valid JSON.", "Set response.format to text or fix the endpoint."),
    "response_too_large": ("The response exceeded the 2 MB limit.", "Use pagination or narrower queries."),
    "response_drift": ("The API answered, but fields the output mapping expects are missing.", "The response structure changed. A repair can be proposed for admin review."),
    "config_error": ("The configuration could not be rendered into a request.", "Validate the integration configuration."),
}


class EngineError(Exception):
    def __init__(self, error_class: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.http_status = http_status


@dataclass
class PreparedRequest:
    method: str
    url: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    json_body: Any = None
    form_body: dict | None = None


@dataclass
class EngineResult:
    status: str
    output: Any = None
    error_class: str = ""
    error: str = ""
    http_status: int | None = None
    duration_ms: int = 0
    attempts: int = 0
    request_preview: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("status", "output", "error_class", "error", "http_status",
                                               "duration_ms", "attempts", "request_preview", "diagnostics")}


def find_operation(config: dict, op_id: str) -> dict:
    for op in config["integration"]["operations"]:
        if op["id"] == op_id:
            return op
    raise EngineError("config_error", f"Unknown operation {op_id!r}")


def validate_inputs(op: dict, inputs: dict) -> list[str]:
    if not isinstance(inputs, dict):
        return ["Inputs must be a JSON object"]
    v = Draft202012Validator(op.get("input_schema") or {"type": "object"})
    return [f"{'/'.join(map(str, e.absolute_path)) or 'inputs'}: {e.message}"[:300] for e in v.iter_errors(inputs)]


def _query_value(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict,)):
        return json.dumps(v, separators=(",", ":"))
    if isinstance(v, list):
        return [_query_value(x) for x in v]
    return str(v)


def build_request(config: dict, op: dict, inputs: dict) -> PreparedRequest:
    integ = config["integration"]
    ctx = {"inputs": inputs}
    req = op.get("request") or {}
    try:
        path_params = templates.render(req.get("path_params") or {}, ctx)
        path = op["path"]
        for name in re.findall(r"\{([A-Za-z0-9_]+)\}", path):
            if name not in path_params or path_params[name] in ("", None):
                raise EngineError("input_invalid", f"Missing value for path parameter {name!r}")
            value = str(path_params[name])
            if value in (".", ".."):
                raise EngineError("input_invalid", f"Invalid value for path parameter {name!r}")
            path = path.replace("{" + name + "}", quote(value, safe=""))
        query = {k: _query_value(v) for k, v in templates.render(req.get("query") or {}, ctx).items()}
        headers = {**(integ.get("default_headers") or {}), **templates.render(req.get("headers") or {}, ctx)}
        headers = {k: str(v) for k, v in headers.items()}
        json_body = templates.render(req["json_body"], ctx) if req.get("json_body") is not None else None
        if json_body is templates.MISSING:
            json_body = None
        form_body = templates.render(req["form_body"], ctx) if req.get("form_body") is not None else None
    except templates.TemplateError as e:
        raise EngineError("config_error", str(e))
    base = integ["base_url"].rstrip("/")
    url = base + path
    if urlsplit(url).netloc != urlsplit(base).netloc:
        raise EngineError("config_error", "Rendered URL left the configured base URL")
    return PreparedRequest(op["method"], url, query, headers, json_body, form_body)


# ------------------------------------------------------------------------------------ throttling & backoff

_THROTTLE: dict[str, float] = {}
_THROTTLE_LOCK = threading.Lock()


def _throttle(key: str, requests_per_minute: int) -> float:
    """Per-connector outbound limit: spaces requests to one API at least 60/rpm seconds apart. Returns seconds waited."""
    gap = 60.0 / max(1, requests_per_minute)
    with _THROTTLE_LOCK:
        now = time.monotonic()
        at = max(now, _THROTTLE.get(key, 0.0))
        _THROTTLE[key] = at + gap
    wait = min(at - now, 30.0)
    if wait > 0:
        _sleep(wait)
    return max(wait, 0.0)


def server_directed_wait(headers: dict) -> float | None:
    """Seconds the API asked us to wait: Retry-After (delta or HTTP date) or a rate-limit reset header."""
    h = {k.lower(): v for k, v in headers.items()}
    ra = (h.get("retry-after") or "").strip()
    if ra:
        if ra.replace(".", "", 1).isdigit():
            return float(ra)
        try:
            return max(0.0, (parsedate_to_datetime(ra) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            pass
    for name in ("x-ratelimit-reset", "ratelimit-reset", "x-rate-limit-reset"):
        v = (h.get(name) or "").strip()
        if v.replace(".", "", 1).isdigit():
            n = float(v)
            return max(0.0, n - time.time()) if n > 1e9 else n  # epoch timestamp or delta seconds
    return None


def retry_wait(attempt: int, backoff: float, headers: dict, max_wait: float = DEFAULT_MAX_WAIT, jitter: bool = True) -> float:
    """Exponential backoff with 'equal jitter' so parallel runs do not retry in lockstep; the API's own hint is a floor."""
    exp = backoff * (2 ** (attempt - 1))
    wait = exp / 2 + _random() * exp / 2 if jitter else exp
    hinted = server_directed_wait(headers)
    if hinted is not None:
        wait = max(wait, hinted)
    return min(wait, max_wait)


# ------------------------------------------------------------------------------------ AWS Signature V4

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_authorization(method: str, host: str, path: str, query: dict, body: bytes, access_key: str, secret_key: str,
                        region: str, service: str, amz_date: str, session_token: str | None = None) -> str:
    """AWS Signature Version 4 ``Authorization`` header for a request that signs host + x-amz-date (+ session token)."""
    date = amz_date[:8]
    pairs = sorted((quote(str(k), safe="-_.~"), quote(str(v), safe="-_.~")) for k, vs in query.items()
                   for v in (vs if isinstance(vs, list) else [vs]))
    canonical_query = "&".join(f"{k}={v}" for k, v in pairs)
    headers = {"host": host, "x-amz-date": amz_date}
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical = "\n".join([method.upper(), quote(path or "/", safe="/-_.~%"), canonical_query, canonical_headers, signed,
                           hashlib.sha256(body).hexdigest()])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), date), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"


def _host_header(url: str) -> str:
    p = urlsplit(url)
    default = {"http": 80, "https": 443}.get(p.scheme)
    return p.hostname + (f":{p.port}" if p.port and p.port != default else "")


def _sign_request(req: PreparedRequest, auth: dict, secret: dict, body: bytes) -> None:
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = secret.get("session_token") or None
    req.headers["x-amz-date"] = amz_date
    if token:
        req.headers["x-amz-security-token"] = token
    req.headers["Authorization"] = sigv4_authorization(
        req.method, _host_header(req.url), urlsplit(req.url).path, req.query, body, secret["access_key_id"],
        secret["secret_access_key"], auth["region"], auth["service"], amz_date, token)


# ------------------------------------------------------------------------------------ OAuth2 client credentials

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


def _token_key(auth: dict, secret: dict) -> str:
    return hashlib.sha256(f"{auth.get('token_url')}|{secret.get('client_id')}".encode()).hexdigest()


def _fetch_oauth_token(client: httpx.Client, auth: dict, secret: dict, timeout: float, check) -> str:
    key = _token_key(auth, secret)
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(key)
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    check(auth["token_url"])
    data = {"grant_type": "client_credentials"}
    if auth.get("scopes"):
        data["scope"] = " ".join(auth["scopes"])
    basic = base64.b64encode(f"{secret['client_id']}:{secret['client_secret']}".encode()).decode()
    try:
        r = client.post(auth["token_url"], data=data, headers={"Authorization": f"Basic {basic}", "Accept": "application/json"}, timeout=timeout)
    except httpx.TimeoutException:
        raise EngineError("timeout", "The OAuth token endpoint did not respond in time")
    except httpx.TransportError as e:
        raise EngineError("network_error", f"OAuth token endpoint unreachable: {type(e).__name__}")
    if r.status_code in (400, 401, 403):
        raise EngineError("auth_invalid", f"The token endpoint rejected the client credentials (HTTP {r.status_code})", r.status_code)
    if r.status_code != 200:
        raise EngineError("upstream_error", f"The token endpoint failed (HTTP {r.status_code})", r.status_code)
    try:
        body = r.json()
        token, ttl = body["access_token"], float(body.get("expires_in", 300))
    except (ValueError, KeyError, TypeError):
        raise EngineError("response_invalid", "The token endpoint did not return an access_token")
    with _TOKEN_LOCK:
        _TOKEN_CACHE[key] = (token, time.time() + ttl)
    return token


def apply_auth(req: PreparedRequest, auth: dict, credential: dict | None) -> None:
    kind = auth.get("type", "none")
    if kind == "none":
        return
    secret = credential or {}
    if kind == "bearer" and secret.get("token"):
        req.headers["Authorization"] = f"Bearer {secret['token']}"
    elif kind == "api_key_header" and secret.get("api_key"):
        req.headers[auth.get("header_name") or "X-API-Key"] = secret["api_key"]
    elif kind == "api_key_query" and secret.get("api_key"):
        req.query[auth.get("param_name") or "api_key"] = secret["api_key"]
    elif kind == "basic" and secret.get("username") is not None:
        raw = f"{secret['username']}:{secret.get('password', '')}".encode()
        req.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    elif kind == "oauth2_authorization_code" and secret.get("access_token"):
        req.headers["Authorization"] = f"Bearer {secret['access_token']}"  # signed in with the provider; refreshed by the caller
    elif kind == "ssws" and secret.get("token"):
        req.headers["Authorization"] = f"SSWS {secret['token']}"  # Okta
    elif kind == "oauth2_client_credentials" and secret.get("client_id") and secret.get("client_secret"):
        pass  # the bearer token is obtained at send time (it expires and is cached)
    elif kind == "aws_sigv4" and secret.get("access_key_id") and secret.get("secret_access_key"):
        pass  # every request is signed at send time
    else:
        raise EngineError("credential_missing", f"The connection does not hold a {kind} credential")


def redact(req: PreparedRequest, auth: dict) -> dict:
    secret_header = (auth.get("header_name") or "").lower() if auth.get("type") == "api_key_header" else ""
    secret_param = auth.get("param_name") if auth.get("type") == "api_key_query" else None
    headers = {k: ("••••••" if k.lower() in ("authorization", "x-amz-security-token", secret_header) else v) for k, v in req.headers.items()}
    query = {k: ("••••••" if k == secret_param else v) for k, v in req.query.items()}
    preview = {"method": req.method, "url": req.url, "query": query, "headers": headers}
    if req.json_body is not None:
        preview["json"] = req.json_body
    if req.form_body is not None:
        preview["form"] = req.form_body
    return preview


def check_success(rule: dict | None, body: Any, status: int) -> None:
    """APIs that answer HTTP 200 for failures (Slack's {"ok": false, "error": "missing_scope"}): a response whose success
    field does not hold the expected value is a failure, classified from the API's error code."""
    if not rule:
        return
    found, value = jsonpath.query(body, rule["path"])
    if found and value == rule["equals"]:
        return
    code = ""
    if rule.get("error_path"):
        ok, err = jsonpath.query(body, rule["error_path"])
        code = str(err) if ok and err not in (None, "") else ""
    details = []
    for path in rule.get("detail_paths") or []:
        ok, v = jsonpath.query(body, path)
        if ok and v not in (None, "", [], {}):
            details.append(f"{path.rsplit('.', 1)[-1]}: {v if isinstance(v, (str, int, float)) else json.dumps(v)[:200]}")
    cls = (rule.get("error_classes") or {}).get(code, "request_rejected")
    msg = f"HTTP {status} but the API reported a failure" + (f": {code}" if code else f" ({rule['path']} is not {rule['equals']!r})")
    raise EngineError(cls, msg + (f" ({'; '.join(details)})" if details else ""), status)


def error_detail(raw: bytes, headers: dict) -> str:
    """A short, readable reason for a failed response: the API's error-message header if it sends one (NVD does),
    otherwise the visible text of an HTML error page, otherwise the start of the body."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    for name in ("message", "x-error-message", "x-error"):
        if (h.get(name) or "").strip():
            return h[name].strip()[:300]
    text = raw[:20000].decode("utf-8", "replace").strip()
    if "html" in h.get("content-type", "").lower() or text[:200].lstrip().lower().startswith(("<!doctype", "<html")):
        text = re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", " ", text)
        text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
        text = "HTML error page: " + (" ".join(text.split()) or "(no text)")
    else:
        text = " ".join(text.split())
    return text[:300]


def _classify_status(code: int) -> str:
    if code == 401:
        return "auth_invalid"
    if code == 403:
        return "forbidden"
    if code == 404:
        return "not_found"
    if code == 429:
        return "rate_limited"
    if code >= 500:
        return "upstream_error"
    if code in (400, 409, 415, 422):
        return "request_rejected"
    return "unexpected_status"


def map_output(resp_cfg: dict, body: Any) -> tuple[Any, list[str]]:
    mapping = resp_cfg.get("mapping") or {}
    optional = set(resp_cfg.get("optional_fields") or [])
    missing: list[str] = []

    def map_one(doc: Any, check: bool) -> dict:
        out = {}
        for name, path in mapping.items():
            found, value = jsonpath.query(doc, path)
            if found:
                out[name] = value
            elif check and name not in optional:
                missing.append(f"{name} ({path})")
        return out

    if resp_cfg.get("items_path"):
        found, items = jsonpath.query(body, resp_cfg["items_path"])
        if not found or not isinstance(items, list):
            return {"items": [], "count": 0}, [f"items ({resp_cfg['items_path']})"]
        if not mapping:
            return {"items": items, "count": len(items)}, []
        mapped = [map_one(it, i == 0) for i, it in enumerate(items)]
        return {"items": mapped, "count": len(mapped)}, missing
    if not mapping:
        return body, []
    return map_one(body, True), missing


def _encode_body(req: PreparedRequest) -> bytes:
    """Serialise the body once so the exact bytes can be signed (SigV4) and sent."""
    if req.json_body is not None:
        req.headers.setdefault("Content-Type", "application/json")
        return json.dumps(req.json_body, separators=(",", ":")).encode()
    if req.form_body is not None:
        req.headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        return urlencode(req.form_body, doseq=True).encode()
    return b""


def _send(client: httpx.Client, req: PreparedRequest, timeout: float, body: bytes = b"", max_bytes: int = MAX_RESPONSE_BYTES) -> tuple[int, dict, bytes]:
    kwargs: dict = {"params": req.query, "headers": req.headers, "timeout": timeout}
    if body:
        kwargs["content"] = body
    with client.stream(req.method, req.url, **kwargs) as r:
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise EngineError("response_too_large", f"Response exceeded {max_bytes // (1024 * 1024) or 1} MB", r.status_code)
        return r.status_code, dict(r.headers), bytes(buf)


def execute(config: dict, op_id: str, inputs: dict, *, credential: dict | None = None,
            transport: httpx.BaseTransport | None = None, allowed_domains: list[str] | None = None,
            live: bool = True, query_overrides: dict | None = None, raw: bool = False) -> EngineResult:
    """Run one operation. ``live=False`` (mock/test runs) skips DNS-based egress checks.

    ``query_overrides`` adds query parameters after the request is rendered (used for incremental ingestion).
    ``raw=True`` returns the un-mapped records (``{"items": [...], "count": n}``) and skips the drift check; the
    ingestion pipeline maps and validates each record itself so one bad record cannot fail the run.
    """
    started = time.monotonic()
    result = EngineResult(status="failed")
    waits: list[float] = []
    throttled = 0.0
    try:
        integ = config["integration"]
        op = find_operation(config, op_id)
        problems = validate_inputs(op, inputs or {})
        if problems:
            raise EngineError("input_invalid", "; ".join(problems[:5]))
        req = build_request(config, op, inputs or {})
        if query_overrides:
            req.query.update({k: str(v) for k, v in query_overrides.items()})
        auth = integ.get("authentication") or {"type": "none"}
        apply_auth(req, auth, credential)
        result.request_preview = redact(req, auth)

        def egress_check(url: str) -> None:
            try:
                if live:
                    egress.check_url(url, allowed_domains)
                else:
                    egress.static_check(url)
            except egress.EgressDenied as e:
                raise EngineError("egress_denied", str(e))

        egress_check(req.url)

        resp_cfg = op.get("response") or {}
        expected = set(resp_cfg.get("status_codes") or [200, 201, 202, 204])
        timeout = float(op.get("timeout_seconds") or integ.get("timeout_seconds") or 15)
        retry = op.get("retry") or {}
        can_retry = op["method"] in IDEMPOTENT_METHODS or bool(op.get("idempotent"))
        max_attempts = int(retry.get("max_attempts", 2 if can_retry else 1)) if can_retry else 1
        backoff = float(retry.get("backoff_seconds", 0.5))
        max_wait = float(retry.get("max_wait_seconds", DEFAULT_MAX_WAIT))
        jitter = retry.get("jitter", True)
        rpm = (integ.get("rate_limit") or {}).get("requests_per_minute")
        throttle_key = f"{integ.get('id')}|{integ.get('base_url')}"
        pag = op.get("pagination")
        pages_left = int(pag.get("max_pages", 5)) if pag else 1
        page_no, cursor, offset, pages = 1, None, 0, 0
        collected: list = []
        first_body: Any = None
        secret = credential or {}

        with httpx.Client(transport=transport, follow_redirects=False) as client:
            if auth.get("type") == "oauth2_client_credentials":
                req.headers["Authorization"] = "Bearer " + _fetch_oauth_token(client, auth, secret, timeout, egress_check)
            partial: EngineError | None = None
            try:
                while pages_left > 0:
                    if pag and pag["type"] == "page":
                        req.query[pag.get("page_param", "page")] = str(page_no)
                        if pag.get("size_param"):
                            req.query[pag["size_param"]] = str(pag.get("size", 50))
                    elif pag and pag["type"] == "offset":
                        req.query[pag["offset_param"]] = str(offset)
                        req.query[pag["size_param"]] = str(pag.get("size", 50))
                    elif pag and pag["type"] == "cursor" and cursor is not None:
                        req.query[pag["cursor_param"]] = str(cursor)
                    body_bytes = _encode_body(req)

                    status, headers, raw_body = 0, {}, b""
                    for attempt in range(1, max_attempts + 1):
                        result.attempts += 1
                        if rpm:
                            throttled += _throttle(throttle_key, int(rpm))
                        if auth.get("type") == "aws_sigv4":
                            _sign_request(req, auth, secret, body_bytes)
                        try:
                            status, headers, raw_body = _send(client, req, timeout, body_bytes, int(resp_cfg.get("max_bytes") or MAX_RESPONSE_BYTES))
                        except httpx.TimeoutException:
                            if attempt == max_attempts:
                                raise EngineError("timeout", f"No response within {timeout:g}s")
                        except httpx.TransportError as e:
                            if attempt == max_attempts:
                                raise EngineError("network_error", f"{type(e).__name__}: {str(e)[:200]}")
                        else:
                            if status not in RETRYABLE_STATUS or attempt == max_attempts:
                                break
                        wait = retry_wait(attempt, backoff, headers, max_wait, bool(jitter))
                        waits.append(round(wait, 3))
                        _sleep(wait)

                    result.http_status = status
                    if status == 401 and auth.get("type") == "oauth2_client_credentials":
                        with _TOKEN_LOCK:
                            _TOKEN_CACHE.pop(_token_key(auth, secret), None)  # force a fresh token next run
                    if status not in expected:
                        cls = _classify_status(status)
                        detail = error_detail(raw_body, headers)
                        raise EngineError(cls, f"HTTP {status}" + (f": {detail}" if detail else ""), status)
                    if (resp_cfg.get("format") or "json") == "text":
                        body: Any = {"text": raw_body.decode("utf-8", "replace")}
                    elif not raw_body.strip():
                        body = {}
                    else:
                        try:
                            body = json.loads(raw_body)
                        except ValueError:
                            raise EngineError("response_invalid", "Response body is not valid JSON", status)
                    check_success(resp_cfg.get("success"), body, status)
                    if first_body is None:
                        first_body = body
                    pages += 1
                    if not pag:
                        break
                    found, items = jsonpath.query(body, resp_cfg["items_path"])
                    items = items if found and isinstance(items, list) else []
                    collected.extend(items)
                    pages_left -= 1
                    if pag["type"] == "page":
                        if len(items) < int(pag.get("size", 50)) or not items:
                            break
                        page_no += 1
                    elif pag["type"] == "offset":
                        offset += len(items)
                        total_ok, total = jsonpath.query(body, pag["total_path"]) if pag.get("total_path") else (False, None)
                        if not items or len(items) < int(pag.get("size", 50)) or (total_ok and isinstance(total, int) and offset >= total):
                            break
                    elif pag["type"] == "link":
                        if pag.get("next_url_path"):  # the next page's URL is in the body (Microsoft Graph's @odata.nextLink)
                            found_next, nxt = jsonpath.query(body, pag["next_url_path"])
                            nxt = nxt if found_next and isinstance(nxt, str) and nxt.startswith(req.url.split("?")[0]) else None
                        else:
                            nxt = _next_link(headers, req.url)
                        if not nxt or not items:  # some APIs (Okta System Log) keep offering a next link after the last page
                            break
                        req.query.update(dict(parse_qsl(urlsplit(nxt).query)))
                    else:
                        ok, cursor = jsonpath.query(body, pag["next_cursor_path"])
                        if not ok or cursor in (None, ""):
                            break
            except EngineError as e:
                if not (raw and pag and collected):
                    raise
                partial = e  # keep the pages already fetched; the caller decides what to do with an incomplete run

        result.diagnostics = {"response_shape": jsonpath.shape(first_body), "pages": pages}
        if waits:
            result.diagnostics["retry_waits"] = waits
        if throttled:
            result.diagnostics["throttled_seconds"] = round(throttled, 3)
        if raw:
            if not pag:
                found, items = jsonpath.query(first_body, resp_cfg["items_path"]) if resp_cfg.get("items_path") else (False, [])
                collected = items if found and isinstance(items, list) else []
                if not found:
                    raise EngineError("response_drift", f"Records not found at {resp_cfg.get('items_path')}", result.http_status)
            result.output = {"items": collected, "count": len(collected)}
            if partial:
                result.status, result.error_class, result.error = "partial", partial.error_class, str(partial)
                result.http_status = partial.http_status or result.http_status
            else:
                result.status = "succeeded"
        else:
            if pag:
                items_cfg = {**resp_cfg, "items_path": "$"}
                output, missing = map_output(items_cfg, collected)
            else:
                output, missing = map_output(resp_cfg, first_body)
            result.output = output
            if missing:
                result.diagnostics["missing_fields"] = missing
                raise EngineError("response_drift", "Expected fields missing from response: " + ", ".join(missing), result.http_status)
            result.status = "succeeded"
    except EngineError as e:
        result.error_class = e.error_class
        result.error = str(e)
        if e.http_status:
            result.http_status = e.http_status
        if waits:
            result.diagnostics["retry_waits"] = waits
    except (KeyError, TypeError) as e:
        result.error_class = "config_error"
        result.error = f"Configuration error: {e}"
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


def _next_link(headers: dict, current_url: str) -> str | None:
    """RFC 8288 ``Link: <url>; rel="next"``. Only same-host, same-path links are followed (no SSRF via the header)."""
    value = next((v for k, v in headers.items() if k.lower() == "link"), "")
    for part in re.split(r",\s*(?=<)", value):
        m = re.match(r'\s*<([^>]+)>(.*)', part)
        if m and re.search(r'rel="?next"?', m.group(2)):
            url = m.group(1)
            a, b = urlsplit(url), urlsplit(current_url)
            if (a.netloc, a.path) == (b.netloc, b.path):
                return url
    return None


def preview(config: dict, op_id: str, inputs: dict) -> dict:
    """Dry run: render the request exactly as it would be sent, with placeholder credentials."""
    op = find_operation(config, op_id)
    problems = validate_inputs(op, inputs or {})
    req = build_request(config, op, inputs or {}) if not problems else None
    if req is None:
        return {"ok": False, "errors": problems}
    auth = config["integration"].get("authentication") or {"type": "none"}
    placeholder = {"token": "x", "api_key": "x", "username": "x", "password": "x", "client_id": "x", "client_secret": "x",
                   "access_key_id": "x", "secret_access_key": "x", "access_token": "x"}
    apply_auth(req, auth, placeholder)
    out = {"ok": True, "request": redact(req, auth)}
    try:
        egress.static_check(req.url)
    except egress.EgressDenied as e:
        out["egress_warning"] = str(e)
    return out
