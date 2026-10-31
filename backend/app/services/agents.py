"""The H.E.D.E.S agents. Each agent is a function with a JSON-Schema output contract.

Every agent runs through the AI gateway, which picks a model per task. Each also has a deterministic
local implementation (used by the built-in offline model and as a safety net). Agents only *propose*:
they return data that deterministic code validates, and they have no access to credentials,
publishing or approval.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from . import catalog as catalog_mod
from . import openapi
from .ai_gateway import Gateway, wrap_document
from .security_review import summarize as summarize_report
from .validation import _schema, normalize_config, validate_config

# --------------------------------------------------------------------------- requirement understanding

REQUIREMENTS_SCHEMA = {
    "type": "object",
    "required": ["summary", "services", "operations", "triggers", "missing", "ready", "reply"],
    "properties": {
        "summary": {"type": "string"},
        "services": {"type": "array", "items": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"}, "slug": {"type": "string"}, "base_url": {"type": "string"}}}},
        "operations": {"type": "array", "items": {"type": "object", "required": ["service", "action"], "properties": {
            "service": {"type": "string"}, "action": {"type": "string"}, "operation_id": {"type": "string"},
            "kind": {"enum": ["read", "create", "update", "delete", "notify"]}}}},
        "triggers": {"type": "array", "items": {"type": "object", "properties": {
            "service": {"type": "string"}, "event": {"type": "string"}, "condition": {"type": "string"}}}},
        "details": {"type": "object"},
        "missing": {"type": "array", "items": {"type": "string"}},
        "ready": {"type": "boolean"},
        "reply": {"type": "string"},
    },
}

VERBS = {
    "create": ["create", "add", "new", "open", "make", "file", "raise", "submit", "log"],
    "read": ["list", "get", "fetch", "show", "read", "find", "search", "look up", "lookup", "retrieve", "sync"],
    "update": ["update", "change", "close", "move", "assign", "edit", "modify", "mark"],
    "delete": ["delete", "remove", "archive"],
    "notify": ["notify", "alert", "post", "send", "message", "announce", "ping", "tell"],
}
STOP = {"I", "We", "My", "Our", "The", "A", "An", "It", "Please", "Also", "And", "Then", "When", "Whenever",
        "Every", "Each", "If", "Can", "Could", "Should", "Would", "This", "That", "Connect", "Integrate",
        "Create", "Automatically", "OpenAPI", "API", "REST", "JSON", "YAML", "Yes", "No", "Ok", "OK", "Hi", "Hello"}


def op_kind(op: dict) -> str:
    text = f"{op.get('id', '')} {op.get('summary') or op.get('description') or ''}".lower()
    if op["method"] == "GET":
        return "read"
    if op["method"] == "DELETE":
        return "delete"
    if op["method"] in ("PUT", "PATCH"):
        return "update"
    if any(w in text for w in ("message", "notify", "notification", "alert", "post_message")):
        return "notify"
    return "create"


def service_ops(svc: dict, documents: list[dict]) -> list[dict]:
    """Operations available for a service, from its uploaded document or the built-in catalog."""
    doc = next((d for d in documents if d["id"] == svc.get("document_id")), None)
    if doc:
        return doc["catalog"]["operations"]
    cfg = catalog_mod.known_services().get(svc.get("slug", ""))
    return cfg["integration"]["operations"] if cfg else []


def _requested_kinds(text: str) -> set[str]:
    t = " " + text.lower() + " "
    return {k for k, words in VERBS.items() if any(re.search(r"\b" + re.escape(w) + r"\w*\b", t) for w in words)}


def _choose_ops(svc: dict, documents: list[dict], text: str, is_trigger_only: bool) -> list[dict]:
    ops = service_ops(svc, documents)
    kinds = _requested_kinds(text)
    t = text.lower()
    # Nouns mentioned near the service ("ticket", "issue", "task", "message") narrow the choice.
    chosen = []
    for op in ops:
        k = op_kind(op)
        if k in kinds or (k == "create" and "notify" in kinds and op_kind(op) == "notify"):
            chosen.append(op)
    if svc.get("slug") in ("slack", "notify") and "notify" in kinds:
        chosen = [op for op in ops if op_kind(op) == "notify"] or chosen
    if svc.get("slug") == "jira" and re.search(r"\b(ticket|issue)s?\b", t):
        chosen = [op for op in ops if op_kind(op) in kinds or op_kind(op) == "create" and "create" in kinds] or chosen
    if is_trigger_only:
        chosen = [op for op in ops if op_kind(op) == "read"][:2]
    if not chosen:
        chosen = [op for op in ops if op_kind(op) in ("read", "create", "notify")][:6]
    if "delete" not in kinds:
        chosen = [op for op in chosen if op_kind(op) != "delete"]
    return [{"service": svc["slug"], "action": (op.get("summary") or op.get("description") or op["id"])[:120],
             "operation_id": op["id"], "kind": op_kind(op)} for op in chosen]


def _mentioned(word: str, text: str) -> bool:
    """A service named like a verb ("Notify") only counts when written as a name, not as the verb."""
    if any(word in vs for vs in VERBS.values()):
        return re.search(r"\b" + re.escape(word.capitalize()) + r"\b", text) is not None
    return re.search(r"\b" + re.escape(word) + r"\b", text.lower()) is not None


def local_requirements(messages: list[dict], documents: list[dict], prior: dict) -> dict:
    user_text = "\n".join(m["content"] for m in messages if m["role"] == "user")
    lower = user_text.lower()
    known = catalog_mod.known_services()
    services: list[dict] = []
    for d in documents:
        services.append({"name": d["catalog"]["title"], "slug": openapi.slugify(d["catalog"]["title"]),
                         "source": "document", "document_id": d["id"], "base_url": d["catalog"].get("base_url", "")})
    for slug, cfg in known.items():
        words = {slug, cfg["integration"]["name"].split()[0].lower()}
        if any(_mentioned(w, user_text) for w in words) and slug not in {s["slug"] for s in services}:
            services.append({"name": cfg["integration"]["name"], "slug": slug, "source": "catalog",
                             "base_url": cfg["integration"]["base_url"]})
    names = {s["slug"] for s in services} | {s["name"].lower() for s in services}
    unknown = []
    for m in re.finditer(r"\b(?:connect|integrate|with|to|use|from|into)\s+(?:my |our |the |a |an |another )?([A-Z][A-Za-z0-9-]{2,})", user_text):
        cand = m.group(1)
        if cand not in STOP and cand.lower() not in names and openapi.slugify(cand) not in names and cand not in unknown:
            unknown.append(cand)

    details = dict(prior.get("details") or {})
    urls = re.findall(r"https?://[^\s,;)'\"]+", user_text)
    for u in urls:
        if "atlassian.net" in u:
            details["jira_base_url"] = u.rstrip("/.")
        elif "base_url" not in details and not any(u.startswith(s.get("base_url") or "#") for s in services):
            details["base_url"] = u.rstrip("/.")
    ch = re.findall(r"(?<![\w&])#([a-z0-9][\w-]{1,40})", lower)
    if ch:
        details["channel"] = "#" + ch[-1]
    pk = re.search(r"\bproject\s+(?:key\s+)?([A-Z][A-Z0-9]{1,9})\b", user_text)
    if pk:
        details["project_key"] = pk.group(1)
    rp = re.search(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\s+(?:repo|repository)\b|\brepo(?:sitory)?\s+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", user_text)
    if rp:
        details["owner"], details["repo"] = [g for g in rp.groups() if g][:2]

    triggers = []
    tm = re.search(r"\b(?:whenever|when|every time|each time|once|if)\s+(?:a |an )?(?:new )?([^,.;]+)", user_text, re.I)
    if tm:
        clause = tm.group(1)
        svc = next((s for s in services if s["slug"] in clause.lower() or s["name"].lower().split()[0] in clause.lower()), None)
        cond = ""
        lab = re.search(r"label(?:ed|led)?\s+(?:as\s+)?[`'\"]?([\w-]+)", clause, re.I)
        if lab:
            cond = f"labels contains {lab.group(1)}"
        pr = re.search(r"priority\s+(?:is\s+)?([\w-]+)", clause, re.I)
        if pr and not cond:
            cond = f"priority eq {pr.group(1)}"
        triggers.append({"service": svc["slug"] if svc else "", "event": clause.strip()[:120], "condition": cond})

    trigger_svc = triggers[0]["service"] if triggers else ""
    operations = []
    for s in services:
        is_trigger_only = s["slug"] == trigger_svc and len(services) > 1
        operations += _choose_ops(s, documents, user_text, is_trigger_only)

    missing = []
    for u in unknown:
        missing.append(f"I don't have documentation for {u}. Upload its OpenAPI document (JSON or YAML) so I can read its operations.")
    if not services and not unknown:
        missing.append("Which application or API should I connect? Upload its OpenAPI document, or name a service such as GitHub, Jira or Slack.")
    for s in services:
        if s["slug"] == "jira" and not details.get("jira_base_url"):
            missing.append("What is your Jira Cloud site URL (for example https://acme.atlassian.net)?")
        if s.get("source") == "document" and not s.get("base_url", "").startswith("http") and not details.get("base_url"):
            missing.append(f"The {s['name']} document has no server URL. What is the API's base URL?")
    for s in services:
        s.pop("source", None)

    summary = "Connect " + ", ".join(s["name"] for s in services) if services else "Not enough information yet"
    if triggers:
        summary += f"; trigger: {triggers[0]['event']}"
    return {"summary": summary, "services": services, "operations": operations, "triggers": triggers,
            "details": details, "missing": missing, "ready": not missing and bool(services), "reply": ""}


def requirements_from_tools(text: str, tools: dict[str, dict]) -> dict:
    """Requirements for the workflow planner, bound to already-published tools named in the description."""
    req = local_requirements([{"role": "user", "content": text}], [], {})
    lower, kinds = text.lower(), _requested_kinds(text)
    trigger_svc = (req["triggers"] or [{}])[0].get("service", "")
    ops = []
    for name, t in tools.items():
        slug, op_id = name.split(".", 1)
        if slug == trigger_svc or not any(w in lower for w in slug.split("-")):
            continue
        k = op_kind({"id": op_id, "method": t.get("method", "POST"), "description": t.get("description", "")})
        if k in kinds and k != "delete":
            ops.append({"service": slug, "operation_id": op_id, "action": t.get("description") or name, "kind": k})
    req["operations"] = ops
    return req


def compose_reply(req: dict) -> str:
    if req["missing"]:
        lines = ["Thanks - I need a little more before I can build this:"]
        lines += [f"- {q}" for q in req["missing"]]
        if req["services"]:
            lines.insert(1, "So far I understand: " + req["summary"] + ".")
        return "\n".join(lines)
    lines = [f"Here's what I'll build: **{req['summary']}**.", ""]
    by_svc: dict[str, list] = {}
    for op in req["operations"]:
        by_svc.setdefault(op["service"], []).append(op)
    for s in req["services"]:
        ops = by_svc.get(s["slug"], [])
        lines.append(f"- **{s['name']}**: " + (", ".join(f"`{o.get('operation_id') or o['action']}`" for o in ops) or "no operations selected"))
    if req["triggers"]:
        t = req["triggers"][0]
        lines.append(f"- **Workflow**: when {t['event']}" + (f" (condition: {t['condition']})" if t.get("condition") else ""))
    lines += ["", "Credentials are never shared with me - you'll connect them through a secure form. "
              "Press **Generate integration** when you're ready, or tell me what to change."]
    return "\n".join(lines)


def analyze_request(gw: Gateway, messages: list[dict], documents: list[dict], prior: dict) -> tuple[dict, str]:
    known = catalog_mod.known_services()
    known_list = "\n".join(f"- {k}: {v['integration']['name']} ({v['integration']['base_url']}) ops: "
                           + ", ".join(o["id"] for o in v["integration"]["operations"]) for k, v in known.items())
    docs = "\n\n".join(wrap_document(d["name"], openapi.catalog_summary(d["catalog"])) for d in documents)
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages[-20:])
    system = ("You are the Requirement Understanding Agent of H.E.D.E.S, a zero-code integration platform. "
              "Turn the conversation into structured integration requirements. Use the slug of a known service or of an "
              "uploaded document's title. For each operation set operation_id to an operation id that exists in the "
              "known services or documents. Ask for anything essential that is missing (documentation for unknown "
              "services, tenant-specific base URLs such as Jira site URLs). Never ask for passwords, tokens or API keys: "
              "credentials are connected through a separate secure form. 'reply' is your conversational answer.")
    prompt = f"Known services:\n{known_list}\n\nUploaded documents:\n{docs or '(none)'}\n\nPreviously collected details: {json.dumps(prior.get('details') or {})}\n\nConversation:\n{convo}"
    res = gw.structured("requirements", system, prompt, REQUIREMENTS_SCHEMA,
                        local=lambda: local_requirements(messages, documents, prior))
    req = res.data
    # Deterministic reconciliation: whatever the model said, bind services to real sources.
    base = local_requirements(messages, documents, prior)
    req.setdefault("details", {})
    req["details"] = {**base["details"], **(req.get("details") or {})}
    for s in req["services"]:
        s["slug"] = openapi.slugify(s.get("slug") or s["name"])
        doc = next((d for d in documents if openapi.slugify(d["catalog"]["title"]) == s["slug"]), None)
        if doc:
            s["document_id"] = doc["id"]
            s["source"] = "document"
        elif s["slug"] in known:
            s["source"] = "catalog"
        elif not res.local:
            s["source"] = "model_knowledge"
    for s in base["services"]:
        if s["slug"] not in {x["slug"] for x in req["services"]}:
            req["services"].append({**s, "source": "document" if s.get("document_id") else "catalog"})
    if not req["operations"]:
        req["operations"] = base["operations"]
    req["missing"] = list(dict.fromkeys((req.get("missing") or []) + [q for q in base["missing"] if "Jira Cloud site" in q or "base URL" in q]))
    if any(s["slug"] == "jira" for s in req["services"]) and req["details"].get("jira_base_url"):
        req["missing"] = [q for q in req["missing"] if "Jira Cloud site" not in q]
    req["ready"] = bool(req["services"]) and not req["missing"]
    if res.local or not req.get("reply"):
        req["reply"] = compose_reply(req)
    return req, res.model


# --------------------------------------------------------------------------- discovery + generation

def config_generation_schema() -> dict:
    return _schema("integration.schema.json").schema


def _draft_for_service(svc: dict, req: dict, documents: list[dict]) -> tuple[dict | None, str]:
    """A deterministic draft built from the service's documentation or catalog entry."""
    op_ids = [o.get("operation_id") for o in req["operations"] if o["service"] == svc["slug"] and o.get("operation_id")]
    details = req.get("details") or {}
    doc = next((d for d in documents if d["id"] == svc.get("document_id")), None)
    if doc:
        cat = doc["catalog"]
        valid_ids = [i for i in op_ids if any(o["id"] == i for o in cat["operations"])]
        base_url = cat.get("base_url") or details.get("base_url") or svc.get("base_url")
        return openapi.catalog_to_config(cat, valid_ids or None, svc["slug"], cat["title"], base_url), "document"
    known = catalog_mod.known_services().get(svc["slug"])
    if known:
        cfg = copy.deepcopy(known)
        ops = cfg["integration"]["operations"]
        if op_ids:
            cfg["integration"]["operations"] = [o for o in ops if o["id"] in op_ids] or ops
        if svc["slug"] == "jira" and details.get("jira_base_url"):
            cfg["integration"]["base_url"] = details["jira_base_url"]
        return cfg, "catalog"
    return None, "model_knowledge"


