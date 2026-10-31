"""Plain-language help for every page, plus the platform's core concepts.

One source of truth: the UI renders it in each page's hideable "About this page" panel, and the AI assistant reads the
same text so its explanations match what users see on screen.
"""
from __future__ import annotations

CONCEPTS = [
    {"term": "Integration", "text": "A connection to an external service, described entirely by a configuration (YAML/JSON): how to log in, which endpoints to call, how to page through results, and how to map the response. No connector code is written."},
    {"term": "Version & approval", "text": "Every change creates a new version. A version is frozen when submitted; an administrator (or owner) reviews exactly what it can do and publishes that exact version; approving your own submission is allowed but flagged in the audit log. The AI can never approve or publish."},
    {"term": "Data source", "text": "An integration operation with an ingest block. It pulls records on a schedule (interval or cron) and maps them into the common format, with retries, rate-limit handling, de-duplication and bad-data quarantine handled by the platform."},
    {"term": "Common format", "text": "Three shapes every source lands in. Event/alert: event_id, timestamp, category, action, actor, target, outcome, severity, message. User: id, name, email, status, last_login. Device: id, hostname, ip, os, owner, last_seen. Every record also keeps source and fetched_at plus the original raw record."},
    {"term": "Incremental sync (high-water mark)", "text": "After each run the newest event timestamp is saved. The next run asks the API only for newer data. 'Re-read full history' clears it; de-duplication keeps events unique."},
    {"term": "Rejected records (dead letters)", "text": "Records that cannot be mapped (missing timestamp, bad IP, ...) are parked with the reason instead of failing the run. Fix the mapping, then reprocess them."},
    {"term": "Credential", "text": "A secret stored encrypted in the vault and referenced by name. Type env:NAME instead of a value to read it from a server environment variable (the operator must list NAME in CONNECTION_ENV_VARS). Credentials can have an expiry; expired ones are never used."},
    {"term": "Tool", "text": "An operation of a published integration that people, workflows, the MCP gateway and the AI assistant can run."},
    {"term": "Drift & repair", "text": "If an API changes its response shape, monitoring detects missing fields and the platform proposes a repaired mapping for admin approval."},
    {"term": "AI assistant", "text": "Chat with the AI about anything in the platform. Tag integrations, sources, workflows or credentials with @ so it reads them directly. It can propose actions, but nothing changes until you approve each action; it can look things up on its own."},
]

