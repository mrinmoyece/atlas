"""The pattern interface - four reasoning strategies behind one contract.

Every pattern receives the same inputs and returns the same measured
result, which is what makes `benchmarks/` an apples-to-apples comparison
rather than an anecdote. The measurements are the point:

    model_calls   the expensive thing (latency + tokens)
    tool_calls    the cheap thing (but each one enters the context window)
    tokens/cost   what finance asks about
    steps         how much the agent flailed

A pattern that finds one more finding at three times the cost is not
obviously better; the benchmark exists so that trade-off is a number
instead of an opinion.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from atlas.context.budget import TokenBudget
from atlas.context.compaction import compact_messages, has_orphan_tool_results
from atlas.domain.types import Category, Finding
from atlas.observability.tracing import span
from atlas.tools.repo import ToolSpec

logger = logging.getLogger(__name__)


class CompactionInvariantError(RuntimeError):
    """Compaction produced a message list no provider will accept."""


class PatternResult(BaseModel):
    """Everything one specialist run produced, plus its cost of production."""

    model_config = ConfigDict(frozen=True)

    pattern: str
    category: Category
    findings: tuple[Finding, ...] = ()
    summary: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    steps: int = 0
    compactions: int = 0
    dropped_findings: int = 0
    duration_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class RunBudgetExceeded(RuntimeError):
    """Raised when the run-level cost ceiling is hit mid-pattern."""


class PatternContext(BaseModel):
    """Inputs a pattern needs. Passed as one object so adding an input does
    not change four call signatures."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    model: BaseChatModel
    # Structurally typed on purpose: `RepoToolkit` or a `FederatedToolkit`
    # wrapping it plus remote MCP servers. Both expose `call(name, args)`
    # with the same "errors become observations" contract, and patterns use
    # nothing else. Naming the concrete class here would have made the MCP
    # client permanently unreachable from the agent path.
    toolkit: Any
    category: Category
    memory_block: str = ""
    max_steps: int = 12
    token_budget: TokenBudget = Field(default_factory=lambda: TokenBudget(limit=12_000))
    keep_recent: int = 6
    # Bound models, memoised per underlying model object. Binding is not
    # free - it rebuilds the schema list and constructs a RunnableBinding -
    # and `_call_model` runs up to `max_steps` times per specialist, so
    # rebinding per call did the same work a few dozen times per run for
    # an identical result. Keyed by object identity because a pattern may
    # legitimately use more than one model (reflexion's critic, say).
    _bound_models: dict[int, Any] = PrivateAttr(default_factory=dict)

    # Called immediately before every model call with the cost of this
    # specialist so far. Returning False halts the pattern.
    #
    # This has to be a callback, and the reason is the whole lesson: a
    # cost ceiling checked when a node *starts* is useless under parallel
    # fan-out, because all four specialists start at the same instant and
    # each sees zero spend. The only place a budget can actually brake is
    # immediately before the next thing that costs money.
    cost_guard: Callable[[float], bool] | None = None
    # Receives cumulative usage after every completed model call. The graph
    # uses this to retain spend when a later operation raises before a
    # PatternResult can be returned.
    usage_observer: Callable[[int, int, float], None] | None = None

    def bound(self, model: BaseChatModel) -> BaseChatModel:
        """`model` with this toolkit's schemas attached, bound once."""
        key = id(model)
        cached = self._bound_models.get(key)
        if cached is None:
            cached = bind_toolkit(model, self.toolkit)
            self._bound_models[key] = cached
        return cached


class Pattern(Protocol):
    name: str

    def run(self, ctx: PatternContext) -> PatternResult: ...


# --------------------------------------------------------------------------
# Shared machinery every pattern reuses
# --------------------------------------------------------------------------


class _Meter:
    """Accumulates usage across a pattern run."""

    def __init__(self) -> None:
        self.model_calls = 0
        self.tool_calls = 0
        self.tokens = 0
        self.cost = 0.0
        self.compactions = 0
        self.started = time.monotonic()

    def record_model(self, message: AIMessage) -> None:
        self.model_calls += 1
        usage = message.usage_metadata or {}
        self.tokens += int(usage.get("total_tokens", 0))
        self.cost += float(message.response_metadata.get("cost_usd", 0.0))

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


