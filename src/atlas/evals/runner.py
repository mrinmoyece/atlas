"""The eval runner: three tiers, one entry point.

Tiered evaluation, mirroring what production teams converge on:

    smoke     seconds. Runs on every PR. Catches "the graph is broken".
    standard  the full golden set with ground-truth scoring + judge.
              This is the merge gate.
    extended  standard + the memory A/B experiment + hallucination rate.
              Nightly.

The gate thresholds live here, in code, next to the runner - not in a wiki.
A quality bar that isn't executable isn't a bar.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from atlas.config import Settings
from atlas.domain.types import DueDiligenceReport
from atlas.evals.judge import DeterministicJudge, Judge, JudgeScore, hallucination_rate
from atlas.evals.scenarios import fixture_repos, model_for
from atlas.evals.scoring import (
    RepoGroundTruth,
    ScoreCard,
    aggregate,
    load_ground_truth,
    score_report,
)
from atlas.graph.build import run_due_diligence
from atlas.memory.hub import MemoryHub


def _repo_root() -> Path:
    """Project root, resolvable from a source checkout, an installed package
    or a container. `parents[3]` alone breaks in the last two."""
    configured = os.environ.get("ATLAS_PROJECT_ROOT")
    if configured:
        return Path(configured).resolve()
    if (Path.cwd() / "fixtures" / "repos").is_dir():
        return Path.cwd().resolve()
    return Path(__file__).resolve().parents[3]


REPO_ROOT = _repo_root()
FIXTURES = REPO_ROOT / "fixtures" / "repos"
GROUND_TRUTH = REPO_ROOT / "evals" / "golden" / "ground_truth.yaml"

# Merge gates, set against the measured `react` baseline
# (F1 0.909, precision 0.909, recall 0.909, 1 trap) with roughly one
# finding's worth of headroom - enough to absorb a scenario tweak, tight
# enough that losing a real capability trips it.
#
# An earlier version used 0.60/0.55/2, which sounds prudent and gates almost
# nothing: precision could have fallen 27% relative and trap count doubled
# while still reporting PASS. A gate with that much slack is decoration.
GATES = {
    "min_f1": 0.80,
    "min_precision": 0.75,
    "min_recall": 0.80,
    "max_traps": 1,
    "max_hallucination_rate": 0.05,
    # Judge dimensions. Until this round the judge was computed, printed in
    # the results table, and gated on by nothing - a column, not a control.
    # Ground-truth scoring is blind to both of these:
    #
    #   prioritisation  catches severity inflation. A run that relabels every
    #                   finding "critical" scores identically on P/R/F1 while
    #                   being useless to a reader, because triage is the
    #                   entire point of a due-diligence report.
    #   actionability   catches findings with no usable remediation. Correct
    #                   and unactionable is still a bad report.
    #
    # Both are computed only over reports that actually produced findings:
    # severity distribution and remediation quality are undefined for an
    # empty report, and the correct empty report on the clean fixture would
    # otherwise be punished for being right.
    "min_judge_prioritisation": 4.0,
    "min_judge_actionability": 4.0,
}


class RepoEvalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    pattern: str
    scorecard: ScoreCard
    judge_overall: float
    judge: JudgeScore
    findings: int
    cost_usd: float
    duration_ms: int
    verdict: str


class EvalRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    tier: str
    pattern: str
    results: tuple[RepoEvalResult, ...]
    aggregate: dict[str, float]
    hallucination_rate: float
    duration_s: float
    passed: bool
    failures: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [
            f"# Eval run ({self.tier}, pattern={self.pattern})",
            "",
            "| repo | P | R | F1 | traps | findings | cost | judge |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in self.results:
            s = r.scorecard
            # On a control repo there is nothing to find, so precision and
            # recall are undefined and the quality column uses the
            # clean-repo definition (1 - 0.25 x false positives). Printing
            # "0.00 / 1.00 / 0.75" put three numbers in a row where the
            # third is not derivable from the first two - defensible in the
            # source, indefensible on a slide.
            control = s.is_control
            precision = "n/a" if control else f"{s.precision:.2f}"
            recall = "n/a" if control else f"{s.recall:.2f}"
            score = f"{s.f1:.2f}{'*' if control else ''}"
            lines.append(
                f"| {r.repo} | {precision} | {recall} | {score} | "
                f"{s.traps_triggered} | {r.findings} | ${r.cost_usd:.4f} | "
                f"{r.judge_overall:.2f} |"
            )
        if any(r.scorecard.is_control for r in self.results):
            lines.append("")
            lines.append(
                "\\* control repository: nothing to find, so precision and recall "
                "are undefined. The score column uses the clean-repo definition "
                "`1 - 0.25 x false_positives`."
            )
        agg = self.aggregate
        lines += [
            "",
            f"**aggregate**: F1={agg.get('f1', 0):.3f} "
            f"precision={agg.get('precision', 0):.3f} "
            f"recall={agg.get('recall', 0):.3f} "
            f"traps={int(agg.get('traps', 0))} "
            f"hallucination_rate={self.hallucination_rate:.3f}",
            "",
            f"**gate: {'PASS' if self.passed else 'FAIL'}**",
        ]
        if self.failures:
            lines += ["", "Gate failures:", *[f"- {f}" for f in self.failures]]
        return "\n".join(lines)


def run_repo(
    repo: str,
    *,
    pattern: str = "react",
    memory: MemoryHub | None = None,
    settings: Settings | None = None,
    learn: bool = False,
) -> DueDiligenceReport:
    """Run the full multi-agent graph on one fixture repo, deterministically.

    `learn` defaults to **False** here, unlike in production. Evaluation and
    the A/B harness control learning order themselves; if the run also wrote
    to memory internally, every record would be written twice - inflating
    the run index (which skews recency decay) and double-counting procedural
    attempts. Measured before this default was set: a two-repo warm arm
    produced 4 episodic records and 20 semantic ones instead of 2 and 10.
    """
    return run_due_diligence(
        repo=repo,
        repo_root=str(FIXTURES / repo),
        model=model_for(repo),
        settings=settings or Settings(provider="scripted"),
        memory=memory,
        pattern_name=pattern,
        thread_id=f"eval-{repo}-{pattern}",
        learn=learn,
    )


def evaluate(
    *,
    tier: str = "standard",
    pattern: str = "react",
    repos: Sequence[str] | None = None,
    judge: Judge | None = None,
) -> EvalRun:
    started = time.monotonic()
    truth = load_ground_truth(GROUND_TRUTH)
    selected = list(repos or fixture_repos())
    if tier == "smoke":
        selected = selected[:1]
    active_judge = judge or DeterministicJudge()

    results: list[RepoEvalResult] = []
    reports: list[DueDiligenceReport] = []
    for repo in selected:
        report = run_repo(repo, pattern=pattern)
        reports.append(report)
        card = score_report(report, truth.get(repo, RepoGroundTruth(repo=repo)))
        score = active_judge.score(report, FIXTURES / repo)
        results.append(
            RepoEvalResult(
                repo=repo,
                pattern=pattern,
                scorecard=card,
                judge_overall=score.overall,
                judge=score,
                findings=len(report.findings),
                cost_usd=report.cost_usd,
                duration_ms=report.duration_ms,
                verdict=report.verdict,
            )
        )

    agg = aggregate([r.scorecard for r in results])
    halluc = _mean_hallucination(reports, selected)

    failures: list[str] = []
    if tier != "smoke":
        if agg.get("f1", 0) < GATES["min_f1"]:
            failures.append(f"F1 {agg['f1']:.3f} < {GATES['min_f1']}")
        if agg.get("precision", 0) < GATES["min_precision"]:
            failures.append(f"precision {agg['precision']:.3f} < {GATES['min_precision']}")
        if agg.get("recall", 0) < GATES["min_recall"]:
            failures.append(f"recall {agg['recall']:.3f} < {GATES['min_recall']}")
        if agg.get("traps", 0) > GATES["max_traps"]:
            failures.append(f"traps {int(agg['traps'])} > {GATES['max_traps']}")
        if halluc > GATES["max_hallucination_rate"]:
            failures.append(f"hallucination rate {halluc:.3f} > {GATES['max_hallucination_rate']}")
        scored = [r for r in results if r.findings]
        for dimension in ("prioritisation", "actionability"):
            key = f"min_judge_{dimension}"
            for result in scored:
                value = getattr(result.judge, dimension)
                if value < GATES[key]:
                    failures.append(f"{result.repo}: judge {dimension} {value:.2f} < {GATES[key]}")

    return EvalRun(
        tier=tier,
        pattern=pattern,
        results=tuple(results),
        aggregate=agg,
        hallucination_rate=halluc,
        duration_s=round(time.monotonic() - started, 3),
        passed=not failures,
        failures=tuple(failures),
    )


def _mean_hallucination(reports: Sequence[DueDiligenceReport], repos: Sequence[str]) -> float:
    """Micro-averaged: pooled bad citations over pooled findings.

    Macro-averaging here would contradict the argument `aggregate()` makes
    for micro-averaging three functions away - a repo with one finding would
    weigh as much as a repo with eleven.
    """
    total = 0
    bad = 0.0
    for report, repo in zip(reports, repos, strict=False):
        n = len(report.findings)
        if not n:
            continue
        total += n
        bad += hallucination_rate([report], FIXTURES / repo) * n
    return round(bad / total, 4) if total else 0.0
