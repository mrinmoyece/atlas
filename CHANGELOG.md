# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com); versioning is
semantic.

## [Unreleased]

### Fixed (adversarial review round)
- **Security**: path confinement used `str.startswith` rather than path
  ancestry, so a sibling directory with a shared name prefix escaped the
  sandbox. Fixed in the toolkit, the API and the judge.
- **Evaluation**: `lstrip("./")` strips a character set, not a prefix, so
  repo-root ground truth could never match and was scored as both a miss and
  a false positive. Baseline F1 moved 0.783 -> 0.870 once corrected. Path
  matching is now one-directional.
- **Memory**: the A/B harness pre-trained on the repositories it then scored.
  Leak removed; the corrected cross-repo result is a null, and is published
  as such alongside a separate longitudinal experiment.
- **Streaming**: `/v1/analyses/stream` awaited the entire run before emitting,
  so every event arrived at once. Now streams per-node updates via
  `stream_due_diligence`.
- **Memory loop**: the graph recalled from memory but never wrote back; runs
  now learn (with a derived, not hardcoded, success signal).
- **Packaging**: source-relative path discovery 404'd inside the container;
  fixtures and project root are now configurable.
- **Spend limiting**: check-then-spend was a TOCTOU race; replaced with
  reserve-and-reconcile. Daily windows are now per principal.
- **Metrics**: histograms retained every raw sample; now bounded bucket
  counters.
- **Audit**: tail truncation was undetectable; added sequence numbers and a
  written-count high-water mark.
- **Judge**: the "position bias control" compared two different reports
  instead of swapping order and always returned agreement; replaced with a
  real comparator that reports whether bias was possible at all.
- CI lint job was red on arrival (40 ruff findings); now clean.
- Gates tightened from 0.60/0.55/2 to 0.80/0.75/0.80/1 against the corrected
  baseline.

### Added
- `tests/test_regressions.py`: a regression test per defect above, plus the
  six regressions introduced by the fixes themselves.
- Suite grew from 85 to 110 tests.

### Fixed (second review pass - regressions from the first round of fixes)
- Spend reservations were taken before request validation, so 404 probes
  accumulated budget that was never released. Reservations now follow
  validation and are released in a `finally`, including on client disconnect
  mid-stream.
- Eval and benchmark runs learned to memory twice (internally and via the
  harness), doubling every record and skewing recency decay. `run_repo` now
  defaults to `learn=False`.
- `pattern_name` defaulted to `"react"` at every layer, making the learned
  procedural-memory hint unreachable. Now defaults to `None`: explicit
  request > learned hint > default.
- `check_spend` survived as a public method with the exact TOCTOU shape the
  reservation fix removed; renamed `peek_spend` and documented as read-only.
- Bare-suffix path matching let `"xa.py"` match ground truth `"a.py"`; now
  boundary-aware.
- CI linted `src tests benchmarks` only, missing `evals` and `scripts`.

## [0.1.0] - 2026-08-03

Initial release.

### Added
- LangGraph orchestration: supervisor + four specialists with parallel
  fan-out, context isolation, reducer-based merging, failure isolation
- Four reasoning patterns (ReAct, Plan-and-Execute, Reflexion, ReWOO) behind
  one measured contract, plus a benchmark harness and results
- Evaluation stack: fixture repos with planted ground truth and traps,
  precision/recall/F1 with micro-averaging, deterministic groundedness
  checks, judge with position-bias controls, three CI tiers with a merge gate
- Four-tier memory (working, episodic, semantic, procedural) with recency
  decay, relevance floor, Laplace-smoothed strategy selection, and an A/B
  harness that measures whether memory helps
- Context engineering: token budgets and compaction that never orphans tool
  results
- MCP server and trust-aware MCP client; A2A agent cards with
  capability-based routing
- Security: hashed API keys with constant-time comparison, RBAC, token-bucket
  rate limiting, pre-flight spend budget, hash-chained audit log, security
  headers, pure-ASGI body limits, secret redaction
- Observability: structured JSON logging with redaction, OpenTelemetry
  spans, Prometheus metrics with bounded cardinality
- FastAPI surface with SSE streaming; Docker (non-root, read-only), k8s
  manifests with egress NetworkPolicy, CI with CodeQL, pip-audit, SBOM
- 85 tests; docs: architecture, 8 ADRs, learning path, security model,
  evals, memory, failure modes, runbook, limitations, interview study guide
