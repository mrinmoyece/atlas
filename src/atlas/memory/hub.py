"""MemoryHub - the single seam between agents and memory.

Agents never touch a tier directly. They ask the hub two questions:

    recall(...)   "what do I know that helps with this task?"
    learn(...)    "here is what happened; remember what's worth keeping"

Keeping it behind one facade buys three things: the A/B harness can disable
memory with a single flag (`enabled=False`) without touching agent code;
retrieval budget is enforced in exactly one place; and the injected block
is rendered identically everywhere, so what the model sees is auditable.

What gets learned, precisely
----------------------------
On run completion the hub writes, from *structured results only*:
  * one episodic record per run (repo, counts, cost)
  * one semantic lesson per confirmed finding class ("in repos using raw
    string SQL, check <pattern>") - derived from the finding's rule and
    evidence, never from free-form model prose
  * one procedural outcome per (repo-kind, strategy) pair

Refusing to memorise free text is deliberate: it is the difference between
a memory system and a hallucination amplifier.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from atlas.domain.types import Category, DueDiligenceReport, Finding
from atlas.memory.embeddings import Embedder
from atlas.memory.tiers import (
    EpisodicMemory,
    MemoryRecord,
    ProceduralMemory,
    SemanticMemory,
)

MEMORY_BLOCK_OPEN = "<prior_experience>"
MEMORY_BLOCK_CLOSE = "</prior_experience>"


class RecallResult(BaseModel):
    """What memory contributed to a step - logged for auditability."""

    model_config = ConfigDict(frozen=True)

    records: tuple[MemoryRecord, ...] = ()
    strategy_hint: str | None = None
    block: str = ""

    @property
    def used(self) -> bool:
        return bool(self.records) or self.strategy_hint is not None


class MemoryHub:
    def __init__(
        self,
        *,
        enabled: bool = True,
        embedder: Embedder | None = None,
        top_k: int = 3,
        decay_half_life_runs: float = 50.0,
    ) -> None:
        self.enabled = enabled
        self.top_k = top_k
        self.episodic = EpisodicMemory()
        self.semantic = SemanticMemory(embedder, decay_half_life_runs=decay_half_life_runs)
        self.procedural = ProceduralMemory()
        self._run_index = 0

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        context_key: str | None = None,
        candidates: Sequence[str] | None = None,
        where: dict[str, object] | None = None,
    ) -> RecallResult:
        """Retrieve prior experience.

        `where` filters on lesson metadata (`repo`, `category`, `rule`) before
        ranking. Callers should use it: relying on lexical similarity alone
        between a generic task description and a specific lesson is how this
        tier silently returned nothing for every production query.
        """
        if not self.enabled:
            return RecallResult()
        hits = self.semantic.search(query, k=self.top_k, now_run=self._run_index, where=where)
        records = tuple(record for _, record in hits)
        hint = self.procedural.best(context_key, candidates=candidates) if context_key else None
        return RecallResult(records=records, strategy_hint=hint, block=_render(records, hint))

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def learn_from_report(
        self,
        report: DueDiligenceReport,
        *,
        context_key: str,
        strategy: str,
        success: bool,
    ) -> int:
        """Persist what this run taught us. Returns records written."""
        if not self.enabled:
            return 0
        self._run_index += 1
        written = 0

        self.episodic.append(
            f"analysed {report.repo}: {len(report.findings)} findings "
            f"({report.counts()}), cost ${report.cost_usd:.4f}",
            run_index=self._run_index,
            repo=report.repo,
            cost_usd=report.cost_usd,
        )
        written += 1

        for lesson, meta in _lessons_from_findings(report.findings):
            self.semantic.add(lesson, run_index=self._run_index, repo=report.repo, **meta)
            written += 1

        self.procedural.record(
            context_key=context_key,
            strategy=strategy,
            success=success,
            cost_usd=report.cost_usd,
            # Model calls, not findings. This read `sum(1 for _ in
            # report.findings)` - the finding count, stored in a field named
            # `total_steps` and averaged into `avg_steps`. Nothing failed;
            # the number was simply about something else, which is the worst
            # kind of metric bug because it looks plausible on a dashboard.
            steps=report.model_calls,
        )
        return written

    @property
    def run_index(self) -> int:
        return self._run_index

    def stats(self) -> dict[str, int]:
        return {
            "episodic": len(self.episodic),
            "semantic": len(self.semantic),
            "procedural": len(self.procedural.table()),
            "runs": self._run_index,
        }

    def history(self, repo: str | None = None, limit: int = 5) -> list[MemoryRecord]:
        """Episodic history, for the API and for operators asking "what has
        this platform already looked at?"."""
        if repo:
            return self.episodic.for_repo(repo)[-limit:]
        return self.episodic.recent(limit)


def _lessons_from_findings(findings: Sequence[Finding]) -> list[tuple[str, dict]]:
    """Derive durable lessons from structured findings.

    One lesson per (rule, category) pair, not per finding: ten SQL injection
    hits in one repo teach one thing, and writing ten near-identical records
    would let a single noisy run dominate future retrieval.
    """
    seen: set[tuple[str, Category]] = set()
    lessons: list[tuple[str, dict]] = []
    for f in findings:
        if not f.rule or not f.is_grounded():
            continue  # ungrounded claims are never memorised
        key = (f.rule, f.category)
        if key in seen:
            continue
        seen.add(key)
        where = f.evidence[0].path if f.evidence else "unknown"
        lessons.append(
            (
                f"{f.category.value}: '{f.rule}' appeared here (e.g. {where}); "
                f"severity {f.severity.value}. Check this pattern early.",
                {"rule": f.rule, "category": f.category.value, "severity": f.severity.value},
            )
        )
    return lessons


def _render(records: Sequence[MemoryRecord], hint: str | None) -> str:
    if not records and not hint:
        return ""
    lines = [MEMORY_BLOCK_OPEN]
    if hint:
        lines.append(f"Recommended strategy based on past runs: {hint}")
    if records:
        lines.append("Relevant lessons from previous analyses:")
        lines.extend(r.render() for r in records)
    lines.append(MEMORY_BLOCK_CLOSE)
    return "\n".join(lines)
