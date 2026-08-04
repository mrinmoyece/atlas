# Limitations

Honest inventory. Each item: what is limited, why it was acceptable, and the
upgrade path.

## Evaluation
- **Two fixture repositories.** Enough to demonstrate scoring, traps and
  pattern differences; not enough for statistical confidence. Upgrade: more
  fixtures across languages, and a held-out set the scripts have never seen.
- **Deterministic model.** Results prove the harness works, not that a live
  model performs this way. Upgrade: `ATLAS_PROVIDER=anthropic`, N runs per
  scenario, pass-rate thresholds instead of exact assertions.
- **Rule-based judge by default.** Cannot assess prose quality. Upgrade: LLM
  judge behind the same `Judge` protocol, nightly tier only.
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
- **Single `context_key`.** Procedural memory does not yet segment by
  language or repo size, so strategy learning is coarse.

## Deployment
- **Single replica, and `k8s/deployment.yaml` says `replicas: 1`.** Four
  things live in process memory: the memory hub, the audit hash-chain, the
  rate-limit and spend ledger, and the LangGraph checkpointer. At two
  replicas each of them silently forks - two divergent audit chains, a spend
  cap that admits 2x its stated limit, and a "resumable" run that only
  resumes if the retry lands on the pod that started it. None of that fails
  loudly, which is what makes it worth stating here. The manifest shipped
  `replicas: 2` until a review caught it.
  Upgrade path, in dependency order: Postgres checkpointer -> shared store
  for memory -> Redis token buckets -> append-only audit sink.

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
  does not.

## Product
- Fixture-scoped repository resolution: the API analyses repositories under
  `fixtures/`, not arbitrary git URLs. Cloning untrusted repositories is a
  meaningful attack surface and would need the sandbox above.
- English-only prompts; no i18n.
- No UI. SSE streaming exists so one could be built.

## What I would do differently
1. Design `context_key` segmentation into procedural memory from day one —
   retrofitting it means re-learning statistics from scratch.
2. Put the eval gate in CI *before* writing the second pattern; it changed
   how I wrote every pattern after it.
3. Make the scripted-model route matcher recency-aware from the start
   (ADR-0004) rather than discovering it through a silent recall drop.
