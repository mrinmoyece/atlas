# Failure modes

What breaks, what Atlas does, what residual risk remains. Ordered by how
often each occurs in practice.

## 1. Model returns unparseable or partial output
**Behaviour.** Lenient extraction (fenced JSON, JSON-in-prose, trailing
commas), strict validation. Findings missing severity, title or location are
**dropped** and counted, never defaulted. Prose-only output is kept as a
summary with `parse_error` set.
**Residual risk.** A model that consistently ignores the output contract
produces empty reports. Visible immediately: the eval gate fails and
`dropped_findings` rises.
**Tested by** `test_findings_without_location_are_dropped_not_defaulted`.

## 2. Tool failure (bad regex, missing file, unexpected exception)
**Behaviour.** Errors become tool observations the model can react to.
`RepoToolkit.call` catches everything, including bugs in tool code.
**Residual risk.** A tool that blocks the event loop evades timeouts; tool
authors must stay non-blocking.
**Tested by** `test_invalid_regex_becomes_an_observation_not_a_crash`.

## 3. Agent loops on the same tool call
**Behaviour.** ReAct detects exact repeats and tells the model it is
repeating; the runtime step budget halts the run regardless.
**Residual risk.** Semantically-identical-but-textually-different calls
(`grep x1`, `grep x2`) are not detected — only the step budget stops those.
**Tested by** `test_step_budget_halts_a_looping_agent`.

## 4. Context window exhaustion
**Behaviour.** Compaction before every model call: monsters truncated,
middle summarised, safe boundaries respected.
**Residual risk.** Compaction loses detail; the digest preserves which tools
were used but not their full output.
**Tested by** `test_compaction_fits_budget_and_keeps_system_prompt`.

## 5. One specialist fails
**Behaviour.** Error recorded in state, run continues, verdict names the
failed specialists. A report missing its dependency section is useful; a 500
is not.
**Residual risk.** Silent quality loss if nobody reads the errors field —
mitigated by surfacing it in the verdict and in `atlas_specialist_runs_total`.
**Tested by** `test_specialist_failure_does_not_fail_the_run`.

## 6. Prompt injection from repository content
**Behaviour.** Structural containment: tool allowlists, no exec tool,
validated output, budgets. Prompt-level guard as defence in depth.
**Residual risk.** Injection can still influence *content* (a misleading
summary). Findings must be treated as evidence-backed leads, not verdicts.

## 7. Hostile or compromised MCP server
**Behaviour.** Allowlist by server and tool, description sanitisation with
logging, `server::tool` namespacing, result caps, fail-closed on unknown
names.
**Residual risk.** A tool on the allowlist that returns hostile *results* is
bounded only by output caps and the injection guard.
**Tested by** `test_remote_tools_are_allowlisted_and_namespaced`.

## 8. Credential compromise
**Behaviour.** Rate limits and daily spend caps bound the damage; the audit
log records every action with actor and cost; hash chaining makes cleanup
detectable.
**Residual risk.** In-process limits multiply by replica count. Redis is the
documented fix.

## 9. Cost runaway
**Behaviour.** Four independent brakes: per-specialist step budget, context
budget, per-principal daily spend checked pre-flight, and cost metrics for
alerting.
**Residual risk.** Spend is projected pessimistically before a run and
reconciled after; a single run can exceed its projection.

## 10. Eval/fixture drift
**Behaviour.** `test_fixtures_and_ground_truth_stay_in_sync` fails if ground
truth cites a path that no longer exists — otherwise scoring silently
degrades as fixtures are edited.
**Residual risk.** A renamed *rule* is not caught; only paths are verified.
