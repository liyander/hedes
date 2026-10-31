"""Reference configurations in examples/*.yaml.

They seed the demo organisation and act as the offline model's built-in knowledge of well-known services.
"""
from __future__ import annotations

from functools import lru_cache

import yaml

from ..core.config import EXAMPLES_DIR, get_settings
from .validation import normalize_config


@lru_cache
def known_services() -> dict[str, dict]:
    base = get_settings().public_base_url.rstrip("/")
    out = {}
    for path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text().replace("${PUBLIC_BASE_URL}", base))
        if isinstance(doc, dict) and "integration" in doc:
            out[doc["integration"]["id"]] = normalize_config(doc)
    return out


@lru_cache
def demo_services() -> dict[str, dict]:
    """Mock-backed configurations used only when DEMO_MODE is on."""
    base = get_settings().public_base_url.rstrip("/")
    out = {}
    for path in sorted((EXAMPLES_DIR / "demo").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text().replace("${PUBLIC_BASE_URL}", base))
        out[doc["integration"]["id"]] = normalize_config(doc)
    return out


class TemplateError(ValueError):
    pass


@lru_cache
def _raw_templates() -> dict[str, dict]:
    out = {}
    for path in sorted((EXAMPLES_DIR / "sources").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        out[doc["template"]["id"]] = doc
    return out


def source_templates() -> list[dict]:
    """Real third-party data sources the platform can set up from a form (metadata only, no secrets)."""
    return [{**doc["template"], "operations": [{"id": o["id"], "description": o.get("description", ""), "entity": (o.get("ingest") or {}).get("entity", "event"),
                                                "pagination": (o.get("pagination") or {}).get("type", "none")}
                                               for o in doc["integration"]["operations"]],
             "auth_type": doc["integration"]["authentication"]["type"], "base_url": doc["integration"]["base_url"]}
            for doc in _raw_templates().values()]


def build_from_template(template_id: str, variables: dict) -> dict:
    """Fill the template's {{variables}} (validated against each pattern) and return a normalised configuration."""
    import copy
    import json
    import re
    doc = _raw_templates().get(template_id)
    if not doc:
        raise TemplateError(f"Unknown template {template_id!r}")
    values: dict[str, str] = {}
    for var in doc["template"].get("variables") or []:
        v = str(variables.get(var["name"], var.get("default", ""))).strip()
        if not v:
            raise TemplateError(f"{var['label']} is required")
        if var.get("pattern") and not re.fullmatch(var["pattern"], v):
            raise TemplateError(f"{var['label']} {v!r} does not look right (expected {var.get('placeholder', var['pattern'])})")
        values[var["name"]] = v
    text = json.dumps({k: v for k, v in copy.deepcopy(doc).items() if k != "template"})
    for name, v in values.items():
        text = text.replace("{{" + name + "}}", v)
    cfg = json.loads(text)
    return normalize_config(cfg)


def needs_base_url(config: dict) -> bool:
    return "your-domain" in config["integration"]["base_url"]
