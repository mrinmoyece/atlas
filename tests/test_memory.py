"""Memory: retrieval quality, decay, smoothing, and what must never be learned."""

from __future__ import annotations

from atlas.domain.types import (
    Category,
    Confidence,
    DueDiligenceReport,
    Evidence,
    Finding,
    Severity,
)
from atlas.memory.embeddings import HashingEmbedder, cosine
from atlas.memory.hub import MemoryHub
from atlas.memory.tiers import ProceduralMemory, SemanticMemory


def _finding(rule: str, path: str = "src/db.py", grounded: bool = True) -> Finding:
    return Finding(
        finding_id=rule,
        category=Category.SECURITY,
        severity=Severity.HIGH,
        confidence=Confidence.HIGH,
        title=rule.replace("_", " "),
        rule=rule,
        evidence=(Evidence(path=path, line=3),) if grounded else (),
    )


def _report(*findings: Finding, repo: str = "acme") -> DueDiligenceReport:
    return DueDiligenceReport(repo=repo, findings=findings, cost_usd=0.02)


# ---------------------------------------------------------------- embeddings


def test_embeddings_are_deterministic_and_normalised():
    e = HashingEmbedder()
    a, b = (
        e.embed("sql injection in the database layer"),
        e.embed("sql injection in the database layer"),
    )
    assert a == b
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9


def test_similar_text_scores_higher_than_unrelated():
    e = HashingEmbedder()
    query = e.embed("sql injection vulnerability in database query")
    close = e.embed("database query built with string concatenation sql injection")
    far = e.embed("css animation timing on the marketing landing page")
    assert cosine(query, close) > cosine(query, far)


# ------------------------------------------------------------------ semantic


def test_filtered_retrieval_still_drops_stale_lessons():
    """The path production actually uses.

    The graph ALWAYS passes `where={"category": ...}`, and the filtered
    branch originally set `floor = 0.0` - so nothing was enforced on the
    only branch that runs. The two tests below both called `search()`
    without `where`, i.e. they covered the branch the system never takes.
    That is the same test-blindness docs/MEMORY.md exists to record, one
    module later.
    """
    mem = SemanticMemory(capacity=300, decay_half_life_runs=5)
    mem.add(
        "security: 'sql_injection' appeared here (e.g. src/db.py); severity critical",
        run_index=1,
        category="security",
    )
    fresh = mem.search("sql injection src/db.py", k=3, where={"category": "security"})
    assert len(fresh) == 1, "a fresh, category-matched lesson must be retrievable"

    for i in range(200):
        mem.add(f"unrelated filler lesson {i}", run_index=i + 2, category="other")
    stale = mem.search("sql injection src/db.py", k=3, where={"category": "security"})
    assert stale == [], "a lesson 199 runs stale under a 5-run half-life must not resurface"


def test_relevance_floor_returns_nothing_when_nothing_matches():
    """Without a floor, an irrelevant store still returns its 'best' k
    records and the agent gets confidently injected garbage."""
    mem = SemanticMemory()
    mem.add("security: sql_injection appeared in db.py", run_index=1)
    assert mem.search("unrelated frontend css animation topic", k=3) == []


def test_recency_decay_prefers_newer_lessons():
    mem = SemanticMemory(decay_half_life_runs=5)
    mem.add("sql injection in database layer", run_index=96)
    mem.add("sql injection in database layer", run_index=100)
    hits = mem.search("sql injection database", k=2, now_run=100)
    assert hits[0][1].run_index == 100
    assert hits[0][0] > hits[1][0]


def test_decay_eventually_drops_stale_lessons_below_the_floor():
    """Decay and the relevance floor compose: a lesson from long ago stops
    being retrieved at all rather than lingering forever."""
    mem = SemanticMemory(decay_half_life_runs=5)
    mem.add("sql injection in database layer", run_index=1)
    assert mem.search("sql injection database", k=3, now_run=1)
    assert mem.search("sql injection database", k=3, now_run=200) == []


def test_capacity_eviction_drops_oldest():
    """Asserting only `len(mem) == 3` passes against an eviction policy that
    drops the *newest* record, or a random one - i.e. against the two
    policies that would make bounded memory actively harmful. The identity
    of the survivors is the whole property."""
    mem = SemanticMemory(capacity=3)
    for i in range(6):
        mem.add(f"lesson number {i} about security", run_index=i)

    assert len(mem) == 3
    survivors = {record.text for record in mem.records}
    assert survivors == {
        "lesson number 3 about security",
        "lesson number 4 about security",
        "lesson number 5 about security",
    }, f"wrong records evicted: {sorted(survivors)}"


# ---------------------------------------------------------------- procedural


def test_laplace_smoothing_beats_lucky_single_success():
    """A 1/1 strategy must not permanently outrank a 47/50 strategy - that
    trap makes procedural memory worse than none."""
    mem = ProceduralMemory()
    mem.record(context_key="ctx", strategy="lucky", success=True)
    for _ in range(50):
        mem.record(context_key="ctx", strategy="proven", success=True)
    mem.record(context_key="ctx", strategy="proven", success=False)
    assert mem.best("ctx") == "proven"


def test_best_returns_none_without_experience():
    assert ProceduralMemory().best("unseen") is None


def test_candidates_filter_restricts_choice():
    mem = ProceduralMemory()
    mem.record(context_key="ctx", strategy="rewoo", success=True)
    mem.record(context_key="ctx", strategy="react", success=True)
    assert mem.best("ctx", candidates=["react"]) == "react"


# ----------------------------------------------------------------------- hub


def test_hub_learns_only_from_grounded_structured_findings():
    """Ungrounded claims must never be memorised: a memory system that
    absorbs unverified assertions is a hallucination amplifier."""
    hub = MemoryHub()
    hub.learn_from_report(
        _report(_finding("sql_injection"), _finding("made_up", grounded=False)),
        context_key="ctx",
        strategy="react",
        success=True,
    )
    hits = hub.semantic.search("sql injection", k=5)
    lessons = " ".join(r.text for _, r in hits)
    assert "sql_injection" in lessons
    assert "made_up" not in lessons


def test_duplicate_rules_collapse_to_one_lesson():
    """Ten SQLi hits in one repo teach one thing; writing ten records would
    let a single noisy run dominate future retrieval."""
    hub = MemoryHub()
    written = hub.learn_from_report(
        _report(*[_finding("sql_injection", path=f"src/f{i}.py") for i in range(5)]),
        context_key="ctx",
        strategy="react",
        success=True,
    )
    assert written == 2  # 1 episodic + 1 semantic lesson


def test_disabled_hub_is_completely_inert():
    hub = MemoryHub(enabled=False)
    assert (
        hub.learn_from_report(
            _report(_finding("sql_injection")), context_key="c", strategy="react", success=True
        )
        == 0
    )
    assert not hub.recall("sql injection").used


def test_recall_renders_auditable_block():
    hub = MemoryHub()
    hub.learn_from_report(
        _report(_finding("sql_injection")), context_key="ctx", strategy="react", success=True
    )
    recall = hub.recall("sql injection in database", context_key="ctx")
    assert recall.used
    assert "<prior_experience>" in recall.block
    assert recall.strategy_hint == "react"


def test_demo_uses_the_graphs_repository_context():
    from scripts.demo import _demo_context_key

    assert _demo_context_key() == "repo:standard"
