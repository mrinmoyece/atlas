"""Reflexion: act, then criticise your own work, then revise.

    ReAct pass  ->  self-critique  ->  revised answer

The critique step is the whole idea: the model is shown its own findings
and asked to attack them - which claims lack evidence, which are duplicates,
which severity ratings are inflated. Then it produces a corrected answer.

Why this usually raises *precision* and lowers *recall*: the critic removes
weakly-evidenced findings, so false positives fall, but genuine findings
with thin evidence get culled too. Whether that is a good trade depends on
who reads the report - a due-diligence memo that cries wolf is worse than
one that misses a medium issue, so precision is usually the right thing to
buy here. The benchmark shows the actual shape of that trade.

Cost: roughly ReAct + 2 model calls. Never free.
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

CRITIQUE_INSTRUCTION = (
    "Now criticise your own draft findings as a hostile reviewer would.\n"
    "For each: is the evidence concrete (real file and line)? Is the severity "
    "justified by exploitability, or inflated? Is it a duplicate of another "
    "finding? Would a sceptical engineer accept it?\n"
    "List the specific problems. Do not produce the corrected report yet."
)

REVISION_INSTRUCTION = (
    "Apply your critique. Produce the final JSON answer containing only "
    "findings that survive it, with corrected severities. Removing a weak "
    "finding is a success, not a loss."
)


class ReflexionPattern:
    name = "reflexion"

    def __init__(self, act_steps: int | None = None) -> None:
        # Cap the acting phase so the critique always fits in the budget;
        # a Reflexion run that spends its whole budget acting is just ReAct.
        self._act_steps = act_steps

    def run(self, ctx: PatternContext) -> PatternResult:
        meter = _Meter()
        act_budget = self._act_steps or max(1, ctx.max_steps - 2)

        messages: list[BaseMessage] = [
            system_message(specialist_prompt(ctx.category, memory_block=ctx.memory_block)),
            HumanMessage(
                content=(
                    "Review this repository for issues in your area. Gather "
                    "evidence with tools, then draft your JSON findings."
                )
            ),
        ]

        steps = 0
        draft_text = ""
        while steps < act_budget:
            steps += 1
            try:
                response, messages = call_model(ctx.model, messages, ctx, meter)
            except Exception as e:  # noqa: BLE001
                return self._failed(ctx, meter, steps, f"act phase failed: {e}")
            messages.append(response)
            if not response.tool_calls:
                draft_text = str(response.content)
                break
            messages.extend(run_tool_calls(response, ctx, meter))

        # --- critique ---
        messages.append(HumanMessage(content=CRITIQUE_INSTRUCTION))
        try:
            critique, messages = call_model(ctx.model, messages, ctx, meter)
        except Exception as e:  # noqa: BLE001
            return self._failed(ctx, meter, steps, f"critique failed: {e}")
        messages.append(critique)

        # --- revise ---
        messages.append(HumanMessage(content=REVISION_INSTRUCTION))
        try:
            revised, _ = call_model(ctx.model, messages, ctx, meter)
        except Exception as e:  # noqa: BLE001
            return self._failed(ctx, meter, steps, f"revision failed: {e}")

        final_text = str(revised.content) or draft_text
        outcome = parse_specialist_output(final_text, ctx.category, source_tool="reflexion")
        return PatternResult(
            pattern=self.name,
            category=ctx.category,
            findings=outcome.findings,
            summary=outcome.summary,
            model_calls=meter.model_calls,
            tool_calls=meter.tool_calls,
            tokens_used=meter.tokens,
            cost_usd=round(meter.cost, 8),
            steps=steps + 2,
            compactions=meter.compactions,
            dropped_findings=outcome.dropped,
            duration_ms=meter.elapsed_ms,
        )

    def _failed(self, ctx: PatternContext, meter: _Meter, steps: int, error: str) -> PatternResult:
        return PatternResult(
            pattern=self.name,
            category=ctx.category,
            model_calls=meter.model_calls,
            tool_calls=meter.tool_calls,
            tokens_used=meter.tokens,
            cost_usd=round(meter.cost, 8),
            steps=steps,
            duration_ms=meter.elapsed_ms,
            error=error,
        )
