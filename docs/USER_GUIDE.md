# IntegraAI — Functional Guide

This guide explains every feature of IntegraAI, who may use it, and what happens behind the scenes.
For component design and trust boundaries see [ARCHITECTURE.md](ARCHITECTURE.md); for setup see the [README](../README.md).

**Contents**

1. [Core ideas](#1-core-ideas)
2. [Roles and permissions](#2-roles-and-permissions)
3. [Signing in, sessions and invitations](#3-signing-in-sessions-and-invitations)
4. [The application layout](#4-the-application-layout)
5. [Dashboard](#5-dashboard)
6. [AI Studio — building integrations by conversation](#6-ai-studio--building-integrations-by-conversation)
7. [Integrations](#7-integrations)
8. [The integration configuration](#8-the-integration-configuration)
9. [Validation and the security review](#9-validation-and-the-security-review)
10. [Safe tests](#10-safe-tests)
11. [Credentials (connections)](#11-credentials-connections)
12. [Approvals](#12-approvals)
13. [Tool Registry](#13-tool-registry)
14. [Test Lab and tool execution](#14-test-lab-and-tool-execution)
15. [The execution engine](#15-the-execution-engine)
16. [Workflows](#16-workflows)
17. [Monitoring and alerts](#17-monitoring-and-alerts)
18. [Self-healing (repair proposals)](#18-self-healing-repair-proposals)
19. [Version History and rollback](#19-version-history-and-rollback)
20. [AI Providers and the AI gateway](#20-ai-providers-and-the-ai-gateway)
21. [Settings](#21-settings)
22. [MCP gateway for AI agents](#22-mcp-gateway-for-ai-agents)
23. [Audit log](#23-audit-log)
24. [Security controls at a glance](#24-security-controls-at-a-glance)
25. [Demo services and seeded data](#25-demo-services-and-seeded-data)
26. [Limits and defaults](#26-limits-and-defaults)
27. [REST API reference](#27-rest-api-reference)
28. [Error classes](#28-error-classes)

---

## 1. Core ideas

| Idea | Meaning |
| --- | --- |
| **Configuration, not code** | Every integration is a declarative JSON/YAML document (base URL, authentication, operations, input schemas, response mappings). One generic engine executes all of them; no service-specific connector code exists. |
| **AI proposes, code decides** | AI agents draft requirements, configurations, test fixtures, repairs and workflows. Deterministic code validates them, applies the security policy and runs the tests. AI output never runs as code. |
| **Humans govern** | Nothing becomes executable until an administrator approves the *exact* configuration (identified by its SHA-256 hash). AI agents have no way to approve. |
| **Credentials stay out of AI** | Secrets are typed into a dedicated form, encrypted in the vault and only decrypted by the engine at execution time. |
| **Immutable versions** | Once a version is submitted it is frozen. Every change, repair or rollback produces or restores a specific version with a full audit trail. |

The life of an integration:

```
Describe (AI Studio) → Generate config → Validate → Security review → Safe tests
      → Submit → Admin approval → Published tools → Execute / Workflows / MCP
      → Monitoring → Drift detected → Repair proposal → Admin approval → New version live
```

---

## 2. Roles and permissions

Every user belongs to an organisation with one of three roles. In the UI the `user` role is called **Builder**.

| Role | Who it is for | Summary |
| --- | --- | --- |
| **Builder** (`user`) | People who create and use integrations | Chat with AI, create drafts, connect credentials, run tests, submit for review, run the tools their role allows, build workflows. Cannot approve or change governance. |
| **Admin** (`admin`) | Reviewers and team leads | Everything a Builder can do, plus: approve/reject/request changes, publish, disable/enable integrations, roll back, set per-tool access and MCP exposure, invite members, change roles (except owner), edit the organisation policy, see all executions and the whole audit log, view AI usage, simulate API drift in the demo. |
| **Platform owner** (`owner`) | Whoever operates the platform | Everything an Admin can do, plus: configure AI providers and models (keys, routing, costs), change the "require tests" policy, grant or remove the owner role. Always allowed to run every tool. |

### Detailed permission matrix

| Capability | Builder | Admin | Owner |
| --- | :---: | :---: | :---: |
| Sign in, view Dashboard, Integrations, Workflows, Tool Registry, Version History, AI Providers | ✓ | ✓ | ✓ |
| Chat in AI Studio, upload OpenAPI documents, generate integrations | ✓ | ✓ | ✓ |
| Open own AI Studio sessions | ✓ | ✓ | ✓ |
| Open other members' AI Studio sessions (by link) | — | ✓ | ✓ |
| Create drafts (paste config / import OpenAPI), edit drafts, run safe tests, request a request preview | ✓ | ✓ | ✓ |
| Submit a version for approval | ✓ | ✓ | ✓ |
| Create credential connections | ✓ | ✓ | ✓ |
| Rotate / test / revoke a connection | creator only | ✓ any | ✓ any |
| Execute a published tool | if role allowed for that tool | if allowed | ✓ always |
| Create workflows | ✓ | ✓ | ✓ |
| Edit, run, activate, pause, archive a workflow | own workflows | ✓ any | ✓ any |
| See executions and alerts | own executions; org alerts | all | all |
| Audit log | own actions | whole organisation | whole organisation |
| View approval queue and review pages | own requests ("Approvals" shows them) | ✓ | ✓ |
| **Approve / reject / request changes** | — | ✓ (not own submissions) | ✓ (not own submissions) |
| Disable / enable an integration, roll back to an approved version | — | ✓ | ✓ |
| Tool settings (allowed roles, MCP exposure, enabled) | — | ✓ | ✓ |
| Invite members, change roles user↔admin | — | ✓ | ✓ |
| Grant or remove the owner role | — | — | ✓ |
| Organisation policy: allowed domains, AI budget, auto-repair threshold | — | ✓ | ✓ |
| Organisation policy: require passing tests before review | — | — | ✓ |
| AI usage and cost report, test a provider connection | — | ✓ | ✓ |
| Add / edit / delete AI providers and models, rotate provider keys | — | — | ✓ |
| Demo control: simulate TaskFlow API drift | — | ✓ | ✓ |
| Create personal API tokens | ✓ | ✓ | ✓ |

### Rules enforced on the server (not just hidden in the UI)

- **Separation of duties** — the person who submitted a version cannot approve, reject or request changes on it.
- **Interactive sessions only for governance** — approving, publishing, rolling back, changing tool access, inviting,
  changing roles, editing policy, managing AI providers and simulating drift all require a signed-in browser session.
  API/MCP tokens are refused even if they belong to an admin.
- **No self-promotion** — nobody can change their own role; only an owner can touch the owner role.
- **Tenant isolation** — every query is filtered by the caller's organisation; IDs from another organisation return 404.

---

## 3. Signing in, sessions and invitations

- **Login** accepts a username *or* email plus password. After 8 failed attempts for the same account from the same
  address within 5 minutes, sign-in is blocked for a few minutes.
- The session is an HttpOnly, SameSite=Strict cookie valid for 12 hours (`/auth/refresh` extends it). Every state-changing
  browser request must carry the `X-Requested-With: integra` header (the UI adds it), which blocks cross-site request forgery.
- **Sign out** from the user menu (bottom of the sidebar).
- **Accounts are created by invitation.** An admin creates an invitation in *Settings → Members → Invite* (email and role:
  Builder or Admin). The platform returns a one-time link `…/#/invite/<token>`, valid 7 days. The invitee opens it, picks a
  username (3–32 chars: lowercase letters, digits, `_ . -`), a name and a password of at least 10 characters, and is signed in.
- Passwords are stored as bcrypt hashes.

---

## 4. The application layout

- **Sidebar** (collapsible with the ☰ button) groups the pages:
  - *Dashboard*, *AI Studio*
  - **Build** — Integrations, Workflows, Test Lab, Credentials
  - **Govern** — Approvals (badge = pending reviews, admins), Tool Registry, Version History, Audit log
  - **Operate** — Monitoring (badge = unread alerts)
  - **Platform** — AI Providers, Settings
- **AI gateway status** (bottom of the sidebar) shows which model would currently generate configurations. Green = an external
  model, orange = falling back to the built-in offline model, red = no eligible model.
- **User menu** — dark-mode toggle, settings & API tokens, sign out.
- **Top bar** — page title, *Build with AI* shortcut, **alert bell** (unread alerts; click one to open its page and mark it read,
  or *Mark all read*), theme toggle.
- Light and dark themes follow the operating system until you choose one; the choice is remembered in the browser.

---

## 5. Dashboard

- **Ask box** — type what you want to connect; it opens AI Studio with that message already sent.
- **Stat tiles** — published integrations, versions awaiting approval, execution success rate (24 h), open repair proposals.
  Each tile links to the relevant page.
- **How an integration goes live** — the five-step lifecycle.
- **Integrations** — the most recently updated integrations with status and live/latest version.
- **Needs attention** — pending reviews (admins) and the newest unread alerts.
- **Recent executions** — latest tool runs; click one for its detail page.

---

## 6. AI Studio — building integrations by conversation

AI Studio has three columns: **Sessions** (your past conversations), the **chat**, and the **work pane** (requirements,
pipeline progress and generated integrations).

### 6.1 Talking to the AI

- Describe the outcome in plain language, or click one of the suggestions. Enter sends, Shift+Enter adds a line.
- **Upload OpenAPI** accepts an OpenAPI 3.x document in JSON or YAML (up to 1 MB). **Use sample TaskFlow API** loads the
  bundled demo specification. Swagger 2.0 and malformed documents are rejected with a clear message.
- Never paste API keys into the chat. Even so, anything that looks like a credential is redacted before it reaches a model.

### 6.2 Requirements (Requirement Understanding Agent)

After each message the agent produces structured requirements, shown in the **Requirements** card:

| Field | Content |
| --- | --- |
| Summary | One-line description of the integration. |
| Services | Each service and where its knowledge comes from: *document* (your OpenAPI upload), *catalog* (built-in reference configs for GitHub, Slack, Jira, Notify), or *model knowledge* (only with an external AI model; flagged as unverified). |
| Operations | The specific operations it plans to expose, e.g. `taskflow.create_task`. Delete operations are only chosen when you ask to delete something. |
| Trigger | For automations ("whenever…", "when…"), the event and any condition, e.g. `labels contains security`. |
| Details | Values picked up from the conversation: URLs, `#channel`, `project SEC`, `owner/repo`. |
| Open questions | What is still missing: documentation for an unknown service, a Jira Cloud site URL, a base URL for a document without a server URL. |

When there are no open questions the card shows **ready to generate**.

### 6.3 The generation pipeline

**Generate integration** starts a background pipeline. Each stage's status, detail and model is stored, and the card refreshes live:

| Stage | What happens |
| --- | --- |
| Collect requirements | Done when the requirements are complete. |
| Discover API | Binds every service to its documentation (uploaded OpenAPI or catalog). Fails with "upload an OpenAPI document" if none exists. |
| Design integration | One integration per service, the chosen operations, the credential name each will need. |
| Generate configuration | A deterministic draft is built from the documentation, then the Configuration Generator agent may improve descriptions and output mappings. It may **not** change the base URL, the authentication type, or add operations; otherwise its output is discarded in favour of the draft. |
| Validate configuration | JSON Schema plus semantic checks (section 9). |
| Security evaluation | Deterministic policy review, plus a plain-language summary from the Security Review agent. |
| Run safe tests | Generated fixtures run through the real engine with a mocked network (section 10). |
| Admin approval | The pipeline submits each version on your behalf. Approval itself is always a separate human decision. |
| Published | Shown once an administrator has approved every generated integration. |

- If a stage fails, **Retry from <stage>** reruns from that stage without repeating earlier successful ones.
- If an admin rejects or requests changes, the Admin approval stage turns red. Describe the fix in the chat and generate again.
  **Regenerate as new version** always creates a new version; it never edits a submitted one.
- If the request includes a trigger (for example "whenever a GitHub issue is labeled security…"), the pipeline also drafts a
  **workflow** and links to it.

### 6.4 Generated integration cards

Each generated integration has a card with four tabs:

- **Overview** — operations, risk level, test result, authentication type. If a credential is still needed, a **Connect**
  button opens the secure credential form. Also links to *View review*, *Open integration* and, once published, *Run it*.
- **Config** — the full generated configuration.
- **Security** — the AI summary and every finding.
- **Tests** — every test case with expected and actual result.

---

## 7. Integrations

The **Integrations** list shows every integration with its status, live version, latest version, authentication type, number
of operations, risk and last update.

### 7.1 Integration status

| Status | Meaning |
| --- | --- |
| `draft` | Not yet submitted. |
| `pending_approval` | A version is waiting for review (and nothing is live yet). |
| `revision` | The latest version was rejected or changes were requested. |
| `published` | A version is live and its tools can run (a newer version may also be pending). |
| `disabled` | An admin switched it off; no tool of this integration can run. |

### 7.2 Version status

`draft` → `pending_approval` → `published` → `superseded` (when a newer version is published). A reviewed version can also
end as `rejected` or `changes_requested`; a pending version replaced by a newer submission becomes `superseded`.

### 7.3 Creating an integration without the AI

*Integrations → Import / paste config*:

- **Import OpenAPI** — paste or choose a file (or load the TaskFlow sample) and click **Read operations**. Tick the operations to
  expose (DELETE operations are unticked by default) and provide a base URL if the document has none. **Create draft** builds the
  configuration, validates it and runs the safe tests.
- **Paste configuration** — YAML or JSON following the schema in section 8. *Start from an example* fills in one of the reference
  configurations under a new id.

### 7.4 Integration detail page

Header actions depend on the selected version:

| Action | Who | When |
| --- | --- | --- |
| Run safe tests | anyone | Draft versions |
| Submit for approval | anyone | Draft versions |
| New revision | anyone | Non-draft versions; copies the config into a new draft (patch version bump) |
| Test Lab | anyone | Integration is published |
| Disable / Enable | admin | Disabling stops all executions immediately |
| Connect | anyone | The referenced credential is not connected |

The **version selector** switches between versions; the hash shown is the configuration fingerprint that approval is bound to.

Tabs:

- **Overview** — every operation (method, path, inputs, outputs, safety: read/write/destructive), details (slug, transport,
  auth, source, author, live version), the published tools and recent repair proposals.
- **Configuration** — drafts are editable here. *Save draft* re-runs validation and the security review and lists any errors.
  Submitted versions are read-only.
- **Security** — findings, AI summary, policy checks, destinations and capabilities.
- **Tests** — every safe-test run (a run made on an earlier configuration is marked as such).
- **Versions** — all versions with status, source, risk, tests and hash. *Compare with live* shows a field-level diff;
  *Restore* (admins) rolls back to a previously approved version (section 19).
- **Approvals** — every review request with decision, reviewer and comment.

---

## 8. The integration configuration

The contract lives in `shared/integration.schema.json`; reference examples are in `examples/*.yaml`.

```yaml
schema_version: "1.0"
integration:
  id: github                       # slug, unique per organisation
  name: GitHub Issues
  version: "1.0.0"                 # managed by the platform
  transport: http
  base_url: https://api.github.com
  timeout_seconds: 15              # optional, 1–60
  default_headers: {Accept: application/vnd.github+json}
  authentication:
    type: bearer                   # none | bearer | api_key_header | api_key_query | basic
    credential_ref: github         # name of a connection (never the secret itself)
    header_name: X-API-Key         # for api_key_header
    param_name: api_key            # for api_key_query
    scopes: [issues:write]         # informational; reviewed by the security policy
  operations:
    - id: create_issue
      description: Create a GitHub issue
      method: POST                 # GET | POST | PUT | PATCH | DELETE
      path: /repos/{owner}/{repo}/issues
      safety: write                # read | write | destructive (derived from method if omitted)
      idempotent: false            # allows retries for POST/PATCH when true
      input_schema: {type: object, required: [owner, repo, title], properties: {...}}
      request:
        path_params: {owner: "${inputs.owner}", repo: "${inputs.repo}"}
        query: {...}
        headers: {...}
        json_body: {title: "${inputs.title}", body: "${inputs.body}"}   # or form_body
      pagination: {type: page, page_param: page, size_param: per_page, size: 50, max_pages: 3}
      retry: {max_attempts: 3, backoff_seconds: 0.5}
      response:
        format: json               # or text
        status_codes: [201]        # success codes
        items_path: "$.items"      # map each list item (optional)
        mapping: {issue_id: "$.id", issue_url: "$.html_url"}
        optional_fields: [issue_url]
```

**Templates.** Request values may contain `${inputs.<name>}` references (with `.field` and `[n]` access). Only lookups are
allowed: no operators, function calls or `eval`. A value that is exactly one reference keeps its JSON type. A reference
embedded in text is converted to text. An optional input that was not provided removes its field from the request.

**Response mapping.** Output fields are JSONPath expressions from a safe subset: `$.a.b`, `$['key']`, `$.list[0]`,
`$.list[*].id`. With `items_path` the mapping is applied to each item and the output is `{items: [...], count: n}`.
A mapped field that is missing from the response and not listed in `optional_fields` is treated as **response drift**.

**Pagination.** `page` sends a page number (and an optional page size) until a short or empty page or `max_pages`.
`cursor` passes the value found at `next_cursor_path` back as `cursor_param` until it is empty. Items from all pages are combined.

---

## 9. Validation and the security review

### 9.1 Validation

A configuration is validated against the JSON Schema, then checked semantically:

- `credential_ref` is required when authentication is used; `header_name` for `api_key_header`; `param_name` for `api_key_query`.
- Operation ids are unique; every `{placeholder}` in a path has a `path_params` entry.
- Every `${inputs.x}` reference exists in the operation's `input_schema`. Only `inputs.*` may be referenced, and `input_schema`
  must itself be valid JSON Schema.
- `Authorization`, `Cookie`, `Host`, `Content-Length`, proxy and forwarding headers may not be set by configuration
  (authentication always comes from the connection).
- An operation cannot have both a JSON and a form body; paginated operations need `items_path`; cursor pagination needs
  both cursor fields; JSONPath expressions must parse.
- Warnings (non-blocking): unused path params, a GET with a body, retries on non-idempotent operations, no output mapping.

### 9.2 Security review

The review is deterministic and authoritative. The AI summary only explains its findings.

| Finding | Severity | Blocks review |
| --- | --- | :---: |
| Configuration does not validate | critical | ✓ |
| Destination not allowed (non-HTTPS, IP literal in a private/reserved/metadata range, `localhost`, `*.internal`, `*.local`, credentials in the URL) | critical | ✓ |
| Credential embedded in the configuration (GitHub/Slack/AWS/Google keys, `sk-…`, private keys, bearer tokens) | critical | ✓ |
| Forbidden header set | critical | ✓ |
| Destination outside the organisation's allowed domains | high | ✓ |
| Connection type does not match the integration's auth type | high | ✓ |
| Destructive (DELETE) operations | high | — |
| Broad scopes (`admin`, `repo`, `write:org`, `delete_repo`, `*`) | high | — |
| Unauthenticated write operations | medium | — |
| Credential not connected yet / connection inactive | medium | — (but approval requires it) |
| Sensitive inputs (password, SSN, card number, IBAN, date of birth…) | medium | — |
| High-entropy literal that may be a secret | medium | — |
| API key sent in the query string | low | — |
| Creates or modifies external data | low | — |
| Large pagination window | low | — |
| Validation warnings | info | — |

**Risk score** = critical 60 + high 25 + medium 10 + low 3 per finding, capped at 100. **Level** is *critical* if any
critical finding exists, otherwise *high* (≥ 50), *medium* (≥ 20) or *low*.

**Policy checks** shown to reviewers: domain checked, schema valid, secrets referenced safely, credential connected, headers safe.

---

## 10. Safe tests

Before approval no live API is ever called. Tests run the real engine with a **mock network**:

- **Happy path** per operation — sample inputs generated from the input schema; the mocked response comes from the OpenAPI
  document's example or response schema when available (so the mapping is checked against the *documented* response),
  otherwise it is synthesised from the mapping. It also checks that the right HTTP method is sent.
- **Input validation** — the first required input is omitted and the run must be rejected (`input_invalid`).
- **Credential failure** — the mock returns HTTP 401 and the run must classify it as `auth_invalid`.

A test run is tied to the configuration hash. Submitting requires a passing run for that exact hash, unless the platform owner
disabled the *require tests* policy.

---

## 11. Credentials (connections)

A **connection** is a named credential. Configurations refer to it by name through `credential_ref`.

| Type | Secret fields | How it is sent |
| --- | --- | --- |
| Bearer token | token | `Authorization: Bearer …` |
| API key (header) | API key | header named by the integration's `header_name` |
| API key (query) | API key | query parameter named by `param_name` |
| Username + password | username, password | HTTP Basic |

- **Connect credential** (Credentials page, or the *Connect* buttons in AI Studio and on integration pages) stores the secret
  encrypted (Fernet) in the vault. The API only ever returns a masked hint such as `••••abcd`.
- **Test** checks the credential against the optional *Test URL*. Without one, it uses a read-only operation without required
  inputs from an integration that uses this connection. A 401 marks the connection **invalid**; success marks it **active**.
- **Rotate** replaces the secret (same name and type) and re-activates the connection.
- **Revoke** deletes the secret. Integrations using it fail with `credential_missing` until a new secret is connected.
- *Used by* lists the integrations that reference the connection.
- The creator or any admin may rotate, test or revoke a connection.
- An admin cannot approve an integration until an **active** connection of the right type with the referenced name exists.

---

## 12. Approvals

**Admins** see the queue (filters: pending, approved, rejected, changes requested, all). **Builders** see their own requests and
their status.

The **review page** shows:

- Requester, version, kind (*new*, *update*, or *AI repair proposal*), risk, decision.
- **Requested actions** (Read / Create / Update / Delete), **security review** outcome, **authorised destination**.
- **Checks**: domain checked, schema valid, secrets referenced safely, credential connected, tests passed, hash unchanged since submission.
- For repairs: the repair agent's diagnosis.
- **What it can do** — every operation with its full URL and safety class, plus the credential and scopes.
- **Changes vs live** — a field-level diff when another version is live.
- The full configuration, security findings, test results and the event history.

**Decisions** (admins, not on their own submissions):

| Decision | Effect |
| --- | --- |
| **Approve & publish** | Re-checks everything: hash unchanged, no blocking findings, configuration valid, active matching credential, passing tests. Then the version is published, the previous live version is superseded, and its tools are replaced by the new version's tools (per-tool access settings are carried over). |
| **Request changes** | A comment is required. The version becomes `changes_requested`; the integration shows `revision`. |
| **Reject** | A comment is required. The version becomes `rejected`. For a repair, the live version stays unchanged. |

If anything blocks approval, the page lists the reasons in a *Cannot approve yet* box and the button is disabled.
Submitting a newer version supersedes any older pending request for the same integration.

---

## 13. Tool Registry

Each operation of a **published** version becomes a **tool** named `<integration>.<operation>`, e.g. `taskflow.create_task`.

- Everyone sees the tools their role may run, with inputs, a **Schema** viewer (input schema plus MCP definition) and a **Run** link.
- **Admins** control each tool:
  - **Roles** — whether builders and/or admins may run it (owners always can).
  - **MCP** — whether AI agents can discover and call it over MCP (off by default). The MCP name is shown, e.g. `taskflow_create_task`.
  - **Enabled** — switch a single tool off without disabling the whole integration.
- Approving an integration does **not** give every user or agent access: role and MCP settings apply on top of approval.
- Only tools of the currently published version are executable. Tools of superseded versions are refused.

---

## 14. Test Lab and tool execution

- Choose a tool. A form is generated from its input schema: required fields are starred, enums become dropdowns, arrays accept
  JSON or comma-separated values.
- **Preview request** renders the exact HTTP request without sending it (method, URL, query, headers, body), with credentials
  shown as `••••••`. It also warns if the destination would be blocked.
- **Execute live** runs the tool through authorisation, the credential vault and the engine. The result shows status, HTTP code,
  duration, attempts, the mapped **output** and the **request sent** (redacted).
- On failure: the error class, its meaning and the recommended action. **Explain with AI** gives a short plain-language diagnosis.
  For `response_drift`, **Propose repair** starts the repair agent and the observed response structure is shown.
- Every execution is recorded and linked from Monitoring (*Open execution* shows a sanitized log).

---

## 15. The execution engine

Every execution (Test Lab, REST API, workflow step, MCP call) follows the same steps:

1. Resolve the tool; check that the integration is published, the tool is enabled and belongs to the live version, the caller's role
   is allowed, and (for MCP) that the tool is exposed.
2. Enforce limits: 60 executions per minute per user, at most 16 concurrent executions.
3. Fetch the credential through the connection named in the configuration.
4. Validate inputs against the operation's JSON Schema.
5. Render the request from templates; path parameters are URL-encoded and `.`/`..` are rejected, so the URL cannot leave the
   configured base URL.
6. Apply authentication.
7. Check the destination: HTTPS only; hostnames are resolved and every address must be public (private, loopback, link-local,
   CGNAT and cloud-metadata ranges are refused); the organisation's allowed domains apply. Only the bundled mock APIs are
   allow-listed as private destinations.
8. Send with the timeout (operation → integration → 15 s). Redirects are not followed, and responses over 2 MB are refused.
9. **Retries** only for idempotent operations (GET, PUT, DELETE, or `idempotent: true`): on 429/502/503/504, timeouts and network
   errors, with exponential backoff (default 0.5 s) honouring `Retry-After` (capped at 10 s). Create/update calls are never
   retried blindly, to avoid duplicates.
10. Check the status code, parse the body, paginate, apply the output mapping and detect drift.
11. Record the execution (status, HTTP code, duration, attempts, error class, redacted request preview, output, response
    structure) and an audit event, then pass the result to monitoring.

---

## 16. Workflows

A workflow chains published tools into an automation.

### 16.1 Creating

- **Describe with AI** — the Workflow Planning agent drafts a graph from your published tools ("When a webhook arrives with
  priority high, create a TaskFlow task and notify the security-team channel in Notify"). Create steps are placed before notify
  steps so a notification can reference what was created.
- **Blank workflow** — start with one step.
- Workflows drafted by AI Studio (requests with a trigger) also appear here.

### 16.2 The editor

- The **graph** shows the trigger and steps; arrows show dependencies, and conditions appear on the step. Click a node to edit it in
  the **inspector**.
- **Trigger** settings:
  - **manual** — run from the editor.
  - **webhook** — shows the URL and signing instructions.
  - **schedule** — every N minutes.
  - The **sample payload** is used for manual runs and dry runs.
- **Step** settings:
  - **Label** and **tool**. The tool's available outputs are listed as ready-to-copy references.
  - **Inputs** — literals or references: `${trigger.field}` for trigger data, `${steps.<id>.output.<field>}` for an earlier step's output.
  - **Runs after** — dependencies. A referenced step must be a (direct or indirect) dependency.
  - **Only if** — conditions that must all hold, with operators `eq`, `neq`, `contains`, `not_contains`, `exists`, `not_exists`,
    `gt`, `lt`, `in`.
  - **On failure** — stop the run, or continue with other steps.
- **Add step** / delete step; **Save** stores a new graph version (v1, v2, …).

### 16.3 Validation

Saving and running check the graph:

- Unique step ids, known dependencies, no cycles.
- Every tool published and runnable by the workflow owner.
- Required inputs provided.
- References only to `trigger.*` or to upstream steps.

Problems appear in a banner above the graph.

### 16.4 Running

- **Dry run** renders each step's request without calling any API. Later steps see placeholders such as `<n1.id>` for
  earlier outputs.
- **Run now** executes for real using the trigger's sample payload.
- Steps whose dependencies are met run **in parallel** (up to 4 at a time).
- A step is **skipped** if its condition is false or a dependency did not complete. After a failure with *stop*, the remaining
  steps are **cancelled**.
- Runs execute **as the workflow owner**, with the owner's current role and tool permissions, and every step is a recorded,
  audited execution.
- **Run history** shows each run and step with inputs, results, errors and links to the executions.

### 16.5 Activation and triggers

- **Activate** requires a valid graph with no warnings. Saving an invalid graph pauses an active workflow. **Pause** stops
  triggers; the delete button archives the workflow.
- **Webhook** triggers: on first activation a signing secret (`whsec_…`) is shown **once**. Senders `POST` JSON to
  `/api/v1/hooks/<workflow-id>` with:
  - `X-Integra-Timestamp`: Unix seconds; it must be within 5 minutes of the server's time.
  - `X-Integra-Signature`: `sha256=` followed by HMAC-SHA256(secret, `"<timestamp>.<raw body>"`) in hex.
  - `X-Integra-Delivery` (optional): a unique id. A repeated id is rejected as a replay.
  - Bodies are limited to 256 KB. The run starts in the background (HTTP 202).
- **Schedule** triggers are checked every 30 seconds and run when their interval has elapsed.

---

## 17. Monitoring and alerts

The Monitoring page refreshes every 15 seconds:

- **Success rate** and number of runs (24 h), **failures**, **p50 / p95 duration**, **open repairs**.
- **Executions per hour** chart (successful vs failed).
- **Error classes** breakdown, **by tool** volume, success rate and average duration.
- **Repair proposals** with diagnosis and a link to the review.
- **Recent executions**; click one for the detail page (result, request, output, response structure, sanitized log,
  *Explain with AI*, *Propose repair*).

**Alerts** (top-bar bell and the Dashboard) are raised automatically and de-duplicated per integration per hour:

| Alert | Raised when | Severity |
| --- | --- | --- |
| `auth_invalid`, `forbidden`, `credential_missing` | Credential rejected, missing scope, or no connection | high |
| `upstream_error` | The external service returns 5xx | medium |
| `not_found`, `request_rejected` | Endpoint missing or the request schema rejected | medium |
| `drift` | Expected response fields are missing | high |
| `approval` | A version was submitted for review | info |
| `repair` | A repair is ready for review, or no safe repair was found | medium / high |

---

## 18. Self-healing (repair proposals)

Self-healing prepares a fix but never changes production by itself.

1. **Detect** — an execution ends in `response_drift` (the API answered, but mapped fields are missing). The engine records the
   observed response **structure**: paths and types only, never values.
2. **Trigger** — after N drift failures on the same version within an hour (organisation policy, default 2), if no repair is
   already open, a repair starts automatically. Anyone can also click **Propose repair** on a drifted execution.
3. **Diagnose** — the Repair agent compares the failing mapping with the observed structure and proposes new JSONPath
   expressions (e.g. `$.assignee.user_name` → `$.assignee.display_name`). Proposals that point to paths absent from the
   observed structure are discarded.
4. **Patch** — a new version (patch bump, source *repair*) is created from the live version with the mapping changes; it is
   validated and security-reviewed.
5. **Regression test** — the normal safe tests plus a test against a response rebuilt from the observed (drifted) structure.
6. **Submit** — if everything passes, it is submitted as an *AI repair proposal* (requester: "AI repair agent") and an alert is raised.
7. **Review** — an admin approves (the fix goes live) or rejects (the live version stays unchanged). If no safe change exists, the
   proposal is marked *failed* and an alert asks for manual review.

Only structure drift is repaired automatically. Other failures get classification, guidance and an AI explanation.

---

## 19. Version History and rollback

- **Version History** lists every version across all integrations: live flag, status, source (*ai*, *manual*, *openapi*,
  *repair*, *seed*), risk, tests, hash, author and date. Click a row to open it on the integration page.
- Numbering: AI regeneration creates the next minor version (1.1.0); revisions and repairs create the next patch (1.1.1).
- **Compare with live** shows added, removed and changed fields.
- **Rollback (Restore)** — admins can republish a *superseded* version whose exact configuration hash was approved before.
  The security policy is re-checked, and the action is recorded as `integration.rollback`. Any other older version must go
  through *New revision* and normal review.

---

## 20. AI Providers and the AI gateway

All AI work goes through one gateway. Each agent call names a **task**:

| Task | Structured JSON | Handles confidential material |
| --- | :---: | :---: |
| conversation | — | — |
| requirements | ✓ | ✓ |
| api_discovery | ✓ | ✓ |
| config_generation | ✓ | ✓ |
| security_review | ✓ | ✓ |
| test_generation | ✓ | ✓ |
| repair | ✓ | ✓ |
| workflow_planning | ✓ | ✓ |
| error_explanation | — | ✓ |

**Choosing a model.** For each call the gateway:

1. Drops models that are disabled or whose provider is disabled.
2. Drops models not permitted for confidential data when the task is confidential.
3. Drops paid models when the organisation's monthly AI budget is spent.
4. Drops models whose context window is too small (over 80% full).
5. Ranks the rest: external models before the built-in one; models whose *preferred tasks* include this task, then
   `general` models, then others; models with reliable JSON output first for structured tasks; then by **priority**
   (lower first).

**Fallback and validation.** Structured output must match the agent's JSON Schema (plus extra rules, such as "do not change the
base URL"). On invalid output the model gets one chance to correct itself. On an error, timeout or second invalid answer the next
model is tried. The **Built-in (offline)** model is always last and runs every agent deterministically, so the platform works
without any API key.

**Privacy.**
- Credential-like strings are redacted from every prompt.
- Uploaded documents and API data are wrapped in `<untrusted_document>` tags, and the model is told never to follow
  instructions inside them.
- Repair prompts receive response structure (types), not values.

**Usage.** Every call (including failures) is recorded with model, task, tokens, latency and estimated cost.

**The AI Providers page.**

- **Everyone** sees the providers, the models and the **task routing** list (which models each task would try, in order).
- **Admins** also see the monthly usage table (calls, tokens and cost by model and task, recent failures) and can **Test** a provider.
- **Owners** can:
  - **Add provider** — name, type, base URL, API key (stored in the vault), first model and preferred tasks. Types:
    *OpenAI-compatible* (OpenAI, NVIDIA, Groq, OpenRouter, Ollama, LM Studio…), *Anthropic Claude*, *Google Gemini*.
  - **Enable / disable** a provider, **rotate** its key, **delete** it.
  - **Register / edit / delete models**: model id, label, priority, context window, input and output cost per million tokens,
    reliable JSON output, permission for confidential material, enabled, preferred tasks.
  - The built-in provider cannot be deleted (only disabled, or its priority changed).
- Providers can also be seeded at first start from `OPENAI_*`, `ANTHROPIC_*` and `GEMINI_*` environment variables.

---

## 21. Settings

- **Organisation policy** (admins; read-only for builders):
  - *Allowed API destinations* — comma-separated domains (subdomains included). Empty means any public HTTPS host. Applied both
    in the security review (blocking) and at runtime.
  - *Monthly AI budget (USD)* — default 25. When reached, only free models are used.
  - *Auto-repair after N drift failures* — default 2.
  - *Require passing safe tests before review* — default on; **owner only**.
- **API & MCP access** — create personal API tokens (shown once, `iat_…`), see when each was last used, revoke them. Includes a
  ready-made MCP client configuration.
- **Members** — list of members and pending invitations. Admins invite and change roles (builder ↔ admin); owners can also grant
  or remove the owner role. You cannot change your own role.
- **Demo controls** — admins can toggle **TaskFlow response drift**. Everyone sees the mock TaskFlow tasks and Notify messages
  created by executions, which proves a call reached the external service.

---

## 22. MCP gateway for AI agents

IntegraAI exposes approved tools to Model Context Protocol clients at `POST /mcp`.

- **Authentication:** `Authorization: Bearer <personal API token>`. The agent acts with the token owner's role.
- **Methods:**
  - `initialize`, which negotiates protocol versions 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25.
  - `ping`, `tools/list` and `tools/call`.
  - Notifications get HTTP 202, and JSON-RPC batches are supported.
- **Visibility:** a tool is listed only if it is published, enabled, marked **MCP** by an admin, and runnable by the caller's role.
  Names are `<integration>_<operation>`. Read-only and destructive hints are included.
- **Calls** go through exactly the same authorisation, rate limits, vault and engine as any other execution, and are recorded with
  actor type *mcp*. Results come back as text plus `structuredContent`, with `isError` on failure.
- MCP tokens can never approve, publish or change governance.

Example client configuration:

```json
{ "mcpServers": { "integra": { "type": "http", "url": "http://127.0.0.1:8000/mcp",
  "headers": { "Authorization": "Bearer <your token>" } } } }
```

---

## 23. Audit log

Every significant action is recorded with actor, action, target and details, for example:

| Group | Actions |
| --- | --- |
| auth / members | `auth.login`, `member.invite`, `member.join`, `member.role` |
| integrations | `integration.create`, `version.create`, `version.update`, `version.test`, `integration.publish`, `integration.disable`, `integration.enable`, `integration.rollback` |
| approvals | `approval.submit`, `approval.approve`, `approval.rejected`, `approval.changes_requested` |
| runtime | `tool.execute`, `tool.settings` |
| credentials | `connection.create`, `connection.rotate`, `connection.test`, `connection.revoke` |
| workflows | `workflow.create`, `workflow.update`, `workflow.activate`, `workflow.pause`, `workflow.archive`, `workflow.run` |
| self-healing | `repair.propose` |
| AI platform | `ai.provider.create/update/delete`, `ai.model.create/update` |
| policy / tokens / demo | `policy.update`, `token.create`, `token.revoke`, `demo.drift`, `platform.seed` |

Admins see the whole organisation; builders see their own actions. Filter by group with the dropdown. Approval records include
the configuration hash.

---

## 24. Security controls at a glance

| Threat | Control |
| --- | --- |
| AI approving or publishing its own work | No code path from agents to approval; approval requires an interactive admin session; reviewer ≠ requester. |
| Config changed after approval | Approval bound to the SHA-256 hash; submitted versions are immutable; the hash is re-verified on approval. |
| SSRF / calls to internal services | HTTPS only, DNS-resolved IP checks, metadata IPs blocked, no redirects, fixed base host, org allow-list. |
| Leaked credentials | Vault encryption, masked hints, redacted previews and logs, secrets blocked in configs, redaction before AI calls. |
| Prompt injection via documents | Untrusted-document wrapping, schema-validated outputs, deterministic re-checks, the AI cannot change destinations or auth. |
| Unauthorised execution | Per-tool role lists, MCP opt-in, published-version check, disabled integrations refused. |
| Duplicate side effects | No automatic retries for non-idempotent operations; webhook replay protection. |
| Abuse / overload | Login throttling, 60 executions per minute per user, concurrency cap, timeouts, payload limits. |
| CSRF / XSS | SameSite=Strict HttpOnly cookie, required custom header, strict Content-Security-Policy, escaped rendering. |
| Repudiation | Audit log for governance, runtime and configuration actions. |

---

## 25. Demo services and seeded data

On first start the platform creates:

- Organisation **Acme Corp** with the default policy.
- Users **owner**, **admin** and **builder**, all sharing the seed password.
- The **Built-in (offline)** AI provider, plus any providers configured in the environment.
- Published reference integrations **GitHub Issues** and **Slack Messaging** (they need real tokens to run) and **Notify**
  (already connected).

Two mock third-party APIs are served under `/mock/` and reached over HTTP like any external API:

| Mock API | Auth | Purpose |
| --- | --- | --- |
| **TaskFlow** (`/mock/taskflow`, spec at `/mock/taskflow/openapi.json`) | Bearer `taskflow-demo-token` | The "never seen before" task-management API used to demonstrate AI onboarding. Its drift switch renames `assignee.user_name` to `assignee.display_name`. |
| **Notify** (`/mock/notify`) | Header `X-API-Key: notify-demo-key` | Team alert messages, used in workflows. |

---

## 26. Limits and defaults

| Item | Value |
| --- | --- |
| Session lifetime | 12 hours |
| Failed logins before lock-out | 8 within 5 minutes per account and address |
| Invitation validity | 7 days |
| Password minimum (invitations) | 10 characters |
| OpenAPI upload | 1 MB, OpenAPI 3.x only |
| Operations per integration | 50 |
| Request timeout | 15 s default, 1–60 s configurable |
| Response size | 2 MB |
| Retry attempts (idempotent) | 2 by default, up to 5 |
| Executions | 60 per minute per user, 16 concurrent |
| Pagination | up to 20 pages, 500 items per page |
| Webhook body / signature window | 256 KB / 5 minutes |
| Scheduler resolution | 30 seconds |
| Workflow steps | 40, up to 4 in parallel per layer |
| AI budget | 25 USD per month by default |
| Auto-repair threshold | 2 drift failures within 1 hour |
| AI request timeout | 60 s |

---

## 27. REST API reference

All endpoints are under `/api/v1`. Interactive documentation is at `/docs`.

| Area | Endpoints |
| --- | --- |
| Auth | `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me`, `POST /auth/accept-invite` |
| Organisation | `GET /organizations/current`, `POST /organizations/invitations`, `PATCH /members/{id}/role`, `PATCH /organizations/current/policy` |
| Tokens | `GET/POST /tokens`, `DELETE /tokens/{id}` |
| AI Studio | `POST/GET /ai/sessions`, `GET/DELETE /ai/sessions/{id}`, `POST /ai/sessions/{id}/messages`, `/documents`, `/generate`, `/revise` |
| AI platform | `GET/POST /ai/providers`, `PATCH/DELETE /ai/providers/{id}`, `POST /ai/providers/{id}/test`, `POST /ai/providers/{id}/models`, `PUT/DELETE /ai/models/{id}`, `GET /ai/routing`, `GET /ai/usage` |
| Integrations | `GET/POST /integrations`, `POST /integrations/import/openapi`, `GET /integrations/{id}`, `/versions`, `POST /integrations/{id}/validate`, `/tests`, `/preview`, `/submit`, `/revise`, `/rollback`, `GET /integrations/{id}/compare?a=&b=`, `GET /versions`, `GET/PUT /versions/{id}`, `GET /catalog/examples`, `POST /admin/integrations/{id}/disable`, `/enable` |
| Approvals | `GET /admin/approvals?status=`, `GET /approvals/mine`, `GET /admin/approvals/{id}`, `POST /admin/approvals/{id}/approve`, `/reject`, `/changes` |
| Credentials | `GET/POST /connections`, `PUT /connections/{id}/secret`, `POST /connections/{id}/test`, `DELETE /connections/{id}` |
| Tools & executions | `GET /tools`, `GET /admin/tools`, `GET /tools/{id}/schema`, `PATCH /admin/tools/{id}`, `POST /tools/{id}/execute`, `GET /executions`, `GET /executions/{id}`, `/logs`, `POST /executions/{id}/explain`, `/repair` |
| Monitoring | `GET /monitoring/metrics?hours=`, `GET /monitoring/failures`, `GET /repairs`, `GET /alerts`, `POST /alerts/{id}/ack`, `POST /alerts/ack-all`, `GET /audit?action=` |
| Workflows | `GET/POST /workflows`, `POST /workflows/plan`, `GET/PUT/DELETE /workflows/{id}`, `POST /workflows/{id}/validate`, `/activate`, `/stop`, `/execute`, `GET /workflows/{id}/runs`, `POST /hooks/{id}` (signed webhook) |
| Demo | `GET /demo/state`, `POST /demo/drift` |
| MCP | `POST /mcp` (outside `/api/v1`) |

Programmatic clients authenticate with `Authorization: Bearer <API token>`.

---

## 28. Error classes

| Class | Meaning | Recommended action |
| --- | --- | --- |
| `input_invalid` | Inputs do not match the input schema | Fix the inputs |
| `credential_missing` | No active connection of the right type | Connect or re-activate the credential |
| `egress_denied` | Destination blocked by the network policy | Use an approved public HTTPS destination |
| `auth_invalid` | HTTP 401 — invalid or expired credential | Update the token and re-test the connection |
| `forbidden` | HTTP 403 — missing permission or scope | Grant the scope or use another account |
| `not_found` | HTTP 404 — endpoint or resource missing | Check identifiers; request a repair if the endpoint moved |
| `rate_limited` | HTTP 429 | Retried for idempotent operations; slow down or raise the quota |
| `upstream_error` | HTTP 5xx | Usually transient; retried for idempotent operations |
| `request_rejected` | HTTP 400/409/415/422 | The request schema may have changed |
| `unexpected_status` | Status not listed as success | Review `status_codes` |
| `timeout` / `network_error` | No response / unreachable | Check the base URL and service health |
| `response_invalid` | Body is not valid JSON | Set `format: text` or fix the endpoint |
| `response_too_large` | Over 2 MB | Use pagination or narrower queries |
| `response_drift` | Answered, but mapped fields are missing | A repair is proposed for admin review |
| `config_error` | The configuration could not be rendered | Validate the configuration |