def generate_config(gw: Gateway, svc: dict, req: dict, documents: list[dict]) -> tuple[dict, dict]:
    """Integration Architect + Configuration Generator. Returns (config, meta)."""
    draft, source = _draft_for_service(svc, req, documents)
    schema = config_generation_schema()
    system = ("You are the Configuration Generator Agent of H.E.D.E.S. Produce a declarative integration configuration "
              "that a generic HTTP engine will execute. Use only ${inputs.<name>} references in request templates, "
              "JSONPath ($.field) in response mappings, and never put credentials in the configuration - authentication "
              "is supplied at runtime from the connection named in credential_ref. Write clear operation descriptions.")
    meta: dict = {"source": source}

    if draft is None:
        models = gw.select_models("config_generation")
        if models and models[0][1].provider_type != "local":
            prompt = f"Requirements:\n{json.dumps(req, indent=1)}\n\nGenerate the configuration for the service {svc['name']!r} with integration.id {svc['slug']!r} and credential_ref {svc['slug']!r}."
            res = gw.structured("config_generation", system, prompt, schema, local=lambda: {},
                                validate_extra=lambda d: [e["message"] for e in validate_config(normalize_config(d))["errors"]][:8])
            cfg = normalize_config(res.data)
            cfg["integration"]["id"] = svc["slug"]
            meta.update(model=res.model, note="Generated from model knowledge - not verified against documentation.")
            return cfg, meta
        raise ValueError(f"No documentation for {svc['name']}. Upload its OpenAPI document.")

    draft = normalize_config(draft)
    host = draft["integration"]["base_url"]

    def guard(data) -> list[str]:
        cfg = normalize_config(data)
        problems = [e["path"] + ": " + e["message"] for e in validate_config(cfg)["errors"]][:8]
        integ = cfg.get("integration") or {}
        if integ.get("base_url") != host:
            problems.append(f"base_url must remain {host}")
        if (integ.get("authentication") or {}).get("type") != draft["integration"]["authentication"]["type"]:
            problems.append("authentication.type must not change")
        allowed = {o["id"] for o in draft["integration"]["operations"]}
        if not {o.get("id") for o in integ.get("operations") or []} <= allowed:
            problems.append("operations must be a subset of: " + ", ".join(sorted(allowed)))
        return problems

    doc = next((d for d in documents if d["id"] == svc.get("document_id")), None)
    prompt = (f"Requirements:\n{json.dumps({k: req[k] for k in ('summary', 'operations', 'triggers', 'details')}, indent=1)}\n\n"
              + (wrap_document(doc["name"], openapi.catalog_summary(doc["catalog"])) + "\n\n" if doc else "")
              + "Draft configuration (derived deterministically from the documentation). Improve descriptions and output "
                "mappings for the user's goal; keep base_url, authentication type and operation ids:\n" + json.dumps(draft, indent=1))
    res = gw.structured("config_generation", system, prompt, schema, local=lambda: draft, validate_extra=guard)
    cfg = normalize_config(res.data)
    cfg["integration"]["id"] = svc["slug"]
    meta["model"] = res.model
    if res.local:
        meta["note"] = "Built deterministically from the " + ("uploaded OpenAPI document." if source == "document" else "built-in service catalog.")
    return cfg, meta


