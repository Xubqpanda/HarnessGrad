# Where HarnessGrad sits: five existing works, and the differences that matter

> Five sources were handed to us as "work that already exists": three papers
> (Evo-Bench, HarnessDev, Harness-Bench) and two repositories (HarnessBench,
> PawBench). This file is the comparison.
>
> **The evidence rule for this file.** Every claim about a source is a quote or a
> number from that source, with its section (papers) or file (repositories). Every
> claim about us is a line in `INTERFACE.md`, a file in this repository, or a
> number in `runs/`. Where we say "we differ", the difference is something a reader
> can check in both artifacts -- not a judgement about whose work is better.
>
> **Two findings that corrected an assumption of ours**, kept because they are the
> kind of thing this file is for: PawBench already runs each task several times and
> records a standard deviation (`task_stats`, `runs_per_task`), so "nobody measures
> noise" is false; and HarnessBench's execution path does not work against the
> published `clawbench-eval`, so it is a scaffold rather than a runner.

---

## 1. The answer in one paragraph

All five measure **what a model can do with, or at, a harness**. Four of them fix a
protocol and report what a set of models or configurations achieves under it. We
build the other half: **the instrument that makes a method's effect on a harness
comparable, re-measurable and refutable** -- method as an untrusted external
process, and noise, artefact identity, cost and "could not measure" recorded on
every point beside the score. And none of the five trains anything: Evo-Bench says
it plainly ("rather than proposing a new optimization algorithm, our work provides
a complementary orthogonal perspective", §2), HarnessDev closes the door
explicitly ("HarnessDev measures model-external learning and does not claim
heuristic learning can replace parameter training", §6.1), and the other three are
diagnostic or scaffolding by construction. That untouched axis is the one our plan
calls **training the harness**: learning the *edit policy* from harness-repository
history and from a verifiable platform reward.

---

## 2. The five, in their own terms

| | **Evo-Bench** (2608.09096) | **HarnessDev** (2609.01437) | **Harness-Bench** (2605.27922) | **PawBench** (agentscope-ai) | **HarnessBench** (reacher-z) |
| --- | --- | --- | --- | --- | --- |
| What it is | benchmark for "harness-evolving capability" | benchmark for creating *and* evolving a harness | diagnostic benchmark of model×harness configurations | model×harness co-evaluation benchmark + leaderboard | CLI scaffold that varies the harness |
| Frame | "can language models truly improve agent harnesses?" (§1) | "shifts the unit of evaluation from task outputs to runnable infrastructure" (Abstract) | "Agent = Model + Harness" (§3) | "Agent Performance = f(Model, Harness)" (README) | fix the model, vary the harness (README) |
| Subject under test | **evolver model** (9 frontier models) | **creator model** (6 creators) | nothing optimized; 6 harnesses × 8 models varied | nothing optimized; 9 models × 3 harnesses | nothing optimized; harness adapters |
| Object improved | "the executable harness code that orchestrates reasoning, tool use, and memory" (§2) | "a runnable harness… frozen and then reused", `H=<E,T,C,S,L,V>` (§3.1) | none (harness is the varied axis) | none (harness is the varied axis) | none |
| The improver itself | a **fixed** hand-written evolve harness `H_evo`; "All nine evolvers receive the same template" (App. E.1) | a **fixed** meta-harness `D` (Claude Code 2.1.177 / Codex 0.144.3) | — | — | — |
| Budget | 20 iterations / 1,000 steps / 48 h | 10 post-H0 evaluation pairs, ≤2 five-task probes | one pass per configuration | CLI-driven repeats | none (no runner) |
| Scale | 160 validation + 448 evaluation tasks | 2,207 instances, 5 suites; 73 versions, 64 switches | 106 tasks; 5,088 (+106) trajectories | 150 tasks; 9 models × 3 harnesses | no tasks in-repo; ClawBench's claimed 153 |
| Who scores | native scorers + fixed judge (Qwen3.7-Plus) | "the platform's fixed verifier; the only measure of correctness" | deterministic validators + fixed judge (claude-sonnet-4.6) | embedded `def grade(...)` + LLM judge (`claude-opus-4-5`); `hybrid` weighted | inherited from `clawbench-eval` (not in-repo) |
| Repeated runs | "We run all experiments one time" (§5.1.1) | avg@3 in RQ1, **single trajectory** in RQ2 | "variance is computed over harness-level averages rather than repeated stochastic runs" (App. C) | **yes**: `runs_per_task`, `task_stats` with mean/std/min/max, pass@k | none (zero code matches for trial/repeat/seed/variance) |
| Artefact identity | iteration index; snapshots retained | frozen **commit**; dedup by `(commit, benchmark)` | image digest in the environment record | adapter name + pinned image/version; timestamped result dirs | adapter slug + `Dockerfile`/`run.sh` |
| Cost accounting | `$` per run (>$500 best, <$1 cheapest) | execution cost total + per task; **creator tokens excluded** | tokens (K) and turns per run | `total_usage` per result | none |
| Released | leaderboard, code, dataset | project page | code (`Qihoo360/harness-bench`) | 27 committed submissions + live site | PyPI package, 7 commits |

### 2b. The two repositories, read as systems

**PawBench** is the closest of the five to being a platform: a runner
(`run_bench.py` + `pawbench/`), a task format, three grading modes, a checkpoint
format, committed result files and a static leaderboard site. Its 150 tasks are
Markdown with YAML front-matter and an **embedded grader** (`## Automated Checks`
holds a `def grade(transcript, workspace_path) -> dict`), graded `automated`,
`llm_judge` or `hybrid`. The extension point is a **harness**: `pawbench/agents/
factory.py` says "Adding a new agent type only requires: 1. Create a new
`ContainerAgent` subclass under `impl/`. 2. Add one entry to `AgentFactory.
_REGISTRY`" -- three implementations today (QwenPaw, OpenClaw, Hermes). Nothing
improves a harness; the harness is chosen. Its per-run checkpoint carries
`summary{pass_rate, avg_score, total_usage, errors, by_label}`, per-task
`results[]` with `usage`/`anomaly`/`timed_out`, and `task_stats` with mean/std and
pass@k. Maturity: 64 commits, Apache-2.0, two test files, and CI that only
deploys the site -- no CI runs the benchmark.

**HarnessBench** re-frames ClawBench (browser-agent, "shared 153-task pool") along
the harness axis and contains no tasks, no scorer and no record writer of its own
("Code reuse. 100% -- HarnessBench imports clawbench-eval rather than forking it").
A harness is "three files plus one `pyproject.toml` entry"
(`docs/adding-a-harness.md`): a `HarnessSpec` dataclass, a `Dockerfile`, `setup.sh`
and `run.sh`, with a documented plugin group
`[project.entry-points."clawbench.harnesses"]`. Two measured problems: its
`run`/`batch` path calls `from clawbench import run_case`, which **does not exist**
in the published `clawbench-eval` 0.10.0 (its own error path exits 10, and no
`importlib.metadata` lookup for that group exists upstream), so the documented
entry point is not consumed by anything; and there is no task dataset, evaluator
or result writer in the repo, so `leaderboard` has nothing to aggregate. Seven
commits, all within three days of April 2026; tests cover matrix expansion and
credential gating only, and state that they "do NOT spin up any container". It is
the shape of a harness benchmark without the benchmark.

---

## 3. Differences we can defend with a number

### D1. Noise: measured by two of them, a *contract field* only here

The problem is stated better than we could state it, by the papers:

* HarnessDev §4.3: *"The same commit can vary by about ±4.75 pair-score points, so
  small gains cannot be attributed to code changes from score alone"*; *"Across 64
  comparable switches, feedback and held-out scores move in the same direction only
  34 times (53.1%), and only 2/9 declared versions are held-out optimal"*; App.
  E.2: *"A probe leg scores over n=5, so a single task moves that leg's score by 20
  points"*; App. H: *"noise of roughly ±3-4 tasks in one full evaluation."*
* Evo-Bench App. D.1: *"a 2.2-point Overall Score range across byte-identical
  I8/I10/I12 revisions"* -- while §5.1.1 says *"We run all experiments one time."*

Our own instance, from `runs/`: **the same base harness tree (`ebcf957af553`), the
same agent model (`deepseek-flash`), the same three tasks, scored 0.333 in one run
and 0.000 in the next** (`flash-three-1` round 0 vs `flash-three-2` round 0). One
parse failure in the agent's tool-call dialect moves a three-task score by a third.

What differs is not noticing the noise. **PawBench already runs tasks repeatedly
and stores mean/std**, and Evo-Bench and HarnessDev discuss it. What differs is
what the record does with it:

| | their handling | ours |
| --- | --- | --- |
| the number | PawBench: mean over `runs_per_task`. Evo-Bench / HarnessDev: one sample, said in prose. Harness-Bench: averages across configurations, explicitly not stochastic repeats | `--trials N`; a task's score is the mean over N |
| the spread | PawBench: `task_stats` std in a result checkpoint. The papers: not recorded | `per_task_std` and `score_std` **on the curve point**, and `n_trials` **omitted** when N=1 rather than written as 0 |
| who can read it | a human reading the leaderboard | anything downstream, including a method's own acceptance rule, because it is part of the point's contract (`INTERFACE.md` §3.1) |

So the claim we can defend is narrower than "we measure noise": **we are the only
one of the five whose record makes the spread a field of the measurement itself --
present when there is more than one sample, absent (not zero) when there is one --
so that "this candidate gained less than the noise" is decidable from the artifact
rather than from a paragraph.**

### D2. Artefact identity is content, not a commit index

HarnessDev's frozen artefact is "one commit" and its dedup key is `(commit,
benchmark)`; Evo-Bench's is an iteration index ("I8/I10/I12"); PawBench's is an
adapter name plus a pinned image tag. Those are addresses of a *history*, not of a
*content*.

We shipped the commit-based version first and measured the cost
(`INTERFACE.md` §4.47): on `loop-terminal_bench-26452c`, **six rounds reported six
different `identity.harness_sha` while four of the trees were byte-identical and
every `diffs/round-N.patch` was 0 bytes**. The cache key was
`(harness_sha, task_id)`, so the cache never hit, the same code was measured four
times, and one round's agent phase bounced between 98 s and 2841 s. After the fix,
one harness is one identity: `loop-terminal_bench-submit-1` reports one sha
(`255cbd5d293e`) for all six rounds.

None of the five records identity for the *improver tool*, which is the other half
of the same problem: our points carry `identity.improver` (name, version, `sha256`,
model) and `identity.skill_sha` (the instructions the tool was given). Measured
reason: several codex builds coexist on one machine (0.125.0 broken, 0.139.0,
0.156.1), and `improvers/skill.md` gained a failure mode between two runs -- same
method name, same improver version, different edits, and nothing in either record
said why.

### D3. Where the method boundary is drawn, and who is untrusted

All five keep scoring out of the subject's hands (HarnessDev: "a harness's
self-reported status is never a scoring input"; PawBench: platform graders plus a
judge; Harness-Bench: deterministic validators plus a judge). `INTERFACE.md` §0
says the same thing.

The difference is what may be a *method*:

* Evo-Bench, HarnessDev, PawBench, HarnessBench all hold the meta-level **fixed in
  their own code** -- a template, a meta-harness, an adapter class, a `HarnessSpec`.
  A new method means a new file inside the benchmark, written by the benchmark's
  authors.
* We admit a method as an **external subprocess** that may not import the platform
  (`methods/<name>/run.py`, mode A/B), sees the platform only through a staged
  channel, and runs under a sandbox. Around every method call and every evaluation
  the platform hashes the referee (`eval/integrity.py`: `driver.py`,
  `harnessgrad/`, `eval/`, `ckpt/`, `data/`, `tools/`, `methods/`, `tests/`,
  `docs/`, `improvers/`, `INTERFACE.md`, `.env`); a run that trips it is not a
  measurement with a defect -- it is not a measurement. Every top-level entry must
  be hashed or excused in writing (`NOT_PLATFORM`), which is what catches a hole
  when the tree grows.

Evo-Bench's reward-hacking audit is the complement of ours: they scan for answer
retrieval and run a Codex-based semantic audit (App. C.2; only MiniMax M3 showed
detector evasion). Ours is about the *platform* being edited. A method here never
sees a task's verifier at all; the container boundary check plus
`candidate.validate` cover the direction they audit.

### D4. Cost is three budgets, not one invoice

HarnessDev: "Execution cost is reported as both the total and the mean per task;
**tokens used by the creator to build or modify the harness are excluded**."
Evo-Bench: `$` per run. PawBench: `total_usage` per result. We keep three ledgers
on every point (`cost.harness_tokens`, `cost.method_generation_tokens`,
`cost.env_generation_tokens`) and make `cumulative` the x-axis, because a
cost-aware acceptance rule fed a mixed number is fed a number its formula is not
about (`INTERFACE.md` §3). This week's item B closed the last hole in it: the
method's own ledger read 0 on every round of every run, because nobody carried the
provider's `usage` down to the record -- and a rule reading a constant is not a weak
rule, it is no rule.

### D5. "Could not measure" is not zero

HarnessDev attributes failures after the fact ("77.8% of failed Data tasks are
attributed to harness defects", §4.2) but they were scored as failures first.
PawBench counts errored and missing tasks as 0 in the leaderboard ("errored/missing
counted 0"). We separate the two events in the record: a task the platform could
not start, or whose check never ran, is `invalid` with a named stage and service,
leaves `per_task` entirely, and is not cached (`INTERFACE.md` §2.5.7). Two measured
instances this week: 83 of 89 Terminal-Bench checks install their own runner over
the network, and a failed install used to be recorded as the harness scoring 0; and
a harness that dies with `APIConnectionError` is now `stage: "harness model call"`,
with the gateway's own connection counts in the reason.

---

## 4. What they have that we do not

Stated plainly, because a comparison that lists only advantages is not a
comparison.

1. **Task construction for harness sensitivity -- Evo-Bench's real contribution.**
   73 harness variants evolved on a disjoint 320-task auxiliary set, 12 diverse
   harnesses `H_aux`, then `Sens(x)` = the Pearson correlation between a task's
   score across harnesses and each harness's leave-one-task-out quality; tasks with
   `Sens(x) ≤ 0` are dropped, the rest are stratified by difficulty and split so
   validation and evaluation follow the same difficulty distribution (Abstract, §3).
   Our split is whatever the dataset declares (`terminal_bench`: 52 train / 37
   eval). **They can show a task set responds to harness quality; we cannot.** This
   is the single most valuable thing to borrow.
2. **Scale and suite coverage.** 2,207 instances across 5 suites (HarnessDev),
   608 tasks (Evo-Bench), 106 tasks × 54 configurations (Harness-Bench), 150 tasks ×
   27 configurations (PawBench). Ours is 89 Terminal-Bench tasks and one demo set.
   Our claim is about comparability rather than coverage, but a second suite is what
   would test it.
3. **Code-quality diagnostics of the evolved artefact.** HarnessDev measures the
   candidate as code: *"of 169 new functions or classes, 113 are reachable from the
   entry point, 31 are reachable only through dead code, and 25 have no caller"*
   (§4.3). We would score a candidate with 25 uncalled functions exactly like one
   without. We record what the method touched (`editable_surface_touched`) but
   nothing about whether the new code is reachable.
4. **A public leaderboard and committed submissions.** `evobench.org` plus a
   HuggingFace dataset; PawBench commits 27 submission JSONs and deploys a site.
   We have a panel on one host.
5. **Cross-model transfer as a headline result.** Evo-Bench lifts a *different*
   policy model (13.9 → 27.9/29.2) from a synthesized harness, which is evidence
   about generality rather than overfitting. Our recorded `agent_model` makes that
   experiment cheap and comparable, but we have not run it.
6. **Genuinely independent graders at scale.** PawBench's tasks carry their own
   embedded grader and an LLM-judge rubric; ours is the task's own check, which is
   more native but also more fragile (83/89 need network to install a runner).

---

## 5. The axis none of the five occupy

Each is a **measurement of a model's one-shot capability** (Evo-Bench, HarnessDev),
a **diagnostic of configurations** (Harness-Bench, PawBench), or the scaffolding
for one (HarnessBench). In every case the improvement loop is a hand-written prompt
over a frozen frontier model, or absent entirely. The open question -- can the
improvement *policy* be trained? -- is not asked, and two of the papers say so in
their own words.

That is the experiment our platform makes possible, and the plan already written:

* **pretrain** the edit policy on harness source, pull requests and issues from the
  repositories these works evolve (codex, openclaw, claude-code, zcode,
  deepseek-harness, pi, opencode) -- no tasks, no ground truth, just "what a change
  to a harness looks like";
* **posttrain** on the platform's own verifiable reward, which is the part that
  cannot be faked because the platform owns sampling, execution, scoring, records
  and the cache keyed by content.

**What we may claim, and what we may not.** Not "the first benchmark for harness
evolution" (Evo-Bench), not "can LLMs create and evolve a harness" (HarnessDev),
not "model×harness co-evaluation" (PawBench, Harness-Bench), not a harness
leaderboard (HarnessBench, PawBench). What is ours, and is checkable:

> They ask **whether a model can improve a harness**. We build the instrument that
> makes the answer comparable and re-measurable -- method as an untrusted external
> process, noise and identity and cost and "could not measure" on every point --
> and then ask whether the improvement **policy itself** can be trained, which the
> platform's own verifiable reward makes testable rather than anecdotal.
