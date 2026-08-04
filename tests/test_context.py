"""Context engineering: budgets and compaction."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from atlas.context.budget import TokenBudget, estimate_tokens, messages_tokens
from atlas.context.compaction import (
    compact_messages,
    default_summariser,
    has_orphan_tool_results,
)


def _conversation(turns: int = 20) -> list:
    messages = [SystemMessage(content="you are a specialist")]
    for i in range(turns):
        messages.append(
            AIMessage(
                content=f"thinking about step {i} " + "x" * 300,
                tool_calls=[{"name": "grep", "args": {"p": i}, "id": f"t{i}"}],
            )
        )
        messages.append(ToolMessage(content="result " + "y" * 400, tool_call_id=f"t{i}"))
    return messages


def test_budget_accounts_for_tool_calls():
    plain = [HumanMessage(content="hello")]
    with_tools = [
        AIMessage(
            content="hello", tool_calls=[{"name": "grep", "args": {"p": "x" * 500}, "id": "1"}]
        )
    ]
    assert messages_tokens(with_tools) > messages_tokens(plain)


def test_compaction_fits_budget_and_keeps_system_prompt():
    budget = TokenBudget(limit=2000, reserve_for_output=200)
    result = compact_messages(_conversation(), budget, keep_recent=6)

    assert result.tokens_after <= budget.usable
    assert isinstance(result.messages[0], SystemMessage)
    assert result.summarised and result.dropped_messages > 0
    assert result.saved_tokens > 0


def test_compaction_never_orphans_tool_results():
    """The bug everyone hits: dropping an AIMessage but keeping its
    ToolMessage makes most providers reject the request outright."""
    budget = TokenBudget(limit=1500, reserve_for_output=200)
    for keep in (1, 2, 3, 5, 8):
        result = compact_messages(_conversation(30), budget, keep_recent=keep)
        assert not has_orphan_tool_results(result.messages), f"orphan with keep_recent={keep}"


def test_oversized_single_message_is_truncated_not_dropped():
    budget = TokenBudget(limit=2000, reserve_for_output=200)
    messages = [SystemMessage(content="sys"), HumanMessage(content="z" * 200_000)]
    result = compact_messages(messages, budget)
    assert result.truncated_messages >= 1
    assert result.tokens_after <= budget.usable
    # content survives in truncated form rather than vanishing
    assert any("truncated" in str(m.content) for m in result.messages)


def test_summary_preserves_which_tools_were_already_used():
    """The single most valuable thing to carry forward is what the agent
    already tried - otherwise it repeats the same tool call."""
    messages = [
        AIMessage(content="looking", tool_calls=[{"name": "grep", "args": {}, "id": "1"}]),
        ToolMessage(content="nothing", tool_call_id="1"),
        AIMessage(content="reading", tool_calls=[{"name": "read_file", "args": {}, "id": "2"}]),
    ]
    digest = default_summariser(messages)
    assert "grep" in digest and "read_file" in digest


def test_no_compaction_when_within_budget():
    budget = TokenBudget(limit=100_000)
    messages = [SystemMessage(content="sys"), HumanMessage(content="short")]
    result = compact_messages(messages, budget)
    assert result.dropped_messages == 0
    assert not result.summarised
    assert result.messages == tuple(messages)


def test_estimate_tokens_is_monotonic():
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)
