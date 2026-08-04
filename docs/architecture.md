# Architecture

## Invariants

1. **Every finding carries evidence.** Enforced by the `Finding` type. An
   unfalsifiable claim cannot be scored, and anything that cannot be scored
   cannot be improved.
2. **Specialists are context-isolated.** A specialist's transcript never
   leaves its graph node. Only findings and a summary cross the boundary.
3. **Tools are confined, bounded and deterministic.** Path confinement,
   output caps, and identical output for identical inputs — the last is a
   precondition for reproducible evals.
4. **Nothing is enforced by prompt alone.** Tool allowlists, spend caps,
   step budgets and RBAC are code paths. Prompts add defence in depth.
5. **Memory is written from structured results only.** Never from prose.
6. **The verdict is derived, not generated.** Synthesis computes from
   findings, so it cannot invent a severity nobody reported.
7. **The eval model is deterministic.** Behaviour is asserted exactly, and
   any variance in results means the *system* changed.

## Layers

### domain/
Provider- and framework-neutral types. `Finding` requires `Evidence`;
`Severity` and `Confidence` are deliberately separate ("probably critical"
and "definitely minor" are different triage decisions). `DueDiligenceReport`
knows how to sort and to identify blocking issues, so presentation logic
does not re-derive business rules.

### llm/
`ScriptedChatModel` is a real `BaseChatModel` whose responses are scripted
per **route**. Routes support composite keys (`"security specialist && apply
your critique"`) and rank by specificity then **recency of the match in the
conversation** — without the recency rule, a reflexion run replays its
critique instead of its revision, which silently halves recall. Usage
metadata is populated so cost and budget paths are exercised in tests.

### context/
Budgets separate *what we send in one call* from *how much work a run may
do*. Compaction implements a five-rule policy (never drop the system
prompt; never drop the recent window; never orphan tool results; summarise
the middle; truncate monsters) and returns a `CompactionResult` so context
loss is observable rather than silent.

### memory/
Four tiers behind one `MemoryHub` facade, so the A/B harness can disable
memory with a flag and injection is rendered identically everywhere.
Embeddings are dependency-free feature hashing — chosen for determinism and
offline CI, with the swap point documented.

### tools/ and mcp_layer/
One set of tool definitions, two transports. The MCP client treats remote
descriptions as untrusted input: allowlist, sanitise, namespace, cap.

### a2a/
Agent cards make the supervisor route by capability tag rather than a
hard-coded roster. That indirection is what allows a specialist to move out
of process or be replaced by a third-party agent later.

### patterns/
Four strategies behind one contract, each returning identical measurements.
Shared machinery (`call_model`, `run_tool_calls`) guarantees every pattern
gets compaction and metering, so the comparison is fair.

### graph/
`StateGraph` with fan-out from `plan` to all specialists and fan-in to
`synthesise`. Reducers in `state.py` define merge semantics for concurrent
writes. Specialist failures are captured, not propagated.

### evals/
Ground-truth scoring, judges with bias controls, and a tiered runner whose
thresholds live in code next to the runner — a quality bar that isn't
executable isn't a bar.

### security/ and api/
Layered controls, outermost first: request context → body limit → security
headers → CORS → authentication → RBAC → rate and spend limits → audit.

## Request flow

```
POST /v1/analyses
  → request id assigned, body size enforced (pure ASGI)
  → API key hashed, compared constant-time → Principal(roles)
  → RBAC: run:create required
  → rate limit (token bucket) + pre-flight spend budget
  → audit: run:create
  → graph.invoke
       plan       : recall memory, pick strategy, select specialists by capability
       fan-out    : 4 specialists run concurrently, isolated contexts
                     each: pattern loop → compaction → tools → parse → validate
       fan-in     : reducers merge findings/summaries/costs/errors
       synthesise : verdict derived from structured findings
  → record actual spend, audit: run:complete
  → response: findings with evidence, counts, cost, request id
```

## Scaling notes

Single-process today. The pieces that would need to change for a fleet, and
what would not:

* **Would change:** the rate limiter and memory are in-process; both need a
  shared store (Redis, Postgres/pgvector). The graph checkpointer would move
  from `InMemorySaver` to a durable checkpointer.
* **Would not change:** domain types, patterns, evals, tool contracts, or the
  security model. Those are the parts that took the thinking.

For durable long-running execution (crash recovery, human-in-the-loop pauses
measured in days), the right move is to run the graph on a durable runtime
rather than reinvent one — see `docs/LIMITATIONS.md`.
