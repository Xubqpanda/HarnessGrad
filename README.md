# HarnessGrad

**A platform for running and comparing harness self-improvement methods.**

We do not propose an RSI method. We build the instrument that makes RSI methods
comparable — and we are therefore not competing with the methods we measure.

## The problem this exists for

Every harness-evolution method reports a final score delta. None of them can say
what that delta is *made of*, and none of them report it on a curve a reader can
compare against another method's. The field has no shared training signal.

HarnessGrad defines one: a base harness, a fixed task set, a sampling loop, and
a curve point that records the harness state, the two models involved, the score
with an interval, and the compute spent to get there.

## The division of labour

| Platform owns | The harness (and its method) owns |
| --- | --- |
| which tasks, how many rounds | reading whatever it wants from the traces |
| running the harness, collecting traces | deciding how to modify the harness |
| **all scoring** | — |
| every curve point, ckpt and token | — |

Scoring is not delegable. A subject that scores itself makes every cross-method
comparison meaningless. See [`INTERFACE.md`](INTERFACE.md).

## Where a method lives

**A method is external to the harness.** It is handed a pristine copy of a base
harness and produces candidate harnesses; the platform then measures those
candidates itself, on its own fixed task set, with its own scorer.

```
        base harness (yours, or ours)
                 │
     ┌───────────┼───────────┐
     ▼           ▼           ▼
   method A   method B   control      ← all outside the platform's harness
     └───────────┼───────────┘
                 ▼
      candidate harnesses
                 │
                 ▼
     platform measures every one      ← same tasks, same scorer, for all methods
```

The first design put the method *inside* the harness. That made it inseparable
from one artifact: it could only ever improve the harness it shipped with, and two
methods could not be compared because each faced a different starting point.
Keeping methods external is what makes "the same method applied to any base
harness" possible at all — and it is why a method can no longer be part of what is
being measured.

**Two execution modes**, differing only in who owns the schedule:

* **Mode A — platform-driven.** The platform owns the loop and calls the method
  once per round for one step.
* **Mode B — method-driven.** The method runs its own search and returns a whole
  trajectory. For methods that already exist and must not be re-wired — RRSI owns
  an annealed 20-round budget, DGM an archive.

## The old in-harness method design, and why it went

The method lives **inside the harness repository**, as the implementation of two
entrypoints the harness declares in `harness.json`:

```json
"improve_entrypoint": ["python", "-m", "method.improve"],
"accept_entrypoint":  ["python", "-m", "method.accept"]
```

The platform invokes them as **subprocesses** — never imports. That is not a
style choice: an imported module is frozen at load time, and the thing this
platform exists to measure is a harness that **rewrites its own improvement
mechanism**. Importing does not complicate that case, it makes it
unrepresentable.

Because the method lives inside the artifact under evolution, the boundary
becomes measurable instead of declarable. Every curve point records
`editable_surface_touched`, read from `git diff`:

```
control method (returns the base)      touched: []                score 0.00
a real method  (edits the harness)     touched: ['agent.py']      score 1.00
```

`"method/accept.py"` appearing there would mean the method rewrote its **own
acceptance rule** — the line most published systems state they do not cross. So
the platform has no "real RSI" mode: a declared mode would be a self-report, and
the genuine/pseudo distinction is a reading, not a setting.

## Two execution modes

* **Mode A — platform-driven.** The platform owns the loop and calls
  `improve_entrypoint` each round. Maximum comparability.
* **Mode B — method-driven.** The method runs its own loop in its own process and
  writes a trajectory file; the platform resolves and scores the states it names.
  For methods that already exist and must not be re-wired in order to be
  measured.

Both write the same `curve.jsonl`, so curves from either land on one axis.

## Install

```bash
./install.sh              # install what is missing, then verify
./install.sh --check      # verify only; change nothing (also useful in CI)
```

That is the whole dependency list: `bubblewrap`, `git`, `python3 >= 3.10`. The platform
itself has **no third-party Python packages** — everything it uses is in the standard
library. `openai` is imported lazily by the reference harness's model call only, so a
harness written in another language or against another provider never needs it.

