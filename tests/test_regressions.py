"""Regression tests for bugs found in review.

Every test here corresponds to a defect that was actually in this codebase
and was caught by an adversarial read rather than by the existing suite.
Each one names the bug, because a fix without a test is just a bug waiting
to come back.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from atlas.api.app import create_app
from atlas.config import Settings
from atlas.domain.types import (
    Category,
    DueDiligenceReport,
    Evidence,
    Finding,
    Severity,
)
from atlas.evals.judge import (
    DeterministicJudge,
    ScoreBasedComparator,
    pairwise_with_position_control,
)
from atlas.evals.scoring import GroundTruthItem, RepoGroundTruth, score_report
from atlas.memory.hub import MemoryHub
from atlas.memory.measure import run_ab
from atlas.observability import metrics
from atlas.security import AuditLog, RateLimiter
from atlas.tools.repo import RepoToolkit


def _finding(rule: str, path: str, severity: Severity = Severity.HIGH) -> Finding:
    return Finding(
        finding_id=rule + path,
        category=Category.DELIVERY,
        severity=severity,
        title=rule,
        rule=rule,
        evidence=(Evidence(path=path, line=1),),
    )


# ---------------------------------------------------------------------------
# C1: path confinement used str.startswith, so a SIBLING directory whose name
# extended the root's name escaped the sandbox entirely.
# ---------------------------------------------------------------------------


def test_sibling_directory_with_shared_prefix_cannot_be_read(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.py").write_text("print('fine')")

    evil = tmp_path / "repo-evil"  # note: "repo-evil".startswith("repo")
    evil.mkdir()
    (evil / "leak.txt").write_text("SECRET=hunter2")

    toolkit = RepoToolkit(root)
    result = toolkit.call("read_file", {"path": "../repo-evil/leak.txt"})

    assert "SECRET" not in result
    assert result.startswith("ERROR")
    assert "escapes the repository root" in result


def test_absolute_and_dotdot_paths_still_rejected(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    toolkit = RepoToolkit(root)
    for candidate in ("/etc/passwd", "../../../etc/passwd", "../"):
        assert toolkit.call("read_file", {"path": candidate}).startswith("ERROR")


async def test_api_rejects_repo_names_that_escape_the_fixture_root(authenticator, auth_headers):
    """Defence in depth: even if the pydantic validator were relaxed, the
    path check must hold."""
    app = create_app(settings=Settings(provider="scripted"), authenticator=authenticator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            # Validator rejects traversal-shaped names before path resolution.
            resp = await c.post(
                "/v1/analyses", json={"repo": "../secrets"}, headers=auth_headers["analyst"]
            )
            assert resp.status_code == 422
            # A syntactically valid but non-existent repo is a clean 404.
            resp = await c.post(
                "/v1/analyses", json={"repo": "no-such-repo"}, headers=auth_headers["analyst"]
            )
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# C2: `lstrip("./")` stripped a character set, so ground truth pinned at the
# repository root ("no tests anywhere", path ".") could never match - and was
# counted as BOTH a false negative and a false positive.
# ---------------------------------------------------------------------------


def test_repo_root_ground_truth_matches():
    truth = RepoGroundTruth(repo="r", must_find=(GroundTruthItem(rule="no_tests", path="."),))
    report = DueDiligenceReport(repo="r", findings=(_finding("no_tests", "."),))
    card = score_report(report, truth)
    assert card.true_positives == 1
    assert card.false_negatives == 0
    assert card.false_positives == 0


def test_path_matching_is_not_bidirectional():
    """A bare filename must not match ground truth in a directory the agent
    never looked at - otherwise vague citations score as precise ones."""
    truth = RepoGroundTruth(
        repo="r", must_find=(GroundTruthItem(rule="bug", path="src/deep/nested/a.py"),)
    )
    report = DueDiligenceReport(repo="r", findings=(_finding("bug", "a.py"),))
    card = score_report(report, truth)
    assert card.true_positives == 0

    # The reverse direction is legitimate: a more-qualified reported path
    # matches a less-qualified expectation.
    truth2 = RepoGroundTruth(repo="r", must_find=(GroundTruthItem(rule="bug", path="a.py"),))
    report2 = DueDiligenceReport(repo="r", findings=(_finding("bug", "src/deep/a.py"),))
    assert score_report(report2, truth2).true_positives == 1


# ---------------------------------------------------------------------------
# C3: the memory A/B harness pre-trained on the very repositories it then
# scored, which is exactly the leak its own documentation claimed to avoid.
# ---------------------------------------------------------------------------


def test_ab_harness_never_warms_on_a_scored_repository():
    seen: list[tuple[str, int]] = []

    def runner(repo: str, hub: MemoryHub) -> DueDiligenceReport:
        seen.append((repo, len(hub.semantic)))
        return DueDiligenceReport(repo=repo, findings=(_finding("r", "a.py"),), cost_usd=0.01)

    def scorer(repo: str, report: DueDiligenceReport) -> float:
        return 1.0

    run_ab(["a", "b"], runner, scorer, warmup_repos=["a", "b"])

    # Cold arm: 2 runs. Warm arm: 2 runs (warmup skipped entirely because
    # both repos are scored). Six would mean the leak is back.
    assert len(seen) == 4
    # The first scored repo must see an empty store.
    warm_runs = seen[2:]
    assert warm_runs[0][1] == 0


def test_ab_scores_before_learning():
    """Within the warm arm, a repository must not benefit from itself."""
    store_sizes: list[int] = []

    def runner(repo: str, hub: MemoryHub) -> DueDiligenceReport:
        store_sizes.append(len(hub.semantic))
        return DueDiligenceReport(repo=repo, findings=(_finding("r", "a.py"),), cost_usd=0.01)

    run_ab(["a", "b"], runner, lambda r, rep: 1.0)
    warm = store_sizes[2:]
    assert warm[0] == 0  # first repo: nothing learned yet
    assert warm[1] > 0  # second repo: learned from the first


# ---------------------------------------------------------------------------
# C5: the graph recalled from memory but never wrote to it, so the hub stayed
# empty forever and "the agent improves" was decoration.
# ---------------------------------------------------------------------------


def test_graph_writes_back_to_memory(legacy_root):
    from atlas.evals.scenarios import model_for
    from atlas.graph.build import run_due_diligence

    hub = MemoryHub(enabled=True)
    assert hub.stats()["semantic"] == 0

    run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=model_for("legacy-billing"),
        memory=hub,
        pattern_name="react",
    )
    stats = hub.stats()
    assert stats["semantic"] > 0 and stats["episodic"] > 0 and stats["runs"] > 0


def test_graph_learns_under_the_repository_context_not_unknown(legacy_root):
    from atlas.evals.scenarios import model_for
    from atlas.graph.build import run_due_diligence

    hub = MemoryHub(enabled=True)
    run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=model_for("legacy-billing"),
        memory=hub,
        pattern_name="react",
    )

    assert hub.procedural.table("repo:standard")
    assert hub.procedural.table("repo:unknown") == []


def test_explicit_pattern_is_not_overridden_by_memory(legacy_root):
    """Procedural memory advises; it must never silently replace a caller's
    explicit choice."""
    from atlas.evals.scenarios import model_for
    from atlas.graph.build import run_due_diligence

    hub = MemoryHub(enabled=True)
    for _ in range(10):
        hub.procedural.record(context_key="repo:standard", strategy="rewoo", success=True)

    report = run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=model_for("legacy-billing"),
        memory=hub,
        pattern_name="reflexion",
    )
    # reflexion drops the duplicate SQL finding; rewoo would have missed
    # api.py entirely. Finding count distinguishes them.
    assert len(report.findings) >= 9


# ---------------------------------------------------------------------------
# M4: the audit chain detected edits and middle-deletions but NOT truncation
# of the tail, which is the easiest tampering of all.
# ---------------------------------------------------------------------------


def test_audit_detects_tail_truncation():
    log = AuditLog()
    for i in range(5):
        log.record(actor="a", action=f"action-{i}")
    assert log.verify()

    del log._entries[3:]  # noqa: SLF001 - simulating tampering
    assert not log.verify()


def test_audit_detects_middle_deletion():
    log = AuditLog()
    for i in range(5):
        log.record(actor="a", action=f"action-{i}")
    del log._entries[2]  # noqa: SLF001
    assert not log.verify()


# ---------------------------------------------------------------------------
# M9: check-then-spend is a TOCTOU race; concurrent requests all passed the
# same check and the daily budget overshot.
# ---------------------------------------------------------------------------


def test_spend_reservation_prevents_concurrent_overshoot():
    """Genuinely concurrent, because the bug being guarded against is a race.

    A sequential list comprehension exercises no interleaving at all and
    would pass against a completely unsynchronised check-then-act. Twenty
    threads released from one barrier will find the window if it exists.
    """
    limiter = RateLimiter(daily_spend_usd=1.0)
    workers = 20
    projected = 0.30
    barrier = threading.Barrier(workers)
    granted: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        ok = limiter.reserve_spend("p", projected).allowed
        with lock:
            granted.append(ok)

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(granted) == workers, "a worker deadlocked inside the limiter"
    # floor(1.00 / 0.30) == 3. Not "at most 3" - exactly 3, because granting
    # fewer would mean the limiter is losing capacity under contention.
    assert sum(granted) == 3, f"{sum(granted)} reservations granted against a $1.00 ceiling"
    assert limiter.spend_for("p") <= 1.0


def test_reservation_is_released_when_the_run_produces_nothing():
    limiter = RateLimiter(daily_spend_usd=1.0)
    assert limiter.reserve_spend("p", 0.9).allowed
    assert not limiter.reserve_spend("p", 0.5).allowed
    limiter.release_reservation("p", 0.9)
    assert limiter.reserve_spend("p", 0.5).allowed


def test_recorded_spend_replaces_its_reservation():
    limiter = RateLimiter(daily_spend_usd=1.0)
    limiter.reserve_spend("p", 0.5)
    limiter.record_spend("p", 0.02, reserved=0.5)
    # Only the real $0.02 should still be committed.
    assert limiter.spend_for("p") == pytest.approx(0.02)
    assert limiter.peek_spend("p", 0.9).allowed


# ---------------------------------------------------------------------------
# M10: histograms retained every raw observation forever.
# ---------------------------------------------------------------------------


def test_histograms_are_bounded_not_accumulating_samples():
    metrics.reset()
    for i in range(2000):
        metrics.record_run(
            repo="r", findings=1, cost_usd=0.001, duration_ms=i, failed_specialists=0
        )
    from atlas.observability.metrics import _HISTOGRAMS

    for entry in _HISTOGRAMS.values():
        # Fixed-size dict: buckets + sum + count. Never one entry per sample.
        assert len(entry) <= 10
        assert entry["count"] == 2000
    rendered = metrics.render()
    assert "atlas_run_duration_ms_bucket" in rendered
    assert 'le="+Inf"' in rendered
    metrics.reset()


# ---------------------------------------------------------------------------
# M1: the "position bias control" compared two different reports rather than
# swapping order, so order_agreement was True by construction.
# ---------------------------------------------------------------------------


def test_pairwise_control_reports_whether_bias_was_even_possible(legacy_root):
    good = DueDiligenceReport(repo="r", findings=(_finding("a", "src/db.py"),))
    bad = DueDiligenceReport(repo="r", findings=(_finding("a", "nope/missing.py"),))
    comparator = ScoreBasedComparator(DeterministicJudge())

    result = pairwise_with_position_control(comparator, ("good", good), ("bad", bad), legacy_root)
    assert result.winner == "good"
    assert result.order_agreement is True
    # The honest part: a scalar judge cannot exhibit position bias, and the
    # result says so instead of implying the control proved something.
    assert result.position_bias_possible is False


# ---------------------------------------------------------------------------
# C4: the stream endpoint awaited the whole run before emitting, so every SSE
# event arrived at the same instant. The original test asserted event order
# only - which passes identically against a non-streaming implementation.
# ---------------------------------------------------------------------------


async def test_stream_endpoint_delivers_the_first_event_before_the_run_ends(
    authenticator, auth_headers, legacy_root, monkeypatch
):
    """Awaiting the worker before draining the queue makes every event arrive
    in one burst at the end. It still passes any test that only checks event
    *order*, which is why the defect survived - and why the previous version
    of this test, which grepped `app.py` for the fixed line, was no better:
    it asserted the shape of the source, not the behaviour of the server.

    httpx's ASGI transport buffers the whole response, so it cannot see
    incremental delivery either. The only way to observe it is to drive the
    ASGI app directly and timestamp each `http.response.body` message.
    """
    from atlas.api import app as app_module

    real_stream = app_module.stream_due_diligence

    def slow_stream(*args, **kwargs):
        # Spacing the node updates in wall-clock time is what makes "burst at
        # the end" distinguishable from genuine incremental delivery. With a
        # scripted model the whole run finishes in milliseconds and both
        # behaviours look identical.
        for update in real_stream(*args, **kwargs):
            time.sleep(0.12)
            yield update

    monkeypatch.setattr(app_module, "stream_due_diligence", slow_stream)

    # No `repo_root=` here: Settings has no such field, and passing it was
    # silently dropped, so this test believed it had pointed the app at a
    # temp repository and had not. `Settings.__init__` now rejects unknown
    # keywords, which is how that surfaced.
    app = app_module.create_app(
        settings=Settings(provider="scripted"),
        authenticator=authenticator,
    )

    payload = json.dumps({"repo": "legacy-billing", "pattern": "react"}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/analyses/stream",
        "raw_path": b"/v1/analyses/stream",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"authorization", auth_headers["analyst"]["authorization"].encode()),
        ],
    }

    sent: list[tuple[float, dict]] = []
    # The body is delivered exactly once; every later call blocks forever the
    # way a real client connection does. Returning the payload repeatedly
    # makes the body-limit middleware count it repeatedly and abort the
    # request - which is what the first version of this test did.
    delivered = {"done": False}
    response_complete = asyncio.Event()

    async def receive():
        if not delivered["done"]:
            delivered["done"] = True
            return {"type": "http.request", "body": payload, "more_body": False}
        # Starlette's StreamingResponse runs a disconnect listener alongside
        # the body iterator and its task group will not exit until both
        # finish. Blocking here forever hangs the test; returning
        # http.disconnect immediately cancels the stream. So: wait for the
        # final body frame, then disconnect - which is what a real client
        # does.
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append((time.monotonic(), message))
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            response_complete.set()

    started = time.monotonic()
    await asyncio.wait_for(app(scope, receive, send), timeout=8)
    total = time.monotonic() - started

    status = next(m for _, m in sent if m["type"] == "http.response.start")
    assert status["status"] == 200

    chunks = [(ts, m) for ts, m in sent if m["type"] == "http.response.body" and m.get("body")]
    assert len(chunks) > 1, "a single body chunk means the response was buffered, not streamed"

    first_at = chunks[0][0] - started
    assert first_at < total * 0.6, (
        f"first event arrived at {first_at:.2f}s of a {total:.2f}s run - "
        "that is a burst at the end, not streaming"
    )


def test_graph_streams_per_node_updates(legacy_root):
    """The real guarantee: updates arrive node by node, not as one blob."""
    from atlas.evals.scenarios import model_for
    from atlas.graph.build import stream_due_diligence

    nodes = []
    for update in stream_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=model_for("legacy-billing"),
        pattern_name="react",
    ):
        nodes.extend(update.keys())

    assert nodes[0] == "plan"
    assert nodes[-1] == "synthesise"
    # Four specialists must each surface as their own update.
    assert {"security", "architecture", "dependency", "delivery"} <= set(nodes)


# ---------------------------------------------------------------------------
# C7: `parents[3]` path discovery resolved inside site-packages once the
# package was installed, so every analysis 404'd in the container.
# ---------------------------------------------------------------------------


def test_fixture_root_is_configurable(monkeypatch, tmp_path):
    (tmp_path / "repos" / "demo").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_FIXTURES_ROOT", str(tmp_path / "repos"))

    import importlib

    from atlas.api import app as app_module

    importlib.reload(app_module)
    try:
        assert app_module.FIXTURES == (tmp_path / "repos").resolve()
        assert (app_module.FIXTURES / "demo").is_dir()
    finally:
        monkeypatch.delenv("ATLAS_FIXTURES_ROOT", raising=False)
        importlib.reload(app_module)


# ---------------------------------------------------------------------------
# Regression from the M9 fix: reservations were taken before the request was
# validated, so 404 probes accumulated budget that was never released. ~100
# probes exhausted a principal's daily budget having spent nothing.
# ---------------------------------------------------------------------------


async def test_unknown_repo_does_not_consume_budget(authenticator, auth_headers):
    limiter = RateLimiter(daily_spend_usd=1.0)
    # $0.25 per-run ceiling: four reservations fit in the $1 daily cap, so
    # six unreleased 404 probes would exhaust it. That is the bug.
    app = create_app(
        settings=Settings(provider="scripted", max_cost_usd=0.25),
        authenticator=authenticator,
        rate_limiter=limiter,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            for _ in range(6):
                resp = await c.post(
                    "/v1/analyses",
                    json={"repo": "no-such-repo"},
                    headers=auth_headers["analyst"],
                )
                assert resp.status_code == 404

            # Six 404s at $0.25 projected each would have exhausted a $1 cap.
            after = await c.post(
                "/v1/analyses",
                json={"repo": "legacy-billing"},
                headers=auth_headers["analyst"],
            )
            assert after.status_code == 200


async def test_completed_run_releases_its_reservation(authenticator, auth_headers):
    limiter = RateLimiter(daily_spend_usd=1.0)
    app = create_app(
        settings=Settings(provider="scripted", max_cost_usd=0.25),
        authenticator=authenticator,
        rate_limiter=limiter,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            for _ in range(5):
                resp = await c.post(
                    "/v1/analyses",
                    json={"repo": "legacy-billing"},
                    headers=auth_headers["analyst"],
                )
                assert resp.status_code == 200
            # Real spend is pennies; only the reservations could exhaust $1.
            assert limiter.spend_for("apikey:analyst") < 0.5


# ---------------------------------------------------------------------------
# Regression from the C5 fix: eval/benchmark runs learned internally AND via
# the harness, doubling every memory record and skewing recency decay.
# ---------------------------------------------------------------------------


def test_eval_runs_do_not_write_memory_twice():
    from atlas.evals.runner import run_repo

    hub = MemoryHub(enabled=True)
    run_repo("legacy-billing", pattern="react", memory=hub)
    # run_repo defaults to learn=False; the harness owns learning order.
    assert hub.stats()["runs"] == 0

    hub2 = MemoryHub(enabled=True)
    run_repo("legacy-billing", pattern="react", memory=hub2, learn=True)
    assert hub2.stats()["runs"] == 1


# ---------------------------------------------------------------------------
# Procedural memory must be able to advise when the caller has no preference,
# and must never override an explicit one.
# ---------------------------------------------------------------------------


def test_memory_can_advise_a_pattern_when_none_is_requested(legacy_root):
    from atlas.evals.scenarios import model_for
    from atlas.graph.build import _ledger, build_graph

    hub = MemoryHub(enabled=True)
    for _ in range(10):
        hub.procedural.record(context_key="repo:standard", strategy="rewoo", success=True)

    app = build_graph(model=model_for("legacy-billing"), memory=hub, pattern_name=None)
    run_id = "advise-test"
    _ledger.begin(run_id)
    try:
        state = app.invoke(
            {
                "repo": "legacy-billing",
                "repo_root": str(legacy_root),
                "run_id": run_id,
                "requested_categories": ["security"],
                "findings": [],
                "results": [],
                "summaries": {},
                "errors": {},
                "tokens_used": 0,
                "cost_usd": 0.0,
                "model_calls": 0,
                "steps": 0,
            },
            config={"configurable": {"thread_id": "advise-test"}},
        )
    finally:
        _ledger.reset(run_id)
    assert state["strategy"] == "rewoo"  # memory advised


def test_explicit_request_still_beats_memory(legacy_root):
    from atlas.evals.scenarios import model_for
    from atlas.graph.build import _ledger, build_graph

    hub = MemoryHub(enabled=True)
    for _ in range(10):
        hub.procedural.record(context_key="repo:standard", strategy="rewoo", success=True)

    app = build_graph(model=model_for("legacy-billing"), memory=hub, pattern_name="reflexion")
    run_id = "explicit-test"
    _ledger.begin(run_id)
    try:
        state = app.invoke(
            {
                "repo": "legacy-billing",
                "repo_root": str(legacy_root),
                "run_id": run_id,
                "requested_categories": ["security"],
                "findings": [],
                "results": [],
                "summaries": {},
                "errors": {},
                "tokens_used": 0,
                "cost_usd": 0.0,
                "model_calls": 0,
                "steps": 0,
            },
            config={"configurable": {"thread_id": "explicit-test"}},
        )
    finally:
        _ledger.reset(run_id)
    assert state["strategy"] == "reflexion"


def test_path_suffix_matching_respects_boundaries():
    """ "xa.py" must not match ground truth "a.py"."""
    truth = RepoGroundTruth(repo="r", must_find=(GroundTruthItem(rule="bug", path="a.py"),))
    report = DueDiligenceReport(repo="r", findings=(_finding("bug", "xa.py"),))
    assert score_report(report, truth).true_positives == 0


# ---------------------------------------------------------------------------
# Round 3: reserving the per-run ceiling created an invariant nobody stated.
# With the defaults (max_cost_usd $5.00, daily cap $1.00 in a test) every
# single request 429'd having spent nothing, and the message blamed the
# daily spend limit. Fail at startup instead.
# ---------------------------------------------------------------------------


def test_budget_config_that_would_reject_everything_fails_at_startup(authenticator):
    from atlas.api.app import BudgetConfigurationError

    with pytest.raises(BudgetConfigurationError) as excinfo:
        create_app(
            settings=Settings(provider="scripted", max_cost_usd=5.0),
            authenticator=authenticator,
            rate_limiter=RateLimiter(daily_spend_usd=1.0),
        )
    message = str(excinfo.value)
    # The error has to name both numbers and the fix; "invalid config" sends
    # the reader back into the source.
    assert "5.00" in message and "1.00" in message
    assert "ATLAS_DAILY_SPEND_USD" in message


def test_coherent_budget_starts_normally(authenticator):
    app = create_app(
        settings=Settings(provider="scripted", max_cost_usd=0.25),
        authenticator=authenticator,
        rate_limiter=RateLimiter(daily_spend_usd=1.0),
    )
    assert app is not None


# ---------------------------------------------------------------------------
# Round 3: `strict_routes` existed, was documented, and could never fire -
# it was checked after the default-route fallback, and every scenario defines
# a default route. Enabling it changed nothing and no test noticed.
# ---------------------------------------------------------------------------


def test_strict_routes_actually_raises_instead_of_falling_back():
    from langchain_core.messages import HumanMessage

    from atlas.llm.scripted import DEFAULT_ROUTE, ScriptedChatModel, ScriptedError, ScriptedTurn

    routes = {
        "security specialist": [ScriptedTurn(content="scripted answer")],
        DEFAULT_ROUTE: [ScriptedTurn(content="fallback answer")],
    }

    permissive = ScriptedChatModel(routes={k: list(v) for k, v in routes.items()})
    assert "fallback" in permissive.invoke([HumanMessage(content="unmatched prompt")]).content

    strict = ScriptedChatModel(routes={k: list(v) for k, v in routes.items()}, strict_routes=True)
    with pytest.raises(ScriptedError) as excinfo:
        strict.invoke([HumanMessage(content="unmatched prompt")])
    # The default route must NOT be offered as an available option - listing
    # it would suggest the fallback was a legitimate outcome.
    assert DEFAULT_ROUTE not in str(excinfo.value)
    assert "security specialist" in str(excinfo.value)


def test_eval_model_is_strict_and_never_needs_the_fallback():
    """Measured, not assumed: across every fixture repo and every pattern,
    zero model calls fall through to the default route. That is what makes
    strictness safe to enable - and what makes the eval numbers a
    measurement of the scenarios rather than of the fallback."""
    from atlas.evals.runner import run_repo
    from atlas.evals.scenarios import fixture_repos, model_for

    assert model_for(fixture_repos()[0]).strict_routes is True

    for repo in fixture_repos():
        for pattern in ("react", "plan_execute", "reflexion", "rewoo"):
            report = run_repo(repo, pattern=pattern)
            # A strict model raises on an unmatched prompt, so the specialist
            # would land in report.errors rather than producing a summary.
            assert not report.errors, f"{repo}/{pattern} hit an unscripted prompt: {report.errors}"


# ---------------------------------------------------------------------------
# Round 3: the judge was computed, rendered in the results table, and gated
# on by nothing. Four criteria of "report quality" that ground truth cannot
# see, and a run could regress all of them to the floor while the gate said
# PASS. A metric that cannot fail the build is a column, not a control.
# ---------------------------------------------------------------------------


def test_severity_inflation_is_invisible_to_ground_truth_but_caught_by_the_judge():
    """The exact failure the judge exists for: relabel every finding
    "critical" and precision, recall and F1 are bit-identical, because
    severity is not part of the match. Only prioritisation moves."""
    from atlas.evals.judge import DeterministicJudge
    from atlas.evals.runner import FIXTURES, GATES, GROUND_TRUTH, run_repo
    from atlas.evals.scoring import RepoGroundTruth, load_ground_truth, score_report

    report = run_repo("legacy-billing", pattern="react")
    truth = load_ground_truth(GROUND_TRUTH).get(
        "legacy-billing", RepoGroundTruth(repo="legacy-billing")
    )
    inflated = report.model_copy(
        update={
            "findings": tuple(
                f.model_copy(update={"severity": Severity.CRITICAL}) for f in report.findings
            )
        }
    )

    honest_card = score_report(report, truth)
    inflated_card = score_report(inflated, truth)
    assert (honest_card.precision, honest_card.recall, honest_card.f1) == (
        inflated_card.precision,
        inflated_card.recall,
        inflated_card.f1,
    ), "ground truth should be blind to severity - that is why the judge is needed"

    judge = DeterministicJudge()
    root = FIXTURES / "legacy-billing"
    assert judge.score(report, root).prioritisation >= GATES["min_judge_prioritisation"]
    assert judge.score(inflated, root).prioritisation < GATES["min_judge_prioritisation"]


def test_judge_dimensions_are_wired_into_the_gate_not_just_the_table():
    from atlas.evals.runner import GATES, evaluate

    # Not `"min_judge_prioritisation" in GATES` - a dict-key check is a
    # tautology that passes against a threshold of 0.0. Assert the gate
    # actually rejects a report that violates it.
    assert GATES["min_judge_prioritisation"] >= 4.0
    assert GATES["min_judge_actionability"] >= 4.0

    run = evaluate(tier="standard", pattern="react")
    assert run.passed and not run.failures
    # Every scored result carries the full breakdown, not only `overall` -
    # gating on a mean would let one dimension collapse unnoticed. And it
    # must CLEAR the gate, not merely be non-zero: `assert x.prioritisation`
    # passes at 0.001.
    for result in run.results:
        if not result.findings:
            continue
        assert result.judge.prioritisation >= GATES["min_judge_prioritisation"]
        assert result.judge.actionability >= GATES["min_judge_actionability"]


def test_empty_report_on_the_clean_repo_is_not_punished_by_the_judge_gate():
    """Severity distribution and remediation quality are undefined with zero
    findings. Gating them would fail the one run that is correct to be
    empty - reflexion on the clean fixture, which drops the trap."""
    from atlas.evals.runner import evaluate

    run = evaluate(tier="standard", pattern="reflexion")
    empty = [r for r in run.results if r.findings == 0]
    assert empty, "expected reflexion to produce an empty report on the clean repo"
    assert run.passed, run.failures


# ---------------------------------------------------------------------------
# Round 4 (blind review): the Round-2 fix for "reservations taken before
# validation" moved the WHOLE of enforce_limits behind resolve_repo - and
# took the request-rate bucket with it. Every request that did not resolve
# to a valid repo became completely unmetered.
# ---------------------------------------------------------------------------


async def test_invalid_requests_still_consume_a_rate_token(authenticator, auth_headers):
    """Rate limiting protects the server, so it must apply to requests that
    turn out to be invalid - those are exactly the ones an attacker sends.

    Measured before the fix: 50 unknown-repo POSTs against a bucket sized
    for 1 returned 50x 404 and zero 429s, each still paying for auth,
    validation, path resolution and an audit write.
    """
    limiter = RateLimiter(requests_per_minute=1, burst=1, daily_spend_usd=25.0)
    app = create_app(
        settings=Settings(provider="scripted", max_cost_usd=0.25),
        authenticator=authenticator,
        rate_limiter=limiter,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            statuses = [
                (
                    await c.post(
                        "/v1/analyses",
                        json={"repo": "no-such-repo"},
                        headers=auth_headers["analyst"],
                    )
                ).status_code
                for _ in range(8)
            ]

    assert 429 in statuses, f"unknown-repo probes were never rate limited: {statuses}"
    assert statuses.count(429) >= 6, statuses
    # ...and the budget is still untouched, which is the Round-2 property
    # this fix had to preserve rather than trade away.
    assert limiter.spend_for("apikey:analyst") == pytest.approx(0.0)


async def test_malformed_requests_also_consume_a_rate_token(authenticator, auth_headers):
    """422s are cheaper to generate than 404s, so they are the better DoS."""
    limiter = RateLimiter(requests_per_minute=1, burst=1, daily_spend_usd=25.0)
    app = create_app(
        settings=Settings(provider="scripted", max_cost_usd=0.25),
        authenticator=authenticator,
        rate_limiter=limiter,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            statuses = [
                (
                    await c.post(
                        "/v1/analyses",
                        json={"repo": "legacy-billing", "pattern": "not-a-pattern"},
                        headers=auth_headers["analyst"],
                    )
                ).status_code
                for _ in range(8)
            ]
    assert 429 in statuses, f"invalid-pattern requests were never rate limited: {statuses}"


# ---------------------------------------------------------------------------
# Round 4: the stream reserved budget INSIDE the generator. By then Starlette
# has sent http.response.start, so the 429 could not become a response - the
# client got a truncated 200 and the server logged
# `RuntimeError: Caught handled exception, but response already started`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["/v1/analyses", "/v1/analyses/stream"])
async def test_over_budget_returns_429_on_both_routes(authenticator, auth_headers, route):
    """Both routes, same parametrised test, deliberately: the defect was that
    one route behaved correctly and the other did not, and no test compared
    them."""
    limiter = RateLimiter(daily_spend_usd=1.0)
    app = create_app(
        settings=Settings(provider="scripted", max_cost_usd=0.25),
        authenticator=authenticator,
        rate_limiter=limiter,
    )
    limiter.reserve_spend("apikey:analyst", 0.95)  # nearly exhausted

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            resp = await c.post(
                route, json={"repo": "legacy-billing"}, headers=auth_headers["analyst"]
            )
    assert resp.status_code == 429, f"{route} returned {resp.status_code}"


async def test_stream_still_releases_its_reservation_after_completing(authenticator, auth_headers):
    """Moving the reservation into the handler must not resurrect the leak it
    was originally moved to avoid."""
    limiter = RateLimiter(daily_spend_usd=1.0)
    app = create_app(
        settings=Settings(provider="scripted", max_cost_usd=0.25),
        authenticator=authenticator,
        rate_limiter=limiter,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            for _ in range(4):
                resp = await c.post(
                    "/v1/analyses/stream",
                    json={"repo": "legacy-billing"},
                    headers=auth_headers["analyst"],
                )
                assert resp.status_code == 200
    # Four runs at a $0.25 reservation each would sit at $1.00 if nothing
    # were released. Real spend is pennies.
    assert limiter.spend_for("apikey:analyst") < 0.5


def test_daily_spend_and_rate_limits_are_actually_configurable(monkeypatch):
    """The startup error told operators to raise ATLAS_DAILY_SPEND_USD. The
    variable did not exist: RateLimiter() was constructed bare, so its
    defaults were unreachable from config, .env.example and every manifest."""
    monkeypatch.setenv("ATLAS_DAILY_SPEND_USD", "3.5")
    monkeypatch.setenv("ATLAS_REQUESTS_PER_MINUTE", "7")
    cfg = Settings()
    assert cfg.daily_spend_usd == 3.5
    assert cfg.requests_per_minute == 7

    app = create_app(settings=Settings(provider="scripted", max_cost_usd=0.25))
    limiter = app.state.rate_limiter
    assert limiter.daily_spend_usd == 3.5


def test_settings_rejects_unknown_keywords():
    """`extra="ignore"` is right for the environment and wrong for explicit
    keywords: `Settings(repo_root=...)` in a test was dropped without a
    word, so the test pointed the app somewhere it did not intend."""
    with pytest.raises(TypeError) as excinfo:
        Settings(provider="scripted", repo_root="/tmp/nope")
    assert "repo_root" in str(excinfo.value)


def test_settings_still_ignores_unrelated_environment_variables(monkeypatch):
    """The environment is not under the caller's control, so an unrecognised
    ATLAS_-prefixed variable must not crash startup."""
    monkeypatch.setenv("ATLAS_SOMETHING_FROM_THE_FUTURE", "1")
    assert Settings(provider="scripted").provider == "scripted"
