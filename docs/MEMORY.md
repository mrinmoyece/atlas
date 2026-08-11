# Memory: does it actually help?

Atlas implements four memory tiers. This document reports whether they earn
their cost, measured by `benchmarks/memory_ab.py`. Regenerate with
`make memory-ab`.

## The four tiers

| tier | what it holds | lifetime | retrieval |
|---|---|---|---|
| working | live message window for this step | one step | n/a (compaction governs it) |
| episodic | one record per completed run: repo, counts, cost | process lifetime | by repo or recency |
| semantic | lessons from findings | process lifetime; capacity 500 | filter, then vectors+decay+floor |
| procedural | best strategy per repo kind | process lifetime | Laplace-smoothed rate |

## Experiment 1 - cross-repository transfer (leak-free)

Question: does analysing repository B benefit from having previously
analysed a *different* repository A?

| arm | mean quality | total cost | findings |
|---|---|---|---|
| cold (memory off) | 0.801 | $0.0394 | 12 |
| warm (memory on) | 0.801 | $0.0439 | 12 |

**delta: +0.000 quality, +0.0045 USD -> no significant effect**

## Experiment 2 - longitudinal re-analysis

Question: does analysing the *same* repository a second time, carrying the
lessons from the first pass, do better? This is the production scenario of
re-running due diligence after remediation. It is not leakage because the
claim being made is explicitly about repeat analysis, not about
generalisation.

| pass | quality | findings | cost |
|---|---|---|---|
| 1st (cold) | 0.952 | 11 | $0.0269 |
| 2nd (warm) | 1.000 | 12 | $0.0312 |

**delta: +0.048 quality -> memory helps**

Experiment 1 per-repository quality:

| repo | cold | warm |
|---|---|---|
| legacy-billing | 0.952 | 0.952 |
| modern-payments | 0.650 | 0.650 |

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