def call_model(
    model: BaseChatModel,
    messages: Sequence[BaseMessage],
    ctx: PatternContext,
    meter: _Meter,
) -> tuple[AIMessage, list[BaseMessage]]:
    """Compact if needed, call the model, meter the result.

    Compaction happens *here*, immediately before every call, rather than
    at the end of a loop iteration. Doing it anywhere else means the one
    call that finally blows the context window is the one that escapes the
    check.
    """
    if ctx.cost_guard is not None and not ctx.cost_guard(meter.cost):
        raise RunBudgetExceeded(
            f"run cost ceiling reached before a {ctx.category.value} model call "
            f"(this specialist has spent ${meter.cost:.4f})"
        )
    working = list(messages)
    if ctx.token_budget.exceeded_by(working):
        result = compact_messages(working, ctx.token_budget, keep_recent=ctx.keep_recent)
        working = list(result.messages)
        meter.compactions += 1
        # Check the invariant rather than trusting it: an orphaned tool
        # result is rejected by most providers, and finding out at the API
        # boundary is far worse than finding out here.
        #
        # An `assert` was wrong twice over: it disappears under `python -O`,
        # which is how plenty of production images run, and when it did fire
        # the pattern's `except Exception` reported it as "model call
        # failed: AssertionError" - an internal invariant breach disguised
        # as a provider problem. A named exception survives -O and says what
        # actually happened.
        if has_orphan_tool_results(working):
            raise CompactionInvariantError(
                "compaction orphaned a tool result: a ToolMessage lost its "
                "preceding AIMessage tool call, which most providers reject"
            )
    with span("llm.call", category=ctx.category.value, messages=len(working)):
        response = ctx.bound(model).invoke(working)
    meter.record_model(response)
    if ctx.usage_observer is not None:
        ctx.usage_observer(meter.model_calls, meter.tokens, meter.cost)
    return response, working


def _openai_tool(spec: ToolSpec) -> dict[str, Any]:
    """One tool schema in the shape every LangChain provider accepts."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters or {"type": "object", "properties": {}, "required": []},
        },
    }


def bind_toolkit(model: BaseChatModel, toolkit: Any) -> BaseChatModel:
    """Advertise the toolkit's schemas to the model before invoking it.

    This is the seam that made every other piece of tooling in this project
    real, and it was missing. `bind_tools()` existed on the scripted model
    with a comment claiming "agents call bind_tools() unconditionally" - and
    `grep -rn bind_tools src/` returned exactly that one definition and no
    call sites. Nothing ever told a model which tools existed.

    Invisible under the scripted model, because scripted tool calls are
    authored into the fixture and arrive whether or not any schema was sent.
    Against a real provider - the README's "one config change" - the model
    would receive no tools, `response.tool_calls` would always be empty,
    ReAct would exit after one step, and every specialist would return
    ungrounded prose that the parser drops. The confinement work, the
    compaction machinery and the ReWOO planner were all unreachable outside
    the scripted oracle.

    Degrades rather than fails: a model that does not implement `bind_tools`
    (or rejects the schemas) is still invoked unbound, because a specialist
    producing a weaker answer beats a specialist producing an exception.
    """
    specs = getattr(toolkit, "specs", None)
    if specs is None:
        return model
    try:
        tools = [_openai_tool(spec) for spec in specs()]
        if not tools:
            return model
        return model.bind_tools(tools)
    except (NotImplementedError, AttributeError, TypeError) as exc:
        logger.warning("model does not support tool binding, invoking unbound: %s", exc)
        return model


def run_tool_calls(response: AIMessage, ctx: PatternContext, meter: _Meter) -> list[ToolMessage]:
    """Execute every tool call on a response, returning tool messages.

    Errors are returned as tool content, never raised: a failed tool is an
    observation the agent should react to, not a crash. That single choice
    is why a bad regex from the model degrades into a retry instead of a
    500.
    """
    out: list[ToolMessage] = []
    for call in response.tool_calls or []:
        name = call.get("name", "")
        args = call.get("args", {}) or {}
        meter.tool_calls += 1
        with span("tool.call", tool=name, category=ctx.category.value):
            content = ctx.toolkit.call(name, args)
        out.append(ToolMessage(content=content, tool_call_id=call.get("id", name)))
    return out


def system_message(prompt: str) -> SystemMessage:
    """Build the system message for a pattern run.

    Takes only the prompt: an earlier version accepted the whole
    `PatternContext` and ignored it, which is the kind of signature that
    invites a caller to assume context is being applied when it isn't.
    """
    return SystemMessage(content=prompt)
