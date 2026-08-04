"""ReAct: interleaved reasoning and acting.

    think -> act -> observe -> think -> ... -> answer

The model decides the next tool call after seeing the previous result. That
adaptivity is ReAct's strength (it can follow a lead it did not anticipate)
and its cost (one model call per step, and the context grows with every
observation).

Failure mode this implementation guards against: **the tool loop**. An
agent that keeps calling `grep` with slightly different patterns will spin
until the budget dies. The step cap is enforced by the runtime, and the
loop also detects exact repeat calls and tells the model it is repeating -
cheaper and more informative than silently letting it burn the budget.
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

from atlas.agents.parsing import parse_specialist_output
from atlas.agents.prompts import specialist_prompt
from atlas.patterns.base import (
    PatternContext,
    PatternResult,
    _Meter,
    call_model,
    run_tool_calls,
    system_message,
)

REPEAT_WARNING = (
    "You already made this exact tool call and received the result above. "
    "Do something different or produce your final JSON answer now."
)


class ReActPattern:
    name = "react"

    def run(self, ctx: PatternContext) -> PatternResult:
        meter = _Meter()
        messages: list[BaseMessage] = [
            system_message(specialist_prompt(ctx.category, memory_block=ctx.memory_block)),
            HumanMessage(
                content=(
                    "Review this repository for issues in your area. Use the tools "
                    "to gather evidence, then produce your final JSON answer."
                )
            ),
        ]
        seen_calls: set[tuple[str, str]] = set()
        steps = 0
        final_text = ""

        while steps < ctx.max_steps:
            steps += 1
            try:
                response, messages = call_model(ctx.model, messages, ctx, meter)
            except Exception as e:  # noqa: BLE001 - provider surface
                return PatternResult(
                    pattern=self.name,
                    category=ctx.category,
                    steps=steps,
                    model_calls=meter.model_calls,
                    tool_calls=meter.tool_calls,
                    tokens_used=meter.tokens,
                    cost_usd=round(meter.cost, 8),
                    compactions=meter.compactions,
                    duration_ms=meter.elapsed_ms,
                    error=f"model call failed: {type(e).__name__}: {e}",
                )

            messages.append(response)

            if not response.tool_calls:
                final_text = str(response.content)
                break

            signature = {
                (c.get("name", ""), str(sorted((c.get("args") or {}).items())))
                for c in response.tool_calls
            }
            repeated = signature & seen_calls
            seen_calls |= signature

            messages.extend(run_tool_calls(response, ctx, meter))
            if repeated:
                messages.append(HumanMessage(content=REPEAT_WARNING))

        outcome = parse_specialist_output(final_text, ctx.category, source_tool="react")
        return PatternResult(
            pattern=self.name,
            category=ctx.category,
            findings=outcome.findings,
            summary=outcome.summary,
            model_calls=meter.model_calls,
            tool_calls=meter.tool_calls,
            tokens_used=meter.tokens,
            cost_usd=round(meter.cost, 8),
            steps=steps,
            compactions=meter.compactions,
            dropped_findings=outcome.dropped,
            duration_ms=meter.elapsed_ms,
            error="" if final_text else "step budget exhausted before a final answer",
        )
