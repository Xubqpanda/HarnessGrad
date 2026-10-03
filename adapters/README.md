# Adapters for external methods

> ## ⚠ Not maintained
>
> **This directory is kept as a historical record. It is not part of what the
> platform offers, and nothing below is a claim about current behaviour.**
>
> The replay route could never produce a platform-measured number: a replayed
> state is another method's harness, driven by that method's own runner, so its
> score stays the method's own and cannot be put on a shared axis. The decision
> was to stop investing in it and do **ports** instead — `methods/<name>/run.py`,
> where a method's *decision rule* is reimplemented against our base harness and
> the platform measures every state itself. Seven published methods now have one;
> see the "second route" section at the bottom of this file.
>
> **What still reads this directory:** `tests/quality/test_adapters.py` (so the
> code does not rot silently) and `adapters/harnessgrad_domain/`, which is **not
> replay** — it is the live interface that lets RRSI's own search loop drive with
> the platform supplying measurement, and it remains maintained.
>
> Two methods have no port and therefore no route at all any more: **MAC**, whose
> code contains no decision rule to port, and **Meta-Harness**, whose search
> framework was never released. Their adapters below still describe what those
> releases contain, which is the only thing anyone can say about them.

An adapter translates an **existing** method's artifacts into the trajectory the
platform reads. It is written *by us, for the platform*, and it never modifies the
method: the method's repository is a read-only source of runs.

    methods_src/<method>/      the method's own code (cloned, never edited)
    <method>_runs/             what the method produced when it ran
    adapters/<method>.py       reads those runs -> HarnessGrad trajectory

This is deliberately different from `base_harness/<name>/old-method-design/`, which is where a
method lives when the harness is *ours*. An external method has its own loop, its
own budget, its own selector, and often its own idea of what a "state" is; the
adapter's whole job is to state that in the platform's vocabulary without
touching the method.

## The nine shapes, and the one conclusion they share

| method | what the artifact IS | what the adapter must do | platform-measurable |
| --- | --- | --- | --- |
| RRSI | a module tree + the method's own runner | extract state from the method's git | **no** — no entrypoint |
| DGM | variants in an archive | read a **tree**, not a sequence; boundary from a prompt | no |
| SICA | per-iteration copies of its own source | copy a tree | no |
| HyperAgents | `gen_<id>/` + retained patches | classify patches **by filename** | no |
| TTHE | **a Python class** | compile to a directory, **preserving the package name** | needs TTHE's runtime |
| AHE | **declarations** (YAML/markdown) for NexAU | compile; the **engine is the method's** | needs `nexau` |
| MAC | one artifact file, `BaseAIMEAgent` subclass | compile | needs its key + endpoint |
| HarnessX | **a YAML**, with the model deliberately outside | compile; **model is a composition input** | needs `harnessx` + a model config |
| Meta-Harness | **output only** | **report that it cannot be compiled** | **no — by construction** |

**Every adapter hit the same wall, from a different direction:** the platform's unit
of exchange is a directory that can solve a task on its own, and **no published
method's harness is that.** TTHE needed its config, AHE its engine, MAC its
credential, HarnessX its package, Meta-Harness its sandbox protocol.

**The platform's assumption is kept rather than relaxed**, because being able to run
a harness standalone is what makes two methods comparable at all. So an adapter has
exactly two jobs, and there is no third: **make the artifact self-contained, or
report honestly that it is not.** RRSI and Meta-Harness are the two ends of that
rule — the first can be materialized but not run, the second cannot even be
materialized.

## How the platform calls an adapter

All nine go through one entrypoint and one contract, because nine scripts with
three different calling conventions is nine scripts, not nine integrations --
which is what this directory contained before the contract existed:

```
    (repo, run_root)         rrsi, dgm
    (run_root)               sica
    (run_root, domain)       hyperagents
    (repo, domain, out_dir)  tthe
    (repo, out_dir)          ahe, mac, harnessx
    (repo)                   metaharness
```

