# ADR-0003: Specialists are context-isolated; only findings cross the boundary

Status: accepted

## Context
The naive multi-agent design shares one message list. Every agent sees every
other agent's work, which sounds collaborative and is actually the reason
naive multi-agent systems cost more than a single agent while performing
worse.

## Decision
Each specialist builds and keeps its own message list inside its graph node.
It returns a `SpecialistResult` — findings plus a one-paragraph summary.
The supervisor never sees a transcript.

## Reasoning
1. **Cost.** Four shared transcripts means every later step pays for all
   four. Isolation keeps each context proportional to one agent's work.
2. **Quality.** Cross-contamination causes anchoring: one specialist's
   speculative finding becomes another's premise.
3. **Parallelism.** Isolated contexts have no write conflicts, so fan-out is
   genuinely concurrent rather than serialised behind a shared buffer.

## Alternatives considered
- **Shared scratchpad.** Enables emergent collaboration; in practice it
  produces expensive echo chambers and non-deterministic ordering effects.
- **Supervisor-mediated message passing.** More flexible, considerably more
  machinery; worth it when specialists must negotiate, which due-diligence
  specialists do not.

## Consequences
Specialists cannot build on each other's discoveries within a run. For this
domain that is acceptable (the four dimensions are close to independent);
for a domain where they must, the fix is a second graph pass rather than
abandoning isolation.
