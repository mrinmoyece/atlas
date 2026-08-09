"""The due-diligence graph.

    plan ──┬─► security ─────┐
           ├─► architecture ─┤
           ├─► dependency ───┼─► synthesise ─► END
           └─► delivery ─────┘

Three decisions in this file are the ones worth defending in an interview.

**1. Context isolation.** Each specialist builds its own message list inside
its node and returns only `SpecialistResult` (findings + a one-paragraph
summary). No specialist ever sees another's transcript, and the supervisor
never sees any of them. Without this, four specialists' transcripts land in
shared state and every later step pays for all of them - the reason naive
multi-agent systems cost more than a single agent while performing worse.

**2. Real parallelism via fan-out.** All specialist nodes are edged from
`plan`, so LangGraph runs them concurrently and merges their writes through
the reducers in `state.py`. This is where wrong reducers silently drop
findings; `tests/test_graph_and_patterns.py` asserts all four contribute.

**3. Failure isolation.** A specialist that throws records an error in
state and the run continues. A due-diligence report missing its dependency
section is useful; a 500 is not.

A checkpointer is attached so `stream_due_diligence` can emit per-node
updates as they land. Note the honest scope: `build_graph` creates a fresh
`InMemorySaver` per call, so checkpoints live for the duration of one run -
enough for streaming and mid-run inspection, NOT enough to resume a run in a
later process. Durable resume needs a persistent checkpointer; see
docs/LIMITATIONS.md.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from atlas.a2a.cards import AgentRegistry, atlas_agent_cards, card_for
from atlas.config import Settings, get_settings
from atlas.context.budget import TokenBudget
from atlas.domain.types import Category, DueDiligenceReport, SpecialistResult
from atlas.graph.state import DDState
from atlas.mcp_layer.client import MCPToolProxy
from atlas.mcp_layer.federation import federate
from atlas.memory.hub import MemoryHub
from atlas.observability.logging import get_logger
from atlas.observability.metrics import record_graph_cache, record_run, record_specialist
from atlas.observability.tracing import span
from atlas.patterns import PatternContext, get_pattern
from atlas.tools.repo import RepoToolkit

log = get_logger(__name__)

DEFAULT_PATTERN = "react"


class _RunLedger:
    """Live cross-branch spend tracking for one run.

    LangGraph state cannot serve this: parallel branches each see state as it
    was at fan-out, so an accumulating counter is invisible to its siblings.
    Specialists therefore report spend here *as they incur it*, and the
    ceiling is checked immediately before each model call.

    Per-specialist subtotals are tracked so a specialist's incremental
    reports can be reconciled against its final figure without double
    counting.
    """

    def __init__(self) -> None:
        self._by_specialist: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def report(self, run_key: str, specialist: str, spent_so_far: float) -> float:
        """Record a specialist's running total; return the whole-run total."""
        with self._lock:
            self._by_specialist[(run_key, specialist)] = max(0.0, spent_so_far)
            return sum(v for (r, _), v in self._by_specialist.items() if r == run_key)

    def settle(self, run_key: str, specialist: str, final_cost: float) -> None:
        with self._lock:
            self._by_specialist[(run_key, specialist)] = max(0.0, final_cost)

    def total(self, run_key: str) -> float:
        with self._lock:
            return round(sum(v for (r, _), v in self._by_specialist.items() if r == run_key), 8)

    def reset(self, run_key: str) -> None:
        with self._lock:
            for key in [k for k in self._by_specialist if k[0] == run_key]:
                del self._by_specialist[key]


_ledger = _RunLedger()


def _make_cost_guard(run_key: str, category: Category, ceiling: float):
    """Guard consulted before every model call in this specialist."""

    def guard(spent_by_this_specialist: float) -> bool:
        run_total = _ledger.report(run_key, category.value, spent_by_this_specialist)
        return run_total < ceiling

    return guard


#: Above this file count a repository is bucketed as "large" for strategy
#: selection. Counting stops here, so the cost is bounded on a monorepo.
LARGE_REPO_FILES = 200
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}


