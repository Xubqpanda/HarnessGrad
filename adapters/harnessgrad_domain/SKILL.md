# The harness RRSI is improving

This domain hands RRSI a **minimal ReAct loop** and a set of short local tasks. RRSI's
search is unchanged; this file is what its roles are told about the world they are
searching in.

## The harness

`domains/harnessgrad/harness/` is a copy of HarnessGrad's `base_harness/loop/`. It is a
Python program with a `harness.json` manifest and one entrypoint:

```
agent.py --task <task.json> --workdir <dir>
```

It reads the task, then alternates:

1. ask a frozen policy model for a completion,
2. parse the reply for either a shell command or an answer,
3. run the command with `cwd=<workdir>`, append the output to the conversation,
4. repeat until an answer appears or the step budget is exhausted.

The answer goes to `<workdir>/answer.txt`; the trace goes to `<workdir>/trace.jsonl`.
Both are the platform's contract (`INTERFACE.md` §1.2), not this domain's invention.

**The model is frozen.** `{policy}` is fixed by the harness's environment. A candidate
that changes which model is called, or what the task text says, is not improving the
harness -- it is changing the experiment.

## What is in the source, and what a search can change

| part | what it decides |
| --- | --- |
| `SYSTEM` | what the model is told it is doing, and the reply format it must use |
| `_parse` | which replies are accepted, and what happens to the rest |
| `run` | the loop: step budget, what is fed back, when it stops |
| `_complete` | how the model is called, retries, timeouts |
| `USAGE` | what the run reports it spent |

That is the whole editable surface, and it is small on purpose: a base harness that is
too capable leaves nothing for a method to find, and one that is too weak fails for
reasons that say nothing about the method.

## The tasks

Short and self-contained (`data/probe_set.py`). Each asks the harness to produce or
inspect something in its working directory and then answer with a value derived from
it. A harness that answers from the prompt alone usually gets it wrong; one that acts,
looks at the result, and then answers usually gets it right.

`S` is the fraction of trials whose answer matches exactly. `C` is the mean tokens per
trial, read from the harness's own `usage` record.

## The three failure modes, so a search does not have to rediscover them

1. **Parse failure ends the run.** The model replies in a shape the parser rejects and
   the loop stops with no answer -- the harness's single most expensive behaviour, and
   it is silent in the score (the trial is simply wrong).
2. **Answering without checking.** One command is issued, its output is never read, and
   the model answers from what it assumed. Measured: a run that created the right file
   and still answered with a placeholder.
3. **Unbounded replies.** A model that rambles consumes the whole context; one trial
   in the base measurement spent 65,000 output tokens and produced nothing. The cost
   rule in the acceptance criteria exists partly for this.

## Running it: one round at a time

RRSI moves its own git branch (`evolve/harnessgrad`) as it accepts candidates, and
`fast_forward` refuses to move a branch backwards. Two rounds started at once therefore
collide:

```
RuntimeError: 5b805c7 is not a fast-forward of evolve/harnessgrad
```

Measured, by running two identical rounds in parallel. Both produced a correct verdict
(independently: candidate A accepted, candidate B measured equal but more expensive and
lost), so nothing was lost in the science -- but the second process aborted at the
fast-forward, the branch had to be reset by hand, and `history.jsonl` ended up with four
copies of each row because both processes append to it.

Run rounds sequentially. `run --start N` is the driver that walks t = N..T; it is the
supported way to do several in a row.

## What the platform sees that the search cannot

Two records come out of a round, and they answer different questions:

* **The harness's cost** (`C`, tokens per trial) is measured by the platform from the
  harness's own `usage` record. The acceptance rule reads it.
* **The search's cost** is what RRSI's four roles spent, recorded by
  `method_usage.json` and surfaced in the domain's `extra` block. It is not part of the
  acceptance rule -- a method does not get rejected for being expensive to run -- but
  without it a curve cannot distinguish a cheap improvement from a bought one.

Measured on the first successful round: the search spent **1,050,412 tokens** across 84
calls to produce candidates whose evaluation cost 769 -> 4,487 -> 2,413 tokens per trial.
The search is roughly 234x the thing it is improving, and only one of those two numbers
used to be observable.

## The boundary

The platform scores the harness; the harness must not touch the platform. It runs in
its own mount namespace where `driver.py`, `eval/` and `.env` are not on disk at all,
and its own source is read-only while it works -- it writes to `--workdir` and to
`.state/` inside its tree, and nowhere else. A candidate that reaches for the scorer,
hardcodes an expected answer, or reads a credential will be rejected by the critic, and
its measured score would not have meant anything anyway.