`adapters/context.py` fixes the shape at three arguments — **`repo`** (where the
method's checkout is, read-only), **`run_dir`** (where the adapter may write), and
**`options`** (method-specific keys) — and `adapters/entrypoint.py` drives any
adapter through it:

```bash
HG_ADAPTER=harnessx HG_REPO=/path/to/harnessx HG_OPTIONS='{"example":"coding"}' \
    python adapters/entrypoint.py     # reads the platform's request on stdin
```

`call()` inspects the adapter's signature rather than keeping a table, because a
table drifts the moment someone writes the tenth adapter, and the drift looks like
a broken adapter.

**The failure this fixed is worth recording.** Two thirds of the adapters could not
be called through the platform at all, and every one of them worked when invoked
directly from Python — because they were tested the way they were written, not the
way they are used. `tests/quality/test_adapters.py` now runs each one through the
entrypoint, which is the property that actually mattered.

## What an adapter must answer

1. **What is a state, and how do you materialize it?** A commit id or a variant
   directory names a state *in the method's* repository. The platform's evaluator
   reads its own working tree, so the adapter must be able to write that state's
   files there. Naming a state is not the same as being able to measure it.
2. **What does the method already record as a score?** (Almost all of them record
   one per round. The platform scores the state itself and reports the method's
   claim beside it.)
3. **What is the editable surface?** For RRSI this is answered by reading the
   config, not by diffing: the search parameters are provably outside it.

## The fifth shape: TTHE, where the artifact is a class

TTHE is the case that tests whether the platform's spec can express a harness it
was not designed around. Its artifact is a Python class —
`text_to_sql/harness_base.py:20` defines `SQLHarness(ABC)` with one abstract
`solve()` and a set of inherited capabilities (`llm`, `execute`, `tables`, ...) —
and its docstring states the extent of the editable surface: *"Subclass this. You
may rewrite ANY part of a harness."*

There is no manifest, no entrypoint and no answer file. TTHE's runner
instantiates the class itself (`text_to_sql/bt.py:133`). So `tthe.py` is a
**compiler**: it turns a class into a directory satisfying
`docs/writing_a_harness.md`. It authors exactly one file — a connecting
entrypoint that adds no capability, because anything it added would be an edit the
platform made to someone else's harness.

Two findings from compiling it, both of which would have been silent:

1. **The package name is part of the interface.** `agents/react.py` does
   `from ..harness_base import SQLHarness`, a relative import that resolves only
   while the package keeps its name. Flattening the tree breaks the harness at
   import time — and it breaks it in a way that scores as a harness failure, i.e.
   as *"this harness does not work"*, a claim about someone else's code produced
   by our packaging choice. The compile therefore preserves `text_to_sql/`.
2. **Compiling is not running.** The import chain reaches
   `text_to_sql/bridge.py:29`, which loads TTHE's own `config.yaml` — gitignored,
   endpoint-specific, and not ours to supply. The compiled harness passes project
   validation and cannot execute without TTHE's runtime. That is the correct
   boundary: a method brings its own environment.

## The sixth shape: AHE, where the artifact declares rather than executes

AHE's harness is a directory of component files, and **nothing in it runs on its
own**. `start.py` shows what does: `AgentConfig.from_yaml(...)` then
`Agent(config=config)` from the **NexAU** framework — a dependency, not part of the
artifact. So AHE's unit is "declarations for someone else's engine", while the
platform's unit is "a directory with an entrypoint that solves a task". The adapter
bridges the two by generating one entrypoint that loads the declarations and drives
NexAU, adding no capability.

The seed harness maps the paper's seven component types onto files, and the
adapter carries that mapping verbatim rather than discovering it:

```
system_prompt        systemprompt.md                          present
tool_description     tool_descriptions/                       present
tool_implementation  tools/                                   present
long_term_memory     LongTermMEMORY.md                        present
middleware           -                                        absent in the seed
skill                -                                        absent in the seed
subagent             -                                        absent in the seed
```

**Four present, three absent** — which is what "deliberately minimal seed" means
concretely, and it is worth the platform reporting rather than assuming: those
three absences are the improvement space.

A third finding, and the first time the contributor validator earned its place on
real work: the first version of this adapter declared `"install": "requirements.txt"`
and did not ship one. `validate_harness.py` failed the compile immediately. The fix
is not to write a requirements file — the EntryPoint needs `nexau`, whose
dependencies live in AHE's own `pyproject.toml` — but to declare `install: null` and
let the method's environment be the method's. **A manifest that names an install
file it does not ship is a claim, and the validator treats it as one.**

## The seventh shape: MAC, and the finding that evaluation discipline does not travel

MAC ships an evaluation API, a sandbox, a cryptographic held-out split
(`X-Verifier-Secret`, injected into the container only after development ends), a
12/24-hour budget, rate limiting and a usage meter. In structure that is the same
kind of thing as this project, aimed at a different question: MAC asks *how well do
frontier LLMs program an agent*; this platform asks *what did a method's edits
actually produce*. It is the first method in the set that is itself a platform.

Its artifact is a single Python file that `load_agent_class`
(`eval_utils/agent_runner.py:49`) imports and instantiates, implementing

```
aime-meta-agent/tools/base_agent.py:106   class BaseAIMEAgent(ABC)
:72    Problem(idx, question)      :86   Prediction(idx, pred)
:168   solve(problems, timeout_sec) -> List[Prediction]
```

which the adapter compiles into a directory the platform can validate, the same
way TTHE's class was compiled.

**What MAC adds to this project's design, and the reason it is worth an adapter:**
its evaluation discipline belongs to the artifact's own benchmark, and **none of it
travels with the compiled artifact**:

```
held_out_split      cryptographic (X-Verifier-Secret injected after development)
budget              43,200 s (12 h); 86,400 s for SWE-Bench and Terminal-Bench
search_quota        2,500 API calls per phase
rate_limit          enforced by the method's ModelProxy (rpm/tpm)
travels_with_artifact   False
```

So a platform that re-measures the compiled artifact is measuring something MAC
never reported: the same code against a different task set, on a different budget,
with no verifier secret and no rate limit. That is not a flaw in either system — it
is what cross-platform measurement *means*, and the platform already has the field
for it. Every curve point records `measured_by_platform`, and a replayed point keeps
the method's own number beside the platform's rather than replacing it. MAC is the
case that shows why that field is not bookkeeping.

## The eighth shape: HarnessX, where the model is a composition input

Structurally HarnessX is the closest of the eight to what this project means by a
harness. The artifact is **a YAML file**: `HarnessConfig`
(`harnessx/core/harness.py:743`) is *"pure behaviour pipeline … defines what the
agent does (processors, tools, workspace, tracer) but carries no model
information"*. The class docstring warns against the other order — *"Do not
instantiate directly"* — and the composition is explicit:

```
agent = model_config.agentic(harness_config)
result = await agent.run(BaseTask(description=...))
```

**The model being outside the artifact is a cleaner split than any other method
here has.** A HarnessX harness can be measured under several models without
becoming a different harness, and the platform already keeps `agent_model` on every
curve point, so the distinction survives into the record.

**The editable surface is thin, and that is the finding.** A harness with
`processors: []` gives a method nothing to edit; the improvement space is the
processor list. The five examples HarnessX ships differ on exactly that axis, and
getting the count right mattered — a naive scan reported **0 for all five**,
including `coding`, whose pipeline is twelve processors long. Five visibly
different harnesses would have looked identical on the platform's axis. The
adapter now parses the list by indentation, without requiring a YAML library,
because needing one *to count* would be a reason this adapter fails on a host where
the harness would have run fine:

```
minimal 0    custom_processor 3    research 9    coding 12    assistant 12
```

And a method can improve this harness in two distinguishable ways: **compose
existing processors** by editing the YAML, or **write a new one** in code. Those
are different kinds of edit, which is what `edit_kind` is for.

## What replay can and cannot produce — read this before claiming anything

All four adapters now materialize states, and running them produced one result
that the design has to state plainly:

| method | shape | steps | files extracted | platform-runnable |
| --- | --- | ---: | ---: | --- |
| RRSI | sequence | 1 | 0 | **no** |
| DGM | tree | 2 | 8 | **no** |
| SICA | sequence of copies | 1 | 125 | **no** |
| HyperAgents | tree | 1 | 0 | **no** |

**Not one of them can be executed by this platform**, and the reason is the same
in every case: a published method's harness is driven by that method's own runner
— harbor and a container for RRSI, a per-run Docker scaffold for DGM, the agent's
own benchmark runner for SICA, a copied-in container for HyperAgents. None is a
freestanding entrypoint. So:

* **Their scores stay theirs.** Every replayed point is marked
  `measurable_by_platform: false` with the reason, and the number is labelled
  method-reported. This is not a shortcoming of the adapters; it is what replay
  means.
* **What replay *does* produce is still substantial**, and none of it needs a
  re-run: the trajectory shape, the boundary reading (can the method change its
  own search rule?), the acceptance rule and whether it is calibrated, what the
  improvement cost, and whether the headline was selected on the set it is
  reported on.
* **Cross-method score comparison requires live runs**, which requires a
  harness the platform can actually invoke. That is what `base_harness/` is for, and
  it is a separate piece of work from adaptation.

Stating this here rather than discovering it in a results section is the point:
the platform's own rule is that a number it did not produce is never presented as
if it had.

## Adapters

| adapter | method | shape | state is | score source |
| --- | --- | --- | --- | --- |
| `rrsi.py` | RRSI | loop-owning | `(repo, third_party/harbor_terminus2, commit)` | `runs/<domain>/frontier.json` |
| `dgm.py` | DGM | archive / tree | `(repo, ., commit)` | `<out>/<run_id>/metadata.json` |
| `sica.py` | SICA | self-modifying | `<exp>/agent_<i>/agent_code/` (a copy) | `<exp>/agent_<i>/benchmarks/` |
| `hyperagents.py` | HyperAgents | names the boundary | `gen_<id>/` + retained patches | `gen_<id>/<domain>_eval/report.json` |
| `tthe.py` | TTHE | **a Python class, not a program** | `agents/*.py` + `harness_base.py` | TTHE's own runner |
| `ahe.py` | AHE | **declarations, not code** | the component files at fixed mount points | the method's own runs |
| `mac.py` | MAC | **a method that is itself a platform** | one artifact file (`artifact.py`) | the artifact's own benchmark |
| `harnessx.py` | HarnessX | **a YAML, and the model is not in it** | `harness_config.yaml` (+ the example's own code) | the method's own runs |
| `metaharness.py` | Meta-Harness | **output only, no process** | nothing invocable | the README's 76.4% |

All four were read, not run. Cloning note: `git clone` times out from this host;
`https://codeload.github.com/<owner>/<repo>/tar.gz/refs/heads/<branch>` works,
but **check the branch name** -- SICA's default is `master`, and guessing `main`
returns a 404 that looks like a missing repository.

## The one reading they all support

Every adapter answers the same question in the method's own terms: **which side
of the boundary did this step move?** The answers differ completely --

| method | can the method change its own search rule? | established by |
| --- | --- | --- |
| RRSI | **no, structurally** | the editable surface is pinned to a directory that excludes the search code; the parameter table is CLI-only |
| DGM | archive management stays outside the patchable surface | `update_archive` is not something a patch can reach |
| SICA | **yes, by construction** | the improver's working directory *is* its own source; the selection rule lives in the same tree |
| HyperAgents | **partly** -- meta level yes, parent selection and evaluation no | patches are filtered by `task_agent.py` / `meta_agent.py`; the method's Limitations name what stays fixed |

That table is the reason the platform records `editable_surface_touched` instead
of accepting a declared mode.

---

## The second route: ports, not replays

Everything above is the **replay** route: an adapter reads what a method already
produced. It is the only route that can speak about a published result, and its
ceiling is stated above -- a replayed state is never platform-runnable, because a
published harness is driven by that method's own runner.

There is a second route, and it answers a different question. A **port**
(`methods/<name>/run.py`) reimplements the method's *decision rule* and composes
it with the platform's shared editor (`methods/editor.py`). The method then acts
on **our** base harness, so the platform measures every state itself and the
curves land on one axis. The trade is exact and worth stating plainly:

| | replay | port |
| --- | --- | --- |
| what it measures | the method's own reported number | the platform's own measurement |
| comparable across methods | **no** | **yes** |
| reproduces the paper's result | as recorded | **no** -- it is evidence about the rule |
| needs the method's engine/credentials | yes | **no** |

A port is evidence about a method's rule, never a reproduction of its results.
That is why every port's docstring carries a "what is transplanted / what is not
/ what is not reproduced" section with `file:line` citations, and why the method
title in the console says "(移植)".

### What each port transplants

| port | the rule it transplants | what mode A removes |
| --- | --- | --- |
| `sica_ci` | newest round whose mean clears the best confidence lower bound | -- |
| `rrsi` | noise-band floor, two-branch cost rule, novelty, annealed edit budget, stall-reserved exploration, prune set, pre-evaluation leakage critic | the `m`-candidate argmax, "stay at `H_t`", exact `delta` calibration |
| `dgm` | archive parent sampling `sigmoid(10*(s-0.5)) / (1+children)`; failure-target selection before editing | parallel children; and the gold patch in its prompt, which the platform withholds on purpose |
| `hyperagents` | `sigmoid(10*(s-mid)) * exp(-(children/8)^3)`; the validity gate that never looks at score | staged-eval rescaling, ensemble `max`, multi-domain averaging |
| `ahe` | pre-registered `predicted_fixes`/`risk_tasks`, graded against observed flips, verdict driving rollback-or-pivot | its k-rollout pass@1 and its tool-using editor |
| `tthe` | fixed branch-role schedule, validity-only gate, the rollback gate ("the incumbent is always admissible"), label-free discipline | the `G x R` pool and the agentic judge |
| `harnessx` | best-so-far acceptance with a cost penalty and a pass-count noise guard | the replay phase of its shipping gate (it requires running the candidate) |

### Two published methods are deliberately absent

* **MAC** has no improvement, acceptance or stopping rule in its code. It is a
  benchmark: it hands a coding CLI a sealed container and scores the artifact on
  a cryptographic held-out split. The only rule it encodes is artifact
  admissibility (one subclass of `BaseAIMEAgent`). A port would have to invent
  the decision rule, so there is none.
* **Meta-Harness** publishes its *output* and not its process. The released
  artifact contains no search, acceptance, budget, novelty or critic code, and
  its README says the search framework is "coming soon". No rule is recoverable,
  and guessing one would be worse than saying so.

Both remain in the replay set, where what they do publish is recorded honestly.
