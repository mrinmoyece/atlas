# ADR-0004: A deterministic scripted model is first-class infrastructure

Status: accepted

## Context
Agent behaviour cannot be regression-tested against a live model: the same
prompt yields different text, different tool ordering, and different cost.

## Decision
Implement `ScriptedChatModel`, a real `BaseChatModel` whose responses are
scripted per **route**, where routes match on markers in the conversation
and rank by (specificity, recency, length). It is the default provider, so
tests, benchmarks, evals and the demo all run offline with no API key.

## Reasoning
- Exact assertions become possible: "the security specialist called grep
  then read_file, produced seven findings, and reflexion's critique removed
  one".
- Cost and token accounting are exercised rather than bypassed, because the
  scripted model populates usage metadata.
- CI is free, fast and hermetic.

## The recency rule (learned the hard way)
Ranking routes by string length alone breaks reflexion: by the revision turn
both "criticise your draft" and "apply your critique" are present in
history, and the longer marker wins regardless of which instruction is
current. The model then replays its critique instead of revising, and recall
silently halves. Conversation order is the only correct disambiguator.

## Consequences
Scripts must be maintained alongside prompts — a prompt reword can
unintentionally change routing. This is a real maintenance cost, accepted in
exchange for deterministic evaluation. Live-model evaluation is one config
change and is explicitly *not* claimed by the offline numbers.
