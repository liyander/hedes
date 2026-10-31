# H.E.D.E.S architecture

## Principle

AI plans; deterministic code decides and executes; humans govern. Every AI output is data that is validated
against a JSON Schema and re-checked by deterministic policy before it can matter. No AI-generated code runs.

## Components

```
browser SPA ──HTTP──▶ FastAPI app
                        ├─ api/            REST routers (auth, ai, integrations, approvals, connections, runtime, workflows)
                        ├─ services/
                        │   ├─ ai_gateway   provider adapters, routing, fallback, budgets, usage, redaction
                        │   ├─ agents       agent functions with JSON output contracts (+ offline implementations)
                        │   ├─ orchestrator persisted AI pipeline state machine
                        │   ├─ integrations versions, tests, submission, approval/publication, rollback
                        │   ├─ engine       generic HTTP execution (pure, no DB)
                        │   ├─ runtime      authorisation + credentials + engine + execution records
                        │   ├─ egress / security_review / validation   deterministic policy
                        │   ├─ vault        encrypted secrets by reference
                        │   ├─ monitoring / repairs                    alerts, drift, self-healing proposals
                        │   ├─ workflows    DAG engine, webhooks, scheduler
                        │   └─ mcp          MCP JSON-RPC adapter over runtime
                        ├─ mock/           TaskFlow + Notify demo APIs (reached over HTTP like any API)
                        └─ static/         SPA
                      SQLite (default) / PostgreSQL
```

## Trust boundaries

| Boundary | Control |
| --- | --- |
| User → AI model | `redact_ai_context` strips credential-like strings; documents and API responses are wrapped in `<untrusted_document>` and the system prompt forbids following instructions inside them; response structures are sent as types only (no values). Tasks marked confidential only go to models with `allow_confidential`. |
| AI model → platform | Output must validate against the agent's schema (one self-correction, then fallback). The config agent may not change the documented base URL, auth type or add operations. Validation and security review run again deterministically. |
| Platform → external APIs | Only published versions execute. URL host is fixed to the config's base URL; path params are percent-encoded and `.`/`..` rejected; scheme must be HTTPS; DNS results checked against private/link-local/metadata ranges; redirects not followed; org allow-list; 2 MB response cap; timeouts; retries only for idempotent operations. |
| Credentials | Entered only in the connection form, Fernet-encrypted in `secrets`, referenced as `vault:<id>`, decrypted only inside `runtime.credential_for` at execution time; API returns a masked hint; previews and logs redact auth headers/params. |
| Governance | Approval endpoints require an interactive admin session (`require_human`), not an API token; reviewer ≠ requester; approval re-checks the config hash, security policy, credential binding and tests. Agents have no code path to approval. |
| Browser | HttpOnly SameSite=Strict session cookie + mandatory `X-Requested-With` header on mutating requests; strict CSP (no inline script); login throttling. |
| Webhooks | HMAC-SHA256 over `timestamp.body`, 5-minute window, delivery-id replay table, 256 KB body cap. |

## Lifecycle

```
draft ──validate/test──▶ pending_approval ──approve──▶ published ──(new version approved)──▶ superseded
  ▲                           │                                   ▲
  └── revise ◀── rejected / changes_requested                     └── rollback (previously approved hash only)
```

Integration status: `draft → pending_approval → published`, `revision` after a rejection, `disabled` (not executable).
Execution status: `succeeded | failed` with an `error_class` (`auth_invalid`, `forbidden`, `not_found`,
`rate_limited`, `upstream_error`, `request_rejected`, `timeout`, `network_error`, `egress_denied`,
`response_drift`, `input_invalid`, `credential_missing`, …).

## Configuration format

See `shared/integration.schema.json` and `examples/*.yaml`. Key points:

- `request.path_params | query | headers | json_body | form_body` hold literals or `${inputs.<name>}` references.
  A value that is exactly one reference keeps its JSON type; unresolved optional references are dropped.
- `response.status_codes` are the success codes; `response.mapping` maps output fields to JSONPath
  (`$.a.b`, `$.list[0]`, `$.list[*].id`); `response.items_path` maps each item of a list;
  `response.optional_fields` may be absent without counting as drift.
- `pagination`: `{type: page, page_param, size_param, size, max_pages}` or `{type: cursor, cursor_param, next_cursor_path}`.
- `authentication.credential_ref` names a connection; secrets never appear in configs (the security review blocks it).

## Data model

users, organizations (with `policy`), memberships(role), invitations, api_tokens, secrets, ai_providers, ai_models,
usage_records, ai_sessions (requirements + per-stage state), ai_messages, integrations, integration_versions
(config, config_hash, validation, security_report), connections, tools, approval_requests, approval_events, test_runs,
workflows, workflow_versions, workflow_runs, workflow_steps, executions, repair_proposals, alerts, audit_logs,
webhook_deliveries. UUID string keys; every tenant-owned row carries `organization_id` and every query filters on it.

## Data-source ingestion

An operation may carry an `ingest` block (`shared/integration.schema.json`). The platform, not the connector, owns
everything around it:

```
scheduler (15 s tick) -> IngestSource (interval, high-water mark, health)
   -> engine.execute(raw=True, query_overrides={since: watermark - overlap})   # pagination, auth, retries, throttle
   -> normalize(record, ingest.fields, value_maps, defaults)                   # common format, per record
        ok       -> NormalizedEvent   (unique per org + integration + event_id: de-duplication)
        invalid  -> DeadLetter        (reasons + raw record, parked once by fingerprint)
   -> IngestRun (fetched / stored / duplicates / rejected) + a mirrored Execution (alerts, drift, metrics)
```

* **Common format**: `event_id, timestamp (UTC), category, action, actor, target, outcome (success|failure|unknown),
  severity (info..critical), message`, plus the original record. `value_maps` translate vendor vocabularies;
  timestamps accept ISO-8601, epoch seconds or milliseconds.
* **Incremental**: the newest ingested timestamp is stored per source; the next run asks the API only for newer data
  (minus a small overlap that de-duplication absorbs). *Re-read full history* clears it.
* **Resilience**: retries use exponential backoff with jitter and honour `Retry-After`/rate-limit-reset; `rate_limit`
  caps requests per minute per connector; a failing source backs off (max 1 h) and is flagged `failing`/`stale`.
* **Health** (`GET /api/v1/sources`): healthy, degraded (many rejected records), stale, failing (with the
  classified error and a suggested fix), paused, pending.
* **Credentials**: optional expiry and rotation age; expired secrets are never used, and alerts fire a week before
  expiry and after the org's rotation period (default 90 days).
