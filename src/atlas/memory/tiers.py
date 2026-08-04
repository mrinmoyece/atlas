"""The four memory tiers.

    working     the live message window for the current step
                (owned by atlas.context - not stored here)
    episodic    what happened in past runs: which repo, what was found,
                what it cost. Append-only, queryable by repo.
    semantic    durable *facts and lessons* extracted from runs, retrieved
                by vector similarity with recency decay.
    procedural  learned *strategies*: which tool sequence works for which
                kind of repository, with success statistics. This is the
                tier almost nobody implements, and the one that actually
                makes an agent cheaper over time.

Design commitments worth defending in review:

  * **Retrieval is scored, decayed and capped.** Unbounded memory injection
    is a context-poisoning bug: yesterday's irrelevant lesson crowds out
    today's evidence. Every retrieval applies exponential recency decay and
    a hard top-k.
  * **Writes are explicit, not implicit.** Memory is written at run
    completion from structured results, never scraped from free text.
    Silent memorisation of hallucinations is how memory systems rot.
  * **Provenance is kept.** Every record stores the run that produced it,
    so a bad lesson can be traced back and evicted.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas.memory.embeddings import DEFAULT_DIM, Embedder, HashingEmbedder, cosine

# Hybrid-retrieval constants, and it is worth being exact about what each
# one buys, because the honest description is narrower than "vectors + decay
# + floor" and the docs used to overclaim.
#
# On a metadata-filtered query, TOPICAL relevance is established by the
# filter, not by cosine similarity - that is the entire reason hybrid
# retrieval was introduced, since the graph's query text and the stored
# lesson text share almost no tokens under a lexical embedder. So the floor
# on that path is not doing topical filtering, and pretending otherwise
# would be the same overclaim in a new place.
#
# What it does enforce is FRESHNESS. The bonus is what a category match is
# worth on its own; the floor is set to half of it, so a lesson with no
# lexical overlap survives less than one decay half-life while a lesson that
# also matches lexically survives proportionally longer. Set to 0.0 - as it
# originally was - nothing was enforced at all and a lesson 199 runs stale
# under a 5-run half-life still came back.
FILTERED_FLOOR_RATIO = 0.6
FILTERED_MATCH_BONUS = 0.25


def _now() -> float:
    return time.time()


class MemoryRecord(BaseModel):
    """One remembered item. Immutable apart from usage statistics, which
    live in `stats` so the record itself stays a stable fact."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: str = "semantic"  # semantic | episodic | procedural
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_index: int = 0  # logical clock: which run created this
    created_at: float = Field(default_factory=_now)

    def render(self) -> str:
        repo = self.metadata.get("repo")
        prefix = f"[{repo}] " if repo else ""
        return f"- {prefix}{self.text}"


class EpisodicMemory:
    """Append-only log of past runs. Cheap to write, cheap to scan."""

    def __init__(self) -> None:
        self._episodes: list[MemoryRecord] = []

    def append(self, text: str, *, run_index: int, **metadata: Any) -> MemoryRecord:
        record = MemoryRecord(kind="episodic", text=text, metadata=metadata, run_index=run_index)
        self._episodes.append(record)
        return record

    def recent(self, n: int = 5) -> list[MemoryRecord]:
        return self._episodes[-n:]

    def for_repo(self, repo: str) -> list[MemoryRecord]:
        return [e for e in self._episodes if e.metadata.get("repo") == repo]

    def __len__(self) -> int:
        return len(self._episodes)


