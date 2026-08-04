"""The runtime brakes - each of which was decorative until it was measured.

Every test here failed against the original implementation. That is the bar
for a test in this file: if it would have passed before the fix, it does not
belong.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import pytest

from atlas.config import Settings
from atlas.evals.scenarios import model_for
from atlas.graph.build import _ledger, run_due_diligence
from atlas.llm.scripted import ScriptedChatModel
from atlas.tools.repo import (
    GREP_DEADLINE_S,
    MAX_READ_LINES,
    RepoToolkit,
    find_ambiguous_quantifier,
)


class _HangingModel(ScriptedChatModel):
    """Blocks inside one specialist, like a wedged network call."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = "\n".join(str(m.content) for m in messages).lower()
        if "security specialist" in text:
            time.sleep(20)
        return super()._generate(messages, stop, run_manager, **kwargs)


# ---------------------------------------------------------------------------
# Specialist timeout
# ---------------------------------------------------------------------------


def test_timeout_bounds_wall_clock_not_just_the_error_message(legacy_root):
    """The original used `with ThreadPoolExecutor(...)`, whose `__exit__`
    calls shutdown(wait=True) - so raising the timeout inside the block
    joined the very worker it had given up on. A 20s hang with a 2s timeout
    took 20s and *reported* "exceeded 2.0s".

    Asserting on the error message alone would have passed. Only the clock
    catches it.
    """
    base = model_for("legacy-billing")
    started = time.monotonic()
    report = run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=_HangingModel(routes=base.routes),
        settings=Settings(provider="scripted", specialist_timeout_s=2.0),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 8.0, f"timeout did not bound the run: {elapsed:.1f}s for a 20s hang"
    assert "security" in report.errors
    assert "exceeded" in report.errors["security"]


def test_other_specialists_still_contribute_when_one_hangs(legacy_root):
    base = model_for("legacy-billing")
    report = run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=_HangingModel(routes=base.routes),
        settings=Settings(provider="scripted", specialist_timeout_s=2.0),
    )
    contributing = {f.category.value for f in report.findings}
    assert contributing, "a single hung specialist must not empty the report"
    assert "security" not in contributing


# ---------------------------------------------------------------------------
# Run cost ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ceiling", "expect_findings", "expect_errors"),
    [
        (5.0, True, False),  # generous ceiling: full run
        (0.0001, False, True),  # ceiling below the first call: nothing runs
    ],
)
def test_cost_ceiling_actually_halts_work(legacy_root, ceiling, expect_findings, expect_errors):
    """Checked at fan-out, this could never fire: all four specialists start
    together and each reads cost 0.0. The brake has to sit immediately
    before the next thing that spends money."""
    report = run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=model_for("legacy-billing"),
        settings=Settings(provider="scripted", max_cost_usd=ceiling),
    )
    assert bool(report.findings) is expect_findings
    assert bool(report.errors) is expect_errors


def test_tighter_ceiling_costs_strictly_less(legacy_root):
    def cost_at(ceiling: float) -> float:
        return run_due_diligence(
            repo="legacy-billing",
            repo_root=str(legacy_root),
            model=model_for("legacy-billing"),
            settings=Settings(provider="scripted", max_cost_usd=ceiling),
        ).cost_usd

    assert cost_at(0.01) < cost_at(5.0)


# ---------------------------------------------------------------------------
# Tool confinement - every tool, not just read_file
# ---------------------------------------------------------------------------


@pytest.fixture
def symlink_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ok.py").write_text("print('fine')")
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("TOP SECRET DATA")
    try:
        os.symlink(secret, root / "link.txt")
    except (OSError, NotImplementedError):  # pragma: no cover - platform
        pytest.skip("symlinks unavailable")
    return root


def test_symlink_escape_blocked_on_every_tool(symlink_repo):
    """`read_file` was confined; `grep`, `list_files` and
    `dependency_manifest` walked and read directly. A symlink inside the repo
    therefore leaked anything on disk through grep while being rejected
    through read_file."""
    toolkit = RepoToolkit(symlink_repo)

    assert "TOP SECRET" not in toolkit.call("read_file", {"path": "link.txt"})
    assert "TOP SECRET" not in toolkit.call("grep", {"pattern": "SECRET"})
    assert "link.txt" not in toolkit.call("list_files", {})


