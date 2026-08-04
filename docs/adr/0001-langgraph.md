# ADR-0001: Build on LangGraph rather than a hand-rolled loop

Status: accepted

## Context
Atlas needs parallel multi-agent orchestration with typed shared state,
merge semantics for concurrent writes, and checkpointing. All of that can be
hand-written (and in a sibling project of mine, is), but it is not free.

## Decision
Use LangGraph 1.x as the orchestration substrate. Own the layers *above* it
(patterns, evaluation, memory, security) and the layers *below* it (tools,
model adapters).

## Reasoning
1. **Reducers are the actual hard part of parallel agents**, and LangGraph
   gives them a first-class, testable form. Writing merge semantics by hand
   is where findings silently vanish.
2. **Checkpointing and streaming** come with it; both would otherwise be
   bespoke.
3. **It is what the market runs.** LangGraph 1.0 shipped in October 2025 and
   is in production at Klarna, Uber, LinkedIn and JPMorgan. Interviewers
   have opinions about it, which makes it useful to have opinions back.

## Alternatives considered
- **Framework-free.** Maximum control; I have built that separately and it
  is the right answer when the runtime *is* the product. Here the runtime is
  a substrate and re-implementing it would add no information.
- **OpenAI Agents SDK / CrewAI / AutoGen.** Lighter or more opinionated
  respectively; less enterprise adoption, and CrewAI/AutoGen hide the state
  model that this project exists to demonstrate.

## Consequences
A framework upgrade can break orchestration, so the graph layer is thin and
everything valuable (domain, patterns, evals, memory, security) is
framework-independent and separately tested. If LangGraph disappeared,
`graph/build.py` would be rewritten and nothing else would move.
