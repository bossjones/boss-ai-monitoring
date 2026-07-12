# Spec briefs — agent-facing extraction of the spec

**Derived from [`../boss-ai-monitoring.html`](../boss-ai-monitoring.html) (canonical) on 2026-07-11
by Claude Fable 5**, cross-checked against a full spec-vs-prompt gap analysis the same day.

The HTML is the human-facing document — styled, illustrated, with the full research digest and
decision history. These briefs are the **agent-facing working copies**: one per build-team pane,
scoped to that pane's file ownership, with schemas, edge cases, and acceptance criteria carried
over verbatim. A 61KB HTML read costs every worker ~20k tokens per lookup; a scoped brief costs
2-4k and omits nothing the pane owns.

## Rules

1. **The HTML wins.** If a brief conflicts with the spec HTML or with observed behavior, the
   evidence wins — file an OQ in `.team/boss-ai-monitoring-build.open-questions.md`. Do not
   silently follow the brief over the spec.
2. **Read-only during a build run**, same as the spec HTML. No status markers here; durable
   progress lives on the board.
3. **Where a brief deviates from the HTML's literal text, the deviation is deliberate and
   flagged** with a `> RUN NOTE:` block (test layout, no-push, browser order — all decided in
   [`prompts/boss-ai-monitoring-build-team.md`](../../../prompts/boss-ai-monitoring-build-team.md)
   rev 3+ and its GROUND TRUTHS).

## Index

| Brief | Pane | Covers |
|---|---|---|
| [`shared.md`](shared.md) | everyone | architecture, event envelope, ground truths, OTel/LangSmith reference, metric definitions |
| [`lead.md`](lead.md) | 👑 lead | Phase 1: scaffold, config module, `bam` CLI, justfile/CI, `.env.sample` |
| [`store.md`](store.md) | 🧱 store | Phase 2: DuckDB schema, batched writer, SQL views |
| [`otlp.md`](otlp.md) | 📡 otlp | Phase 3: OTLP http/json receiver |
| [`jsonl-langsmith.md`](jsonl-langsmith.md) | 📜 jsonl | Phases 4+5: transcript reader, LangSmith poller |
| [`web.md`](web.md) | 🖥 web | Phases 6+7: dashboard, SSE, agent frontend loop |
| [`jobs.md`](jobs.md) | ⚙️ jobs | Phases 8+9: quality jobs, marimo, Docker, docs proposals |
| [`validator.md`](validator.md) | ✅ validator | TDD enforcement, GATE checklist, live-telemetry recipe |