def test_legitimate_files_still_readable(symlink_repo):
    """Confinement that also blocks valid reads is not a fix."""
    toolkit = RepoToolkit(symlink_repo)
    assert "print" in toolkit.call("read_file", {"path": "ok.py"})
    assert "ok.py" in toolkit.call("grep", {"pattern": "print"})


# ---------------------------------------------------------------------------
# ReDoS
# ---------------------------------------------------------------------------


def test_nested_quantifier_rejected_before_execution(toolkit):
    result = toolkit.call("grep", {"pattern": "(a+)+$"})
    assert result.startswith("ERROR")
    assert "nested quantifier" in result


def test_the_deadline_bounds_total_work_across_a_large_tree(tmp_path):
    """What the wall-clock deadline actually guarantees - and only this.

    The previous version of this test ran `(a|a|aa|aaa)+b`, which the static
    guard rejects in microseconds, and then asserted a 5-second bound. It
    was asserting a generous bound against an error path: it passed against
    an implementation with no deadline at all, and it did not catch the
    `{n,m}` guard bypass.

    The deadline cannot interrupt one running `regex.search()` - nothing in
    Python can. What it does bound is cumulative work across many lines and
    files, which is the other way a model-chosen pattern burns a specialist.
    So that is what is measured here, with a pattern the guard *allows*.
    """
    root = tmp_path / "repo"
    root.mkdir()
    slow_line = ("qwertyuiopasdfghjklzxcvbnm" * 30) + "\n"
    for n in range(40):
        with (root / f"file{n}.txt").open("w") as handle:
            handle.writelines(slow_line for _ in range(20_000))

    pattern = r"a.*b.*c.*d.*e.*f.*zzz"
    assert find_ambiguous_quantifier(pattern) is None, "the guard must allow this pattern"

    toolkit = RepoToolkit(root)
    started = time.monotonic()
    result = toolkit.call("grep", {"pattern": pattern})
    elapsed = time.monotonic() - started

    assert elapsed < GREP_DEADLINE_S + 1.5, (
        f"grep ran {elapsed:.1f}s against a {GREP_DEADLINE_S}s deadline"
    )
    assert "search stopped" in result or "stopped after" in result or "no matches" in result


def test_overlong_pattern_rejected(toolkit):
    assert toolkit.call("grep", {"pattern": "a" * 500}).startswith("ERROR")


# ---------------------------------------------------------------------------
# read_file streams rather than materialising
# ---------------------------------------------------------------------------


