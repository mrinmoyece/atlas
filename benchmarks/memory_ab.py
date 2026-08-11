"""Does memory help? Run the A/B and write docs/MEMORY.md.

    python -m benchmarks.memory_ab

Cold arm: memory disabled entirely.
Warm arm: memory enabled and pre-trained on the fixture set, with the strict
rule that a repository is scored *before* its own run is memorised - so no
repository can benefit from having already seen itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

from atlas.domain.types import DueDiligenceReport
from atlas.evals.runner import GROUND_TRUTH, run_repo
from atlas.evals.scenarios import fixture_repos
from atlas.evals.scoring import RepoGroundTruth, load_ground_truth, score_report
from atlas.memory.hub import MemoryHub
from atlas.memory.measure import run_ab

DOC = Path(__file__).resolve().parents[1] / "docs" / "MEMORY.md"


def _longitudinal(scorer) -> str:
    """Analyse one repository twice, learning in between."""
    repo = "legacy-billing"
    cold_hub = MemoryHub(enabled=False)
    warm_hub = MemoryHub(enabled=True)

    cold = run_repo(repo, pattern="react", memory=cold_hub)
    first = run_repo(repo, pattern="react", memory=warm_hub)
    warm_hub.learn_from_report(first, context_key="repo:standard", strategy="react", success=True)
    second = run_repo(repo, pattern="react", memory=warm_hub)

    cold_q, warm_q = scorer(repo, cold), scorer(repo, second)
    delta = warm_q - cold_q
    verdict = (
        "memory helps"
        if delta > 0.02
        else "memory hurts"
        if delta < -0.02
        else "no significant effect"
    )
    return "\n".join(
        [
            "| pass | quality | findings | cost |",
            "|---|---|---|---|",
            f"| 1st (cold) | {cold_q:.3f} | {len(cold.findings)} | ${cold.cost_usd:.4f} |",
            f"| 2nd (warm) | {warm_q:.3f} | {len(second.findings)} | ${second.cost_usd:.4f} |",
            "",
            f"**delta: {delta:+.3f} quality -> {verdict}**",
        ]
    )


def main() -> int:
    truth = load_ground_truth(GROUND_TRUTH)

    def runner(repo: str, hub: MemoryHub) -> DueDiligenceReport:
        return run_repo(repo, pattern="react", memory=hub)

    def scorer(repo: str, report: DueDiligenceReport) -> float:
        card = score_report(report, truth.get(repo, RepoGroundTruth(repo=repo)))
        return card.quality

    repos = fixture_repos()
    # Experiment 1: cross-repository transfer, leak-free.
    # No warmup set: with two fixtures the honest question is "does repo 2
    # benefit from having seen repo 1", answered by run_ab's
    # learn-after-score ordering. Passing warmup_repos=repos would pre-train
    # on the very repositories being scored - the leak this harness exists
    # to avoid.
    result = run_ab(repos, runner, scorer, context_key="repo:generic")

    # Experiment 2: longitudinal re-analysis. Different question, and NOT
    # leakage as long as it is labelled as what it is: "does analysing the
    # same repository again, with the lessons from the first pass, do
    # better?" That is a real production scenario (re-running due diligence
    # after remediation) and it is where memory should pay off first.
    longitudinal = _longitudinal(scorer)
    episodic_row = (
        "| episodic | one record per completed run: repo, counts, cost | "
        "process lifetime | by repo or recency |"
    )
    semantic_row = (
        "| semantic | lessons from findings | process lifetime; capacity 500 | "
        "filter, then vectors+decay+floor |"
    )

    body = (
        f"""# Memory: does it actually help?

Atlas implements four memory tiers. This document reports whether they earn
their cost, measured by `benchmarks/memory_ab.py`. Regenerate with
`make memory-ab`.

## The four tiers

| tier | what it holds | lifetime | retrieval |
|---|---|---|---|
| working | live message window for this step | one step | n/a (compaction governs it) |
{episodic_row}
{semantic_row}
| procedural | best strategy per repo kind | process lifetime | Laplace-smoothed rate |

## Experiment 1 - cross-repository transfer (leak-free)

Question: does analysing repository B benefit from having previously
analysed a *different* repository A?

{result.render()}

## Experiment 2 - longitudinal re-analysis

Question: does analysing the *same* repository a second time, carrying the
lessons from the first pass, do better? This is the production scenario of
re-running due diligence after remediation. It is not leakage because the
claim being made is explicitly about repeat analysis, not about
generalisation.

{longitudinal}

Experiment 1 per-repository quality:

| repo | cold | warm |
|---|---|---|
"""
        + "\n".join(
            f"| {repo} | {result.cold.per_repo.get(repo, 0):.3f} | "
            f"{result.warm.per_repo.get(repo, 0):.3f} |"
            for repo in repos
        )
        + """

