# ADR-0008: The executive verdict is derived, not generated

Status: accepted

## Context
The obvious final step in a multi-agent report is "ask the model to
summarise everything". It reads well and it is the single most dangerous
step in the pipeline.

## Decision
`synthesise` computes the verdict from structured findings: blocking issues
are those at high or critical severity, counts come from the data, and
failed specialists are named explicitly. No model call by default.

## Reasoning
A generated summary can invent a severity nobody reported, drop a critical
finding for narrative flow, or soften a blocking issue into a
recommendation. Since the verdict is the only part a decision-maker reads,
it is the last place hallucination is acceptable. Deriving it makes the
report's headline as trustworthy as its data.

## Alternatives considered
- **LLM narrative as the verdict.** Better prose, unverifiable content.
- **LLM narrative layered on top of the derived verdict.** Reasonable, and
  the documented enhancement path: the derived facts stay authoritative and
  the prose is decoration.

## Consequences
The verdict reads mechanically. That is the correct trade for a document
someone signs a cheque against.
