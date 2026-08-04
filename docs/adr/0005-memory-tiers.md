# ADR-0005: Four memory tiers, written only from structured results

Status: accepted

## Context
"The agent has memory" is a claim every project makes. Done badly, memory
makes agents worse: it amplifies hallucinations, poisons context with
irrelevant lessons, and locks onto lucky strategies.

## Decision
Four tiers — working (context), episodic (run records), semantic (distilled
lessons, vector-retrieved with decay), procedural (strategy success
statistics) — behind one `MemoryHub`. Writes derive **only** from structured
`Finding` objects that already carry evidence.

## Reasoning
- **Structured-only writes.** Memorising prose means one confident wrong
  claim is written down and retrieved as fact forever.
- **Relevance floor.** Without one, top-k retrieval on an irrelevant store
  still returns its "best" records. Returning nothing is correct.
- **Recency decay at read time.** Old lessons should lose, not disappear.
- **Laplace smoothing.** A 1/1 strategy must not outrank 47/50.
- **Measurement.** `run_ab` scores each repo *before* memorising it, so no
  repository benefits from having seen itself.

## Alternatives considered
- **Full-transcript memory.** Simple, and unboundedly expensive.
- **LLM-written lessons.** Richer, costs a model call per run, and
  reintroduces the prose-memorisation risk. Documented as an upgrade with
  eyes open.

## Consequences
Memory quality depends on finding quality — garbage findings produce garbage
lessons. Mitigated by requiring evidence before a finding is memorable at
all.
