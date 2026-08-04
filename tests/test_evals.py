"""The evaluation stack itself. If scoring is wrong, every other number lies."""

from __future__ import annotations

from atlas.domain.types import (
    Category,
    DueDiligenceReport,
    Evidence,
    Finding,
    Severity,
)
from atlas.evals.judge import DeterministicJudge, hallucination_rate
from atlas.evals.runner import FIXTURES, GATES, GROUND_TRUTH, evaluate
from atlas.evals.scoring import (
    GroundTruthItem,
    RepoGroundTruth,
    _paths_match,
    aggregate,
    load_ground_truth,
    score_report,
)


def _finding(rule: str, path: str, severity: Severity = Severity.HIGH, line: int = 1) -> Finding:
    return Finding(
        finding_id=rule + path,
        category=Category.SECURITY,
        severity=severity,
        title=rule,
        rule=rule,
        evidence=(Evidence(path=path, line=line),),
    )


def _truth(**kwargs) -> RepoGroundTruth:
    return RepoGroundTruth(repo="r", **kwargs)


# ------------------------------------------------------------------ scoring


def test_exact_match_scores_a_true_positive():
    truth = _truth(must_find=(GroundTruthItem(rule="sql_injection", path="src/db.py"),))
    card = score_report(
        DueDiligenceReport(repo="r", findings=(_finding("sql_injection", "src/db.py"),)), truth
    )
    assert (card.true_positives, card.false_positives, card.false_negatives) == (1, 0, 0)
    assert card.precision == 1.0 and card.recall == 1.0


def test_severity_below_minimum_does_not_match():
    """Reporting a critical issue as 'low' is not a find."""
    truth = _truth(
        must_find=(GroundTruthItem(rule="sql_injection", path="src/db.py", min_severity="high"),)
    )
    card = score_report(
        DueDiligenceReport(
            repo="r", findings=(_finding("sql_injection", "src/db.py", Severity.LOW),)
        ),
        truth,
    )
    assert card.true_positives == 0 and card.false_negatives == 1


def test_traps_are_counted_separately_from_false_positives():
    truth = _truth(should_not_find=(GroundTruthItem(rule="sql_injection", path="src/ok.py"),))
    card = score_report(
        DueDiligenceReport(repo="r", findings=(_finding("sql_injection", "src/ok.py"),)), truth
    )
    assert card.traps_triggered == 1 and card.false_positives == 1
    assert card.quality < card.f1  # trap penalty applied


def test_clean_repo_with_no_findings_scores_perfectly():
    """A control repo where nothing is planted: reporting nothing is right,
    and naive F1 would wrongly return 0.0 here."""
    card = score_report(DueDiligenceReport(repo="r"), _truth())
    assert not card.has_positives
    assert card.f1 == 1.0


def test_micro_average_is_not_skewed_by_the_control_repo():
    dirty = score_report(
        DueDiligenceReport(
            repo="dirty",
            findings=tuple(_finding(f"rule{i}", f"f{i}.py") for i in range(10)),
        ),
        RepoGroundTruth(
            repo="dirty",
            must_find=tuple(GroundTruthItem(rule=f"rule{i}", path=f"f{i}.py") for i in range(10)),
        ),
    )
    clean = score_report(
        DueDiligenceReport(repo="clean", findings=(_finding("noise", "x.py"),)),
        RepoGroundTruth(repo="clean"),
    )
    agg = aggregate([dirty, clean])
    # 10 TP, 1 FP, 0 FN pooled -> precision 0.909, not the 0.5 a macro
    # average over {1.0, 0.0} would produce.
    assert agg["precision"] > 0.9
    assert agg["true_positives"] == 10 and agg["false_positives"] == 1


def test_ground_truth_file_loads_and_covers_both_fixtures():
    truth = load_ground_truth(GROUND_TRUTH)
    assert {"legacy-billing", "modern-payments"} <= set(truth)
    assert len(truth["legacy-billing"].must_find) >= 10
    assert truth["modern-payments"].must_find == ()


# ------------------------------------------------------------------- judge


def test_judge_detects_fabricated_file_citations():
    real = DueDiligenceReport(repo="r", findings=(_finding("sql_injection", "src/db.py"),))
    fake = DueDiligenceReport(repo="r", findings=(_finding("sql_injection", "does/not/exist.py"),))
    root = FIXTURES / "legacy-billing"
    judge = DeterministicJudge()
    assert judge.score(real, root).groundedness > judge.score(fake, root).groundedness
    assert hallucination_rate([fake], root) == 1.0
    assert hallucination_rate([real], root) == 0.0


