# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com); versioning is
semantic.

## [Unreleased]

This section describes the current working tree after the `0.1.0` project
version; it is not a published GitHub release and no release tag exists yet.

### Fixed (enterprise-readiness audit)
- Procedural memory now learns under the same repository-size context used by
  recall instead of writing every strategy result to `repo:unknown`.
- Context truncation reserves space for its marker rather than exceeding the
  message allocation.
- Audit records retain a bounded, verifiable in-memory tail and can restore an
  append-only JSONL chain after restart; injected sinks are preserved even
  while empty. Request buckets have bounded identity state with idle eviction
  and a shared overflow bucket.
- Authenticated Prometheus scraping works in Compose and through the optional
  Kubernetes ServiceMonitor; audit JSONL is mounted on persistent storage and
  Kubernetes uses `Recreate` so revisions never write the chain concurrently.
- Runtime dependencies are reproducibly hash-locked for Python 3.12, audited
  from the deployed image, and represented by an image-derived CycloneDX SBOM.
- CI actions and container bases are digest-pinned, generated artifacts are
  drift-checked, Gitleaks scans history, Trivy gates the image, and
  public-repository branch/security settings are enforced. The image scan uses
  a digest-pinned Trivy container so it works under the repository's action
  allowlist without invoking an unapproved transitive action.

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

### Fixed (blind review rounds)
- Live providers were never bound to tools, and federated tool specs were
  unreachable from the production graph. Both local and allowlisted remote
  tools are now on the actual agent path rather than only demos/tests.
- Specialist timeouts waited for timed-out workers during executor teardown;
  timeout handling now returns at the configured bound.
- The run cost ceiling read a stale fan-out value and spend projection was
  lower than the per-run ceiling. Cost is enforced per model call, and a run
  reserves the configured ceiling.
- Repository walking followed symlinks around confinement, regex guards had
  complexity bypasses, and reads could allocate based on attacker-controlled
  line counts. Tool traversal, deadlines and input/output bounds were hardened.
- Request-rate enforcement skipped invalid repositories, while streaming
  exhaustion could raise after the response started. Invalid requests consume
  rate capacity and stream preflight returns a normal 429.
- Concurrent runs for one repository shared cost-ledger state. Ledgers are now
  run-scoped.
- Filtered semantic retrieval bypassed its relevance floor/decay, and
  procedural context used repository-name length. Production retrieval now
  exercises the intended controls and context uses repository characteristics.
- Ground-truth cardinality, trap path matching, step accounting and pairwise
  judge tie handling were corrected; regression tests exercise production
  branches rather than guard short-circuits.
- Security headers now cover error responses; MCP allowlists deny by default;
  `ATLAS_DAILY_SPEND_USD` is a real documented setting.
- Benchmark and demo copy now distinguishes authored scripted behaviour from
  live-model evidence and does not claim unreachable memory/tool injection.

### Documentation
- Deployment is explicitly scoped to one local replica, with PostgreSQL,
  Redis, OIDC/JWKS, tenant isolation, managed secrets, off-box WORM audit and
  multi-region HA documented as future extension paths rather than features.
- Added RBAC and deployment validation matrices, example single-instance
  SLO/RTO targets, authenticated metrics setup, prompt-injection response, the
  spend-budget invariant, audit retention/persistence boundaries and runtime
  SBOM follow-ups.
- Synchronized performance budgets with `perf/benchmark.py` while preserving
  the 2026-08-09 measurements as a dated baseline.
- The offline suite was expanded with regression coverage for the enterprise
  readiness findings; overview documentation no longer hardcodes a count that
  becomes stale whenever coverage improves.
- Added a canonical documentation map and an evidence-linked AI system design
  case study; tracked the generated standard eval snapshot so CI drift checks
  now operate on a committed artifact.
- Corrected live-provider claims: current eval/benchmark runners force the
  scripted model, and the Anthropic adapter does not yet price usage metadata.
- Specialist failures now emit error metrics, streaming runs contribute run
  metrics, partial completions are explicit in the audit log, and the demo uses
  the graph's real repository-size memory context.
- Specialist failures retain usage incurred before the exception, and streaming
  reports carry token/model-call totals into procedural-memory statistics.
- Streaming procedural learning now records the graph plan's selected strategy
  and the complete structured report, including specialist summaries and usage,
  matching non-streaming learning semantics.
- Provider clients now use a bounded eight-entry identity LRU that retains each
  cached `Settings` object; this is separate from the bounded compiled-graph
  cache.
- Graph cost governance now requires a unique admitted run ID before execution.
  Missing, unknown and retired IDs fail closed before model calls, while retired
  tombstones are TTL- and capacity-bounded without weakening active admission.

## [0.1.0] - 2026-08-03

Initial release.

### Added
- LangGraph orchestration: supervisor + four specialists with parallel
  fan-out, context isolation, reducer-based merging, failure isolation
- Four reasoning patterns (ReAct, Plan-and-Execute, Reflexion, ReWOO) behind
  one measured contract, plus a benchmark harness and results
- Evaluation stack: fixture repos with planted ground truth and traps,
  precision/recall/F1 with micro-averaging, deterministic groundedness
  checks, judge with position-bias controls, three eval tiers with smoke and
  standard wired into CI
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
