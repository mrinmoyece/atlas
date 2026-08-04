"""Scoring agent output against planted ground truth.

This is the module that turns "the agent seems good" into numbers.

Matching rule: a reported finding matches a ground-truth item when the
**rule** and the **file path** agree, and the reported severity is at least
the required minimum. Path matching is suffix-based because agents report
paths relative to different roots; rule matching is exact after
normalisation, because fuzzy rule matching would let a vague agent score
well by accident.

Metrics reported:
    precision  of what the agent claimed, how much was real
    recall     of what was there, how much it found
    f1         harmonic mean - the single number for the benchmark
    traps      how many "should_not_find" decoys it fell for

Traps are scored separately and deliberately: an agent that reports every
possible issue gets perfect recall, and the trap count is what exposes it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from atlas.domain.types import DueDiligenceReport, Finding, Severity

# One trap penalty, used by both per-repo `quality` and the aggregate, so
# the same metric name cannot mean two different things depending on which
# code path rendered it.
TRAP_PENALTY = 0.1


class GroundTruthItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    path: str
    category: str = ""
    min_severity: str = "info"
    note: str = ""
    # How many distinct instances of this rule exist at this path.
    #
    # Without cardinality the scorer punished correctness: `src/db.py` has
    # TWO genuine SQL injection sites, the agent reported both, and the
    # second was counted as a false positive because one ground-truth item
    # had already been consumed. Worse, it *rewarded* suppression - the
    # pattern that dropped one real finding scored higher precision. A
    # scoring rule that pays an agent to hide a vulnerability is worse than
    # no scoring rule.
    max_instances: int = 1

    def matched_by(self, finding: Finding) -> bool:
        if _normalise_rule(finding.rule) != _normalise_rule(self.rule):
            return False
        if finding.severity.rank < _min_rank(self.min_severity):
            return False
        return any(_paths_match(e.path, self.path) for e in finding.evidence)


class RepoGroundTruth(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    description: str = ""
    must_find: tuple[GroundTruthItem, ...] = ()
    should_not_find: tuple[GroundTruthItem, ...] = ()


class ScoreCard(BaseModel):
    """Scored result for one repository."""

    model_config = ConfigDict(frozen=True)

    repo: str
    true_positives: int
    false_positives: int
    false_negatives: int
    traps_triggered: int
    missed: tuple[str, ...] = ()
    spurious: tuple[str, ...] = ()

    @property
    def is_control(self) -> bool:
        """True when there was nothing to find.

        On a control repository precision and recall are undefined - there
        is no denominator - and `f1` uses the clean-repo definition instead.
        Renderers need to know that so they do not print three numbers where
        the third cannot be derived from the first two.
        """
        return (self.true_positives + self.false_negatives) == 0

    @property
    def precision(self) -> float:
        claimed = self.true_positives + self.false_positives
        return self.true_positives / claimed if claimed else 1.0

    @property
    def recall(self) -> float:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else 1.0

    @property
    def has_positives(self) -> bool:
        """Whether ground truth contained anything to find at all."""
        return (self.true_positives + self.false_negatives) > 0

    @property
    def f1(self) -> float:
        """F1, with the clean-repo case defined explicitly.

        A repository with nothing planted is a *control*: the correct
        behaviour is to report nothing, and precision/recall are undefined
        (0/0). Naive F1 returns 0.0 there, which punishes a perfect run and
        drags any macro-average toward nonsense. So: no positives available
        and no false positives raised scores 1.0, degrading with each false
        positive. This is why `aggregate()` micro-averages by default.
        """
        if not self.has_positives:
            return max(0.0, 1.0 - 0.25 * self.false_positives)
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def quality(self) -> float:
        """F1 with a trap penalty.

        A trap is worse than an ordinary false positive: it means the agent
        flagged code that a careful reviewer would recognise as correct, so
        it is penalised explicitly rather than being averaged away.
        """
        return max(0.0, round(self.f1 - TRAP_PENALTY * self.traps_triggered, 6))

    def render(self) -> str:
        return (
            f"{self.repo}: P={self.precision:.2f} R={self.recall:.2f} "
            f"F1={self.f1:.2f} traps={self.traps_triggered} quality={self.quality:.2f}"
        )


def load_ground_truth(path: str | Path) -> dict[str, RepoGroundTruth]:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
    out: dict[str, RepoGroundTruth] = {}
    for repo, body in raw.items():
        out[repo] = RepoGroundTruth(
            repo=repo,
            description=body.get("description", ""),
            must_find=tuple(GroundTruthItem(**item) for item in body.get("must_find") or []),
            should_not_find=tuple(
                GroundTruthItem(**item) for item in body.get("should_not_find") or []
            ),
        )
    return out


def score_report(report: DueDiligenceReport, truth: RepoGroundTruth) -> ScoreCard:
    findings: Sequence[Finding] = report.findings
    unmatched = list(findings)
    true_positives = 0
    missed: list[str] = []

    for item in truth.must_find:
        # Consume up to `max_instances` matches: additional genuine instances
        # of the same rule at the same location are true positives, not
        # noise. Anything beyond the declared count is still a false
        # positive, so an agent cannot farm precision by repeating itself.
        hits = [f for f in unmatched if item.matched_by(f)][: item.max_instances]
        if hits:
            true_positives += 1  # the item itself is found
            for hit in hits:
                unmatched.remove(hit)
        else:
            missed.append(f"{item.rule}@{item.path}")

    traps = 0
    spurious: list[str] = []
    for finding in unmatched:
        is_trap = any(
            _normalise_rule(finding.rule) == _normalise_rule(t.rule)
            and any(_paths_match(e.path, t.path) for e in finding.evidence)
            for t in truth.should_not_find
        )
        if is_trap:
            traps += 1
        spurious.append(f"{finding.rule or finding.title}@{_first_path(finding)}")

    return ScoreCard(
        repo=truth.repo,
        true_positives=true_positives,
        false_positives=len(unmatched),
        false_negatives=len(missed),
        traps_triggered=traps,
        missed=tuple(missed),
        spurious=tuple(spurious),
    )


def aggregate(cards: Sequence[ScoreCard]) -> dict[str, float]:
    """Micro-averaged metrics (pooled counts), plus macro for comparison.

    Micro-averaging is the right default here because the fixture set is
    deliberately imbalanced: one repository carries eleven planted issues
    and the control carries none. Macro-averaging would give the control
    equal weight to the entire dirty repo and let a single false positive
    halve the headline number. Both are reported so the difference is
    visible rather than hidden in a helper.
    """
    if not cards:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "quality": 0.0, "traps": 0}

    tp = sum(c.true_positives for c in cards)
    fp = sum(c.false_positives for c in cards)
    fn = sum(c.false_negatives for c in cards)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    traps = sum(c.traps_triggered for c in cards)

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "quality": round(max(0.0, f1 - TRAP_PENALTY * traps), 4),
        "traps": traps,
        "macro_f1": round(sum(c.f1 for c in cards) / len(cards), 4),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
    }


def _normalise_rule(rule: str) -> str:
    return (rule or "").strip().lower().replace("-", "_").replace(" ", "_")


def _paths_match(reported: str, expected: str) -> bool:
    """Match a reported path against a ground-truth path.

    Three bugs this function previously had, all worth knowing about:

    1. `str.lstrip("./")` strips a *character set*, not a prefix, so the
       repo-root path "." became "" and was rejected as empty - making
       root-level ground truth (like "no tests anywhere") unmatchable, and
       scoring it as a miss AND a false positive simultaneously.
       `removeprefix` is the correct operation.
    2. Matching was bidirectional (`expected.endswith(reported)`), so a
       finding citing a bare "a.py" matched ground truth at
       "src/nested/a.py" that it never looked at. Suffix matching must be
       one-directional: the reported path may be more qualified than the
       expected one, never less.
    3. Directory-shaped ground truth did not match anything inside it. The
       clean fixture declares a `no_tests` trap at `tests/`, and an agent
       falsely reporting missing tests cites `tests/test_repository.py` -
       which matched neither `reported == "tests/"` nor
       `reported.endswith("/tests/")`. The trap was unfireable, so an agent
       that tripped BOTH `no_tests` and `no_ci` on the control repo scored
       one trap instead of two. With `max_traps: 1` as the gate and the
       current baseline sitting exactly at 1, that was load-bearing
       headroom the repository did not actually have.
    """
    reported = (reported or "").strip().removeprefix("./")
    expected = (expected or "").strip().removeprefix("./")
    # A trailing slash means "this directory and anything under it".
    if expected.endswith("/") and len(expected) > 1:
        prefix = expected.rstrip("/")
        if reported == prefix or reported.startswith(prefix + "/"):
            return True
        # Also allow the reported path to be more qualified on the left:
        # "src/tests/x.py" satisfies ground truth "tests/".
        return f"/{prefix}/" in f"/{reported}"
    if expected in (".", ""):
        # Ground truth pinned at the repository root (e.g. "no tests exist")
        # is satisfied by any location, including "." itself.
        return True
    if not reported:
        return False
    # Boundary-aware suffix match. A plain `endswith` would let "xa.py"
    # match ground truth "a.py", inventing a true positive out of a
    # coincidental substring.
    return reported == expected or reported.endswith("/" + expected)


def _first_path(finding: Finding) -> str:
    return finding.evidence[0].path if finding.evidence else "?"


def _min_rank(minimum: str) -> int:
    """Rank for a ground-truth minimum severity, derived from the domain
    enum rather than a duplicated table that could drift from it."""
    try:
        return Severity(minimum.strip().lower()).rank
    except ValueError:
        return 0