def test_read_file_does_not_load_the_whole_file(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    big = root / "huge.txt"
    with big.open("w") as handle:
        for i in range(200_000):
            handle.write(f"line {i}\n")

    toolkit = RepoToolkit(root)
    started = time.monotonic()
    out = toolkit.call("read_file", {"path": "huge.txt", "max_lines": 5})
    elapsed = time.monotonic() - started

    assert out.count("\n") <= 5
    # Streaming to line 5 of a 200k-line file should be near-instant; reading
    # the whole file first would not be.
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# Round 4: bounding the *scan* is not bounding the *memory*.
# ---------------------------------------------------------------------------


@pytest.fixture
def big_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with (root / "huge.txt").open("w") as handle:
        for i in range(400_000):
            handle.write(f"line {i} of a very large generated lockfile\n")
    return root


def _peak_mb() -> int:
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def test_read_file_window_is_capped_regardless_of_max_lines(big_repo):
    """`max_lines` comes from the model. Uncapped, one call with
    max_lines=100_000_000 held every line of an 88MB file in a list to
    return 8_000 characters - 665MB allocated for 8KB of output. Streaming
    the file was only half the fix."""
    toolkit = RepoToolkit(big_repo)
    before = _peak_mb()
    out = toolkit.call("read_file", {"path": "huge.txt", "max_lines": 100_000_000})
    growth = _peak_mb() - before

    assert len(out) <= 8_000
    assert out.count("\n") <= MAX_READ_LINES
    assert growth < 60, f"read_file grew RSS by {growth}MB to return {len(out)} chars"


def test_grep_streams_instead_of_reading_whole_files(big_repo):
    """MAX_LINES_SCANNED bounded the scan while `read_text()` before it was
    unbounded - the same 88MB file cost 368MB of RSS to look at its first
    200k lines."""
    toolkit = RepoToolkit(big_repo)
    before = _peak_mb()
    toolkit.call("grep", {"pattern": "line 399999"})
    growth = _peak_mb() - before
    assert growth < 60, f"grep grew RSS by {growth}MB"


# ---------------------------------------------------------------------------
# Round 4: the cost ledger was a module global keyed by REPOSITORY NAME, while
# the comment above it claimed "a run-scoped ledger keyed by thread_id". Two
# concurrent analyses of the same repo - two principals, one POST each - are
# trivially reachable, and both failure directions were live.
# ---------------------------------------------------------------------------


def test_concurrent_runs_of_the_same_repo_do_not_share_a_budget(legacy_root):
    """Measured before the fix: run B was halted by run A's spend (pooled
    ceiling), and either run's cleanup wiped the other's four in-flight
    subtotals, under-counting its ceiling by up to 4x."""
    results: dict[int, Any] = {}
    barrier = threading.Barrier(2)

    def analyse(index: int) -> None:
        barrier.wait()
        results[index] = run_due_diligence(
            repo="legacy-billing",
            repo_root=str(legacy_root),
            model=model_for("legacy-billing"),
            settings=Settings(provider="scripted", max_cost_usd=5.0),
        )

    threads = [threading.Thread(target=analyse, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == 2, "a concurrent run did not finish"
    for index, report in results.items():
        assert not report.errors, f"run {index} was halted by the other run: {report.errors}"
        assert report.findings, f"run {index} produced nothing"
    # Identical inputs, identical outputs - no cross-run interference at all.
    assert len(results[0].findings) == len(results[1].findings)


def test_the_ledger_does_not_leak_keys_across_runs(legacy_root):
    """Per-run keys accumulate for the process lifetime unless they are
    dropped when the run ends."""
    before = len(_ledger._by_specialist)
    for _ in range(3):
        run_due_diligence(
            repo="legacy-billing",
            repo_root=str(legacy_root),
            model=model_for("legacy-billing"),
            settings=Settings(provider="scripted"),
        )
    assert len(_ledger._by_specialist) == before, "ledger keys leaked after the run finished"


# ---------------------------------------------------------------------------
# Round 5: no syntactic guard over quantified GROUPS can be complete, because
# the counterexample has no group. `a*a*...*b` is exponential against 60
# characters and is, syntactically, entirely reasonable.
# ---------------------------------------------------------------------------


def test_a_group_free_exponential_pattern_is_still_bounded(tmp_path):
    """The guard accepts this pattern - correctly, there is nothing wrong
    with it - and it backtracks exponentially anyway. Before grep ran in a
    killable subprocess this hung a 40-second probe with no output."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bait.txt").write_text("a" * 60 + "!\n")

    pattern = "a*a*a*a*a*a*a*a*a*a*b"
    assert find_ambiguous_quantifier(pattern) is None, (
        "this test is only meaningful for a pattern the static guard allows"
    )

    started = time.monotonic()
    result = RepoToolkit(root).call("grep", {"pattern": pattern})
    elapsed = time.monotonic() - started

    assert elapsed < GREP_DEADLINE_S + 3.0, f"grep ran {elapsed:.1f}s - it was not killed"
    assert "exceeded" in result and "terminated" in result


def test_the_subprocess_boundary_does_not_break_ordinary_greps(toolkit):
    """A guarantee that costs correctness is not a guarantee. Same results
    through the process boundary as without it."""
    direct = toolkit._scan([str(p) for p in toolkit._walk()], "def ", "*")
    through = toolkit.call("grep", {"pattern": "def ", "glob": "*"})
    assert through == direct
    assert "src/" in through


def test_a_killed_grep_does_not_take_the_specialist_with_it(tmp_path):
    """The child dies; the toolkit keeps working."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bait.txt").write_text("a" * 60 + "!\n")
    (root / "ok.py").write_text("import os\n")
    toolkit = RepoToolkit(root)

    assert "exceeded" in toolkit.call("grep", {"pattern": "a*a*a*a*a*a*a*a*a*a*b"})
    # Still healthy afterwards.
    assert "ok.py" in toolkit.call("grep", {"pattern": "import os"})
    assert "import os" in toolkit.call("read_file", {"path": "ok.py"})
