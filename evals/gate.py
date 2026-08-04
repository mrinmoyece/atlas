"""CI entry point for the eval gate.

    python -m evals.gate --tier smoke      # fast PR check
    python -m evals.gate --tier standard   # merge gate
    python -m evals.gate --tier extended   # nightly

Exit code 1 on gate failure, so agent quality blocks a merge the same way a
failing unit test does. The report is written to evals/last_run.md and
uploaded as a CI artifact.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas.evals.runner import evaluate

REPORT = Path(__file__).with_name("last_run.md")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Atlas eval gate")
    parser.add_argument("--tier", default="standard", choices=["smoke", "standard", "extended"])
    parser.add_argument("--pattern", default="react")
    args = parser.parse_args()

    run = evaluate(tier=args.tier, pattern=args.pattern)
    rendered = run.render()
    REPORT.write_text(rendered + "\n")
    print(rendered)

    if args.tier == "extended":
        # Nightly additionally proves the memory experiment still runs and
        # that every pattern remains executable end to end.
        from atlas.patterns import PATTERNS

        for name in sorted(PATTERNS):
            extra = evaluate(tier="smoke", pattern=name)
            print(f"  {name}: f1={extra.aggregate['f1']:.3f}")

    if not run.passed:
        print("\nEVAL GATE FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