ALL_CATEGORIES = (
    Category.SECURITY,
    Category.ARCHITECTURE,
    Category.DEPENDENCY,
    Category.DELIVERY,
)


class _GraphCache:
    """Compiled graphs, reused across runs with the same configuration.

    Compilation is not free. LangGraph calls `inspect.getsource` on every
    node during compile (`pregel/_utils.get_function_nonlocals`), which
    tokenises and ASTs the source. Profiled: **9.9ms against a 38.4ms run -
    26% of every run spent rebuilding an identical graph.**

    The graph is a pure function of its configuration, so reuse is safe.
    The nodes close over `model`, `settings` and `memory`, which is why the
    key includes them: two runs with different models must not share a
    compiled graph.

    Keyed by object IDENTITY, and the cache therefore holds a strong
    reference to each key object. That is deliberate, not an oversight:
    `id()` is only unique among *live* objects, so a cache keyed on a
    collected object's id can hand back a graph closed over a completely
    different model. Holding the reference makes the id stable for as long
    as the entry exists. The cost is bounding the cache, which is done
    below.
    """

    #: Small on purpose. A handful of distinct configurations is normal;
    #: hundreds means something is constructing models in a loop, and an
    #: unbounded cache would turn that into a leak.
    MAX_ENTRIES = 8

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple, tuple[Any, tuple]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(*objects: Any, extra: tuple) -> tuple:
        return (tuple(id(o) for o in objects), extra)

    def get_or_build(self, *, objects: tuple, extra: tuple, build) -> Any:
        key = self._key(*objects, extra=extra)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                self.hits += 1
                record_graph_cache(hit=True)
                return entry[0]

        # Built OUTSIDE the lock: compilation takes ~10ms and holding a
        # global lock across it would serialise every concurrent run for no
        # reason. A duplicate build under a race is cheap and harmless.
        compiled = build()

        with self._lock:
            # `objects` is stored alongside the graph purely to keep those
            # references alive - see the class docstring.
            self._entries[key] = (compiled, objects)
            self._entries.move_to_end(key)
            while len(self._entries) > self.MAX_ENTRIES:
                self._entries.popitem(last=False)
            self.misses += 1
            record_graph_cache(hit=False)
        return compiled

    def clear(self) -> None:
        """Drop every entry and reset the counters.

        The counters go too, deliberately: this is a reset, and a test that
        clears the cache but inherits another test's hit count is asserting
        against process history rather than its own behaviour. Production
        never calls this - the Prometheus counters are fed by
        `record_graph_cache`, which is separate.
        """
        with self._lock:
            self._entries.clear()
            self.hits = 0
            self.misses = 0


_graph_cache = _GraphCache()


