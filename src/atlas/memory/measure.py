"""Does memory actually help? A/B harness.

Almost every agent project asserts that memory improves quality. Almost
none measures it. This module runs the same task set twice - once with a
cold, disabled hub and once with a warm hub trained on the preceding tasks
- and reports the delta in quality, cost and steps.

The result can be negative, and that is the point. Memory has real costs:
tokens spent on injected lessons, and the risk of anchoring the agent on a
pattern that does not apply to the current repo. A harness that can only
confirm the hypothesis is not a harness. `docs/MEMORY.md` records the
measured numbers, including where memory *hurt*.

Protocol: the caller injects the runner and the scorer, so this module
stays independent of both the graph and the eval package.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from atlas.domain.types import DueDiligenceReport
from atlas.memory.hub import MemoryHub

# (repo_name, hub) -> report
Runner = Callable[[str, MemoryHub], DueDiligenceReport]
# (repo_name, report) -> quality score in [0, 1]
Scorer = Callable[[str, DueDiligenceReport], float]


class ArmResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    mean_quality: float
    total_cost_usd: float
    total_findings: int
    per_repo: dict[str, float]


class ABResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    cold: ArmResult
    warm: ArmResult

    @property
    def quality_delta(self) -> float:
        return self.warm.mean_quality - self.cold.mean_quality

    @property
    def cost_delta(self) -> float:
        return self.warm.total_cost_usd - self.cold.total_cost_usd

    @property
    def verdict(self) -> str:
        if self.quality_delta > 0.02:
            return "memory helps"
        if self.quality_delta < -0.02:
            return "memory hurts"
        return "no significant effect"

    def render(self) -> str:
        lines = [
            "| arm | mean quality | total cost | findings |",
            "|---|---|---|---|",
            f"| cold (memory off) | {self.cold.mean_quality:.3f} | "
            f"${self.cold.total_cost_usd:.4f} | {self.cold.total_findings} |",
            f"| warm (memory on) | {self.warm.mean_quality:.3f} | "
            f"${self.warm.total_cost_usd:.4f} | {self.warm.total_findings} |",
            "",
            f"**delta: {self.quality_delta:+.3f} quality, "
            f"{self.cost_delta:+.4f} USD -> {self.verdict}**",
        ]
        return "\n".join(lines)


def run_ab(
    repos: Sequence[str],
    runner: Runner,
    scorer: Scorer,
    *,
    warmup_repos: Sequence[str] | None = None,
    context_key: str = "default",
    strategy: str = "react",
) -> ABResult:
    """Run `repos` cold and warm.

    Two independent leakage guards, because this experiment is easy to rig
    by accident:

    1. Any repository appearing in BOTH `warmup_repos` and `repos` is
       skipped during warmup - otherwise it would be analysed, memorised,
       and then scored on a hub that has already seen it.
    2. Within the scored loop, each repository is scored *before* its own
       result is memorised, so it can only benefit from repositories that
       preceded it.

    `warmup_repos` defaults to nothing at all: with a two-repo fixture set
    the honest experiment is repo-2-learns-from-repo-1, not "pre-train on
    everything".
    """
    cold_hub = MemoryHub(enabled=False)
    cold_scores: dict[str, float] = {}
    cold_cost = 0.0
    cold_findings = 0
    for repo in repos:
        report = runner(repo, cold_hub)
        cold_scores[repo] = scorer(repo, report)
        cold_cost += report.cost_usd
        cold_findings += len(report.findings)

    warm_hub = MemoryHub(enabled=True)
    # Leakage guard: a warmup repository that is ALSO scored would let that
    # repository benefit from having already analysed itself, which makes
    # the whole experiment meaningless. Excluding the overlap is not a
    # detail - it is the difference between a measurement and a press
    # release. Repos still learn from each other via the in-loop
    # learn-after-score below.
    scored = set(repos)
    for repo in warmup_repos or []:
        if repo in scored:
            continue
        report = runner(repo, warm_hub)
        warm_hub.learn_from_report(report, context_key=context_key, strategy=strategy, success=True)

    warm_scores: dict[str, float] = {}
    warm_cost = 0.0
    warm_findings = 0
    for repo in repos:
        report = runner(repo, warm_hub)
        warm_scores[repo] = scorer(repo, report)
        warm_cost += report.cost_usd
        warm_findings += len(report.findings)
        # learn AFTER scoring, so a repo never benefits from its own run
        warm_hub.learn_from_report(report, context_key=context_key, strategy=strategy, success=True)

    return ABResult(
        cold=ArmResult(
            label="cold",
            mean_quality=_mean(cold_scores.values()),
            total_cost_usd=round(cold_cost, 6),
            total_findings=cold_findings,
            per_repo=cold_scores,
        ),
        warm=ArmResult(
            label="warm",
            mean_quality=_mean(warm_scores.values()),
            total_cost_usd=round(warm_cost, 6),
            total_findings=warm_findings,
            per_repo=warm_scores,
        ),
    )


def _mean(values) -> float:
    values = list(values)
    return round(sum(values) / len(values), 6) if values else 0.0
