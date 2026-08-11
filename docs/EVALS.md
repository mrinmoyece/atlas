# Evaluation methodology

## The gap this addresses

Observability tells you what happened. Only evaluation tells you whether the
result was right. Atlas keeps those concerns separate and makes the quality
bar executable.

## Three sources of truth, deliberately separate

| Source | Answers | Can it be wrong? |
|---|---|---|
| Planted ground truth | did it find the real issues, and did it invent any? | No — the answer key is known by construction |
| Deterministic checks | do cited files exist? is severity inflated? | No — verifiable from the filesystem |
| Judge (rule-based or LLM) | is the report prioritised and actionable? | Yes — hence bias controls |

Only the last needs a judge. The most damaging hallucination class — citing
a file that does not exist — is caught deterministically, which is faster,
free and unarguable.

## Ground truth and traps

`evals/golden/ground_truth.yaml` lists, per fixture repo:

- `must_find` — planted issues, each with rule, path and minimum severity.
  Reporting a critical issue as "low" does not count as finding it.
- `should_not_find` — **traps**: code that pattern-matches as vulnerable but
  is correct (a parameterised query, an explicitly configured timeout).

Traps exist because an agent that reports everything achieves perfect
recall. Trap count is reported separately and penalised in `quality`.

## Matching rule

A finding matches a ground-truth item when the normalised **rule** matches,
the **path** matches by suffix, and the reported severity is at least the
required minimum. Rule matching is exact-after-normalisation on purpose:
fuzzy matching lets a vague agent score well by accident.

## Micro vs macro averaging

The fixture set is intentionally imbalanced: `legacy-billing` carries eleven
planted issues, `modern-payments` carries none. That makes macro-averaging
misleading in two ways — the empty control gets equal weight to the entire
dirty repo, and its precision/recall are degenerate (0/0). Atlas reports
**micro-averaged** headline numbers (pooled TP/FP/FN) and macro alongside,
so the difference is visible rather than hidden.

## Judge bias controls

- **Anchored criteria**: each score is computed from a stated, checkable
  property (share of citations that resolve to real files, share carrying
  line numbers, share of the report rated critical) rather than an
  impression.
- **Position-bias control** exists for *pairwise* comparison
  (`pairwise_with_position_control`), and reports
  `position_bias_possible=False` for the default scalar comparator - because
  a judge that scores each report independently cannot exhibit position
  bias, and claiming the control "passed" there would be meaningless.
- **Determinism in CI**: the default judge is rule-based, so the merge gate
  never flakes and costs nothing.

Not implemented, and therefore not claimed: an LLM-backed judge, and any
self-preference control (there is no model prompt to control). The `Judge`
protocol is the seam where a model-backed judge would attach.

## Three tiers

| Tier | When | Contents | Blocking |
|---|---|---|---|
| `smoke` | every PR | one repo, one pattern | yes (fast failure) |
| `standard` | merge | full golden set, scoring + judge, gates | **yes** |
| `extended` | manual / available for scheduling | standard + a smoke pass per pattern | no |

Gates live in `GATES` in `src/atlas/evals/runner.py`, next to the runner —
a quality bar that isn't executable isn't a bar:

```python
GATES = {
    "min_f1": 0.80,
    "min_precision": 0.75,
    "min_recall": 0.80,
    "max_traps": 1,
    "max_hallucination_rate": 0.05,
    "min_judge_prioritisation": 4.0,
    "min_judge_actionability": 4.0,
}
```

**The judge gates were added after a review pointed out that the judge was
decoration.** It ran on every repo, its score appeared in the results table,
and no threshold consumed it - so all four dimensions could regress to the
floor while the gate printed PASS. Two of them are now enforced, chosen
because ground-truth scoring is structurally blind to them:

- **prioritisation** catches severity inflation. Relabel every finding
  `critical` and precision, recall and F1 come back bit-identical, because
  severity is not part of the match - `test_severity_inflation_is_invisible
  _to_ground_truth_but_caught_by_the_judge` asserts exactly that. A report
  where everything is critical has no triage value, which is most of a due
  diligence report's value.
- **actionability** catches findings with no usable remediation. Correct and
  unactionable is still a bad report.

`groundedness` and `signal` are deliberately *not* gated here: they overlap
almost entirely with `max_hallucination_rate`, which already blocks the same
failure with a sharper definition. Two gates on one property just means one
of them is never the binding constraint.

Both judge gates are evaluated only over reports that produced findings.
Severity distribution and remediation quality are undefined for an empty
report, and the clean fixture under `reflexion` correctly produces zero
findings - gating it would fail the one run that is right to be empty.

`max_traps: 1` has deliberately **zero** headroom against the current
baseline. A trap means the agent flagged code a careful reviewer would
recognise as correct, which is the specific failure this project exists to
catch - so a second one should block a merge, not be absorbed.

## Running

```bash
make evals                          # standard tier, the merge gate
python -m evals.gate --tier smoke
python -m evals.gate --tier extended
python -m evals.gate --pattern reflexion
```

Exit code 1 on gate failure. The report is written to `evals/last_run.md`
and uploaded as a CI artifact. The committed
[standard-tier snapshot](../evals/last_run.md) is regenerated in CI so quality
evidence remains reviewable without downloading an artifact.

The checked-in CI workflow runs smoke and standard tiers. `extended` is a
manual tier; no nightly schedule is currently shipped.

## What these numbers do and do not mean

**They measure the harness, not the models.** Each pattern's behaviour is
scripted in `src/atlas/evals/scenarios.py`; the scores that follow are the
harness correctly detecting the consequences of behaviours that were
authored. That is a real and uncommon thing to have built - it is not a
finding about model quality, and describing it as one is the easiest claim
in this repository to falsify (open `scenarios.py`).

Producing live-model numbers needs a runner that injects a provider model
instead of the scripted `model_for(repo)`, N>=10 repetitions per scenario,
and pass-rate distributions instead of point estimates. The current
`run_repo`, benchmark and memory A/B paths instantiate the scripted model
directly; setting `ATLAS_PROVIDER` does not change them. No live-provider
runner or experiment exists yet, so nothing here is evidence about live-model
performance.

## Scoring rules worth knowing

**Cardinality.** Ground-truth items carry `max_instances`. `src/db.py` has
two genuine SQL injection sinks; without a declared count the second real
finding was scored as a false positive, and a pattern that *suppressed* one
of them scored higher precision. A scoring rule that pays an agent to hide a
vulnerability is worse than no scoring rule.

**Clean-repo F1.** A control repository with nothing planted has undefined
precision/recall (0/0). Naive F1 returns 0.0 there, punishing a perfect run.
Atlas defines it as `1 - 0.25 x false_positives`, and micro-averages the
headline so the control cannot dominate.
