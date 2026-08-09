"""Context compaction - deciding what to forget.

"Orchestration is the easy part; the hard part is deciding what to
remember, what gets dropped, and how you stop old context from polluting
new answers." Compaction is that decision, made explicit.

Policy implemented here (in priority order):

  1. **Never drop the system prompt.** It carries the agent's contract.
  2. **Never drop the last N turns.** Recency dominates relevance for the
     immediate next step; dropping it makes the agent incoherent.
  3. **Never orphan a tool result.** A `ToolMessage` whose originating
     `AIMessage` tool call was dropped will make most providers 400. This
     is the bug everyone hits when they write naive truncation, so the
     pairing is enforced structurally here and covered by a test.
  4. **Summarise the middle, don't delete it.** Dropped turns are replaced
     by one compact digest message, so long-range facts survive as text
     even when the raw turns are gone.
  5. **Truncate oversized single messages** (a 200KB file dump) rather
     than letting one tool result evict the entire history.

The summariser is injectable: the default is deterministic and extractive
(no model call) so tests and CI stay fast and free; production can pass an
LLM-backed summariser with the same signature.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, ConfigDict

from atlas.context.budget import TokenBudget, estimate_tokens, messages_tokens

Summariser = Callable[[Sequence[BaseMessage]], str]

_MAX_SINGLE_MESSAGE_TOKENS = 3000
_TRUNCATION_NOTE = "\n...[truncated by context compaction]..."


class CompactionResult(BaseModel):
    """What compaction did - surfaced so it can be logged, traced and
    asserted on. Silent context loss is a debugging nightmare."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    messages: tuple[BaseMessage, ...]
    tokens_before: int
    tokens_after: int
    dropped_messages: int
    truncated_messages: int
    summarised: bool

    @property
    def saved_tokens(self) -> int:
        return self.tokens_before - self.tokens_after


def default_summariser(messages: Sequence[BaseMessage]) -> str:
    """Extractive, deterministic digest of dropped turns.

    Keeps tool names and the first line of each message - enough for the
    agent to remember *what it already tried*, which is the single most
    valuable thing to preserve (it prevents repeating the same tool call).
    """
    tools_used: list[str] = []
    lines: list[str] = []
    for m in messages:
        for tc in getattr(m, "tool_calls", []) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name and name not in tools_used:
                tools_used.append(name)
        text = str(m.content).strip().splitlines()
        if text:
            lines.append(f"- {m.__class__.__name__}: {text[0][:160]}")
    digest = "\n".join(lines[:20])
    tools = ", ".join(tools_used) if tools_used else "none"
    return f"[earlier context compacted]\ntools already used: {tools}\n{digest}"


def _truncate(message: BaseMessage, max_tokens: int) -> tuple[BaseMessage, bool]:
    content = str(message.content)
    if estimate_tokens(content) <= max_tokens:
        return message, False
    note_tokens = estimate_tokens(_TRUNCATION_NOTE)
    if max_tokens <= note_tokens:
        new_content = _TRUNCATION_NOTE[: max_tokens * 4]
        return message.model_copy(update={"content": new_content}), True
    keep_chars = max(0, max_tokens - note_tokens) * 4
    head_chars = int(keep_chars * 0.7)
    tail_chars = int(keep_chars * 0.3)
    head = content[:head_chars]
    tail = content[-tail_chars:] if tail_chars else ""
    new_content = f"{head}{_TRUNCATION_NOTE}{tail}"
    return message.model_copy(update={"content": new_content}), True


def compact_messages(
    messages: Sequence[BaseMessage],
    budget: TokenBudget,
    *,
    keep_recent: int = 6,
    summariser: Summariser | None = None,
) -> CompactionResult:
    """Fit `messages` into `budget`, losing as little meaning as possible."""
    summarise = summariser or default_summariser
    tokens_before = messages_tokens(messages)

    # Step 1: cap individual monsters before considering any dropping.
    working: list[BaseMessage] = []
    truncated = 0
    for m in messages:
        new_m, was_truncated = _truncate(m, _MAX_SINGLE_MESSAGE_TOKENS)
        truncated += int(was_truncated)
        working.append(new_m)

    if messages_tokens(working) <= budget.usable:
        return CompactionResult(
            messages=tuple(working),
            tokens_before=tokens_before,
            tokens_after=messages_tokens(working),
            dropped_messages=0,
            truncated_messages=truncated,
            summarised=False,
        )

    system = [m for m in working if isinstance(m, SystemMessage)]
    rest = [m for m in working if not isinstance(m, SystemMessage)]

    # Step 2: choose the recent window, then expand it *backwards* so we
    # never start on an orphaned ToolMessage.
    split = max(0, len(rest) - keep_recent)
    split = _pull_back_to_safe_boundary(rest, split)
    older, recent = rest[:split], rest[split:]

    if not older:
        # Nothing droppable: shrink harder rather than exceed the budget.
        squeezed = [_truncate(m, _MAX_SINGLE_MESSAGE_TOKENS // 3)[0] for m in recent]
        result_messages = [*system, *squeezed]
        return CompactionResult(
            messages=tuple(result_messages),
            tokens_before=tokens_before,
            tokens_after=messages_tokens(result_messages),
            dropped_messages=0,
            truncated_messages=truncated + len(squeezed),
            summarised=False,
        )

    digest = HumanMessage(content=summarise(older))
    result_messages: list[BaseMessage] = [*system, digest, *recent]

    # Step 3: if still over, drop from the *oldest* end of the recent
    # window, always at a safe boundary, until it fits.
    while messages_tokens(result_messages) > budget.usable and len(recent) > 1:
        cut = _pull_forward_to_safe_boundary(recent, 1)
        recent = recent[cut:]
        result_messages = [*system, digest, *recent]

    return CompactionResult(
        messages=tuple(result_messages),
        tokens_before=tokens_before,
        tokens_after=messages_tokens(result_messages),
        dropped_messages=len(older) + (len(rest) - len(older) - len(recent)),
        truncated_messages=truncated,
        summarised=True,
    )


def _pull_back_to_safe_boundary(messages: Sequence[BaseMessage], split: int) -> int:
    """Move `split` earlier until messages[split] is not a ToolMessage whose
    AIMessage would be left behind."""
    while 0 < split < len(messages) and isinstance(messages[split], ToolMessage):
        split -= 1
    return split


def _pull_forward_to_safe_boundary(messages: Sequence[BaseMessage], cut: int) -> int:
    """Move `cut` later so we never leave a ToolMessage at position 0."""
    while cut < len(messages) and isinstance(messages[cut], ToolMessage):
        cut += 1
    return cut


def has_orphan_tool_results(messages: Sequence[BaseMessage]) -> bool:
    """Validation helper used by tests and by the graph before every call:
    every ToolMessage must be preceded by an AIMessage requesting that id."""
    requested: set[str] = set()
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tid:
                    requested.add(tid)
        elif isinstance(m, ToolMessage):
            if m.tool_call_id not in requested:
                return True
    return False
