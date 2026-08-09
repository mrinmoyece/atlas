# Performance

Regenerate with `python -m perf.benchmark --write`. Enforced in CI by
`make perf`, which exits non-zero on a breached budget.

## Measured

6 concurrent clients x 12 iterations, ASGI in-process,
scripted model. Latencies in milliseconds.

| endpoint | samples | p50 | p95 | p99 | max | budget p95/p99 |
|---|---|---|---|---|---|---|
| `healthz` | 72 | 2.7 | 12.6 | 22.2 | 22.2 | 30 / 90 |
| `metrics` | 72 | 11.7 | 35.4 | 74.4 | 74.4 | 90 / 170 |
| `analyse` | 72 | 312.3 | 404.4 | 427.6 | 427.6 | 950 / 1100 |
| `analyse_404` | 72 | 20.8 | 41.2 | 51.6 | 51.6 | 130 / 160 |

## What these numbers are

The **platform**, not a model. Atlas runs a deterministic scripted provider,
so there is no network call to an LLM in any of these paths. What is being
measured is routing, auth, validation, the four-way graph fan-out, report
serialisation and the streaming generator.

That is deliberate. A benchmark dominated by provider latency varies by an
order of magnitude between runs and can gate nothing. This one is stable
enough that a 2x regression in Atlas's own code trips a budget.

## What these numbers are not

- **Not throughput.** Concurrency here is asyncio tasks against an
  in-process app; there is no socket, no TLS, no serialisation across a
  process boundary, no other tenant on the box.
- **Not a capacity plan.** For that, run `perf/locustfile.py` against a
  deployed instance and increase load until the error rate breaks 1%.
- **Not real-provider latency.** Add `ATLAS_PROVIDER=anthropic` and the p99
  becomes a measurement of somebody else's service.

## Reading it

p50 is what a user usually sees. **p95 and p99 are what your worst-served
users see** - and they are the ones who file the ticket. No mean is
reported, because a mean hides exactly the tail that matters.


## Caching: what was added, what it buys, and where it buys nothing

Profiling a run showed 26% of the time inside `inspect.getsource` —
LangGraph calls it on every node during compile
(`pregel/_utils.get_function_nonlocals`), tokenising and ASTing the source.
Measured: **9.9ms of a 38.4ms run**, spent rebuilding an identical graph.

`build_graph_cached` memoises the compiled graph on the configuration that
shapes it. Measured effect:

| | median run |
|---|---|
| fresh model each run | 36.2 ms |
| model reused | 17.7 ms |
| **saving** | **18.5 ms (51%)** |

### The honest half

That 51% only lands **when the model object is reused**. In the eval and
demo paths it is not: `ScriptedChatModel` walks a fixture's turns in order,
so it is stateful and must be per-run — two concurrent runs sharing one
would consume each other's script. Measured hit rate there is **0/6**.

The cache therefore buys nothing for the scripted paths and a great deal
for a deployed service, where `_provider_client` now holds one long-lived
provider client per configuration. `atlas_graph_compile_cache_hits_total`
and `..._misses_total` exist precisely so this is visible rather than
assumed — a miss rate near 100% means callers are constructing models per
run and the cache is dead weight.

### What was NOT cached, and why

Tool results. The obvious candidate — four specialists analysing one
repository look like they should repeat `list_files` and `repo_stats` — and
the measurement said otherwise: **29 tool calls across all 8 repo x pattern
combinations, 0 redundant**. A cache there would have been complexity
serving a hit rate of zero. Worth stating because it is the optimisation
everyone assumes is needed.