The script does not stop at "bwrap is installed", because that is not the same as the
sandbox working: a kernel with unprivileged user namespaces disabled, or an AppArmor
policy restricting them (the default on newer Ubuntu), leaves `bwrap` present and failing
at runtime. So it finishes by asking the platform to build a real namespace and look for
itself inside it. If that fails it says why and refuses to call the machine ready —
rather than letting a run fall back to `--no-sandbox` and produce numbers that mean
something other than what they say.

## If your harness has dependencies

A harness that needs a package its task image does not have must say so in `harness.json`:

```json
{"install": "requirements.txt"}
```

Then configure it **once, per harness** — not per task, and not per benchmark:

```bash
python3 tools/configure_harness.py --harness base_harness/loop --dataset terminal_bench
python3 tools/configure_harness.py --harness base_harness/loop --list   # what is built
```

That resolves the dependencies for every Python version the dataset's images use and
places them in an overlay outside every image, which runs then mount read-only with
`PYTHONPATH`. Measured for Terminal-Bench: **4 builds, ~112 MB, about two minutes**,
against 89 derived images and ~3.7 GB for the alternative. The image's own interpreter
still runs the harness, so §2.5.3 is untouched and no task image is modified.

Each build is verified by importing what it installed, and every curve point records
which interpreter ran the harness and where its dependencies came from
(`env.harness_runtime`). A run whose harness declares dependencies but has none
configured says so at the door and names the command above — it does not quietly report
`score 0.000`.

## Run it

```bash
M="python $PWD/methods/echo_base/run.py"
python3 driver.py --harness loop --mode A --rounds 2 --run-id control \
    --method-entrypoint "python $PWD/methods/noop/run.py"
python3 driver.py --harness loop --mode A --rounds 2 --run-id treat-A \
    --method-entrypoint "$M"
python3 driver.py --harness loop --mode B --rounds 2 --run-id treat-B \
    --method-entrypoint "$M"
python3 tools/plot_curve.py runs/control runs/treat-A runs/treat-B
```

```
run                                policy       curve (one glyph per round)
------------------------------------------------------------------------------
control                            all          ▁▁    0.00..0.00
loop-demo-all-mA                   all          ▁███  0.00..1.00
```

Runs with zero network and zero cost, against a local 8-task dataset. The point
is not the scores — it is that the loop, the curve points, the ckpts and the
cost accounting are exercised end to end.

Two locations, deliberately not the same one:

| | where | why |
| --- | --- | --- |
| the **record** | `runs/<run-id>/` | `curve.jsonl` and `run_meta.json`; what happened, kept with the platform |
| the **working tree** | `../harnessgrad_work/<run-id>/workspace/` | the harness copy and every checkpoint commit; outside the platform, so a harness cannot reach the driver or `.env` by `..` |

The working tree is where the git history of every checkpoint lives, so it is not
disposable until the run is exported (`tools/export_harnesses.py runs/<run-id>`).
`--work-root` moves it; the driver refuses to start if it would land inside the
platform. `runs/<id>/run_meta.json` records the path, so the export tool does not
have to guess.

Each task's harness runs inside a mount namespace, so it cannot see the platform at
all. `--no-sandbox` turns that off for debugging; the two modes are not comparable,
and `run_meta.json` records which one a run used. `bwrap` must be installed
(`apt install bubblewrap`), or the driver refuses to start rather than quietly
running without it.

## Layout

```
install.sh            install + verify (bubblewrap, git, python3 >= 3.10)
docs/writing_a_harness.md  how to contribute a harness (start here if that is you)
INTERFACE.md          the frozen contract -- read this first
driver.py             the entrypoint: argparse, wiring, the order refusals happen in
harnessgrad/           the platform's implementation, one responsibility per module
                      (not `_harnessgrad/`: that is the runtime channel inside a
                      workspace; this is the referee's own code)
  identity.py             what a measurement says it is (harness hash, models, improver)
  environments.py         what a task needs to run in, and what could not be measured
  records.py              curve points, diffs, states, traces, verdicts, run_meta
  channel.py              what a method is shown, and where
  methods.py              calling an external method, and keeping it out of the platform
  loops/                  the three schedules: eval_side, mode_a, mode_b
base_harness/loop/       reference base harness: a minimal ReAct loop
methods/noop/         control method: returns the base harness unchanged
methods/echo_base/    reference method: makes one real edit
data/demo.py          local dataset, no network
eval/                 scoring and execution (platform-owned)
ckpt/git_state.py     harness state, backed by git
tools/plot_curve.py   text rendering of a curve
tools/validate_harness.py  checks a harness against the contributor spec
tests/quality/        boundary rules R1-R4, enforced by AST
```

