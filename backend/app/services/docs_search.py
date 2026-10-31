"""Finding API documentation online for AI Studio.

* ``search`` looks a service up in the public APIs.guru OpenAPI directory (2,500+ APIs, no key needed).
* ``fetch_spec`` downloads an OpenAPI document from a URL (found in the directory or pasted by the user).

Every download goes through the platform's egress policy (public HTTPS only, private and metadata addresses blocked,
redirects re-checked) and a size cap. Documents are untrusted data: they are parsed, never executed, and anything
sent on to an AI model is wrapped as untrusted.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urljoin

import httpx

from . import egress, openapi

DIRECTORY_URL = "https://api.apis.guru/v2/list.json"
DIRECTORY_NAME = "APIs.guru OpenAPI directory"
MAX_SPEC_BYTES = 15 * 1024 * 1024
MAX_DIRECTORY_BYTES = 20 * 1024 * 1024
MAX_OPERATIONS = 400  # very large APIs (GitHub has ~1,000 operations) are trimmed to what is relevant
_CACHE: dict = {"at": 0.0, "data": None}
_LOCK = threading.Lock()
URL = re.compile(r"https://[^\s<>()\"']+")
SPEC_HINT = re.compile(r"(\.json|\.ya?ml)$|openapi|swagger|api-docs", re.I)


class DocsError(Exception):
    pass


def _get(url: str, limit: int) -> bytes:
    """GET with egress checks on every hop (redirects are followed manually and re-checked)."""
    for _ in range(4):
        try:
            egress.check_url(url)
        except egress.EgressDenied as e:
            raise DocsError(f"Blocked by the network policy: {e}")
        try:
            with httpx.stream("GET", url, timeout=30, follow_redirects=False,
                              headers={"Accept": "application/json, application/yaml, text/yaml, */*", "User-Agent": "HADES-docs-fetcher"}) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    raise DocsError(f"HTTP {r.status_code} from {url}")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > limit:
                        raise DocsError(f"The document is larger than {limit // (1024 * 1024)} MB")
                return bytes(buf)
        except httpx.HTTPError as e:
            raise DocsError(f"Could not download {url}: {type(e).__name__}")
    raise DocsError("Too many redirects")


def _directory() -> dict:
    with _LOCK:
        if _CACHE["data"] is not None and time.time() - _CACHE["at"] < 24 * 3600:
            return _CACHE["data"]
    import json
    data = json.loads(_get(DIRECTORY_URL, MAX_DIRECTORY_BYTES))
    with _LOCK:
        _CACHE.update(at=time.time(), data=data)
    return data


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def search(name: str, limit: int = 5) -> list[dict]:
    """Best matching directory entries for a service name, best first."""
    want = _norm(name)
    if len(want) < 3:
        return []
    scored = []
    for key, entry in _directory().items():
        ver = (entry.get("versions") or {}).get(entry.get("preferred") or "") or {}
        info = ver.get("info") or {}
        provider, _, service = key.partition(":")
        title = info.get("title") or key
        domain = _norm(provider.split(".")[0])
        score = 0
        if domain == want:
            score = 100 - (5 if service else 0)            # github.com beats github.com:enterprise
        elif _norm(title) == want:
            score = 90
        elif want in domain or want in _norm(service):
            score = 60
        elif want in _norm(title):
            score = 40
        if score:
            scored.append((score, key, {"key": key, "title": title, "provider": provider,
                                        "openapi_version": str(ver.get("openapiVer") or ""),
                                        "spec_url": ver.get("swaggerUrl") or ver.get("swaggerYamlUrl") or "",
                                        "docs_url": (info.get("x-origin") or [{}])[0].get("url", "") if isinstance(info.get("x-origin"), list) else ""}))
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return [s[2] for s in scored[:limit]]


def fetch_spec(url: str, focus: str = "") -> dict:
    """Download and parse an OpenAPI 3 document. ``focus`` (the user's request) picks the relevant operations of huge APIs."""
    raw = _get(url, MAX_SPEC_BYTES)
    try:
        catalog = openapi.parse_openapi(raw.decode("utf-8", "replace"))
    except openapi.OpenAPIError as e:
        raise DocsError(str(e))
    ops = catalog["operations"]
    if len(ops) > MAX_OPERATIONS:
        words = {w for w in re.findall(r"[a-z]{3,}", focus.lower())} - {"the", "and", "when", "create", "with", "into", "from", "every"}
        ops.sort(key=lambda o: -sum(w in f"{o['id']} {o['path']} {o['summary']} {' '.join(o.get('tags') or [])}".lower() for w in words))
        catalog["operations"] = ops[:MAX_OPERATIONS]
        catalog["trimmed_from"] = len(ops)
    return catalog


SETUP_WORDS = re.compile(r"scope|token|permission|oauth|api key|apikey|authori[sz]|authenticat|credential|required|must|invite|"
                         r"install|admin|role|access|rate limit|error|not_in|forbidden|denied|bot|app password|header", re.I)


def page_text(url: str, limit: int = 3 * 1024 * 1024) -> str:
    """Visible text of a documentation page (HTML stripped), fetched through the egress policy."""
    import html as html_mod
    raw = _get(url, limit).decode("utf-8", "replace")
    raw = re.sub(r"(?is)<(script|style|nav|header|footer|svg|noscript)\b.*?</\1\s*>", " ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6]|/td|/th)\b[^>]*>", "\n", raw)
    text = html_mod.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if len(line) > 2)


KEY_WORDS = re.compile(r"scope|token|permission|invite|member of|must be|not_in|missing|forbidden|install|role", re.I)
ERROR_CODE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")  # error tables list codes such as missing_scope on their own line


def setup_excerpt(text: str, limit: int = 12000) -> str:
    """The passages of a page that matter for setting an integration up, within ``limit`` characters, in page order.
    Scopes, tokens, permissions and the error-code table come first; other setup-related lines fill the rest."""
    lines = [line[:400] for line in text.splitlines()]
    first = {i for i, line in enumerate(lines) if KEY_WORDS.search(line) or ERROR_CODE.match(line)}
    second = {i for i, line in enumerate(lines) if SETUP_WORDS.search(line)} - first
    chosen: set[int] = set()
    size = 0
    for tier in (first, second):
        for i in sorted(tier):
            group = [j for j in (i, i + 1) if j < len(lines) and j not in chosen]  # a term's line and the line explaining it
            cost = sum(len(lines[j]) + 1 for j in group)
            if size + cost > limit:
                continue
            chosen.update(group)
            size += cost
    return "\n".join(lines[i] for i in sorted(chosen))


def spec_urls_in(text: str) -> list[str]:
    """Links in a chat message that look like OpenAPI / Swagger documents."""
    urls = (m.group(0).rstrip(".,;:!?") for m in URL.finditer(text or ""))
    return list(dict.fromkeys(u for u in urls if SPEC_HINT.search(u.split("?", 1)[0])))
