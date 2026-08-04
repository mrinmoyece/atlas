"""ReWOO: Reasoning WithOut Observation.

    plan every tool call up front  ->  execute them all  ->  solve once

Two model calls total, regardless of how many tools run. ReAct needs one
model call *per* tool result; ReWOO needs none in the middle, because the
planner commits to the whole evidence-gathering sequence before seeing any
of it.

The trade-off, stated honestly: ReWOO cannot follow a lead. If the first
grep reveals something unexpected, ReAct pivots and ReWOO does not. It wins
on cost and latency, and loses on tasks that need adaptation - which is
exactly the kind of claim the benchmark turns into a number instead of a
belief.

Implementation note: the plan is parsed defensively. A planner that emits
malformed steps degrades to "run whatever steps were valid" rather than
failing the run, because a partial evidence set still produces a usable
report.
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from atlas.agents.parsing import extract_json_object, parse_specialist_output
from atlas.agents.prompts import specialist_prompt
from atlas.patterns.base import (
    PatternContext,
    PatternResult,
    _Meter,
    call_model,
    system_message,
)

MAX_PLANNED_STEPS = 8

PLANNER_INSTRUCTION = (
    "Plan your entire evidence-gathering sequence BEFORE seeing any results. "
    'Reply with JSON: {"steps": [{"tool": "<tool name>", "args": {...}}]}. '
    f"Use at most {MAX_PLANNED_STEPS} steps. Choose broadly - you will not get "
    "a second chance to look."
)

SOLVER_INSTRUCTION = (
    "Above are the results of every tool call you planned. Produce your final JSON answer now."
)


class ReWOOPattern:
    name = "rewoo"

    def run(self, ctx: PatternContext) -> PatternResult:
        meter = _Meter()
        system = system_message(specialist_prompt(ctx.category, memory_block=ctx.memory_block))
        messages: list[BaseMessage] = [system, HumanMessage(content=PLANNER_INSTRUCTION)]

        try:
            plan_response, _ = call_model(ctx.model, messages, ctx, meter)
        except Exception as e:  # noqa: BLE001
            return self._failed(ctx, meter, f"planner failed: {e}")

        steps = self._parse_plan(plan_response)

        # Execute the whole plan with no model in the loop - the defining
        # property of ReWOO.
        observations: list[BaseMessage] = []
        for i, step in enumerate(steps):
            meter.tool_calls += 1
            content = ctx.toolkit.call(step["tool"], step["args"])
            observations.append(
                ToolMessage(content=f"[{step['tool']}] {content}", tool_call_id=f"rewoo-{i}")
            )

        solver_messages: list[BaseMessage] = [
            system,
            HumanMessage(
                content="Evidence gathered from your plan:\n\n"
                + "\n\n".join(str(o.content) for o in observations)[:20000]
            ),
            HumanMessage(content=SOLVER_INSTRUCTION),
        ]
        try:
            solve_response, _ = call_model(ctx.model, solver_messages, ctx, meter)
        except Exception as e:  # noqa: BLE001
            return self._failed(ctx, meter, f"solver failed: {e}")

        outcome = parse_specialist_output(
            str(solve_response.content), ctx.category, source_tool="rewoo"
        )
        return PatternResult(
            pattern=self.name,
            category=ctx.category,
            findings=outcome.findings,
            summary=outcome.summary,
            model_calls=meter.model_calls,
            tool_calls=meter.tool_calls,
            tokens_used=meter.tokens,
            cost_usd=round(meter.cost, 8),
            steps=len(steps) + 2,
            compactions=meter.compactions,
            dropped_findings=outcome.dropped,
            duration_ms=meter.elapsed_ms,
        )

    def _parse_plan(self, response) -> list[dict]:
        """Extract steps from either JSON content or emitted tool calls."""
        steps: list[dict] = []
        payload = extract_json_object(str(response.content))
        if payload:
            for item in (payload.get("steps") or [])[:MAX_PLANNED_STEPS]:
                if not isinstance(item, dict):
                    continue
                tool = str(item.get("tool", "")).strip()
                args = item.get("args") or {}
                if tool and isinstance(args, dict):
                    steps.append({"tool": tool, "args": args})
        # Some models express the plan as tool calls instead of JSON prose;
        # accept both rather than failing on formatting.
        for call in response.tool_calls or []:
            if len(steps) >= MAX_PLANNED_STEPS:
                break
            steps.append({"tool": call.get("name", ""), "args": call.get("args") or {}})
        return steps

    def _failed(self, ctx: PatternContext, meter: _Meter, error: str) -> PatternResult:
        return PatternResult(
            pattern=self.name,
            category=ctx.category,
            model_calls=meter.model_calls,
            tool_calls=meter.tool_calls,
            tokens_used=meter.tokens,
            cost_usd=round(meter.cost, 8),
            duration_ms=meter.elapsed_ms,
            error=error,
        )
