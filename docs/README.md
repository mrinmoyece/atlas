# Documentation map

Each Atlas document has one primary concern. Cross-cutting documents link to
the canonical detail instead of redefining it.

| Concern | Canonical document | Scope |
|---|---|---|
| Project overview and local quickstart | [README](../README.md) | audience, headline evidence, install/demo/API entry points |
| Component architecture and invariants | [Architecture](architecture.md) | layers, boundaries and scaling seam |
| End-to-end AI system design | [AI system design](ai-system-design.md) | request/state lifecycle, memory, evals, guardrails, telemetry and performance trade-offs |
| Evaluation methodology | [Evaluation](EVALS.md) | fixtures, scoring, judges, tiers and executable thresholds |
| Current evaluation evidence | [Standard eval snapshot](../evals/last_run.md) | generated result committed and drift-checked in CI |
| Memory design and evidence | [Memory](MEMORY.md) | tiers, retrieval failure analysis and A/B results |
| Performance evidence | [Performance](PERFORMANCE.md) | platform benchmark scope, latency budgets and cache measurements |
| Threat model and controls | [Security model](SECURITY_MODEL.md) | trust boundaries, control mapping and endpoint permissions |
| Vulnerability reporting | [Security policy](../SECURITY.md) | private disclosure channel, scope and response expectation |
| Deployment and operations | [Runbook](RUNBOOK.md) | configuration, deployment, monitoring, incidents, rollback and reference SLIs/SLOs |
| Runtime failure semantics | [Failure modes](FAILURE_MODES.md) | degradation behavior, tests and residual risks |
| Unsupported capabilities and roadmap | [Limitations](LIMITATIONS.md) | current boundaries and evidence-based upgrade paths |
| Development and testing | [Contributing](../CONTRIBUTING.md) | setup, quality gates, generated artifacts and PR rules |
| Learning curriculum | [Learning path](LEARNING_PATH.md) | progressive reading order and exercises |
| Interview preparation | [Interview study guide](INTERVIEW_STUDY_GUIDE.md) | audience-specific rehearsal; it links to canonical technical detail |
| Change history | [Changelog](../CHANGELOG.md) | released and unreleased changes |

## Decision records

ADRs explain why a durable design choice was made. Current behavior remains
defined by source and tests.

| ADR | Decision |
|---|---|
| [0001](adr/0001-langgraph.md) | Use LangGraph rather than a hand-rolled orchestration loop |
| [0002](adr/0002-ground-truth-evaluation.md) | Evaluate against planted ground truth, not model judgement alone |
| [0003](adr/0003-context-isolation.md) | Keep specialist contexts isolated; cross only findings and summaries |
| [0004](adr/0004-deterministic-scripted-model.md) | Treat the scripted model as first-class test infrastructure |
| [0005](adr/0005-memory-tiers.md) | Use four memory tiers and structured-only writes |
| [0006](adr/0006-mcp-trust-boundary.md) | Treat remote MCP servers as untrusted input |
| [0007](adr/0007-security-controls.md) | Make run creation and spend the privileged operation |
| [0008](adr/0008-verdict-is-derived.md) | Derive the executive verdict from structured findings |

## Intentional omissions

There is no separate deployment guide, observability guide, SLO document or
roadmap. Those concerns are small enough to remain coherent in the
[runbook](RUNBOOK.md), [AI system design](ai-system-design.md) and
[limitations](LIMITATIONS.md). There is also no live-model benchmark report:
the current eval/benchmark runners intentionally instantiate the scripted
provider, and no statistically meaningful live-provider experiment has been
implemented.