PAGES = {
    "dashboard": {
        "title": "Dashboard",
        "summary": "A snapshot of what is live, what needs a decision and how healthy things are.",
        "steps": ["Type what you want to connect in the box to start in AI Studio.", "Check the four tiles: published integrations, approvals waiting, success rate, repairs proposed.", "Open anything under 'Needs attention' to act on it."],
        "options": [{"name": "Stat tiles", "text": "Each tile links to the page that explains it."}, {"name": "Integrations table", "text": "Click a row to open the integration and its step-by-step guide."}],
        "tips": ["The AI button at the bottom right is available on every page."],
    },
    "studio": {
        "title": "AI Studio",
        "summary": "Talk to the AI. Ask questions, tag things you want it to look at, and let it act for you - with your approval first.",
        "steps": ["Type @ and pick an integration, source, workflow or credential to give the AI that context.", "Ask in plain language: 'why is my Okta source failing?', 'explain pagination', 'run the CISA feed now'.", "When the AI proposes an action you see a card with the exact action and its inputs. Approve or reject it; nothing runs before you decide.", "Use 'Build an integration' to have the AI draft a configuration from your description or API docs."],
        "options": [{"name": "@ tags", "text": "Tagged items are read by the AI (configuration, health, recent errors, mapping) before it answers. Secrets are never included."}, {"name": "Approval cards", "text": "Every action that changes something or calls an external service waits for your approval. Read-only lookups run automatically and are shown as 'looked up'."}, {"name": "Threads", "text": "Conversations are saved; reopen one from the list."}, {"name": "Build an integration", "text": "The guided pipeline: requirements, API discovery, configuration, validation, security review, tests, submit."}],
        "tips": ["If I lack documentation for a service, I search the public APIs.guru OpenAPI directory, or read a documentation link you paste; every source used is listed under Sources referred.", "Each open question says how to find the answer. Generating first shows exactly which database records will be created and waits for your confirmation.", "The AI cannot approve or publish integrations, rotate secrets or change policy - those stay with administrators.", "An AI provider must be configured under AI Providers; there is no fake offline answer."],
    },
    "integrations": {
        "title": "Integrations",
        "summary": "Every connection, its status and version. Start a new one from a ready-made source template, a pasted configuration, an OpenAPI file or the AI.",
        "steps": ["Pick 'Add data source' to set up Okta, GitHub, Jira, NVD, AbuseIPDB or CISA KEV from a form.", "Open an integration to see its guide: describe, validate, connect credential, test, submit, approve, run.", "Use Import / paste config to bring your own YAML or JSON configuration."],
        "options": [{"name": "Status", "text": "draft, pending approval, revision (changes requested), published, disabled."}, {"name": "Add data source", "text": "A form generates a valid configuration from a template; you fill in only your domain or organisation."}, {"name": "Import / paste config", "text": "Validated immediately with clear, path-level errors."}],
        "tips": ["Ask the AI to explain any integration: open it and press 'Discuss with AI'."],
    },
    "workflows": {
        "title": "Workflows",
        "summary": "Chain published tools into automations with conditions, parallel steps and references to earlier outputs.",
        "steps": ["Create a workflow and add steps that call tools.", "Use ${trigger.x} and ${steps.id.output.x} to pass data.", "Dry-run to see every request without sending it, then activate.", "Trigger manually, by signed webhook, or on a schedule."],
        "options": [{"name": "Conditions", "text": "A step runs only when its conditions are true."}, {"name": "On failure", "text": "Stop the run or continue."}, {"name": "Webhook", "text": "Signed with HMAC and protected against replay."}],
        "tips": [],
    },
    "testlab": {
        "title": "Test Lab",
        "summary": "Run a published tool by hand with your own inputs and inspect the exact request, response and error guidance.",
        "steps": ["Choose a tool.", "Fill the generated form (from the tool's input schema).", "Run it and read the request preview (secrets redacted), output and diagnostics."],
        "options": [{"name": "Explain error", "text": "Asks the AI to explain a failure in plain language."}],
        "tips": ["This calls the real external service."],
    },
    "credentials": {
        "title": "Credentials",
        "summary": "Secrets for external services, stored encrypted. Configurations only reference them by name.",
        "steps": ["Press Connect credential.", "Choose the type that matches the integration (Bearer, API key, Basic, Okta SSWS, OAuth2 client credentials, AWS SigV4).", "Optionally set an expiry date; you are alerted a week before.", "Use Test to verify, Rotate to replace the secret, Revoke to disable."],
        "options": [{"name": "env:NAME", "text": "Enter env:MY_VAR instead of a secret to read the value from a server environment variable at use time. Only variables listed in the server setting CONNECTION_ENV_VARS are allowed."}, {"name": "Lifetime column", "text": "Days since rotation and expiry; warnings appear when rotation is due or the secret expires soon."}],
        "tips": ["Secrets are never shown again, never sent to an AI model and never written to logs."],
    },
    "approvals": {
        "title": "Approvals",
        "summary": "Administrators review and publish the exact version a builder submitted.",
        "steps": ["Open a request.", "Read what it can do, where it connects, the security findings and the test results.", "Approve to publish, request changes, or reject."],
        "options": [{"name": "Any administrator", "text": "Every administrator (and owner) can decide, including on versions they submitted; self-approvals are flagged in the audit log."}, {"name": "Hash binding", "text": "Approval is tied to the exact configuration hash; any change needs resubmission."}],
        "tips": ["API tokens and the AI assistant can never approve."],
    },
    "tools": {
        "title": "Tool Registry",
        "summary": "Every operation of every published integration, who may run it and whether AI agents (MCP) can see it.",
        "steps": ["Find a tool.", "Run it (opens Test Lab), or change who can use it.", "Expose it to MCP clients if you want external AI agents to call it."],
        "options": [{"name": "Enabled", "text": "Turn a tool off without unpublishing the integration."}, {"name": "Allowed roles", "text": "Restrict to administrators, for example."}, {"name": "MCP", "text": "Lets API-token clients list and call the tool."}],
        "tips": [],
    },
    "versions": {
        "title": "Version History",
        "summary": "Every configuration version, with who made it, why, and how it differs from another version.",
        "steps": ["Pick two versions to compare.", "Administrators can roll back to a version that was approved before."],
        "options": [],
        "tips": [],
    },
    "audit": {
        "title": "Audit log",
        "summary": "A tamper-evident record of who did what, including every action the AI assistant performed on a user's behalf.",
        "steps": ["Filter by action or actor.", "Open an entry for its details."],
        "options": [],
        "tips": ["Assistant actions are logged as the approving user with source 'assistant'."],
    },
    "sources": {
        "title": "Data Sources",
        "summary": "Every scheduled connection: is it working, when it last ran, how many records arrived and what went wrong. Browse the common events, users and devices.",
        "steps": ["Read the Connections table: health, last/next run, recent runs, records stored/updated/rejected, credential age.", "Press 'Test' to fetch one page and preview the mapped records without storing anything.", "Press 'Run now' to ingest immediately.", "Open Details to change the schedule (interval or cron), pause, or re-read the full history.", "Use Data to search events, users and devices; Rejected to fix and reprocess bad records."],
        "options": [{"name": "Health", "text": "healthy, degraded (many rejected records), stale (no success for 3 intervals), failing (with the classified error and a fix), paused, not run yet."}, {"name": "Schedule", "text": "Minutes interval or a 5-field cron expression (UTC). Failing sources back off automatically."}, {"name": "Add data source", "text": "Open Integrations to create one from a template."}],
        "tips": ["Failures send Slack/email alerts when notifications are configured in Settings."],
    },
    "healing": {
        "title": "Self-healing",
        "summary": "Every automatic investigation of a failure: what broke, the root cause, each fix that was tried, the outcome and a timestamped log of every step.",
        "steps": ["Use the tabs to see sessions that need an admin, have a fix awaiting approval, were fixed, or are still running.", "Open a session to read its log (failure detected, evidence, each fix attempted, root cause, AI analysis, outcome, notifications, the admin's decision on the fix).", "If a fix was prepared, press Review the fix to approve or reject it in Approvals; the session is updated with your decision.", "After fixing something by hand (for example rotating a credential), press Investigate again; the new run is added to the same log."],
        "options": [{"name": "Outcome", "text": "Fixed, Fix awaiting approval, Needs an admin, or Investigating."}, {"name": "Runs", "text": "How many times the failure was investigated. Recurrences of the same failure within 6 hours join the open session."}, {"name": "Modes (Settings)", "text": "Off; Investigate and prepare fixes (every configuration fix waits for approval); or Fix automatically: non-critical fixes are tested, published, re-checked and rolled back if they make things worse. Critical fixes (new host, authentication or headers, added/removed operations, changed methods or inputs, anything touching an operation that deletes data, or a high security rating) always wait for an administrator."}],
        "tips": ["Self-healing never changes production directly: fixes are new versions that an administrator approves.", "Secrets are never changed by the AI; credential problems are reported with the steps to fix them."],
    },
    "monitoring": {
        "title": "Monitoring",
        "summary": "Execution health: success rate, latency, error classes, drift and repair proposals.",
        "steps": ["Watch the success rate and the executions-per-hour chart.", "Open a failed execution to see the request, response shape and what to do.", "Review repair proposals that the platform prepared after repeated drift."],
        "options": [{"name": "Error classes", "text": "auth_invalid, forbidden, rate_limited, upstream_error, response_drift, ... each with a plain-language meaning."},
                    {"name": "Self-healing", "text": "When it is on (Settings), every failure is traced: the AI finds the root cause and tries to fix it - "
                     "re-checking a recovered service, re-testing a credential, or preparing a corrected version for approval. What it cannot fix is "
                     "emailed to administrators with the root cause, what it tried, what is still failing and what it needs."},
                    {"name": "Investigate and fix", "text": "On any failed execution: run one self-healing investigation now, even when automatic self-healing is off."}],
        "tips": ["Self-healing never changes production directly: configuration fixes are new versions an administrator approves.",
                 "Secrets are never changed by the AI; a broken credential is always reported to an administrator."],
    },
    "providers": {
        "title": "AI Providers",
        "summary": "The AI models behind the assistant and the integration builder. Connect Amazon Bedrock, NVIDIA, Groq, xAI Grok, Gemini, OpenAI, Anthropic, OpenRouter, Ollama or any OpenAI-compatible endpoint; the platform routes each task to a suitable model within your budget.",
        "steps": ["Press Add provider (owner only) and pick a provider from the list. Base URL, key label and example model ids are filled in for you.", "Amazon Bedrock: choose the region, then AWS access keys or a Bedrock API key (or type env:NAME to read an environment variable). Use a model or inference-profile id that is enabled under Bedrock > Model access.", "OpenAI-compatible and Gemini: paste the key, press Fetch models and pick one.", "Tick the tasks it should be preferred for (general covers everything, including the AI assistant and creating integrations), then press Test."],
        "options": [{"name": "Use it for", "text": "general serves every task. Tick assistant, config generation, etc. to prefer a model for those; lower priority numbers are tried first, others are fallbacks."}, {"name": "Allow confidential", "text": "Whether API documentation and configurations may be sent to the model."}, {"name": "Rotate key / Edit", "text": "Replace credentials, rename, or change the base URL / AWS region without re-adding the models."}, {"name": "Budget", "text": "Set in Settings; over budget only free models run."}],
        "tips": ["The AI assistant requires at least one real provider.", "Model ids are examples: providers rename and retire models, so check their current list.", "Local servers (Ollama, LM Studio) keep everything on your machine."],
    },
    "settings": {
        "title": "Settings",
        "summary": "Organisation policy, alert delivery (Slack and email), members and API tokens.",
        "steps": ["Set allowed API destinations and thresholds in Organisation policy.", "Under Alert delivery add a Slack webhook and/or SMTP details, choose the minimum severity and press Send test.", "Invite members and set their roles; create API tokens for MCP clients."],
        "options": [{"name": "Credential rotation days", "text": "How old a secret may get before an alert."}, {"name": "Alert after N failures", "text": "Consecutive failed runs before a source raises an alert."}, {"name": "Minimum severity", "text": "Only alerts at or above this level are sent out."}, {"name": "Email templates", "text": "Each kind of alert (credential problem, review requested, API changed, data source problem, integration failure) has its own email. Customise the subject and intro, Preview it, and Send test email to check it in your inbox."}],
        "tips": ["The Slack webhook and SMTP password are stored in the encrypted vault."],
    },
    "users": {
        "title": "Users",
        "summary": "Accounts and roles for your organisation (administrators only).",
        "steps": ["Press Add user, choose a username, email, role and an initial password, and share the password securely.",
                  "Change a role from the dropdown; Disable removes access immediately (sessions and API tokens stop working).",
                  "Reset password sets a new password for a user who is locked out."],
        "options": [{"name": "Builder", "text": "Builds, tests and runs integrations and data sources; submits them for review."},
                    {"name": "Admin", "text": "Also approves and publishes, manages users, policy and alert delivery."},
                    {"name": "Owner", "text": "Also configures AI providers and can create or change other owners."}],
        "tips": ["The first account created at setup is an owner.", "You cannot change your own role or disable yourself, and at least one active owner always remains.",
                 "Accounts are disabled rather than deleted so the audit log stays complete."],
    },
}


def page(key: str) -> dict:
    return PAGES.get(key, {})


def knowledge_text(page_key: str | None = None) -> str:
    out = ["PLATFORM CONCEPTS"]
    out += [f"- {c['term']}: {c['text']}" for c in CONCEPTS]
    out.append("\nPAGES")
    for k, p in PAGES.items():
        out.append(f"- {p['title']} (#/{k}): {p['summary']}")
    cur = PAGES.get(page_key or "")
    if cur:
        out.append(f"\nTHE USER IS CURRENTLY ON: {cur['title']}\nHow to use it: " + " | ".join(cur["steps"]))
    return "\n".join(out)
