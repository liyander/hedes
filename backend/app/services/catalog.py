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


def needs_base_url(config: dict) -> bool:
    return "your-domain" in config["integration"]["base_url"]