# --------------------------------------------------------------------------- security narrative

SECURITY_SCHEMA = {"type": "object", "required": ["summary"], "properties": {
    "summary": {"type": "string"}, "concerns": {"type": "array", "items": {"type": "string"}}}}


def explain_risks(gw: Gateway, report: dict) -> tuple[dict, str]:
    system = ("You are the Security Review Agent. Explain, for an administrator deciding whether to approve, what this "
              "integration can do and the main risks. Do not invent findings; the deterministic findings are authoritative.")
    slim = {k: report[k] for k in ("score", "level", "destinations", "capabilities", "operations", "findings")}
    res = gw.structured("security_review", system, json.dumps(slim), SECURITY_SCHEMA,
                        local=lambda: {"summary": summarize_report(report),
                                       "concerns": [f["title"] + ": " + f["detail"] for f in report["findings"] if f["severity"] in ("critical", "high")]})
    return res.data, res.model


# --------------------------------------------------------------------------- tests

def _doc_from_mapping(resp: dict) -> Any:
    """Synthesise a response body that satisfies the configured mapping (when no documented schema exists)."""
    def put(doc: dict, path: str, value):
        segs = re.findall(r"\.([A-Za-z0-9_\-]+)|\['([^']+)'\]", path)
        keys = [a or b for a, b in segs]
        cur = doc
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
            if not isinstance(cur, dict):
                return
        if keys:
            cur[keys[-1]] = value
    item: dict = {}
    for name, path in (resp.get("mapping") or {}).items():
        put(item, path, 42 if name.endswith(("id", "number")) else True if name in ("ok", "delivered") else f"sample {name}")
    ip = resp.get("items_path")
    if not ip:
        return item
    if ip == "$":
        return [item]
    wrapper: dict = {}
    put(wrapper, ip, [item])
    return wrapper


