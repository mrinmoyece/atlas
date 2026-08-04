"""Plan-and-Execute: decompose first, then work the plan.

    plan  ->  [execute step, observe] x N  ->  synthesise

The middle ground between ReAct and ReWOO. A plan is made up front (so the
agent has a spine and does not wander), but each step still runs its own
model turn (so it can adapt within the step). Costs more than ReWOO, less
than ReAct, and tends to produce better *coverage* because the plan forces
breadth the greedy ReAct loop often skips.

Guard implemented here: **plan drift**. Without a check, the executor
quietly stops following the plan and turns into ReAct with extra steps. The
executor tracks remaining steps explicitly and re-anchors the model to the
current step on every turn.
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

from atlas.agents.parsing import extract_json_object, parse_specialist_output
from atlas.agents.prompts import specialist_prompt
from atlas.patterns.base import (
    PatternContext,
    PatternResult,
    _Meter,
    call_model,
    run_tool_calls,
    system_message,
)

MAX_PLAN_STEPS = 5

PLANNER_INSTRUCTION = (
    "Before investigating, write a short plan. Reply with JSON: "
    '{"plan": ["step 1", "step 2", ...]} - at most '
    f"{MAX_PLAN_STEPS} steps, each a concrete investigation goal "
    "(not a tool call)."
)

SYNTHESIS_INSTRUCTION = (
    "You have worked through the plan. Produce your final JSON answer now, "
    "using only evidence you actually gathered."
)


class PlanExecutePattern:
    name = "plan_execute"

    def run(self, ctx: PatternContext) -> PatternResult:
        meter = _Meter()
        system = system_message(specialist_prompt(ctx.category, memory_block=ctx.memory_block))

        plan_messages: list[BaseMessage] = [system, HumanMessage(content=PLANNER_INSTRUCTION)]
        try:
            plan_response, _ = call_model(ctx.model, plan_messages, ctx, meter)
        except Exception as e:  # noqa: BLE001
            return self._failed(ctx, meter, f"planner failed: {e}")

        plan = self._parse_plan(plan_response)
        if not plan:
            plan = ["Investigate the highest-risk area for your speciality."]

        messages: list[BaseMessage] = [
            system,
            HumanMessage(
                content="Your plan:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan))
            ),
        ]

        steps_taken = 0
        budget_per_step = max(1, ctx.max_steps // max(1, len(plan)))

        for index, step in enumerate(plan, start=1):
            # Re-anchor on every step: this is the plan-drift guard.
            messages.append(
                HumanMessage(
                    content=f"Now execute step {index} of {len(plan)}: {step}\n"
                    "Call tools as needed. Reply without tool calls when this "
                    "step is done."
                )
            )
            for _ in range(budget_per_step):
                if steps_taken >= ctx.max_steps:
                    break
                steps_taken += 1
                try:
                    response, messages = call_model(ctx.model, messages, ctx, meter)
                except Exception as e:  # noqa: BLE001
                    return self._failed(ctx, meter, f"executor failed: {e}")
                messages.append(response)
                if not response.tool_calls:
                    break
                messages.extend(run_tool_calls(response, ctx, meter))

        messages.append(HumanMessage(content=SYNTHESIS_INSTRUCTION))
        try:
            final_response, _ = call_model(ctx.model, messages, ctx, meter)
        except Exception as e:  # noqa: BLE001
            return self._failed(ctx, meter, f"synthesis failed: {e}")

        outcome = parse_specialist_output(
            str(final_response.content), ctx.category, source_tool="plan_execute"
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
            steps=steps_taken + 2,
            compactions=meter.compactions,
            dropped_findings=outcome.dropped,
            duration_ms=meter.elapsed_ms,
        )

    def _parse_plan(self, response) -> list[str]:
        payload = extract_json_object(str(response.content))
        if not payload:
            return []
        steps = payload.get("plan") or []
        return [str(s).strip() for s in steps if str(s).strip()][:MAX_PLAN_STEPS]

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
