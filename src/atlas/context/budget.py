"""Token budgeting.

Two independent controls, often confused:

  * **Context budget** - how many tokens we're willing to *send* on one
    call. Exceeding it means compaction, not failure.
  * **Run budget** (steps / cost) - how much total work a run may do.
    Exceeding it means stopping. Lives in the graph layer.

Estimation is a heuristic (chars/4) rather than a tokenizer call: it is
provider-independent, dependency-free, and deterministic, which matters
more here than being exact. The moment you use a real tokenizer your
compaction tests become vendor-coupled. The trade-off is documented in
docs/LIMITATIONS.md.
"""

from __future__ import annotations

from collections.abc import Iterable

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def messages_tokens(messages: Iterable[BaseMessage]) -> int:
    total = 0
    for m in messages:
        total += estimate_tokens(str(m.content))
        # tool calls carry real payload weight; ignoring them under-counts
        # exactly the messages most likely to blow the window
        for tc in getattr(m, "tool_calls", []) or []:
            total += estimate_tokens(str(tc))
    return total


class TokenBudget(BaseModel):
    """A budget for one agent's context window."""

    model_config = ConfigDict(frozen=True)

    limit: int = Field(gt=0, description="Max tokens to send in one model call")
    reserve_for_output: int = Field(default=1024, ge=0)

    @property
    def usable(self) -> int:
        return max(256, self.limit - self.reserve_for_output)

    def exceeded_by(self, messages: Iterable[BaseMessage]) -> int:
        """Tokens over budget, or 0 if within it."""
        return max(0, messages_tokens(messages) - self.usable)