## Design decisions that matter more than the number

**Only structured findings are memorised.** Lessons are derived from
`Finding` objects that already carry evidence - never from free-form model
prose. Memorising prose is how a memory system becomes a hallucination
amplifier: one confident wrong claim gets written down and then retrieved
as fact forever.

**Retrieval has a relevance floor - and the two paths enforce different
things.** On an *unfiltered* query the floor does the qualifying: without
`min_score` an empty or irrelevant store still returns its top-k "best"
records and the agent is confidently injected with garbage. On a
*metadata-filtered* query - which is every query the graph actually issues -
topical relevance comes from the filter, and the floor enforces *freshness*
instead.

Being exact about that matters, because the filtered floor was originally
set to `0.0`, which enforced nothing at all on the only path production
takes: a lesson 199 runs stale under a 5-run half-life still came back. And
the two unit tests guarding the floor both called `search()` *without*
`where`, so they covered the branch the system never takes. That is the same
test-blindness described below, one module later and one round after it was
supposedly learned from.

**Recency decay is applied at retrieval, not write time.** A lesson from 200
runs ago should lose to a lesson from last week without being deleted -
deletion loses information that may matter again.

**Procedural memory is Laplace-smoothed.** A strategy that succeeded once
(1/1 = 100%) must not permanently outrank one that succeeded 47 times out of
50. Naive success-rate tracking locks the agent onto a lucky fluke, which
makes procedural memory *worse* than having none. See
[`StrategyStats.score`](../src/atlas/memory/tiers.py).

**Two leakage guards.** In the warm arm each repository is scored and only
then memorised, so it can only benefit from repositories that preceded it;
and any repository that appears in both the warmup set and the scored set is
skipped during warmup. Without both guards the experiment leaks and the
result is meaningless. (An earlier version of this harness passed the scored
repositories as the warmup set — the exact leak these guards now prevent.
The number below is from the corrected run.)

## Why experiment 1 shows no effect, and why that is reported anyway

Two fixture repositories is simply not enough transfer surface: the only
cross-repo pair is (dirty -> clean), and the lessons learned from a
deliberately unsafe billing service do not apply to a deliberately healthy
payments service. A larger, more homogeneous fixture set would be needed to
detect cross-repo transfer, and that is listed in LIMITATIONS.md.

The alternative was to keep the original harness, which pre-trained on the
repositories it then scored and produced a comfortable positive number. That
number was wrong. Reporting a null result from a correct experiment is worth
more than a positive one from a broken experiment, and a repository whose
entire argument is "measure agent quality honestly" does not get to make an
exception for its own headline metric.

## The retrieval bug this document exists to record

Pure vector search **failed completely in production** and the unit tests
could not see it. Traced end to end:

1. The graph queried `hub.recall("technical due diligence for repository X")`.
2. Stored lessons read `"security: 'sql_injection' appeared here (e.g.
   src/db.py); severity critical..."`.
3. Under a lexical hashing embedder with a relevance floor, those strings
   share almost no tokens. Every candidate fell below the floor.
4. Semantic retrieval therefore returned **zero records on every real run**,
   while passing its unit tests - because those tests queried it with
   *lesson-shaped* text, which of course matched.

The tests were testing the embedder, not the retrieval path the system
actually uses. That is the kind of bug that survives a green suite
indefinitely.

**The fix is hybrid retrieval**, which is what production vector stores do:
filter on structured metadata first (`category`, `repo`), then rank the
survivors by similarity. A category-filtered candidate set is already
known-relevant, so the similarity floor is relaxed for it - the floor exists
to reject *unrelated* records, and a category match is not unrelated.
Retrieval now happens per specialist (`where={"category": ...}`) rather than
once at plan level with a generic query.

`scripts/demo.py` ACT 4 prints both levels side by side: the unfiltered
plan-level query still returns 0 records, the category-filtered specialist
query returns the relevant lessons. Keeping both visible is deliberate -
the failure mode is more instructive than the fix.

## Honesty note on the longitudinal delta

**This is a single run (n=1) against a deterministic model.** The
evaluation model is deterministic and scripted, and the scripted
security specialist is *memory-sensitive*: when a `<prior_experience>` block
is present it also reports the missing-authentication issue that the cold
run misses. So the delta demonstrates that the harness detects and
quantifies a memory effect - it is **not** evidence that memory improves a
live model, because a deterministic model cannot be influenced by content.

Measuring the real effect requires a separate live-provider runner that
injects a provider model instead of `model_for(repo)`, plus N repetitions.
That runner and experiment have not been implemented. Any number in this
document quoted as evidence about live-model behaviour is being misquoted.
"""
    )
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(body)
    print(result.render())
    print(f"\nwritten to {DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
