# Interview study guide

How to own this project under questioning. Each section: the question you'll
be asked, the answer, and the follow-up that separates a real answer from a
rehearsed one.

> The single strongest thing in this repository is not a feature. It is that
> an adversarial review found eight real defects — including a demonstrable
> path-traversal escape and a leaked memory experiment — and every one was
> fixed with a regression test that names the bug (`tests/test_regressions.py`).
> Lead with that when asked "what went wrong?"

---

## 1. Multi-agent orchestration

**Q: Why multi-agent at all? Wouldn't one agent with all the tools be simpler?**

Simpler, yes, and usually better — which is the honest starting point. Four
specialists earn their keep here for three reasons: each has a narrow prompt
(a security prompt and a delivery-maturity prompt want opposite things), they
run genuinely in parallel so wall-clock is the slowest specialist rather than
the sum, and failure is isolated so a broken dependency analysis still yields
a usable report. If any of those three stopped being true I'd collapse it back
to one agent.

**Q: What's the hard part of parallel agents?**

Reducers. `graph/state.py` defines how concurrent writes merge — findings
accumulate, cost sums, errors merge by key. Get one wrong and results silently
vanish: no exception, no error, just a shorter report. That's why there's a
test asserting all four categories contribute.

**Q: Context isolation — explain it.**

Each specialist keeps its own message list inside its node and returns only
findings plus a summary. The supervisor never sees a transcript. Without it,
four transcripts land in shared state and every later step pays for all four —
that's why naive multi-agent costs *more* than a single agent and performs
*worse*. Cost: specialists can't build on each other mid-run. Acceptable here
because the four dimensions are near-independent; if they weren't, I'd add a
second graph pass rather than abandon isolation.

---

## 2. Evaluation (the strongest section — spend time here)

**Q: How do you know the agent is any good?**

Planted ground truth. `fixtures/repos/` contains repositories with
deliberately planted issues and an answer key in `evals/golden/ground_truth.yaml`,
so precision and recall are real numbers rather than impressions. The clean
control repo also contains **traps** — code that pattern-matches as vulnerable
but is correct — because an agent that reports everything gets perfect recall
and must be caught.

**Q: Why not just use LLM-as-judge?**

A judge can't measure what the agent *missed*, and it measures its own biases
as much as the agent's quality. So: ground truth for recall, deterministic
checks for the worst hallucination class (does the cited file actually exist —
no model needed), and a judge only for what neither can express (prioritisation,
actionability).

**Q: Talk me through a metrics decision you got wrong.**

Two. First, I macro-averaged F1 across an imbalanced fixture set — 11 planted
issues in one repo, zero in the control — which let a single false positive on
the control halve the headline number, and made precision/recall degenerate
(0/0) there. Now it micro-averages pooled counts and reports macro alongside.
Second, path matching used `lstrip("./")`, which strips a *character set*, not
a prefix — so ground truth pinned at the repo root (`"."`) could never match and
was scored as a miss *and* a false positive simultaneously. Fixing it moved F1
from 0.783 to 0.870. Every published number was understated until then.

**Q: What's the gate?**

`GATES` in `evals/runner.py`, enforced in CI on every PR. F1 ≥ 0.80,
precision ≥ 0.75, recall ≥ 0.80, ≤1 trap, ≤5% hallucination. They were
originally much looser (0.60/0.55/2) — which sounds prudent and gates almost
nothing: precision could have dropped 27% relative while still passing. A gate
with that much slack is decoration.

---

## 3. Memory (be ready for the honesty question)

**Q: Does your memory system actually help?**

Cross-repository: **no measurable effect**, and that's the number in
`docs/MEMORY.md`. Longitudinal re-analysis of the same repository: +0.047
quality. Two fixture repos is not enough transfer surface — the only cross-repo
pair is dirty→clean, and lessons from an unsafe billing service don't apply to
a healthy payments service.

**Q: So why is the null result in your README?**

Because the first version of the harness pre-trained on the very repositories
it then scored, and produced a comfortable positive number that was wrong. The
review caught it. A repository whose whole argument is "measure agent quality
honestly" doesn't get to make an exception for its own headline metric.

**Q: What stops memory from making things worse?**

