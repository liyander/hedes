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


def template_guide(template_id: str) -> dict:
    """The vetted, provider-specific setup guide of one template: where to get the credential, steps, docs."""
    doc = _raw_templates().get(template_id)
    if not doc:
        raise TemplateError(f"Unknown template {template_id!r}")
    t, auth = doc["template"], doc["integration"]["authentication"]
    cred = t.get("credential") or {}
    return {"id": t["id"], "title": t["title"], "category": t.get("category", ""), "summary": t.get("summary", ""),
            "docs": t.get("docs", ""), "free_tier": t.get("free_tier", ""), "base_url": doc["integration"]["base_url"],
            "credential": {"type": auth["type"], "name": cred.get("name") or auth.get("credential_ref"), "how": cred.get("how", ""),
                           "header_name": auth.get("header_name", "")} if auth["type"] != "none" else None,
            "variables": [{k: v[k] for k in ("name", "label", "placeholder", "help") if k in v} for v in t.get("variables") or []],
            "setup": list(t.get("setup") or []), "source": "template"}


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
        text = text.replace("{{" + name + "}}", json.dumps(v)[1:-1])  # JSON-escaped: a value cannot alter the structure
    cfg = json.loads(text)
    return normalize_config(cfg)


def needs_base_url(config: dict) -> bool:
    return "your-domain" in config["integration"]["base_url"]


# Official documentation and where to get a credential, for the services the platform ships reference configurations for.
SERVICE_GUIDES = {
    "github": {"docs": "https://docs.github.com/en/rest",
               "credential": "GitHub > your avatar > Settings > Developer settings > Personal access tokens > Fine-grained tokens. "
                             "Give it access to the repositories and the Issues permission it needs.",
               "operations": {"create_issue": "https://docs.github.com/en/rest/issues/issues#create-an-issue",
                              "list_issues": "https://docs.github.com/en/rest/issues/issues#list-repository-issues",
                              "add_comment": "https://docs.github.com/en/rest/issues/comments#create-an-issue-comment"},
               "setup": ["Create a fine-grained personal access token and select the repositories it may use.",
                         "Repository permissions: Issues - Read and write (read-only is enough for listing).",
                         "If the organisation requires approval for fine-grained tokens, an organisation owner must approve it."],
               "errors": [("401 Bad credentials", "The token is wrong, expired or revoked.", "Create a new token and rotate it in Credentials."),
                          ("403 Resource not accessible by personal access token", "The token lacks the Issues permission or the repository.",
                           "Edit the token: add the repository and Issues - Read and write."),
                          ("404 Not Found", "The repository is private and the token cannot see it, or owner/repo is misspelled.",
                           "Check owner/repo and the token's repository access.")]},
    "slack": {"docs": "https://api.slack.com/methods",
              "credential": "api.slack.com/apps > your app > OAuth & Permissions: add the chat:write scope, install the app and copy the Bot User OAuth Token (xoxb-…).",
              "operations": {"post_message": "https://api.slack.com/methods/chat.postMessage",
                             "list_channels": "https://api.slack.com/methods/conversations.list"},
              "setup": ["Create a Slack app at api.slack.com/apps (From scratch) in your workspace.",
                        "OAuth & Permissions > Bot Token Scopes: add chat:write (and channels:read to list channels). Add chat:write.public "
                        "to post in public channels without inviting the app.",
                        "Install (or Reinstall) to Workspace after changing scopes, then copy the Bot User OAuth Token. It starts with xoxb-; "
                        "user (xoxp-) and app-level (xapp-) tokens do not work here.",
                        "Invite the app to each channel it posts to: type /invite @your-app in the channel."],
              "errors": [("missing_scope", "The token does not have the scope the method needs (the response lists needed and provided).",
                          "Add the scope under Bot Token Scopes, reinstall the app and rotate the token."),
                         ("not_in_channel", "The app is not a member of the channel.", "Invite it with /invite @your-app, or add chat:write.public."),
                         ("channel_not_found", "The channel name or ID is wrong, or the app cannot see a private channel.",
                          "Use the channel ID (channel details > About) and invite the app."),
                         ("invalid_auth / not_authed", "The token is wrong, revoked or missing.", "Copy the Bot User OAuth Token again and rotate it.")]},
    "jira": {"docs": "https://developer.atlassian.com/cloud/jira/platform/rest/v3/",
             "credential": "id.atlassian.com/manage-profile/security/api-tokens > Create API token. The username is your Atlassian account email.",
             "operations": {"create_issue": "https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-post",
                            "get_issue": "https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-get"},
             "setup": ["Create an API token at id.atlassian.com > Security > API tokens. Use your Atlassian account email as the username.",
                       "The account needs the Browse projects and Create issues permissions in the target project.",
                       "Use the project key (the prefix of issue numbers, e.g. SEC) and an issue type that exists in that project."],
             "errors": [("401 Unauthorized", "Wrong email or API token (a password does not work).", "Create a new API token and rotate it."),
                        ("400 issuetype / project errors", "The project key or issue type does not exist, or a required field is missing.",
                         "Check the project key and issue type in Jira's project settings.")]},
}

