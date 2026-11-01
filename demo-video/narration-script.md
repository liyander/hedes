# H.A.D.E.S demo narration

Teams glue together dozens of SaaS APIs. When one of them changes, or a token expires, someone finds out days later.

Meet HADES. Describe an integration in plain English. The AI builds it, a human approves it, and the platform runs it, heals it, and lets you manage it from Slack and WhatsApp.

The dashboard shows what's live, what needs a decision, and overall health. One rule runs everything: AI plans, deterministic code executes, humans approve.

In AI Studio, I load a task-management API spec, and describe the outcome I want.

The AI extracts the services, operations and trigger. Before generating, it shows exactly which records it will create.

The pipeline runs. API discovery, a declarative config instead of code, schema validation, a security review, and safe tests on mocked responses. Six of six pass, and it goes to approval.

Credentials go through a secure form into an encrypted vault. The AI never sees them.

The admin sees every check: domain, schema, secrets, credential, tests, and an unchanged hash. Approval publishes exactly that hash. The AI can never approve its own work.

The request also drafted a workflow: a graph of steps that can run manually, on a signed webhook, on a schedule, or automatically on a failure, an alert, or a new record.

A dry run renders every request without sending anything.

Data sources pull from Okta, CloudTrail, Google, Microsoft, GitHub, Jira and more, into one common format, with de-duplication and a dead-letter queue.

Now, self-healing. I set it to fix automatically, then simulate the vendor changing its API.

The next call fails with response drift. A field the integration depends on has moved.

HADES opens a case, collects evidence, finds the root cause, remaps the field, and publishes version one point zero point one, after validation and tests. Every step is logged. Critical fixes always wait for an admin.

Monitoring tracks success rate, latency and error classes, with every repair in one place.

From Slack, reply under a failure alert, and the AI investigates and explains the root cause.

Ask it to fix the problem, and HADES posts Approve and Reject buttons. Only the person who asked can approve.

On WhatsApp, admins get personal alerts with one-tap fix links, plus a chatbot.

In the app, the assistant answers from live data, and every change it proposes becomes an approval card.

Bring any model: Bedrock, Gemini, OpenAI, Anthropic, Groq, Ollama, routed per task with budgets and fallbacks. A built-in offline model powered this entire demo.

The tool registry sets which roles may run each tool, and which tools are exposed to AI agents over M C P.

Versions are immutable, with one-click rollback, and the audit log records every action, by people, the AI, agents and chat.

HADES. AI plans, code executes, humans approve. Live on Zoho Catalyst, with a hundred and ninety-two passing tests. Thanks for watching.
