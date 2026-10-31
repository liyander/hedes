# H.E.D.E.S

AI-assisted, configuration-driven, zero-code integration platform. Users describe an integration in plain
language; AI agents read the API's documentation and produce a declarative configuration; deterministic code
validates, security-checks and tests it; an administrator approves the exact version; a generic engine runs it.
Monitoring detects failures and drift and prepares repairs, which go back through review.

```
 AI agents (propose)          Deterministic core (decide & execute)          Humans (govern)
 ─────────────────────        ─────────────────────────────────────          ───────────────
 requirements                 JSON-Schema + semantic validation              admin approval bound
 API discovery         ──▶    security policy + SSRF egress checks    ──▶    to the config hash
 config generation            mock-transport tests                           roles per tool, MCP
 security narrative           generic HTTP engine (no service code)          exposure, rollback
 repair / workflow plans      vault-held credentials, audit log
```

## Quick start

**Windows:** double-click `run.bat` (or `run.bat 9000` for another port, `run.bat test` to run the tests). On first run it
creates `backend\.venv`, installs dependencies, copies `.env.example` to `.env`, starts the server and opens the browser.
Python 3.10+ is the only prerequisite. The launcher always starts in **real-data mode** (no mock APIs or sample data).
Other modes: `run.bat demo` (turns the bundled mocks on, for demos only) and `run.bat reset` (moves the database to a
timestamped backup so you start empty - use it once if your database was created by an earlier demo-mode build).

**Manually:**

```bash
cd backend
pip install -r requirements.txt
SEED_PASSWORD=IntegraDemo2026 uvicorn app.main:app --port 8000      # PowerShell: $env:SEED_PASSWORD="IntegraDemo2026"; uvicorn ...
```

Open http://127.0.0.1:8000. Three accounts are seeded (same password): **builder** (user), **admin**, **owner**
(platform owner). Without `SEED_PASSWORD` a random password is generated and written to `backend/data/seed-credentials.txt`.

With Docker (PostgreSQL): `cp .env.example .env && docker compose up --build`.

The **AI assistant** (chat, `@` tagging, approval-gated actions) needs a real model: add OpenAI-compatible
(OpenAI, NVIDIA, Groq, Ollama, LM Studio…), Anthropic or Gemini credentials via `.env` or **AI Providers** (owner).
The guided *Build an integration* pipeline still falls back to a deterministic offline model so it works without a key.

The product runs against **real services only**. The bundled mock APIs, sample integrations and demo buttons exist
only when `DEMO_MODE=true` (used by the test suite and the demo script below).

Tests: `cd backend && python -m pytest tests -q` (unit tests plus an end-to-end run of the demo against a live server).

## Config-driven data sources (real APIs)

*Integrations → Add data source* sets up a real service from a form. Each template is a YAML file in
`examples/sources/`; you only supply your domain or organisation and a credential:

