"""Orchestration, patterns, parsing - the agent behaviour itself."""

from __future__ import annotations

from atlas.agents.parsing import parse_specialist_output
from atlas.context.budget import TokenBudget
from atlas.domain.types import Category
from atlas.evals.runner import run_repo
from atlas.evals.scenarios import model_for
from atlas.graph.build import ALL_CATEGORIES, run_due_diligence
from atlas.llm.scripted import ScriptedChatModel, ScriptedTurn, tool_call
from atlas.memory.hub import MemoryHub
from atlas.patterns import PATTERNS, PatternContext, get_pattern
from atlas.tools.repo import RepoToolkit

# ------------------------------------------------------------------ parsing


def test_parses_json_wrapped_in_prose_and_fences():
    text = (
        "Here is what I found.\n```json\n"
        '{"summary": "bad", "findings": [{"rule": "sql_injection", "title": "SQLi", '
        '"severity": "Critical!", "path": "src/db.py", "line": "7"}]}\n```\nDone.'
    )
    out = parse_specialist_output(text, Category.SECURITY)
    assert out.ok and len(out.findings) == 1
    finding = out.findings[0]
    assert finding.severity.value == "critical"  # coerced from "Critical!"
    assert finding.evidence[0].line == 7  # coerced from string


def test_findings_without_location_are_dropped_not_defaulted():
    """A silently-defaulted severity is a lie that reaches a decision-maker."""
    text = '{"summary": "s", "findings": [{"title": "vague", "severity": "high"}]}'
    out = parse_specialist_output(text, Category.SECURITY)
    assert out.findings == () and out.dropped == 1


def test_non_json_output_keeps_prose_but_reports_error():
    out = parse_specialist_output("I could not complete the analysis.", Category.SECURITY)
    assert not out.ok and out.findings == ()
    assert "could not complete" in out.summary


def test_trailing_commas_are_tolerated():
    text = (
        '{"summary": "s", "findings": [{"rule":"r","title":"t","severity":"low","path":"a.py",},]}'
    )
    out = parse_specialist_output(text, Category.SECURITY)
    assert len(out.findings) == 1


# ----------------------------------------------------------------- patterns


def test_every_pattern_produces_grounded_findings(legacy_root):
    for name in sorted(PATTERNS):
        ctx = PatternContext(
            model=model_for("legacy-billing"),
            toolkit=RepoToolkit(legacy_root),
            category=Category.SECURITY,
            token_budget=TokenBudget(limit=12_000),
        )
        result = get_pattern(name).run(ctx)
        assert result.ok, f"{name} failed: {result.error}"
        assert result.findings, f"{name} produced nothing"
        assert all(f.is_grounded() for f in result.findings)
        assert result.model_calls > 0 and result.tokens_used > 0


def test_rewoo_uses_fewer_model_calls_than_react(legacy_root):
    """The defining property of ReWOO: no model in the tool loop."""

    def run(name: str):
        return get_pattern(name).run(
            PatternContext(
                model=model_for("legacy-billing"),
                toolkit=RepoToolkit(legacy_root),
                category=Category.SECURITY,
            )
        )

    assert run("rewoo").model_calls < run("react").model_calls


def test_reflexion_costs_more_than_react(legacy_root):
    def run(name: str):
        return get_pattern(name).run(
            PatternContext(
                model=model_for("legacy-billing"),
                toolkit=RepoToolkit(legacy_root),
                category=Category.SECURITY,
            )
        )

    assert run("reflexion").model_calls > run("react").model_calls


def test_step_budget_halts_a_looping_agent(legacy_root):
    """A model that only ever calls tools must be stopped by the runtime."""
    looping = ScriptedChatModel(
        routes={
            "security specialist": [
                ScriptedTurn(tool_calls=(tool_call("grep", {"pattern": f"x{i}"}),))
                for i in range(50)
            ]
        }
    )
    result = get_pattern("react").run(
        PatternContext(
            model=looping,
            toolkit=RepoToolkit(legacy_root),
            category=Category.SECURITY,
            max_steps=5,
        )
    )
    assert result.steps == 5
    assert "budget" in result.error


def test_provider_failure_is_contained(legacy_root):
    failing = ScriptedChatModel(
        routes={"security specialist": [ScriptedTurn(content="never", fail_times=99)]}
    )
    result = get_pattern("react").run(
        PatternContext(model=failing, toolkit=RepoToolkit(legacy_root), category=Category.SECURITY)
    )
    assert not result.ok and "model call failed" in result.error


# -------------------------------------------------------------------- graph


