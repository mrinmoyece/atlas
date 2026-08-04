"""The four reasoning patterns, selectable by name.

    react         adaptive, 1 model call per step, best at following leads
    plan_execute  planned breadth, moderate cost, best coverage
    reflexion     react + self-critique, highest precision, highest cost
    rewoo         2 model calls total, cheapest, cannot adapt mid-run

`benchmarks/run_benchmark.py` measures all four on the same golden set;
results live in benchmarks/RESULTS.md.
"""

from atlas.patterns.base import Pattern, PatternContext, PatternResult
from atlas.patterns.plan_execute import PlanExecutePattern
from atlas.patterns.react import ReActPattern
from atlas.patterns.reflexion import ReflexionPattern
from atlas.patterns.rewoo import ReWOOPattern

PATTERNS: dict[str, Pattern] = {
    ReActPattern.name: ReActPattern(),
    PlanExecutePattern.name: PlanExecutePattern(),
    ReflexionPattern.name: ReflexionPattern(),
    ReWOOPattern.name: ReWOOPattern(),
}


def get_pattern(name: str) -> Pattern:
    try:
        return PATTERNS[name]
    except KeyError:
        raise ValueError(f"unknown pattern {name!r}; available: {sorted(PATTERNS)}") from None


__all__ = [
    "Pattern",
    "PatternContext",
    "PatternResult",
    "PATTERNS",
    "get_pattern",
    "ReActPattern",
    "PlanExecutePattern",
    "ReflexionPattern",
    "ReWOOPattern",
]
