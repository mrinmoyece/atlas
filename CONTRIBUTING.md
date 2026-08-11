# Contributing

## Setup

```bash
make install
make demo      # offline end-to-end showcase
make gate      # lint + tests + evals + generated-report drift + performance
```

Start with the [documentation map](docs/README.md) and
[AI system design](docs/ai-system-design.md) before structural changes.

## Ground rules

- Read [AGENTS.md](AGENTS.md). Its invariants bind humans and AI agents alike.
- Every behaviour change ships with tests; every bug fix ships with a
  regression test that fails without the fix.
- **Changes that alter agent behaviour must keep the eval gate green.** If a
  change legitimately moves the numbers, update `GATES` in
  `src/atlas/evals/runner.py` in the same PR and say why in the description.
- New tools declare confinement and output bounds, and are added to the MCP
  definitions from the same source.
- Significant design decisions get an ADR in `docs/adr/`.
- Never memorise free-form model output into semantic memory.

## Pull requests

- One logical change; keep diffs reviewable.
- Imperative subject ≤72 chars; the body explains *why*.
- CI must be green: lint, tests, evals (smoke + gate), benchmark, security
  scan, Docker build.
- Update `CHANGELOG.md` under Unreleased.

## Regenerating artifacts

```bash
make benchmark   # benchmarks/RESULTS.md
make memory-ab   # docs/MEMORY.md
make evals       # evals/last_run.md
```

`make gate` is the contributor-facing aggregate for lint, tests, standard
evals, deterministic benchmark/memory report drift and performance budgets;
`make lint`, `make test` and `make evals` remain useful separately when
isolating a failure. CI may run the same concepts as separate jobs and adds
packaging and security checks. Do not describe `make evals` as
the whole gate or assume a passing unit suite proves eval quality.