def test_judge_penalises_everything_is_critical():
    root = FIXTURES / "legacy-billing"
    balanced = DueDiligenceReport(
        repo="r",
        findings=(
            _finding("a", "src/db.py", Severity.CRITICAL),
            _finding("b", "src/api.py", Severity.MEDIUM),
            _finding("c", "src/db.py", Severity.LOW),
        ),
    )
    inflated = DueDiligenceReport(
        repo="r",
        findings=tuple(
            _finding(f"r{i}", "src/db.py", Severity.CRITICAL, line=i + 1) for i in range(3)
        ),
    )
    judge = DeterministicJudge()
    assert judge.score(balanced, root).prioritisation > judge.score(inflated, root).prioritisation


# ------------------------------------------------------------------ runner


def test_standard_tier_passes_its_own_gates():
    run = evaluate(tier="standard", pattern="react")
    assert run.passed, f"gate failures: {run.failures}"
    assert run.aggregate["f1"] >= GATES["min_f1"]
    assert run.hallucination_rate <= GATES["max_hallucination_rate"]
    assert len(run.results) == 2


def test_smoke_tier_is_a_fast_subset():
    run = evaluate(tier="smoke", pattern="react")
    assert len(run.results) == 1
    # The standard tier completes in well under a second with the scripted
    # model. `< 30` was 600x slack and would not notice a 100x regression.
    assert run.duration_s < 2.0, f"standard tier took {run.duration_s:.2f}s"


def test_reflexion_beats_react_on_precision():
    """The benchmark's headline claim, asserted so it cannot silently regress."""
    react = evaluate(tier="standard", pattern="react")
    reflexion = evaluate(tier="standard", pattern="reflexion")
    assert reflexion.aggregate["precision"] > react.aggregate["precision"]
    assert reflexion.aggregate["traps"] < react.aggregate["traps"]


def test_rewoo_is_cheaper_but_finds_less():
    react = evaluate(tier="standard", pattern="react")
    rewoo = evaluate(tier="standard", pattern="rewoo")
    assert rewoo.aggregate["recall"] < react.aggregate["recall"]


def test_report_renders_a_readable_table():
    rendered = evaluate(tier="smoke", pattern="react").render()
    assert "| repo |" in rendered and "gate:" in rendered


def test_fixtures_and_ground_truth_stay_in_sync():
    """Ground truth cites file paths; if a fixture is renamed, scoring
    silently degrades unless this fails first."""
    truth = load_ground_truth(GROUND_TRUTH)
    for repo, gt in truth.items():
        root = FIXTURES / repo
        assert root.is_dir(), f"missing fixture repo {repo}"
        for item in gt.must_find:
            if item.path in (".", ".github"):
                continue
            assert (root / item.path).exists(), f"{repo}: ground truth cites missing {item.path}"

        # Traps were skipped entirely by this test, which is how a trap that
        # could never fire survived. The clean fixture declared `no_tests` at
        # `tests/`; an agent falsely reporting missing tests cites
        # `tests/test_repository.py`, which matched neither `== "tests/"` nor
        # `endswith("/tests/")`. Two of the five declared traps on the
        # control repo were unfireable, and `max_traps: 1` is the gate.
        for item in gt.should_not_find:
            if item.path in (".", ""):
                continue
            assert (root / item.path).exists(), f"{repo}: trap cites missing path {item.path}"


def test_every_declared_trap_can_actually_be_triggered():
    """A trap that no plausible finding can match is not a control, it is
    silent headroom against a gate with none to spare."""
    truth = load_ground_truth(GROUND_TRUTH)
    for repo, gt in truth.items():
        for item in gt.should_not_find:
            target = FIXTURES / repo / item.path
            # What an agent would actually cite: the file itself, or a file
            # inside it when the ground truth names a directory.
            if target.is_dir():
                inner = next((p for p in sorted(target.rglob("*")) if p.is_file()), None)
                assert inner is not None, f"{repo}: trap directory {item.path} is empty"
                cited = str(inner.relative_to(FIXTURES / repo))
            else:
                cited = item.path.rstrip("/")
            assert _paths_match(cited, item.path), (
                f"{repo}: trap {item.rule!r} at {item.path!r} cannot be triggered - "
                f"a finding citing {cited!r} does not match it"
            )


def test_fixture_directories_survive_a_clone():
    """Git does not track empty directories.

    `legacy-billing/.github/workflows` is meaningfully empty - it is what
    the `no_ci` ground truth points at - so on a fresh clone it simply did
    not exist, the citation stopped resolving, and the hallucination rate
    went from 0.000 to 0.083, failing the gate. Green in the working copy,
    red in a fresh checkout.
    """
    truth = load_ground_truth(GROUND_TRUTH)
    for repo, gt in truth.items():
        root = FIXTURES / repo
        for item in [*gt.must_find, *gt.should_not_find]:
            if item.path in (".", ""):
                continue
            target = root / item.path.rstrip("/")
            if not target.is_dir():
                continue
            assert any(target.iterdir()), (
                f"{repo}: ground truth cites directory {item.path!r}, which is "
                "empty - git will not track it and it will not survive a clone. "
                "Add a marker file explaining why it is empty, taking care to "
                "put it somewhere that does not change what the fixture means."
            )