def generate_tests(config: dict, documents: list[dict]) -> list[dict]:
    """Testing Agent: deterministic, schema-derived fixtures (happy path, input validation, auth failure)."""
    doc_ops = {}
    for d in documents:
        for op in d["catalog"]["operations"]:
            doc_ops[op["id"]] = op
    cases = []
    auth = config["integration"]["authentication"]["type"]
    for op in config["integration"]["operations"]:
        schema = op["input_schema"]
        inputs = {k: openapi.sample_value(v, k) for k, v in (schema.get("properties") or {}).items()
                  if k in (schema.get("required") or []) or "default" not in v}
        status = (op["response"].get("status_codes") or [200])[0]
        documented = doc_ops.get(op["id"])
        if documented and (documented.get("response_example") is not None or documented.get("response_schema")):
            body = documented["response_example"] if documented.get("response_example") is not None else openapi.sample_value(documented["response_schema"])
            origin = "documented response schema"
        else:
            resp = op["response"]
            if op.get("ingest") and not resp.get("mapping"):
                paths: dict[str, str] = {}
                for fname, fspec in op["ingest"]["fields"].items():  # a field may be a JSONPath or {path | paths, transform, ...}
                    for n, path in enumerate([fspec] if isinstance(fspec, str) else ([fspec["path"]] if fspec.get("path") else []) + list(fspec.get("paths") or [])):
                        paths[f"{fname}_{n}" if n else fname] = path
                resp = {**resp, "mapping": paths}
            body = _doc_from_mapping(resp)
            origin = "synthesised from mapping"
        if status == 204:
            body = None
        cases.append({"operation": op["id"], "name": f"{op['id']}: happy path ({origin})", "inputs": inputs,
                      "mock_status": status, "mock_response": body, "expect": "succeeded"})
        required = schema.get("required") or []
        if required:
            bad = {k: v for k, v in inputs.items() if k != required[0]}
            cases.append({"operation": op["id"], "name": f"{op['id']}: rejects missing '{required[0]}'", "inputs": bad,
                          "mock_status": status, "mock_response": body, "expect": "input_invalid"})
        if auth != "none":
            cases.append({"operation": op["id"], "name": f"{op['id']}: classifies expired credentials", "inputs": inputs,
                          "mock_status": 401, "mock_response": {"message": "Bad credentials"}, "expect": "auth_invalid"})
    return cases


