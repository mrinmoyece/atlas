---
name: add-fixture
description: Add a fixture repository with planted ground truth and traps so evaluation coverage grows. Use when asked to add test repos, eval cases, or improve eval coverage.
---

# Add an evaluation fixture

## Procedure

1. **Create** `fixtures/repos/<name>/` with realistic structure. Plant
   issues that a real reviewer would find, at varied severities.
2. **Plant traps too.** Every fixture needs code that *looks* wrong and is
   not (parameterised queries, configured timeouts, intentional
   suppressions). Without traps, an agent that reports everything scores
   perfectly.
3. **Write the answer key** in `evals/golden/ground_truth.yaml`:
   `must_find` (rule, category, path, min_severity, note) and
   `should_not_find`.
4. **Script the model** in `src/atlas/evals/scenarios.py` via
   `REPO_SCENARIOS[<name>]`. Include realistic misses — a scenario that
   finds everything teaches the harness nothing.
5. **Verify sync**: `test_fixtures_and_ground_truth_stay_in_sync` must pass.
6. **Re-baseline** gates: `make evals`. If aggregate numbers shift, update
   `GATES` deliberately and say why.

## Rules
- Never plant a real secret. Use obviously fake values.
- Keep fixtures small; they are read by tools with output caps.
- Ground-truth paths must exist — the sync test enforces it.

## Checklist
- [ ] both `must_find` and `should_not_find` populated
- [ ] scripted scenarios include at least one realistic miss
- [ ] sync test passes; `make evals` passes
- [ ] gate changes (if any) justified in the PR