| Template | Data (common format) | Login | Paging |
| --- | --- | --- | --- |
| CISA Known Exploited Vulnerabilities | alerts | none | single page (CISA's GitHub mirror) |
| GitHub organisation security | users, Dependabot alerts | personal access token | `Link` header |
| Okta | users, System Log events | SSWS API token | `Link` header (cursor) |
| Jira Cloud | users, issues | email + API token (Basic) | offset (`startAt`) and cursor (`nextPageToken`) |
| NVD CVE database | alerts | none (optional key) | offset (`startIndex`) |
| AbuseIPDB blacklist | alerts | API key header | none |

Every source lands in one of three common shapes (event/alert, user, device; each keeps `source`, `fetched_at` and the
raw record), with per-field transforms, incremental sync, de-duplication, a dead-letter queue, retries with
backoff/jitter and `Retry-After`, per-connector rate caps, interval **or cron** schedules and a health page.
Users and devices are upserted; events are append-only. Secrets can be stored in the vault or read from the server's
environment with `env:NAME`. Adding another source is writing a YAML file (or asking the assistant to draft one).

Only the CISA feed is verified end to end against the live internet by the test suite; the others need your accounts, and
are validated and exercised with the engine's request/response tests.

**Alerts** can be pushed to Slack (incoming webhook) and/or email (SMTP) from *Settings → Alert delivery*; a source
that fails N runs in a row (default 3) raises one.

## AI assistant

*AI Studio → Assistant* (and the **Ask AI** button on every page) answers questions about the platform and your own
configuration. Type `@` to tag an integration, source, workflow or credential; it then reads that item's configuration,
health and recent errors (never secrets). It runs read-only lookups itself; anything that changes data or calls an
external API is shown as an **approval card** with the exact inputs and runs only when you approve, with your own
permissions, audited. It cannot approve or publish integrations, reveal or rotate secrets, or change policy. Every
page also has a hideable **Page guide** (usage, options, tips), and each integration shows a step-by-step setup guide.

## Demo script (≈5 minutes, needs `DEMO_MODE=true`)

1. **builder** → *AI Studio* → “I want to connect another task-management service and create tasks automatically.”
   The AI asks for documentation.
2. Click **Use sample TaskFlow API** (an OpenAPI document for a bundled mock service the platform has never seen).
   Requirements become ready → **Generate integration**. Watch the pipeline: discovery → config → validation →
   security → safe tests (mocked) → submitted for approval.
3. **Connect** the credential (`taskflow-demo-token`). It goes to the vault; the AI never sees it.
4. Sign in as **admin** → *Approvals*. The review shows what the integration can do, the destination, the
   policy checks, the tests and the configuration hash. **Approve & publish**. (The builder cannot approve, and an
   admin cannot approve their own submission.)
5. *Test Lab* → `taskflow.create_task` → **Preview request** (credentials redacted) → **Execute live**. The task
   appears in *Settings → Demo controls*. No connector code was added.
6. Self-healing: *Settings* → **Simulate API change** (TaskFlow renames `assignee.user_name` to
   `assignee.display_name`). Execute twice → `response_drift` → the repair agent proposes v+0.0.1, regression-tests
   it against the observed structure and submits it. Approve it in *Approvals* (or reject: the live version stays).
7. *Workflows* → **Describe with AI**: “When a webhook arrives with priority high, create a TaskFlow task and notify
   the security-team channel in Notify” → dry run → activate (copy the webhook secret) → send signed webhooks.
8. *Tool Registry* → tick **MCP** for a tool; *Settings* → create an API token; point an MCP client at `/mcp`.

## What is implemented

| Area | Where |
| --- | --- |
| Integration schema (the contract) | `shared/integration.schema.json`, `shared/workflow.schema.json` |
| Generic engine: templated path/query/headers/JSON/form bodies, 8 auth types (bearer, API key, basic, Okta SSWS, OAuth2 client credentials with token caching, AWS SigV4), pagination (page, cursor, offset, `Link` header), timeouts, idempotent-only retries with exponential backoff + jitter + `Retry-After`/rate-limit-reset hints, per-connector requests-per-minute cap, 2 MB cap, JSONPath mapping, error classification | `backend/app/services/engine.py` |
| Data-source ingestion: an operation's `ingest` block maps any API into one common event format (`event_id, timestamp, category, action, actor, target, outcome, severity, message`); incremental pulls from a saved high-water mark, de-duplication, per-record validation with a dead-letter queue, run history, per-connection health, credential expiry/rotation alerts | `services/ingest.py`, `api/sources.py`, UI *Data Sources* |
| Restricted templates (`${inputs.x}`) and JSONPath subset - no `eval` | `services/templates.py`, `services/jsonpath.py` |
| SSRF/egress: scheme, private/link-local/metadata IPs (DNS-resolved), no redirects, org domain allow-list | `services/egress.py` |
| Validation + deterministic security review (inline secrets, forbidden headers, destructive ops, scopes, credential binding) | `services/validation.py`, `services/security_review.py` |
| AI gateway: adapters (OpenAI-compatible, Anthropic, Gemini, offline), task routing by purpose/JSON/context/confidentiality, budgets, fallback, schema-validated output with one self-correction, usage + cost, secret redaction, untrusted-document wrapping | `services/ai_gateway.py` |
| Agents: requirements, discovery, architect/config, security narrative, tests, repair, workflow planning, error explanation | `services/agents.py` |
| Persisted pipeline state machine with per-stage retry | `services/orchestrator.py` |
| Lifecycle: immutable submitted versions, hash-bound approval, separation of duties, credential + test gates, publish, disable, rollback to previously approved hashes, version diff | `services/integrations.py` |
| Vault (Fernet-encrypted secrets addressed by reference) | `services/vault.py` |
| Runtime authorisation, per-user rate limit, concurrency cap, execution records | `services/runtime.py` |
| Monitoring, alerts, drift detection, automatic repair proposals | `services/monitoring.py`, `services/repairs.py` |
| Workflows: DAG, parallel layers, conditions, step-output references, dry run, manual/webhook/schedule triggers, HMAC signatures + replay protection | `services/workflows.py` |
| MCP gateway (JSON-RPC: initialize, tools/list, tools/call) | `services/mcp.py`, `POST /mcp` |
| OpenAPI 3 import | `services/openapi.py` |
| RBAC (user/admin/owner), API tokens (never allowed to govern), CSRF header for cookie sessions, invitations, audit log | `core/security.py`, `api/` |
| UI (no build step) | `backend/app/static/` |

Interactive API docs: http://127.0.0.1:8000/docs.

## Deliberate simplifications (vs. the blueprint)

- **Modular monolith, no Redis/Celery**: background work (pipeline, repairs, webhook runs, scheduler) uses in-process
  threads. Fine for a single instance; move to a queue before scaling out.
- **Vanilla-JS SPA instead of Next.js/React Flow**: served by FastAPI, no build step; the workflow editor is a custom
  SVG canvas with an inspector.
- **Own provider adapters instead of LiteLLM**: three adapters cover OpenAI-compatible, Anthropic and Gemini APIs.
- **Tables are created with `create_all`** rather than Alembic migrations; add Alembic before the schema evolves in production.
- **Vault** is an encrypted table, not HashiCorp Vault / a cloud KMS. The interface (`store_secret`/`read_secret` by ref) is the seam.
- **Not implemented**: OAuth authorization-code/PKCE connection flows (tokens are pasted into the connection form),
  GraphQL/other transports, streaming chat (the UI polls), OpenTelemetry export.
- **DNS rebinding**: destinations are resolved and checked before each request, but the connection is not pinned to the
  checked IP; run the app behind an egress proxy/firewall in production.

See **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** for a full explanation of every feature and the three roles, and
`docs/ARCHITECTURE.md` for the data model, trust boundaries and the configuration format.