Three things, each guarding a specific failure. Lessons derive only from
structured `Finding` objects that already carry evidence — memorising model
prose turns memory into a hallucination amplifier. Retrieval has a relevance
floor, so an irrelevant store returns *nothing* rather than its "best" garbage.
And procedural memory is Laplace-smoothed, because a strategy that won 1-for-1
must not outrank one that won 47 of 50 — that trap makes procedural memory
worse than none.

---

## 4. Patterns

**Q: Which reasoning pattern should we use?**

Depends on what you're buying, and I have numbers. Reflexion: F1 0.952,
precision 1.000, zero traps, ~2.5× the tokens of ReAct — its self-critique is
the only thing that removes the false positive on the clean repo. ReWOO: 
cheapest, recall 0.636, because it commits to an evidence plan before seeing
evidence, so anything outside the plan is invisible. Policy: ReWOO for bulk
screening, Reflexion for anything a human acts on without re-checking.

**Q: How did you make that comparison fair?**

Shared machinery. Every pattern goes through the same `call_model` and
`run_tool_calls`, so all four get compaction and metering; bypassing them would
make a pattern look artificially cheap. Same fixtures, same deterministic model,
same scoring.

---

## 5. Context engineering

**Q: What breaks when you truncate context naively?**

You orphan a `ToolMessage` whose originating `AIMessage` tool call was dropped,
and most providers reject the request outright. Compaction expands cut points
*backwards* to a safe boundary, summarises the dropped middle — preserving which
tools were already tried, which is what stops the agent repeating itself — and
truncates individual oversized messages rather than letting one tool result
evict the history. There's a parametrised test over `keep_recent` values, and
`call_model` asserts the invariant before every call.

---

## 6. Security

**Q: Walk me through a vulnerability you shipped and fixed.**

Path confinement used `str.startswith(root)`. That's a string prefix check, not
path ancestry: with root `/srv/repo`, the path `../repo-evil/secrets` resolves
to `/srv/repo-evil/secrets`, whose string form starts with `/srv/repo` — so it
passed. Reproduced live, fixed with `Path.is_relative_to`, and the regression
test uses a sibling directory with a shared name prefix, because the original
test only tried `../../etc/passwd` and gave false confidence.

**Q: What's different about securing an agent platform?**

Every request spends money, so *creating a run* is the privileged action, not
writing data. That drove the RBAC model and a pre-flight spend budget — checking
after the run tells you the money is already gone. It was also a TOCTOU race
until review: concurrent requests all passed the same check, so it now reserves
inside the lock and reconciles on completion.

**Q: Prompt injection?**

Contained structurally, not linguistically. Repository content is untrusted
input the agent reads; remote MCP tool descriptions are attacker-authored text
headed for a prompt. Controls: per-run tool allowlists (a hallucinated tool
can't execute), no exec tool at all, `server::tool` namespacing so a remote tool
can't shadow a local one, description sanitisation, output validation that drops
what it can't verify, and budgets that bound the waste. What it does *not* do is
stop injection influencing answer *content* — that's stated plainly in the
security model rather than papered over.

---

## 7. Trade-offs you should volunteer

- **LangGraph vs framework-free.** Used LangGraph here because reducers and
  checkpointing are the hard part and it does them well; I've built the
  framework-free version separately, which is the right answer when the runtime
  *is* the product. Being able to argue both sides is the point.
- **Deterministic scripted model.** Makes CI free, fast and exact — and means
  none of the numbers are claims about live-model performance. Stated in the
  README, the benchmark and the memory doc.
- **Verdict is derived, not generated.** The one thing a decision-maker reads is
  computed from structured findings, so it can't hallucinate a severity nobody
  reported.

## 8. "What would you do differently?"

1. Design the eval gate *before* the second pattern — it changed how I wrote
   every pattern after it.
2. Segment procedural memory by repo language and size from day one;
   retrofitting means re-learning statistics from zero.
3. Make the scripted-model route matcher recency-aware from the start. It cost
   me a silent halving of recall, and the bug is documented in ADR-0004.
4. More fixture repositories before claiming anything about memory transfer.

## 9. Ten-minute walkthrough script

1. *Problem* (1 min): 89% of teams with production agents have observability,
   52% have evals. That gap is where quality dies.
2. *Demo* (3 min): `make demo` — narrate the traps and the pattern table.
3. *Deep dive* (4 min): pick **evaluation** — ground truth, traps, micro vs
   macro, the gate.
4. *Honesty* (2 min): the review findings, the null memory result, and why both
   are in the repo rather than quietly fixed.
