# Limitations

Honest inventory. Each item: what is limited, why it was acceptable, and the
upgrade path.

## Evaluation
- **Two fixture repositories.** Enough to demonstrate scoring, traps and
  pattern differences; not enough for statistical confidence. Upgrade: more
  fixtures across languages, and a held-out set the scripts have never seen.
- **Deterministic model.** Results prove the harness works, not that a live
  model performs this way. The current eval, benchmark and memory A/B paths
  instantiate `model_for(repo)` directly, so `ATLAS_PROVIDER=anthropic` does
  not switch them. Upgrade: add an explicit provider-injected runner, N runs
  per scenario and pass-rate thresholds instead of exact assertions.
- **Rule-based judge by default.** Cannot assess prose quality. Upgrade: LLM
  judge behind the same `Judge` protocol, extended/manual tier only.
- **Rule renames are not caught** by the fixture/ground-truth sync test —
  only paths are verified.

## Tools
- **ReDoS is bounded by a process boundary, not by pattern analysis.**
  `find_ambiguous_quantifier` rejects the obvious payloads fast, but a
  denylist over quantified *groups* cannot be complete: `a*a*a*a*a*a*a*a*a*b`
  is exponential against 60 characters and contains no group to inspect. So
  every grep runs in a forked child that is killed at
  `GREP_DEADLINE_S + GREP_KILL_GRACE_S`. That holds, and it costs a fork per
  grep. Upgrade: a linear-time engine (`re2`, or the `regex` module's real
  timeout) would remove both the fork and the guard.
- **Fork-only.** On a platform without `fork`, grep falls back to running
  in-process and logs a warning - the bound is lost, loudly rather than
  silently.

## Memory
- **Feature-hashing embeddings.** Deterministic and dependency-free; weak on
  paraphrase. Upgrade: real embeddings + pgvector behind `Embedder`.
- **In-process stores.** Lost on restart, not shared across replicas.
  Upgrade: Postgres for episodic/procedural, pgvector for semantic.
- **Deterministic lesson distillation.** Cheap and safe; less rich than
  LLM-written lessons. Upgrade documented in ADR-0005.
- **Repository-size-only `context_key`.** Procedural memory segments strategy
  learning into bounded `repo:standard` and `repo:large` buckets. It does not
  yet distinguish language, framework or tenant, so learning remains coarse
  within each size class.

## Deployment
- **Single replica, and `k8s/deployment.yaml` says `replicas: 1`.** Four
  things live in process memory: the memory hub, the audit hash-chain, the
  rate-limit and spend ledger, and the LangGraph checkpointer. At two
  replicas each of them silently forks - two divergent audit chains, a spend
  cap that admits 2x its stated limit, and a "resumable" run that only
  resumes if the retry lands on the pod that started it. None of that fails
  loudly, which is what makes it worth stating here. The manifest shipped
  `replicas: 2` until a review caught it.
  This is a local single-instance reference deployment, not an HA topology.
  Upgrade path, in dependency order: PostgreSQL durable LangGraph
  checkpointer -> PostgreSQL/pgvector durable memory -> Redis distributed
  rate/spend limits -> off-box append-only WORM audit sink. Each requires
  migration, failure-mode and restore testing; naming the extension point does
  not mean it is implemented.

## Orchestration
- **Single-process.** Parallelism is asyncio/threads within one node, and
  the checkpointer is in-memory. Upgrade: durable checkpointer + worker
  pool.
- **No mid-run cancellation.** A run completes even if the client
  disconnects.
- **Specialists cannot build on each other** within a run (ADR-0003). A
  second graph pass is the fix if a domain needs it.
- **No human-in-the-loop approval gate.** Atlas is read-only, so nothing
  needs approving; a write-capable version would need one.

## Security
- **No external code sandbox** — so no exec tool is shipped. This is the
  largest gap between this and a platform that runs untrusted code.
- **In-process rate limiting** multiplies by replica count. Redis fixes it.
- **API keys, not OIDC.** `Principal` is shaped for the swap.
- **No multi-tenancy.** Memory and audit are global.
- **Audit log is in-process** by default; a JSONL sink exists, a WORM store
  does not. The in-memory tail is bounded (10,000 entries by default), so old
  entries are evicted. JSONL adds local restart persistence, not protection
  from host loss or an attacker who controls that host. Upgrade: send entries
  and independently anchored chain heads to managed off-box WORM storage.
- **Static API keys, not generic OIDC/JWKS.** The principal seam makes a future
  verifier possible; issuer/audience validation, key rotation and claim-to-role
  mapping are not present.
- **No tenant isolation.** Adding a tenant id only at the API is insufficient:
  repository resolution, memory, checkpoint keys, budgets, metrics and audit
  all need an enforced tenant boundary and isolation tests.
- **Environment secrets, not managed-secret integration.** Deployment can
  inject environment values, but no Vault/cloud secret-manager integration or
  automatic rotation is shipped.
- **No multi-region HA.** Durable regional stores, replication/failover,
  idempotency and reconciliation are future architecture work, not a replica
  count change.
- **Live-provider cost is not priced.** Runtime metering consumes
  `response_metadata["cost_usd"]`; the scripted model supplies it, but the
  current `ChatAnthropic` adapter does not. Pre-flight reservations still bound
  concurrent admissions, but sequential live requests settle at zero, so the
  daily spend ledger and run-cost brake are not effective cost controls on that
  path. Upgrade: normalise provider usage through a versioned pricing adapter
  and test cached-input/output-token cases before claiming monetary enforcement.

## Observability
- **Retrieval and compaction lack exported metrics.** `RecallResult` and
  `CompactionResult` make the data available in process, while Prometheus
  currently has no retrieval or compaction-savings series. Upgrade: bounded
  counters/histograms for attempts, hits, injected records, latency,
  compactions and saved tokens.
- **No HTTP request/error SLI is emitted by Atlas.** Availability and status-code
  objectives require ingress metrics or an external probe. The shipped metrics
  cover graph/specialist execution, auth, limits, tokens, scripted cost and
  cache use.

## Supply chain
- **Dependency SBOM, not a complete runtime inventory.** CI produces a
  CycloneDX Python dependency SBOM. It should be extended with base-image and
  OS package inventory, image-attached provenance/signing and scanning of the
  released runtime artifact.

## Product
- Fixture-scoped repository resolution: the API analyses repositories under
  `fixtures/`, not arbitrary git URLs. Cloning untrusted repositories is a
  meaningful attack surface and would need the sandbox above.
- English-only prompts; no i18n.
- No UI. SSE streaming exists so one could be built.

## What I would do differently
1. Design richer `context_key` segmentation into procedural memory from day
   one. Repository size is now represented, but adding language, framework or
   tenant dimensions means re-learning statistics for the new buckets.
2. Put the eval gate in CI *before* writing the second pattern; it changed
   how I wrote every pattern after it.
3. Make the scripted-model route matcher recency-aware from the start
   (ADR-0004) rather than discovering it through a silent recall drop.