AUTH_GUIDE = {
    "bearer": "an API token (bearer token). Look for 'API tokens', 'Personal access tokens' or 'Developer settings' in the service's account settings.",
    "api_key_header": "an API key. Look for 'API keys' in the service's account, developer or admin settings.",
    "api_key_query": "an API key. Look for 'API keys' in the service's account, developer or admin settings.",
    "basic": "a username (often your email) and a password or API token. Many services issue an API token to use instead of your password.",
    "ssws": "an Okta API token: Okta Admin Console > Security > API > Tokens > Create token.",
    "oauth2_authorization_code": "nothing to copy: press Continue with <provider> in Credentials and approve read access (an administrator sets up the OAuth app once in Settings > Sign-in providers).",
    "oauth2_client_credentials": "a client ID and client secret from an OAuth app (often under 'Apps', 'Integrations' or 'API clients' in the admin console).",
    "aws_sigv4": "an AWS access key pair for an IAM user or role with permission for this API (IAM console > Users > Security credentials).",
}


def credential_help(slug: str, auth_type: str) -> str:
    if auth_type in (None, "", "none"):
        return "No credential is needed."
    specific = (SERVICE_GUIDES.get(slug) or {}).get("credential")
    return specific or f"You need {AUTH_GUIDE.get(auth_type, 'the credential the API documentation describes.')}"


def how_to_find(question: str) -> str:
    """Where a user finds the answer to one of the AI Studio's open questions."""
    q = question.lower()
    if "jira" in q and "url" in q:
        return "It is the address you open Jira at in your browser, e.g. https://yourcompany.atlassian.net - look at the address bar on any Jira page."
    if "base url" in q or "server url" in q:
        return ("The API documentation lists it under 'Base URL', 'Servers', 'Endpoint' or 'Host' (for example https://api.vendor.com/v1). "
                "It is usually not the same as the website address.")
    if "documentation" in q or "openapi" in q:
        return ("On the vendor's developer site (often developer.<vendor>.com or docs.<vendor>.com) look for 'API reference' and a download "
                "called OpenAPI, Swagger, openapi.json or openapi.yaml. Paste that link here or upload the file. Without a link I search the public "
                "APIs.guru directory automatically.")
    if "which application" in q or "which api" in q:
        return "Name the product you use (for example GitHub, Jira, Slack, Stripe) or paste a link to its API documentation."
    if "channel" in q:
        return "Open the channel in Slack: its name is shown at the top (for example #security)."
    if "project" in q:
        return "In Jira the project key is the prefix of every issue number (for example SEC in SEC-123)."
    return "Check the service's API documentation or account settings, or ask the person who administers that service."