# --------------------------------------------------------------------------- repair

REPAIR_SCHEMA = {"type": "object", "required": ["diagnosis", "mapping_changes", "confidence"], "properties": {
    "diagnosis": {"type": "string"},
    "mapping_changes": {"type": "object", "additionalProperties": {"type": "string"}},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}


def _tokens(path: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", re.sub(r"\[\d+\]|\[\*\]", "", path.lower())) if t and t != "$"}


def local_repair(op: dict, missing_fields: list[str], response_shape: dict) -> dict:
    mapping = op["response"].get("mapping") or {}
    items_path = op["response"].get("items_path")
    shape = response_shape
    if items_path:
        # Paths in the mapping are relative to each item: re-root the shape at the first item.
        root = "$[0]" if items_path == "$" else items_path + "[0]"
        shape = {"$" + p[len(root):]: t for p, t in response_shape.items() if p.startswith(root)}
    changes, notes = {}, []
    used = set(mapping.values())
    for entry in missing_fields:
        name = entry.split(" (")[0]
        old = mapping.get(name)
        if not old:
            continue
        old_leaf = _tokens(old)
        old_parent = _tokens(old.rsplit(".", 1)[0]) if "." in old[2:] else set()
        best, best_score = None, 0.0
        for path, typ in shape.items():
            if typ in ("object", "array") or path in used:
                continue
            toks = _tokens(path)
            overlap = len(old_leaf & toks) / max(1, len(old_leaf | toks))
            parent_bonus = 0.3 if old_parent and old_parent <= toks else 0
            score = overlap + parent_bonus
            if score > best_score:
                best, best_score = path, score
        if best and best_score >= 0.3:
            changes[name] = best
            notes.append(f"'{name}' was read from {old}, which no longer exists; the response now has {best}.")
    diagnosis = (" ".join(notes) or "The response structure changed but no confident replacement was found.") + \
        " (Schema drift detected by comparing the configured mapping with the observed response structure.)"
    return {"diagnosis": diagnosis, "mapping_changes": changes, "confidence": 0.75 if changes else 0.1}


def propose_repair(gw: Gateway, config: dict, op_id: str, error_class: str, missing_fields: list[str], response_shape: dict) -> tuple[dict, str]:
    op = next(o for o in config["integration"]["operations"] if o["id"] == op_id)
    system = ("You are the Repair Agent. An integration's output mapping no longer matches the API response. Given the "
              "current mapping, the fields that failed and the observed response STRUCTURE (types only, no values), "
              "propose new JSONPath expressions for the failed fields. Only use paths that exist in the observed structure.")
    prompt = json.dumps({"operation": op_id, "error_class": error_class, "current_mapping": op["response"].get("mapping"),
                         "items_path": op["response"].get("items_path"), "missing_fields": missing_fields,
                         "observed_structure": response_shape}, indent=1)
    res = gw.structured("repair", system, prompt, REPAIR_SCHEMA, local=lambda: local_repair(op, missing_fields, response_shape))
    return res.data, res.model


def sample_from_shape(response_shape: dict) -> Any:
    """Rebuild a structurally equivalent document from a shape (for regression-testing a repair)."""
    root: Any = None
    for path in sorted(response_shape, key=len):
        typ = response_shape[path]
        value = {"object": {}, "array": [], "string": "sample", "number": 1, "boolean": True, "null": None}[typ]
        if path == "$":
            root = value
            continue
        segs = re.findall(r"\.([A-Za-z0-9_\-]+)|\['([^']+)'\]|\[(\d+)\]", path[1:])
        cur = root
        for i, (a, b, idx) in enumerate(segs):
            last = i == len(segs) - 1
            if idx:
                if not isinstance(cur, list):
                    break
                if not cur:
                    cur.append(value if last else {})
                cur = cur[0]
            else:
                key = a or b
                if not isinstance(cur, dict):
                    break
                if last:
                    cur.setdefault(key, value)
                cur = cur.setdefault(key, {})
    return root


# --------------------------------------------------------------------------- workflow planning

def _workflow_schema() -> dict:
    return _schema("workflow.schema.json").schema


def local_workflow(req: dict, tools: dict[str, dict]) -> dict:
    trig = (req.get("triggers") or [{}])[0]
    details = req.get("details") or {}
    trigger_svc = trig.get("service", "")
    targets = [o for o in req.get("operations") or [] if o["service"] != trigger_svc and o.get("kind") in ("create", "notify", "update")]
    targets.sort(key=lambda o: o.get("kind") == "notify")  # create first, so notifications can reference its output
    nodes: list[dict] = []
    first_create = None
    sample = {"title": "SQL injection in login form", "body": "Reported by the scanner", "url": "https://github.com/acme/platform/issues/101",
              "labels": ["security"], "number": 101, "priority": "high", "author": "alex"}
    for i, o in enumerate(targets):
        name = f"{o['service']}.{o['operation_id']}"
        tool = tools.get(name) or {}
        props = ((tool.get("input_schema") or {}).get("properties")) or {}
        required = ((tool.get("input_schema") or {}).get("required")) or list(props)[:2]
        ref_out = ""
        if first_create and tools.get(first_create["tool"]):
            fields = tools[first_create["tool"]].get("output_fields") or []
            pick = next((f for f in fields if re.search(r"key|url|number", f)), fields[0] if fields else "")
            ref_out = f" ({'${steps.' + first_create['id'] + '.output.' + pick + '}'})" if pick else ""
        inputs = {}
        for k in required:
            lk = k.lower()
            if lk in ("title", "summary", "subject", "name"):
                inputs[k] = "${trigger.title}"
            elif lk in ("text", "message", "body", "description"):
                inputs[k] = "New security issue: ${trigger.title}" + ref_out + " ${trigger.url}"
            elif lk == "channel":
                inputs[k] = details.get("channel", "#security")
            elif lk == "project_key":
                inputs[k] = details.get("project_key", "SEC")
            elif lk in ("owner", "repo"):
                inputs[k] = details.get(lk, "")
            elif lk == "priority":
                inputs[k] = "${trigger.priority}"
            elif "severity" in lk:
                inputs[k] = "critical"
            else:
                inputs[k] = "${trigger." + k + "}"
        for k in ("labels", "severity", "priority", "description"):
            if k in props and k not in inputs:
                inputs[k] = ["security"] if k == "labels" and props[k].get("type") == "array" else \
                    "critical" if k == "severity" else "high" if k == "priority" else "${trigger.body}"
        node = {"id": f"n{i + 1}", "label": o["action"][:120], "tool": name, "inputs": inputs, "on_failure": "stop"}
        if first_create:
            node["depends_on"] = [first_create["id"]]
        if trig.get("condition"):
            m = re.match(r"(\w+)\s+(contains|eq|neq)\s+(.+)", trig["condition"])
            if m:
                node["when"] = [{"left": "${trigger." + m.group(1) + "}", "op": m.group(2), "right": m.group(3).strip()}]
        nodes.append(node)
        if first_create is None and o.get("kind") in ("create", "update"):
            first_create = node
    return {"trigger": {"type": "webhook" if trig else "manual", "description": trig.get("event", "Run manually"), "sample": sample},
            "nodes": nodes or [{"id": "n1", "tool": "notify.send_message", "inputs": {"channel": "general", "text": "${trigger.title}"}}]}


def plan_workflow(gw: Gateway, req: dict, tools: dict[str, dict]) -> tuple[dict, str]:
    system = ("You are the Workflow Planning Agent. Build a workflow graph that connects the available tools to meet the "
              "requirements. Reference trigger data as ${trigger.<field>} and earlier step outputs as "
              "${steps.<node_id>.output.<field>} (the referenced node must be in depends_on). Use 'when' conditions "
              "for filters. Only use tool names from the list.")
    tool_list = {k: {"inputs": v.get("input_schema"), "outputs": v.get("output_fields")} for k, v in tools.items()}
    prompt = f"Requirements:\n{json.dumps(req, indent=1)}\n\nAvailable tools:\n{json.dumps(tool_list, indent=1)}"
    res = gw.structured("workflow_planning", system, prompt, _workflow_schema(), local=lambda: local_workflow(req, tools))
    return res.data, res.model


# --------------------------------------------------------------------------- error explanation

def explain_error(gw: Gateway, tool_name: str, error_class: str, error: str, guide: tuple[str, str]) -> tuple[str, str]:
    system = "You are the H.E.D.E.S failure analyst. In 2-3 sentences explain the failure and the next step for a non-expert."
    msg = f"Tool: {tool_name}\nError class: {error_class}\nDetail: {error[:400]}\nKnown guidance: {guide[0]} {guide[1]}"
    res = gw.text("error_explanation", system, [{"role": "user", "content": msg}], local=lambda: f"{guide[0]} {guide[1]}")
    return res.text, res.model

