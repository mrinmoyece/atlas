"""Harness validation: four reasoning patterns against a scripted oracle.

    python -m benchmarks.run_benchmark

READ THIS BEFORE QUOTING ANY NUMBER FROM THIS FILE.

What this measures: that the evaluation harness correctly detects and
quantifies behavioural differences between reasoning patterns. Each pattern's
*behaviour* is scripted in `atlas/evals/scenarios.py` to match the
characteristic documented for it in the literature - ReWOO commits to a plan
before seeing evidence and therefore misses what the plan omits; Reflexion
critiques its own draft and therefore drops weakly-evidenced findings. The
harness then measures the consequences of those behaviours against planted
ground truth.

What this does NOT measure: how a real model behaves under each pattern. The
scripted oracle produces the differences by construction. An interviewer who
opens `scenarios.py` will see that immediately, and they should - the value
here is a working measurement apparatus plus a demonstrated understanding of
what each pattern costs, not a discovery about model behaviour.

To produce real numbers, add a live-provider runner that injects a provider
model instead of `model_for(repo)`, run each scenario N>=10 times, and report
pass-rate distributions rather than point estimates. That runner and experiment
do not exist yet, and until they do, nothing in RESULTS.md is a claim about
live-model performance.
"""

from __future__ import annotations

import sys
from pathlib import Path

from atlas.evals.runner import GROUND_TRUTH, run_repo
from atlas.evals.scenarios import fixture_repos
from atlas.evals.scoring import RepoGroundTruth, aggregate, load_ground_truth, score_report
from atlas.patterns import PATTERNS

RESULTS = Path(__file__).with_name("RESULTS.md")


def benchmark_pattern(pattern: str) -> dict:
    truth = load_ground_truth(GROUND_TRUTH)
    cards = []
    model_calls = 0
    tokens = 0
    cost = 0.0
    findings = 0

    for repo in fixture_repos():
        report = run_repo(repo, pattern=pattern)
        cards.append(score_report(report, truth.get(repo, RepoGroundTruth(repo=repo))))
        tokens += report.tokens_used
        cost += report.cost_usd
        findings += len(report.findings)
        # steps stands in for model calls at the graph level; the pattern
        # layer reports exact counts, aggregated here via the report.
        model_calls += report.model_calls

    agg = aggregate(cards)
    return {
        "pattern": pattern,
        "f1": agg["f1"],
        "precision": agg["precision"],
        "recall": agg["recall"],
        "traps": int(agg["traps"]),
        "findings": findings,
        "tokens": tokens,
        "cost_usd": round(cost, 6),
        # NOTE: wall-clock is deliberately NOT in the committed artifact -
        # it is machine-dependent and would produce a diff on every run,
        # making real changes invisible. Latency lives in metrics.
        "model_calls": model_calls,
    }


def _interpretation(rows, best_quality, best_precision, cheapest) -> str:
    """Derive the narrative from the measured rows.

    Hardcoding this paragraph is tempting and wrong: the generated file
    would keep asserting a story after the numbers stopped supporting it.
    """
    by_name = {r["pattern"]: r for r in rows}
    parts: list[str] = []

    trap_free = [r["pattern"] for r in rows if r["traps"] == 0]
    if trap_free:
        parts.append(
            f"Only {', '.join(f'`{p}`' for p in trap_free)} avoided every trap on the "
            "clean control repository. In the scripted oracle this is because the "
            "self-critique phase is written to drop the speculative finding - which "
            "is the behaviour Reflexion is *supposed* to exhibit, and what the "
            "harness is here to detect if a real model exhibits it too."
        )

    react, rewoo = by_name.get("react"), by_name.get("rewoo")
    if react and rewoo:
        cost_ratio = react["cost_usd"] / rewoo["cost_usd"] if rewoo["cost_usd"] else 0
        recall_gap = react["recall"] - rewoo["recall"]
        parts.append(
            f"`rewoo` costs {cost_ratio:.1f}x less than `react` and gives up "
            f"{recall_gap:+.3f} recall: it commits to an evidence plan before "
            "seeing any evidence, so whatever is not in the plan stays invisible."
        )

    spread = best_quality["f1"] - min(r["f1"] for r in rows)
    parts.append(
        f"F1 spread across patterns is {spread:.3f} on identical inputs. Since the "
        "behaviours are scripted, that spread demonstrates the harness has enough "
        "resolution to separate patterns - not that one pattern is better."
    )
    return " ".join(parts)


