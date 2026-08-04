"""A deterministic chat model - the foundation of every test and benchmark.

Why this exists
---------------
You cannot regression-test an agent against a live model: the same prompt
returns different text, tool calls arrive in different orders, and costs
vary run to run. So Atlas ships a real `BaseChatModel` implementation whose
output is fully scripted. With it, CI can assert *exact* multi-agent
behaviour - "the security specialist called read_file then grep, produced
two findings, and the supervisor merged them" - which is impossible with a
live model.

Routing, not a flat queue
-------------------------
Naive scripted models pop from a single list. That breaks the moment agents
run in parallel, because the pop order becomes a race. Instead each script
is keyed by a *route* (a marker string that appears in the agent's system
prompt, e.g. "security"), and each route keeps its own independent cursor
under a lock. Parallel fan-out is then deterministic regardless of thread
interleaving - which is exactly what a benchmark harness needs.

Usage metadata is populated too (token counts derived from message lengths),
so the cost-accounting and budget code paths are exercised by tests rather
than bypassed.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, ConfigDict, Field


def tool_call(name: str, args: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
    """Build a LangChain-shaped tool call dict."""
    return {
        "name": name,
        "args": args,
        "id": call_id or f"call_{name}_{abs(hash(str(args))) % 10000}",
    }


class ScriptedTurn(BaseModel):
    """One scripted model response."""

    model_config = ConfigDict(frozen=True)

    content: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    # Simulate provider flakiness: raise this many times before succeeding.
    fail_times: int = 0
    # Artificial latency in ms, used by the benchmark harness to model
    # patterns that trade more model calls for fewer tool calls.


class ScriptedError(RuntimeError):
    """Raised by the scripted model to exercise retry paths."""


DEFAULT_ROUTE = "__default__"


class ScriptedChatModel(BaseChatModel):
    """Deterministic `BaseChatModel` with per-route scripts.

    routes: marker string -> ordered turns. The marker is matched against
    the concatenated system+human message text; the longest matching marker
    wins, so "security_deep" beats "security" when both are present.
    """

    routes: dict[str, list[ScriptedTurn]] = Field(default_factory=dict)
    # cost per token, deliberately fake-but-stable so budget assertions hold
    input_cost_per_token: float = 3e-6
    output_cost_per_token: float = 15e-6
    strict_routes: bool = False

    # Mutable runtime state lives in one dict that we only ever *mutate*,
    # never rebind. Rebinding attributes on a pydantic model silently
    # routes through __pydantic_private__ and the updates get lost - a
    # subtle bug worth knowing about when subclassing BaseChatModel.
    _state: Any = None

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        object.__setattr__(
            self,
            "_state",
            {"cursors": {}, "failures": {}, "lock": threading.Lock(), "calls": 0},
        )

    # ------------------------------------------------------------------
    # BaseChatModel contract
    # ------------------------------------------------------------------

    @property
    def _llm_type(self) -> str:
        return "atlas-scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        # Tool schemas do not change scripted behaviour, but agents call
        # bind_tools() unconditionally, so it must exist and be chainable.
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        route = self._match_route(messages)
        turn = self._next_turn(route)

        input_tokens = max(1, sum(len(str(m.content)) for m in messages) // 4)
        output_tokens = max(1, len(turn.content) // 4 + 8 * len(turn.tool_calls))
        message = AIMessage(
            content=turn.content,
            tool_calls=list(turn.tool_calls),
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            response_metadata={
                "model_name": "atlas-scripted",
                "route": route,
                "cost_usd": round(
                    input_tokens * self.input_cost_per_token
                    + output_tokens * self.output_cost_per_token,
                    8,
                ),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _match_route(self, messages: list[BaseMessage]) -> str:
        """Longest-specificity match.

        A marker may be a composite: "security specialist && apply your
        critique" matches only when *every* part is present. That is what
        lets one model script distinct behaviour per (agent, phase) pair -
        e.g. the reflexion revision turn returns a different answer from the
        initial draft turn, for the same specialist. Without composites you
        cannot script four reasoning patterns against one model, and the
        benchmark degenerates into "same answer, different cost".
        """
        haystack = "\n".join(str(m.content) for m in messages).lower()
        best = ""
        best_weight: tuple[int, int, int] = (-1, -1, -1)
        for marker in self.routes:
            if marker == DEFAULT_ROUTE:
                continue
            parts = [p.strip().lower() for p in marker.split("&&") if p.strip()]
            if not parts or not all(part in haystack for part in parts):
                continue
            # Ranking, in priority order:
            #   1. more parts  -> more specific route
            #   2. LATER match -> more recent instruction
            #   3. longer text -> tie-break
            #
            # (2) is the subtle one and it is load-bearing. A reflexion run
            # accumulates every phase instruction in its history, so by the
            # revision turn both "criticise your draft" and "apply your
            # critique" are present. Ranking on string length alone picks
            # whichever happens to be longer - which silently replayed the
            # critique instead of the revision and quietly halved recall.
            # Conversation order is the only correct disambiguator.
            recency = max(haystack.rfind(part) for part in parts)
            weight = (len(parts), recency, sum(len(p) for p in parts))
            if weight > best_weight:
                best, best_weight = marker, weight
        if best:
            return best
        # Order matters here, and it did not used to. `strict_routes` was
        # checked *after* the default-route fallback, and every scenario
        # defines a default route - so the flag could never fire. It was
        # switched on, tests stayed green, and nothing was actually being
        # enforced. Strict has to mean "no fallback", or it means nothing.
        if self.strict_routes:
            raise ScriptedError(
                "no scripted route matched this prompt and strict_routes is on, "
                "so the default route was not used. A silent fallback here makes "
                "eval numbers measure the fallback instead of the scenario. "
                f"available={sorted(m for m in self.routes if m != DEFAULT_ROUTE)}"
            )
        # One branch, because there was only ever one behaviour: the
        # previous `if DEFAULT_ROUTE in self.routes: return DEFAULT_ROUTE`
        # followed by `return DEFAULT_ROUTE` made the condition read like a
        # decision and decide nothing. A model with no default route and
        # strict_routes off falls back to the (missing) default and
        # `_next_turn` raises with a clear message, which is the intended
        # behaviour and is now the only path.
        return DEFAULT_ROUTE

    def _next_turn(self, route: str) -> ScriptedTurn:
        state = self._state
        with state["lock"]:
            state["calls"] += 1
            script = self.routes.get(route, [])
            idx = state["cursors"].get(route, 0)
            if idx >= len(script):
                # Exhausted script: repeat the last *terminal* turn (one with
                # no tool calls) so that patterns needing extra model calls -
                # reflexion's critique/revise, plan-execute's synthesis - still
                # converge on the same answer instead of falling off a cliff.
                # Repeating a tool-calling turn would loop forever, so those
                # degrade to a plain terminal response instead.
                if script and not script[-1].tool_calls:
                    return script[-1]
                return ScriptedTurn(content="No further analysis.")
            turn = script[idx]
            key = (route, idx)
            failures = state["failures"].get(key, 0)
            if failures < turn.fail_times:
                state["failures"][key] = failures + 1
                raise ScriptedError(
                    f"scripted failure {failures + 1}/{turn.fail_times} on route {route!r}"
                )
            state["cursors"][route] = idx + 1
            return turn

    @property
    def call_count(self) -> int:
        return self._state["calls"]

    def reset(self) -> None:
        state = self._state
        with state["lock"]:
            state["cursors"].clear()
            state["failures"].clear()
            state["calls"] = 0