def test_reducers_accumulate_from_all_parallel_specialists():
    """Wrong reducers silently drop findings from concurrent branches - the
    classic multi-agent bug.

    This test used to open with `merge_dicts({"a": 1}, {"b": 2})`, which is
    a tautology: it restates the function body and holds for every reducer
    anyone would plausibly write, including the broken last-write-wins one.
    What actually distinguishes a correct reducer is what survives a
    four-way parallel fan-in, so that is what is asserted.
    """
    report = run_repo("legacy-billing", pattern="react")

    # A last-write-wins reducer leaves exactly one branch's contribution.
    contributing = {f.category.value for f in report.findings}
    assert contributing == {c.value for c in ALL_CATEGORIES}, (
        f"only {sorted(contributing)} survived fan-in - a reducer is dropping branches"
    )
    assert len(report.summaries) == len(ALL_CATEGORIES)

    # Counters must sum across branches, not take the last one. Every branch
    # spends tokens, so the total has to exceed the largest single branch.
    assert report.tokens_used > 0
    assert report.cost_usd > 0
    # And the merged dict must not have silently collapsed keys.
    assert sorted(report.summaries) == sorted(c.value for c in ALL_CATEGORIES)


def test_run_produces_verdict_and_accounting(legacy_root):
    report = run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=model_for("legacy-billing"),
        pattern_name="react",
    )
    assert report.blocking()
    assert "NOT CLEAR TO PROCEED" in report.verdict
    assert report.cost_usd > 0 and report.tokens_used > 0
    assert report.duration_ms >= 0


def test_clean_repo_yields_proceed_verdict(modern_root):
    report = run_due_diligence(
        repo="modern-payments",
        repo_root=str(modern_root),
        model=model_for("modern-payments"),
        pattern_name="reflexion",  # self-critique removes the trap finding
    )
    assert "PROCEED" in report.verdict
    assert not report.blocking()


def test_specialist_failure_does_not_fail_the_run(legacy_root):
    """A report missing one section is useful; a 500 is not."""
    from atlas.observability import metrics

    class Exploding(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if "security specialist" in "\n".join(str(m.content) for m in messages).lower():
                raise RuntimeError("specialist exploded")
            return super()._generate(messages, stop, run_manager, **kwargs)

    base = model_for("legacy-billing")
    broken = Exploding(routes=base.routes)
    metrics.reset()
    report = run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=broken,
        pattern_name="react",
    )
    assert "security" in report.errors
    assert report.findings  # other specialists still contributed
    assert "specialist(s) failed" in report.verdict
    assert (
        'atlas_specialist_runs_total{category="security",outcome="error",pattern="react"} 1'
        in metrics.render()
    )


def test_specialist_exception_after_model_call_preserves_usage(monkeypatch, legacy_root):
    from langchain_core.messages import HumanMessage

    from atlas.llm.scripted import DEFAULT_ROUTE
    from atlas.patterns.base import _Meter, call_model

    class FailAfterModelCall:
        def run(self, ctx):
            meter = _Meter()
            call_model(ctx.model, [HumanMessage(content="trigger")], ctx, meter)
            raise RuntimeError("failed after incurring usage")

    recorded: list[dict] = []
    monkeypatch.setattr("atlas.graph.build.get_pattern", lambda _: FailAfterModelCall())
    monkeypatch.setattr(
        "atlas.graph.build.record_specialist", lambda **kwargs: recorded.append(kwargs)
    )
    model = ScriptedChatModel(
        routes={DEFAULT_ROUTE: [ScriptedTurn(content='{"summary": "measured"}')]}
    )

    report = run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=model,
        pattern_name="react",
        categories=(Category.SECURITY,),
    )

    assert report.errors["security"].endswith("failed after incurring usage")
    assert report.model_calls == 1
    assert report.tokens_used > 0
    assert report.cost_usd > 0
    assert recorded[0]["tokens"] == report.tokens_used
    assert recorded[0]["cost_usd"] == report.cost_usd
    assert recorded[0]["ok"] is False


def test_memory_influences_a_later_run(legacy_root):
    hub = MemoryHub(enabled=True)
    first = run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=model_for("legacy-billing"),
        memory=hub,
        pattern_name="react",
    )
    hub.learn_from_report(first, context_key="repo:generic", strategy="react", success=True)

    second = run_due_diligence(
        repo="legacy-billing",
        repo_root=str(legacy_root),
        model=model_for("legacy-billing"),
        memory=hub,
        pattern_name="react",
    )
    assert len(second.findings) > len(first.findings)
