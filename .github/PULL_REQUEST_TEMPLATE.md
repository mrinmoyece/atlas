# What & why

<!-- The change and the reason. Link issues. -->

## Checklist

- [ ] `make gate` passes locally (lint + tests + standard evals + performance)
- [ ] Tests added/updated (regression test for bug fixes)
- [ ] Agent-behaviour change → eval coverage updated
- [ ] Eval gate green; if `GATES` moved, justified below
- [ ] New findings carry evidence; new tools are confined and bounded
- [ ] Docs updated (ADR for decisions, FAILURE_MODES / LIMITATIONS as needed)
- [ ] Regenerated `benchmarks/RESULTS.md` / `docs/MEMORY.md` if numbers moved
- [ ] `CHANGELOG.md` updated
- [ ] Deployment/security docs updated for new config, permissions or failure modes

## Gate movement justification

<!-- Required if GATES changed. Why is the new threshold correct? -->

## Risk & rollback

<!-- Blast radius if wrong; how to roll back. -->
