# Pattern benchmark (harness validation)

> **What these numbers are.** Each pattern's behaviour is *scripted* in
> `src/atlas/evals/scenarios.py` to exhibit the characteristic documented
> for it (ReWOO cannot follow a lead outside its plan; Reflexion drops
> weakly-evidenced findings during self-critique). This table shows that
> the harness correctly measures the consequences of those behaviours
> against planted ground truth.
>
> **What they are not.** A discovery about how real models behave under
> each pattern. The differences below are authored, not observed. Running
> this against a live provider requires a separate runner that injects
> a provider model instead of the scripted `model_for(repo)`, plus N>=10
> repetitions per scenario. That runner and experiment do not exist yet,
> so no row here is evidence about model quality.

Regenerate with `make benchmark`.

| pattern | F1 | precision | recall | traps | findings | model calls | tokens | cost |
|---|---|---|---|---|---|---|---|---|
| `reflexion` | 0.952 | 1.000 | 0.909 | 0 | 10 | 33 | 20805 | $0.1002 |
| `plan_execute` | 0.909 | 0.909 | 0.909 | 1 | 12 | 27 | 16289 | $0.0874 |
| `react` | 0.909 | 0.909 | 0.909 | 1 | 12 | 17 | 7711 | $0.0394 |
| `rewoo` | 0.737 | 0.875 | 0.636 | 1 | 9 | 16 | 7209 | $0.0349 |

## Reading the table

* **Best quality: `reflexion`** (F1 0.952).
* **Best precision: `reflexion`** (1.000) with 0 trap(s) triggered - it is the pattern that most reliably declines to report code that is actually fine.
* **Cheapest: `rewoo`** ($0.0349, 7209 tokens) - and its recall is 0.636, which is the trade.

Only `reflexion` avoided every trap on the clean control repository. In the scripted oracle this is because the self-critique phase is written to drop the speculative finding - which is the behaviour Reflexion is *supposed* to exhibit, and what the harness is here to detect if a real model exhibits it too. `rewoo` costs 1.1x less than `react` and gives up +0.273 recall: it commits to an evidence plan before seeing any evidence, so whatever is not in the plan stays invisible. F1 spread across patterns is 0.216 on identical inputs. Since the behaviours are scripted, that spread demonstrates the harness has enough resolution to separate patterns - not that one pattern is better.

Practical routing policy these numbers suggest:

* high-volume screening -> `rewoo` (cheapest at $0.0349, recall 0.636)
* anything a human acts on without re-checking -> `reflexion` (precision 1.000, 0 trap(s))

## The honest summary

The differences above were authored in `scenarios.py` and measured by `scoring.py`. That makes this a test of the *measurement apparatus*, which is a real and useful thing to have built - most agent projects have no apparatus at all - but it is not a benchmark of reasoning patterns, and describing it as one would be the single easiest claim in this repository to falsify.

The routing policy above therefore restates each pattern's documented trade-off, confirmed to be measurable here. Whether a given model actually exhibits it is an open question. Answering it requires the provider-injected repeated-run harness described above.