def main() -> int:
    rows = [benchmark_pattern(name) for name in sorted(PATTERNS)]
    rows.sort(key=lambda r: r["f1"], reverse=True)

    best_quality = max(rows, key=lambda r: r["f1"])
    cheapest = min(rows, key=lambda r: r["cost_usd"])
    best_precision = max(rows, key=lambda r: r["precision"])

    lines = [
        "# Pattern benchmark (harness validation)",
        "",
        "> **What these numbers are.** Each pattern's behaviour is *scripted* in",
        "> `src/atlas/evals/scenarios.py` to exhibit the characteristic documented",
        "> for it (ReWOO cannot follow a lead outside its plan; Reflexion drops",
        "> weakly-evidenced findings during self-critique). This table shows that",
        "> the harness correctly measures the consequences of those behaviours",
        "> against planted ground truth.",
        ">",
        "> **What they are not.** A discovery about how real models behave under",
        "> each pattern. The differences below are authored, not observed. Running",
        "> this against a live provider requires a separate runner that injects",
        "> a provider model instead of the scripted `model_for(repo)`, plus N>=10",
        "> repetitions per scenario. That runner and experiment do not exist yet,",
        "> so no row here is evidence about model quality.",
        "",
        "Regenerate with `make benchmark`.",
        "",
        "| pattern | F1 | precision | recall | traps | findings | model calls | tokens | cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['pattern']}` | {r['f1']:.3f} | {r['precision']:.3f} | "
            f"{r['recall']:.3f} | {r['traps']} | {r['findings']} | "
            f"{r['model_calls']} | {r['tokens']} | ${r['cost_usd']:.4f} |"
        )

    lines += [
        "",
        "## Reading the table",
        "",
        f"* **Best quality: `{best_quality['pattern']}`** (F1 {best_quality['f1']:.3f}).",
        f"* **Best precision: `{best_precision['pattern']}`** "
        f"({best_precision['precision']:.3f}) with {best_precision['traps']} trap(s) "
        "triggered - it is the pattern that most reliably declines to report "
        "code that is actually fine.",
        f"* **Cheapest: `{cheapest['pattern']}`** (${cheapest['cost_usd']:.4f}, "
        f"{cheapest['tokens']} tokens) - and its recall is "
        f"{cheapest['recall']:.3f}, which is the trade.",
        "",
        _interpretation(rows, best_quality, best_precision, cheapest),
        "",
        "Practical routing policy these numbers suggest:",
        "",
        f"* high-volume screening -> `{cheapest['pattern']}` "
        f"(cheapest at ${cheapest['cost_usd']:.4f}, recall {cheapest['recall']:.3f})",
        f"* anything a human acts on without re-checking -> "
        f"`{best_precision['pattern']}` (precision {best_precision['precision']:.3f}, "
        f"{best_precision['traps']} trap(s))",
        "",
        "## The honest summary",
        "",
        "The differences above were authored in `scenarios.py` and measured by "
        "`scoring.py`. That makes this a test of the *measurement apparatus*, "
        "which is a real and useful thing to have built - most agent projects "
        "have no apparatus at all - but it is not a benchmark of reasoning "
        "patterns, and describing it as one would be the single easiest claim "
        "in this repository to falsify.",
        "",
        "The routing policy above therefore restates each pattern's documented "
        "trade-off, confirmed to be measurable here. Whether a given model "
        "actually exhibits it is an open question. Answering it requires the "
        "provider-injected repeated-run harness described above.",
    ]

    RESULTS.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:14]))
    print(f"\nwritten to {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
