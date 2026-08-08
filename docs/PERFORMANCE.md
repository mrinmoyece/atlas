# Performance

Regenerate with `python -m perf.benchmark --write`. Enforced in CI by
`make perf`, which exits non-zero on a breached budget.

## Measured

6 concurrent clients x 12 iterations, ASGI in-process,
scripted model. Latencies in milliseconds.

| endpoint | samples | p50 | p95 | p99 | max | budget p95/p99 |
|---|---|---|---|---|---|---|
| `healthz` | 72 | 2.4 | 16.3 | 36.7 | 36.7 | 30 / 90 |
| `metrics` | 72 | 10.1 | 34.2 | 55.4 | 55.4 | 90 / 170 |
| `analyse` | 72 | 311.9 | 374.6 | 435.1 | 435.1 | 950 / 1100 |
| `analyse_404` | 72 | 21.3 | 41.8 | 62.8 | 62.8 | 130 / 160 |

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