Each harness owns its own `method/`:

```
base_harness/loop/
├── harness.json          declares the entrypoints
├── agent.py              the artifact under improvement
└── method/
    ├── protocol.py       the stdin/stdout contract
    ├── improve.py        <- what to try next (a method rewrites THIS)
    └── accept.py         <- what counts as better
```

## Discovered by building it

Three defects the skeleton caught before any paid evaluation was wired in —
each is the kind that would otherwise have been read as a result:

1. **The framework's own channel leaked into the checkpoints.** Staging traces
   under `_harnessgrad/` inside the workspace meant a no-op Trainer still minted
   a new sha every round, so the control curve appeared to move. Fixed by
   gitignoring the channel.
2. **`generation_tokens` was hardcoded to zero.** That is exactly the reporting
   gap this framework criticises other work for. Now a declared field.
3. **A relative repo path silently zeroed every score.** The harness could not
   be launched from the sandbox cwd, and the failure read as "the method did
   nothing" rather than "the instrument is broken".
4. **A harness could read the platform, and a method could rewrite it.** The
   working tree used to sit at `runs/<id>/workspace`, two `..` from the platform,
   `eval/` and `.env`. Measured, not assumed: a harness could read all three and
   write anywhere in the platform directory. Three fixes, in increasing strength:

   * the working tree moved outside the platform (`--work-root`, default
     `../harnessgrad_work`), and the driver refuses to start if the two overlap;
   * the platform hashes its own files around every method call, so a method that
     edits its own scorer fails the run — `FAILING RUN`, the paths named, no
     `curve.jsonl`, exit `3`;
   * the harness now runs in its **own mount namespace** (`eval/sandbox.py`), where
     `HarnessGrad/` is not present at all. Measured from inside: `.env`,
     `driver.py`, `harnessgrad/`, `eval/`, `methods/` and other runs' workspaces do not exist —
     the path is an empty mount point, not a refused read. Cost: **+11.1 ms** per task, +0.53% against a model call, and
     no image, so a harness may still be written in any language.

   The second is detection and stays: it is now what protects the platform from the
   *method*, which still runs as an ordinary subprocess. The third is enforcement.

   One contract narrows, and it is worth knowing why: inside the sandbox the code in
   the harness's own tree is **read-only**. The same copy scores every task in the
   round, so a harness that rewrote `agent.py` while answering task 1 would be a
   different program by task 2 and the curve would report one harness where two ran.

   A harness still has two writable places: `--workdir` (the task's cwd, fresh per
   task) and `.state/` inside its own tree (a per-task tmpfs, gitignored, for harnesses
   that write relative to themselves; `HARNESSGRAD_STATE` names it). Verified in the
   sandbox: writing `.state/cache.json` succeeds, rewriting `agent.py` raises
   `OSError`, and neither the host tree nor the sha moves.
   What is hashed is the referee (`driver.py`, `harnessgrad/`, `eval/`, `ckpt/`,
   `data/`, `tools/`,
   `methods/`, `tests/`, `docs/`, `INTERFACE.md`, `.env`), never `base_harness/` —
   that is the candidate, and every run edits a copy of it.

   `tools/plot_curve.py` labels each run's isolation and **refuses to silently mix
   them**: runs made under different sandbox plans, or before the sandbox existed,
   put numbers on the same axis that were produced under different rules. It says so
   and exits non-zero.

   **Known gap:** the namespace constrains the filesystem and nothing else. A
   harness keeps the network and the agent's API key, because it cannot run without
   them, so it could exfiltrate that key. Closing it needs egress filtering; see
   INTERFACE.md §4.8. `--no-sandbox` exists for debugging, and `run_meta.json`
   records which mode produced a run.

## Not yet decided

Listed so they are not mistaken for oversights: which dataset; the metric
definition beyond "defined on a task set and pairable"; whether Trainers get a
budget for cheap pre-validation; and **contamination** — whether the agent model
has seen the benchmark tasks is not detectable from inside the framework and is
the largest known threat to the validity of any curve this produces.
