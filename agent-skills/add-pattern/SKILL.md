---
name: add-pattern
description: Add a new reasoning pattern (e.g. Tree-of-Thoughts, self-consistency) to Atlas and get it into the benchmark. Use when asked to add a reasoning strategy or agent loop variant.
---

# Add a reasoning pattern

## Procedure

1. **Implement** `src/atlas/patterns/<name>.py` with a class exposing
   `name` and `run(ctx: PatternContext) -> PatternResult`.
2. **Reuse the shared machinery**: `call_model` (compaction + metering) and
   `run_tool_calls`. Bypassing them makes your pattern unfairly cheap in the
   benchmark and skips compaction.
3. **Report honest measurements**: `model_calls`, `tool_calls`,
   `tokens_used`, `cost_usd`, `steps`, `compactions`, `dropped_findings`.
4. **Contain failure**: every model call wrapped; return a `PatternResult`
   with `error` set rather than raising.
5. **Register** in `PATTERNS` (`src/atlas/patterns/__init__.py`) and add the
   name to `VALID_PATTERNS` in `src/atlas/api/schemas.py`.
6. **Script the phases**: if your pattern issues distinct instruction turns,
   add composite routes in `scenarios.py` keyed on unique phrases from those
   instructions (see ADR-0004 on recency-aware matching).
7. **Benchmark**: `make benchmark`, then update the interpretation section
   of `benchmarks/RESULTS.md` — a row without an explanation is noise.

## Checklist
- [ ] uses `call_model`/`run_tool_calls`; no direct `model.invoke`
- [ ] step budget respected; failures contained
- [ ] registered in `PATTERNS` and `VALID_PATTERNS`
- [ ] scripted routes use phrases unique to that pattern's instructions
- [ ] `test_every_pattern_produces_grounded_findings` passes
- [ ] RESULTS.md regenerated and interpreted