def build_graph(
    *,
    model,
    settings: Settings | None = None,
    memory: MemoryHub | None = None,
    pattern_name: str | None = None,
    registry: AgentRegistry | None = None,
    categories: Sequence[Category] = ALL_CATEGORIES,
    checkpointer=None,
    remote_tools: tuple[MCPToolProxy, ...] = (),
):
    """Compile the due-diligence graph.

    `remote_tools` federates zero or more remote MCP servers onto every
    specialist's toolkit. Empty by default: remote tools are an enhancement,
    and the eval numbers must come from a fixed, local, deterministic tool
    surface or they are not comparable between runs.
    """
    cfg = settings or get_settings()
    hub = memory or MemoryHub(
        enabled=cfg.memory_enabled,
        top_k=cfg.memory_top_k,
        decay_half_life_runs=cfg.memory_decay_half_life_runs,
    )
    agents = registry or AgentRegistry(atlas_agent_cards())

    def plan_node(state: DDState) -> dict:
        """Supervisor: choose specialists by *capability*, not by name, and
        pull any relevant prior experience from memory."""
        with span("graph.plan", repo=state.get("repo", "")):
            wanted = state.get("requested_categories") or [c.value for c in categories]
            # Capability-driven routing: the roster is discovered from agent
            # cards, so adding a specialist never edits this function.
            table = agents.routing_table()
            selected = [c for c in wanted if table.get(c)]

            # Plan-level recall is for the *strategy* hint only. Lesson
            # retrieval happens per specialist, where a category filter makes
            # it actually work - see the specialist node below.
            recall = hub.recall(
                f"technical due diligence for repository {state.get('repo', '')}",
                context_key=_context_key(state),
                candidates=list(get_pattern_names()),
            )
            # Precedence: explicit request > learned preference > default.
            #
            # `pattern_name=None` means "no preference", which is what lets
            # procedural memory actually influence anything. An earlier
            # version defaulted it to "react" at every layer, so the learned
            # hint was unreachable dead code - memory advised and nobody
            # listened. An explicit request still always wins: a caller who
            # asks for reflexion must not silently get something else.
            strategy = pattern_name or recall.strategy_hint or DEFAULT_PATTERN
            log.info(
                "plan",
                extra={
                    "ctx": {
                        "repo": state.get("repo"),
                        "specialists": selected,
                        "strategy": strategy,
                        "memory_used": recall.used,
                    }
                },
            )
            return {
                "plan": selected,
                "strategy": strategy,
                "memory_block": recall.block,
            }

    def make_specialist_node(category: Category):
        def node(state: DDState) -> dict:
            if category.value not in (state.get("plan") or []):
                return {}  # not selected for this run

            # Run-level cost ceiling.
            #
            # Reading `state["cost_usd"]` here does NOT work and it took a
            # measurement to notice: all four specialists fan out from `plan`
            # simultaneously, so every one of them reads 0.0 and the check can
            # never fire. Verified by setting the ceiling to $0.0001 and
            # watching a full-price run complete.
            #
            # A cross-branch budget needs shared state that updates *during*
            # the fan-out, which graph state by definition does not. Hence a
            # run-scoped ledger keyed by thread_id, incremented as each
            # specialist finishes.
            started = time.monotonic()
            # `run_id`, not `repo`. The comment above claimed the ledger was
            # "keyed by thread_id" while the code keyed it by repository
            # name, and both failure directions were live: two concurrent
            # analyses of the same repo shared one ceiling (run B halted by
            # run A's spend), and either run's `reset` wiped the other's four
            # in-flight subtotals, under-counting its ceiling by up to 4x.
            run_key = state.get("run_id") or state.get("repo", "")
            card = card_for(category)
            with span("graph.specialist", category=category.value, agent=card.name):
                try:
                    # Federation is a no-op unless remote MCP servers were
                    # configured, so the default path is the plain confined
                    # toolkit and nothing about it changes.
                    toolkit = federate(RepoToolkit(state["repo_root"]), remote_tools)
                    # Per-specialist recall, filtered by category. The
                    # security specialist gets security lessons; a generic
                    # run-level query matched nothing at all.
                    specialist_recall = hub.recall(
                        f"{category.value} issues previously found in this kind of repository",
                        where={"category": category.value},
                    )
                    memory_block = "\n".join(
                        block
                        for block in (state.get("memory_block", ""), specialist_recall.block)
                        if block
                    )
                    ctx = PatternContext(
                        model=model,
                        toolkit=toolkit,
                        category=category,
                        memory_block=memory_block,
                        max_steps=cfg.max_steps_per_specialist,
                        token_budget=TokenBudget(limit=cfg.context_token_budget),
                        keep_recent=cfg.compaction_keep_recent,
                        cost_guard=_make_cost_guard(run_key, category, cfg.max_cost_usd),
                    )
                    # Wall-clock timeout for the specialist.
                    #
                    # Two wrong implementations, both instructive:
                    #
                    # 1. `with ThreadPoolExecutor(...) as pool` - `__exit__`
                    #    calls shutdown(wait=True), so raising the timeout
                    #    inside the block joins the worker you just abandoned.
                    #    Measured: a 20s hang with a 2s timeout took 20s and
                    #    reported "exceeded 2.0s". Decorative.
                    # 2. `shutdown(wait=False)` - fixes the run, but
                    #    ThreadPoolExecutor registers an atexit hook that joins
                    #    every worker at interpreter shutdown. The *run* is
                    #    bounded; the *process* then hangs for the remainder of
                    #    the stuck call. A test suite discovers this as a hang,
                    #    which is exactly how it was found here.
                    #
                    # A daemon thread has neither problem: nothing joins it,
                    # and the interpreter will not wait for it at exit.
                    # Python still cannot kill a thread, so a wedged tool
                    # occupies one until the process ends - real, bounded, and
                    # documented in LIMITATIONS.md. Killing it properly means
                    # running tools in a subprocess, the same boundary an
                    # exec-capable tool would need anyway.
                    strategy_name = state.get("strategy") or pattern_name or DEFAULT_PATTERN
                    box: dict[str, Any] = {}

                    def _run_pattern() -> None:
                        try:
                            box["result"] = get_pattern(strategy_name).run(ctx)
                        except BaseException as exc:  # noqa: BLE001
                            box["error"] = exc

                    worker = threading.Thread(
                        target=_run_pattern,
                        name=f"spec-{category.value}",
                        daemon=True,
                    )
                    worker.start()
                    worker.join(timeout=cfg.specialist_timeout_s)
                    if worker.is_alive():
                        raise TimeoutError(
                            f"{category.value} specialist exceeded {cfg.specialist_timeout_s}s"
                        )
                    if "error" in box:
                        raise box["error"]
                    outcome = box["result"]
                except Exception as e:  # noqa: BLE001 - isolate specialist failure
                    log.exception("specialist_failed", extra={"ctx": {"category": category.value}})
                    return {
                        "errors": {category.value: f"{type(e).__name__}: {e}"},
                        "results": [SpecialistResult(category=category, error=str(e)[:500])],
                    }

            duration_ms = int((time.monotonic() - started) * 1000)
            # Reconcile: the guard charged incrementally during the run;
            # settle the difference so the ledger matches actual spend.
            _ledger.settle(run_key, category.value, outcome.cost_usd)
            record_specialist(
                category=category.value,
                pattern=outcome.pattern,
                findings=len(outcome.findings),
                tokens=outcome.tokens_used,
                cost_usd=outcome.cost_usd,
                duration_ms=duration_ms,
                ok=outcome.ok,
            )
            result = SpecialistResult(
                category=category,
                findings=outcome.findings,
                summary=outcome.summary,
                tokens_used=outcome.tokens_used,
                cost_usd=outcome.cost_usd,
                model_calls=outcome.model_calls,
                steps=outcome.steps,
                error=outcome.error,
            )
            update: dict = {
                # NOTE: only findings + summary cross the boundary. The
                # specialist's transcript stays inside this function.
                "findings": list(outcome.findings),
                "results": [result],
                "summaries": {category.value: outcome.summary},
                "tokens_used": outcome.tokens_used,
                "cost_usd": outcome.cost_usd,
                "model_calls": outcome.model_calls,
                "steps": outcome.steps,
            }
            if outcome.error:
                update["errors"] = {category.value: outcome.error}
            return update

        return node

    def synthesise_node(state: DDState) -> dict:
        """Merge specialist output into an executive verdict.

        Deliberately *not* an LLM call by default: the verdict is derived
        from structured findings, so it cannot hallucinate a severity that
        no specialist reported. An LLM narrative is an optional enhancement
        layered on top, never the source of truth.
        """
        with span("graph.synthesise", repo=state.get("repo", "")):
            findings = state.get("findings") or []
            blocking = [f for f in findings if f.severity.rank >= 3]
            counts: dict[str, int] = {}
            for f in findings:
                counts[f.severity.value] = counts.get(f.severity.value, 0) + 1

            if blocking:
                top = "; ".join(f"{f.title} ({f.severity.value})" for f in blocking[:3])
                verdict = (
                    f"NOT CLEAR TO PROCEED without remediation. "
                    f"{len(blocking)} blocking issue(s): {top}. "
                    f"Full severity mix: {counts}."
                )
            elif findings:
                verdict = (
                    f"PROCEED WITH CONDITIONS. No blocking issues; "
                    f"{len(findings)} lower-severity item(s) to schedule. Mix: {counts}."
                )
            else:
                verdict = "PROCEED. No issues found by the engaged specialists."

            errors = state.get("errors") or {}
            if errors:
                verdict += f" NOTE: {len(errors)} specialist(s) failed: {sorted(errors)}."
            return {"verdict": verdict}

    graph = StateGraph(DDState)
    graph.add_node("plan", plan_node)
    for category in categories:
        graph.add_node(category.value, make_specialist_node(category))
    graph.add_node("synthesise", synthesise_node)

    graph.add_edge(START, "plan")
    for category in categories:
        # fan-out: every specialist edges from plan -> they run in parallel
        graph.add_edge("plan", category.value)
        # fan-in: synthesise waits for all of them
        graph.add_edge(category.value, "synthesise")
    graph.add_edge("synthesise", END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def build_graph_cached(
    *,
    model,
    settings: Settings | None = None,
    memory: MemoryHub | None = None,
    pattern_name: str | None = None,
    registry: AgentRegistry | None = None,
    categories: Sequence[Category] = ALL_CATEGORIES,
    checkpointer=None,
    remote_tools: tuple[MCPToolProxy, ...] = (),
):
    """`build_graph`, memoised on the configuration that shapes the graph.

    A caller supplying its own `checkpointer` bypasses the cache entirely:
    a checkpointer carries run state, and handing two runs the same one
    would let a resumed run see another's checkpoints. Correctness first;
    that path is rare and pays the 10ms.
    """
    if checkpointer is not None:
        return build_graph(
            model=model,
            settings=settings,
            memory=memory,
            pattern_name=pattern_name,
            registry=registry,
            categories=categories,
            checkpointer=checkpointer,
            remote_tools=remote_tools,
        )

    return _graph_cache.get_or_build(
        objects=(model, settings, memory, registry, remote_tools),
        extra=(pattern_name, tuple(c.value for c in categories)),
        build=lambda: build_graph(
            model=model,
            settings=settings,
            memory=memory,
            pattern_name=pattern_name,
            registry=registry,
            categories=categories,
            remote_tools=remote_tools,
        ),
    )


def stream_due_diligence(
    *,
    repo: str,
    repo_root: str,
    model,
    settings: Settings | None = None,
    memory: MemoryHub | None = None,
    pattern_name: str | None = None,
    categories: Sequence[Category] = ALL_CATEGORIES,
    thread_id: str | None = None,
    remote_tools: tuple[MCPToolProxy, ...] = (),
):
    """Yield `(node_name, update)` as each graph node completes.

    This is genuine incremental streaming: LangGraph's `stream_mode="updates"`
    emits each node's state delta the moment that node finishes, so a client
    sees the security specialist's result while dependency analysis is still
    running. Building the event list from the *final* report instead - which
    is the tempting shortcut - produces a response that looks streamed and
    isn't, because every event lands at the same instant.
    """
    app = build_graph_cached(
        model=model,
        settings=settings,
        memory=memory,
        pattern_name=pattern_name,
        categories=categories,
        remote_tools=remote_tools,
    )
    run_id = uuid.uuid4().hex
    initial = _initial_state(repo, repo_root, categories, run_id)
    try:
        yield from app.stream(
            initial,
            config={"configurable": {"thread_id": thread_id or f"dd-{repo}"}},
            stream_mode="updates",
        )
    finally:
        # Per-run keys would otherwise accumulate for the process lifetime.
        # Dropping them here is safe precisely because they are per-run: no
        # other analysis can be reading this key.
        _ledger.reset(run_id)


def _initial_state(
    repo: str, repo_root: str, categories: Sequence[Category], run_id: str
) -> DDState:
    return {
        "repo": repo,
        "repo_root": repo_root,
        "run_id": run_id,
        "requested_categories": [c.value for c in categories],
        "findings": [],
        "results": [],
        "summaries": {},
        "errors": {},
        "tokens_used": 0,
        "cost_usd": 0.0,
        "model_calls": 0,
        "steps": 0,
    }


def get_pattern_names() -> list[str]:
    from atlas.patterns import PATTERNS

    return sorted(PATTERNS)


def _context_key(state: DDState | dict) -> str:
    """Bucket a repository into a *kind*.

    Procedural memory learns which strategy wins per context. A per-repo key
    would never accumulate enough evidence to beat the Laplace prior, so
    repos are grouped by a property of the repository itself.

    The previous version bucketed on `len(repo) > 24` - the number of
    characters in the repository *name*. It called itself "coarse"; it was
    not coarse, it was unrelated. Every fixture landed in `repo:standard`,
    and in production the entire context dimension of procedural memory was
    a proxy for how long someone typed the directory name.

    Size is measured from the tree, with the count bounded so a monorepo
    does not turn strategy selection into a filesystem walk. Language mix is
    the documented next dimension (LIMITATIONS.md).
    """
    root = state.get("repo_root")
    if not root:
        return "repo:unknown"
    try:
        files = 0
        for path in Path(root).rglob("*"):
            if path.is_file() and not any(p in _SKIP_DIRS for p in path.parts):
                files += 1
                if files > LARGE_REPO_FILES:
                    break
    except OSError:
        return "repo:unknown"
    return f"repo:{'large' if files > LARGE_REPO_FILES else 'standard'}"


def run_due_diligence(
    *,
    repo: str,
    repo_root: str,
    model,
    settings: Settings | None = None,
    memory: MemoryHub | None = None,
    pattern_name: str | None = None,
    categories: Sequence[Category] = ALL_CATEGORIES,
    thread_id: str | None = None,
    learn: bool = True,
    remote_tools: tuple[MCPToolProxy, ...] = (),
) -> DueDiligenceReport:
    """Build, invoke, shape the result - and close the memory loop.

    `learn=True` writes the run's structured findings back to memory after
    the report exists. Without this the hub is read-only forever: every run
    recalls from an empty store and the "agent improves over time" claim is
    decoration. The A/B harness passes `learn=False` because it controls
    learning order itself.
    """
    started = time.monotonic()
    hub = memory
    app = build_graph_cached(
        model=model,
        settings=settings,
        memory=hub,
        pattern_name=pattern_name,
        categories=categories,
        remote_tools=remote_tools,
    )
    run_id = uuid.uuid4().hex
    initial = _initial_state(repo, repo_root, categories, run_id)
    try:
        final = app.invoke(
            initial, config={"configurable": {"thread_id": thread_id or f"dd-{repo}"}}
        )
    finally:
        # Per-run keys would otherwise accumulate for the process lifetime.
        # Safe to drop precisely because they are per-run: no concurrent
        # analysis can be reading this key.
        _ledger.reset(run_id)
    duration_ms = int((time.monotonic() - started) * 1000)
    report = DueDiligenceReport(
        repo=repo,
        findings=tuple(final.get("findings") or []),
        summaries=final.get("summaries") or {},
        verdict=final.get("verdict", ""),
        tokens_used=final.get("tokens_used", 0),
        cost_usd=round(final.get("cost_usd", 0.0), 8),
        model_calls=final.get("model_calls", 0),
        duration_ms=duration_ms,
        errors=final.get("errors") or {},
    )
    record_run(
        repo=repo,
        findings=len(report.findings),
        cost_usd=report.cost_usd,
        duration_ms=duration_ms,
        failed_specialists=len(report.errors),
    )

    if learn and hub is not None and hub.enabled:
        # `success` is derived, not hardcoded: a run where specialists failed
        # or nothing was grounded should not reinforce the strategy that
        # produced it. Always passing success=True degenerates procedural
        # memory into "whichever strategy ran most often wins".
        success = not report.errors and any(f.is_grounded() for f in report.findings)
        hub.learn_from_report(
            report,
            context_key=_context_key({"repo_root": repo_root}),
            strategy=final.get("strategy") or pattern_name or DEFAULT_PATTERN,
            success=success,
        )
    return report
