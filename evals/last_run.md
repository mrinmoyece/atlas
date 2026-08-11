# Eval run (standard, pattern=react)

| repo | P | R | F1 | traps | findings | cost | judge |
|---|---|---|---|---|---|---|---|
| legacy-billing | 1.00 | 0.91 | 0.95 | 0 | 11 | $0.0269 | 5.00 |
| modern-payments | n/a | n/a | 0.75* | 1 | 1 | $0.0125 | 5.00 |

\* control repository: nothing to find, so precision and recall are undefined. The score column uses the clean-repo definition `1 - 0.25 x false_positives`.

**aggregate**: F1=0.909 precision=0.909 recall=0.909 traps=1 hallucination_rate=0.000

**gate: PASS**
