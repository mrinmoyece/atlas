# ADR-0002: Evaluate against planted ground truth, not model judgement alone

Status: accepted

## Context
Agent quality is normally asserted, not measured. LLM-as-judge is the usual
answer, but a judge alone measures the judge's biases as much as the agent's
quality, and it cannot tell you whether the agent *missed* something.

## Decision
Ship fixture repositories with deliberately planted issues and an answer key
(`evals/golden/ground_truth.yaml`). Score precision, recall and F1 against
it. Add an LLM/rule judge on top for qualities ground truth cannot express
(prioritisation, actionability), with explicit bias controls.

## Reasoning
- Recall is impossible to measure without knowing the complete answer set.
- The clean control repo carries **traps** — code that pattern-matches as
  vulnerable but is correct — because an agent that reports everything
  achieves perfect recall and must be caught.
- The most damaging hallucination class (citing a file that does not exist)
  is detectable *deterministically*; no judge required.

## Alternatives considered
- **Judge-only.** Cheap to build, but blind to misses and vulnerable to
  self-preference and position bias.
- **Real-world repositories.** More realistic, but the answer key is unknown
  and would have to be hand-curated — and it would drift every time the
  upstream repo changed.

## Consequences
Fixtures must stay in sync with ground truth (there is a test for that).
Scores are about the fixture set, not the universe — stated in the README
rather than implied away.
