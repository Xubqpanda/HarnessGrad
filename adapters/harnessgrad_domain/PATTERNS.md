# Patterns from measured runs of this harness

Observed, not hypothesised. Every row comes from a real trial of
`base_harness/loop` on `data/probe_set.py` with `deepseek-flash`.

## Round 0, base harness: S = 0.500

| task | what the model did | outcome |
| --- | --- | --- |
| create `calc.py`, run `mul(6,7)` | 0 commands, 4 completions, no answer emitted | wrong |
| write 3 lines, count them | 1 command, answered `3` | pass |
| sum 1..10, run it | 1 command, answered `55` | pass |
| create nested path | 1 command, answered `zeta` | pass |
| write a script, run it, see it fail, fix it, run again | 0 commands, **65,536 output tokens** | wrong |
| count words in a string | 0 commands, answered the literal `<your final answer>` | wrong |

Two patterns are worth naming because a search will meet them immediately.

**The harness has no way to recover from a bad reply.** In the three failures the model
produced something the parser could not use, and the loop ended. Nothing was attempted
a second time, nothing was reported, and the trial scored 0 for a reason that has
nothing to do with the model's capability. A single retry on an unparsable reply, or
feeding the parse error back as the next observation, addresses all three.

**The answer is written even when there is none.** `<your final answer>` was recorded
as this harness's answer to a task, which is worse than failing loudly: the platform
stores it, the score is 0, and a reader cannot tell "the model did not know" from "the
harness reported a placeholder as a result".

## Token spread across trials of the same harness

| run | tokens | score |
| --- | --- | --- |
| base evaluation, one round | 28,015 | 0.500 |
| the same harness, next round | 3,765 | 0.500 |

A 7.4x spread at an identical score. The cost rule in the acceptance criteria
(`Delta C <= beta0 + beta1 * Delta S`) exists for exactly this: without it, a candidate
that spends four times as much to stand still looks the same as one that does not.

## Run-to-run variance is real, and larger than expected

The harness calls its model at `temperature=0.0`, which is easy to read as "so it is
deterministic". It is not. Three evaluations of the *same* harness on the *same* six
tasks:

| evaluation | d01 | d05 | d06 | S |
| --- | --- | --- | --- | --- |
| first | fail | fail | fail | 0.500 |
| RRSI baseline | pass | fail | pass | 0.833 |
| repeat | pass | fail | fail | 0.667 |

`d05` is the only task that is stable (it fails every time), and `d01` and `d06` each
flip. `temperature=0.0` removes sampling randomness; it does not make a hosted model
bit-reproducible, and this endpoint is not.

**What follows for calibration.** `k` repetitions inside one evaluation are *not*
independent draws of the noise band: with `k=2` the two trials agreed on all six tasks
(`rewards` were `[1.0, 1.0]` or `[0.0, 0.0]` throughout), so the within-evaluation
standard deviation came out `0.00000` and `delta` was calibrated to `0.0`. A zero band
makes `S' >= S* - delta` almost vacuous and admits candidates that did not improve
anything. The band has to come from **re-running the base evaluation** and taking the
spread of `S` across those runs -- roughly 0.17 here, which is one task of six -- not
from the trials of a single one.

Until that is done, treat this domain's acceptance as permissive: it will not reject a
candidate for standing still.

## Where the editable surface actually is

The reference harness is 218 lines and almost all of it is reachable:

| edit | expected effect |
| --- | --- |
| accept a reply that is JSON inside prose | recovers the parse failures above |
| retry once on an unparsable reply | same, without changing the prompt |
| require an observation before answering | blocks the `<your final answer>` case |
| cap output tokens per completion | bounds the 65,536-token runaway |
| feed the parse error back as the next message | lets the model correct itself |
| raise `MAX_STEPS` | helps only if the model is already using the steps it has |

The last row is the trap. More steps do not help a harness that stops after one
completion for a parsing reason, and a search that reaches for the step budget first
will spend its annealed early rounds on the one lever that cannot move this score.