class SemanticMemory:
    """Vector-retrieved lessons with recency decay and capacity eviction."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        *,
        capacity: int = 500,
        decay_half_life_runs: float = 50.0,
    ) -> None:
        self._embedder = embedder or HashingEmbedder(DEFAULT_DIM)
        self._records: list[MemoryRecord] = []
        self._vectors: list[list[float]] = []
        self._capacity = capacity
        self._half_life = max(1.0, decay_half_life_runs)

    @property
    def records(self) -> tuple[MemoryRecord, ...]:
        """Read-only view, so tests can assert *which* records survived
        eviction rather than only how many."""
        return tuple(self._records)

    def add(self, text: str, *, run_index: int, **metadata: Any) -> MemoryRecord:
        record = MemoryRecord(kind="semantic", text=text, metadata=metadata, run_index=run_index)
        self._records.append(record)
        self._vectors.append(self._embedder.embed(text))
        self._evict_if_needed()
        return record

    def search(
        self,
        query: str,
        *,
        k: int = 3,
        now_run: int | None = None,
        min_score: float = 0.05,
        where: dict[str, Any] | None = None,
    ) -> list[tuple[float, MemoryRecord]]:
        """Top-k by decayed similarity, optionally filtered by metadata.

        **Hybrid retrieval, and it is not optional.** Pure vector search
        failed completely in production here and the unit tests could not see
        it: lessons are written as "security: 'sql_injection' appeared here
        (e.g. src/db.py)" while the graph queries "technical due diligence for
        repository X". Under a lexical embedder those share almost no tokens,
        every candidate fell below the relevance floor, and the semantic tier
        returned **zero records on every real run** while passing tests that
        queried it with lesson-shaped text.

        The fix is what production vector stores actually do: filter on
        structured metadata first (repo, category), then rank what survives by
        similarity. A filtered candidate set is already known-relevant, so the
        floor is relaxed for it - the floor exists to reject *unrelated*
        records, and a category match is not unrelated.
        """
        if not self._records:
            return []
        qv = self._embedder.embed(query)
        current = now_run if now_run is not None else self._max_run()

        def matches(record: MemoryRecord) -> bool:
            if not where:
                return True
            return all(record.metadata.get(key) == value for key, value in where.items())

        candidates = [
            (rec, vec)
            for rec, vec in zip(self._records, self._vectors, strict=False)
            if matches(rec)
        ]
        if not candidates:
            return []

        # A metadata-filtered set is pre-qualified, so similarity only has
        # to *order* it. Without the filter, similarity must also *qualify*
        # it, and the floor is what does that.
        #
        # But "pre-qualified" is not "unconditional", and the first version
        # of this read `floor = 0.0 if filtered else min_score` - which
        # disabled the floor entirely on the only path production uses. The
        # graph always passes `where={"category": ...}`, so every record in
        # a category came back regardless of relevance or age: a query for
        # "frontend css animation" returned a SQL-injection lesson, and a
        # lesson 199 runs stale under a 5-run half-life still scored above
        # a floor of exactly zero.
        #
        # And the tests could not see it, for the second time in this
        # module's history: `test_relevance_floor_returns_nothing_when_
        # nothing_matches` and `test_decay_eventually_drops_stale_lessons`
        # both called `search()` WITHOUT `where` - testing the branch the
        # running system never takes. That is verbatim the failure mode
        # docs/MEMORY.md was written to record.
        #
        # A category match is a prior, not a pass. Filtered candidates get a
        # relevance bonus and a proportionally lower floor; they do not get
        # to skip the floor.
        filtered = bool(where)
        floor = (
            max(min_score * FILTERED_FLOOR_RATIO, FILTERED_MATCH_BONUS * 0.5)
            if filtered
            else min_score
        )

        scored: list[tuple[float, MemoryRecord]] = []
        for record, vec in candidates:
            sim = cosine(qv, vec)
            age = max(0, current - record.run_index)
            decay = math.pow(0.5, age / self._half_life)
            # Filtered candidates keep a similarity floor of zero but still
            # decay, so stale lessons lose to fresh ones within a category.
            score = max(sim, 0.0) * decay
            if filtered:
                # The category match is worth a fixed relevance bonus, so a
                # known-relevant lesson with weak lexical overlap can clear
                # the (lowered) floor - which is the whole point of hybrid
                # retrieval. Decay still applies, so a stale lesson falls
                # below it eventually no matter how well it matches.
                score = (FILTERED_MATCH_BONUS + max(sim, 0.0)) * decay
            if score > floor:
                scored.append((score, record))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:k]

    def _max_run(self) -> int:
        return max((r.run_index for r in self._records), default=0)

    def _evict_if_needed(self) -> None:
        """Evict oldest-by-run first. Simple, predictable, and easy to
        explain; LFU/LRU-hybrid policies are noted as an upgrade path."""
        while len(self._records) > self._capacity:
            oldest = min(range(len(self._records)), key=lambda i: self._records[i].run_index)
            self._records.pop(oldest)
            self._vectors.pop(oldest)

    def __len__(self) -> int:
        return len(self._records)


class StrategyStats(BaseModel):
    """Success statistics for one strategy in one context."""

    model_config = ConfigDict(frozen=True)

    strategy: str
    attempts: int = 0
    successes: int = 0
    total_cost_usd: float = 0.0
    total_steps: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0

    @property
    def avg_cost(self) -> float:
        return self.total_cost_usd / self.attempts if self.attempts else 0.0

    def score(self, *, prior_weight: float = 2.0) -> float:
        """Laplace-smoothed success rate.

        Smoothing is not decoration: without it a strategy that succeeded
        once (1/1 = 100%) permanently beats one that succeeded 47/50, and
        the agent locks onto a lucky fluke. This is the exploration/
        exploitation trap that makes naive procedural memory *worse* than
        no memory at all.
        """
        return (self.successes + prior_weight * 0.5) / (self.attempts + prior_weight)


class ProceduralMemory:
    """Learns *how* to work: which strategy wins for which kind of context."""

    def __init__(self) -> None:
        self._stats: dict[tuple[str, str], StrategyStats] = {}

    def record(
        self,
        *,
        context_key: str,
        strategy: str,
        success: bool,
        cost_usd: float = 0.0,
        steps: int = 0,
    ) -> StrategyStats:
        key = (context_key, strategy)
        prev = self._stats.get(key) or StrategyStats(strategy=strategy)
        updated = StrategyStats(
            strategy=strategy,
            attempts=prev.attempts + 1,
            successes=prev.successes + int(success),
            total_cost_usd=prev.total_cost_usd + cost_usd,
            total_steps=prev.total_steps + steps,
        )
        self._stats[key] = updated
        return updated

    def best(self, context_key: str, *, candidates: Iterable[str] | None = None) -> str | None:
        """Highest smoothed-score strategy for this context, or None if we
        have no experience at all (caller then uses its default)."""
        pool = {
            strategy: stats for (ctx, strategy), stats in self._stats.items() if ctx == context_key
        }
        if candidates is not None:
            pool = {k: v for k, v in pool.items() if k in set(candidates)}
        if not pool:
            return None
        return max(pool.items(), key=lambda kv: kv[1].score())[0]

    def table(self, context_key: str | None = None) -> list[StrategyStats]:
        return [
            stats
            for (ctx, _), stats in sorted(self._stats.items())
            if context_key is None or ctx == context_key
        ]

    def to_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for (ctx, _strategy), stats in self._stats.items():
                f.write(json.dumps({"context": ctx, **stats.model_dump()}) + "\n")

    @classmethod
    def from_jsonl(cls, path: Path) -> ProceduralMemory:
        mem = cls()
        if not path.exists():
            return mem
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            ctx = data.pop("context")
            mem._stats[(ctx, data["strategy"])] = StrategyStats(**data)
        return mem
