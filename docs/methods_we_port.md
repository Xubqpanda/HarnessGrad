# The methods we port, and what a port is here

> `methods/` holds seven ports of published methods plus the platform's own reference
> methods. This file says, for each one, **what was transplanted, what could not be, and
> why** -- so that "a run of this method" is read for what it is: evidence about a
> *decision rule* measured on this platform, never a reproduction of the original's
> scores.
>
> Line references come from reading the checkouts named in `adapters/SOURCES.md` at the
> digests recorded there; `adapters/fetch.sh` re-downloads them. Every port's own
> `run.py` docstring says the same things in more detail; `methods/<name>/method.json`
> carries the one-paragraph version.

---

## 1. The sentence this file exists to make checkable

**We port decision rules, not systems.** The original papers ship a loop, an improver
model, an evaluation protocol and a rule, all entangled. What a method is *here* is the
rule: the platform owns the schedule, the task set, execution, scoring and the record.

The evidence for the split is mechanical: **0 of the 7 ports starts its own model
client** (no `OpenAI(...)`, no `chat.completions`); all 7 call the shared
`methods/editor.py`. The improver -- codex, a model endpoint, a CLI -- is resolved by
`tools/improver.py` and named on the curve point. So a run of "RRSI" here means *RRSI's
decision rule, with this platform's loop and whichever improver the run resolved*.

---

## 2. What is in `methods/`, and what is deliberately not

| method | source | what we transplanted |
| --- | --- | --- |
| RRSI | arXiv 2609.24972 (`google-research/rrsi`) | the per-round decision rule: noise floor, two-branch cost rule, novelty, annealed edit budget, stall/exploration, leakage critic |
| DGM | arXiv 2505.22954 (`jennyzzt/dgm`) | the archive rule: parent sampling, child-count penalty, retention gate |
| HyperAgents | arXiv 2603.19461 (`facebookresearch/Hyperagents`) | parent sampling (`top-3` centred sigmoid, `exp(-(c/8)^3)`) and the validity gate |
| SICA | Robeyns, Szummer, Aitchison 2025 (ICLR'25 workshop; `MaximeRobeyns/self_improving_coding_agent`) | the selection rule: newest iteration whose mean clears the best one's confidence lower bound (`runner.py:88-148`) |
| AHE | arXiv 2604.25850 (`china-qijizhifeng/agentic-harness-engineering`) | pre-registration, component-level attribution (5 verdicts), pivot-after-two, a real rollback |
| HarnessX | arXiv 2606.14249 (`darwin-agent/HarnessX`) | the acceptance gate (historical best, tolerance, passed-count noise guard, cost penalty) and the pre-registration |
| TTHE | arXiv 2607.08124 (`junnie00/TTHE`) | fixed branch-role schedule, validity gate, proposal card, "the incumbent is always acceptable" |
| codex | -- | not a paper method: the improver moved *out* of the method (an external coding agent), so that a rule and a tool can vary independently |
| `llm_improver`, `echo_base`, `noop` | this platform | the shared diagnose-and-edit loop; a contract self-test; the floor every curve must beat |

Two adapted methods have **no port**, and the reasons are different in kind:

* **MAC** (`ant-research/meta-agent-challenge`) *is a platform*. It ships an evaluation
  API, a sandbox, a cryptographic held-out split and a budget, and it puts a general
  coding CLI inside a two-container sandbox to write `agent.py` itself. There is **no
  acceptance, rejection, parent-selection or stopping rule in its code** -- that strategy
  lives in the (unpublished) meta-agent it calls. And its evaluation discipline does not
  travel with the artifact: `adapters/mac.py:54-61` records
  `travels_with_artifact: False`. There is nothing to port.
* **Meta-Harness** publishes an **output**, not a process. See §6.

---

## 3. The seven ports

Each entry: the rule we took, the functions that carry it, what mode A cannot express, and
the honest summary. All of them end the same way -- *evidence about the rule, not a
reproduction*.

### 3.1 RRSI -- regularized recursive self-improvement

**Idea.** Harness self-evolution memorizes the evolve set: gains in distribution shrink or
vanish OOD. RRSI does not shrink the edit surface; it regularizes the *search trajectory* --
an annealed budget on how many independent edits one candidate may bundle, suppression of
already-falsified hypotheses, a turn to never-tried components when stalled; on the
selection side a noise floor, a cost rule that makes extra tokens pay for themselves, and a
pre-evaluation leakage critic. Reported: up to +14.1 on the evolve split, up to +4.7 across
five OOD benchmarks, 30% fewer policy tokens.

```
ΔS = S' - S_t ;  ΔC = (C' - C_t) / C_t
c  = ΔC <= β0 + β1·ΔS                     if ΔS > δ
     w_s·ΔS - w_c·ΔC + w_n·ν(l') > 0      otherwise
admissible(H')  ⟺  S' >= S* - δ  ∧  c
H_{t+1} = argmax_{admissible} S'   ;   else H_t
```

**Transplanted.** `_edit_budget`, `_stall_flag`, `_exploration`, `_prune_set`, `_novelty`,
`_cost_rule`, `_judge`, `_critic_precheck`/`_critic_review`, `_classify_diff`, `_normalize`,
`_has_evidence` -- RRSI's algorithms as pure functions against this platform's curve points.
The critic is real: a deterministic denylist first, then a model review, and a rejection
sends the objections back to the proposer for a bounded number of repairs. The state the
rules need (which component each edit touched) is rebuilt from the points, with the one
thing points cannot carry -- `touched_components` -- written into `method_reported` and read
back next round.

**Mode A cannot express.** (1) `argmax` over `m=2` candidates has a single term; (2) "no
admissible candidate ⇒ stay at `H_t`" does not exist, because mode A advances to whatever it
was handed -- so this file **re-adjudicates** the round that produced the current state and
labels it `re_adjudicated: true`; (3) `δ` cannot be calibrated, since the platform records
one score per task per round and no per-trial rewards -- the formula is kept for reference
and `main` reads `HG_RRSI_DELTA` (or a fallback) and records which.

**The one place we are stricter than RRSI.** RRSI substitutes `ΔC = 0` whenever either side
reports no token count, which lets a cost rule that never fired look like one that passed.
We compute the same 0.0 but set `cost_rule_active: false` and leave `admissible: null`.

**Also different:** the original proposer/critic run `claude-opus-4-8`; the harnessgrad
domain config uses `deepseek-flash` and discloses it (`_models_doc`).

### 3.2 DGM -- the archive

**Idea.** Do not chase one lucky lineage. Keep an archive and sample the next parent with a
weight that rises with score and falls with how many children it already has, so the search
spreads out. A variant enters the archive if it is not worse than the initial score minus a
hand-set noise tolerance.

```
p(c) ∝ sigmoid(10·(score_c - 0.5)) · 1/(1 + children_c)     # sampled, not argmax
retain (keep_better):  score_c >= initial_score - noise_leeway
```

**Transplanted.** The sampling distribution, the child-count penalty, the retention rule
(`keep_better`/`keep_all`, default recorded), and the shape of picking *which failure to
target* before editing.

**Different.** One candidate per round, no parallel children, and no rejection. The
`1/(1+children)` term would be inert on a straight chain, so the method writes its parentage
into `method_reported` and reads it back. `noise_leeway` becomes `HG_DGM_NOISE_LEEWAY`: 0.1 is
an absolute accuracy on SWE-bench where a round submits hundreds of instances, and on a small
task set it is many interval widths wide.

**What is lost.** DGM's diagnostic prompt carries the gold patch, the private test patch and
the test log. The platform excludes expected answers on purpose, so **the DGM we can run is
materially weaker than the published one**, and the file says so.

**Found while porting.** `--update_archive` defaults to `keep_all`, not `keep_better`; and
the `best` branch sorts ascending and then takes `[:k]`, i.e. the *worst* k. We do not port
that branch.

### 3.3 HyperAgents -- the meta-level is editable

**Idea.** A meta-agent rewrites the whole repository, including the meta-agent itself; the
task agent is evaluated per domain; parents are still sampled to keep exploration open -- but
the sigmoid is centred on the mean of the top three rather than on a fixed 0.5, and the child
penalty is a cube that only bites past ~8 children.

```
mid = mean(top-3 scores)
p(c) ∝ sigmoid(10·(score_c - mid)) · exp(-(children_c / 8)^3)
valid parent = the run was evaluated, and the meta patch exists    # score never enters
```

**Transplanted.** The sampling distribution, the validity gate as a retention rule, and a
switch for which setting ran (recorded per point).

**Different.** No real archive: one candidate per round, so the method records what the
archive rule *would* have done. The validity gate becomes "this round was measured and this
method's own edit was accepted as a change". Staged-eval rescaling, the agent/ensemble
element-wise max and the multi-domain average have no counterpart.

**Found while porting.** HyperAgents ships **two** selectors that disagree: `gl_utils.py:570-584`
really samples proportional to the formula, while `select_next_parent.py:49-57` computes the
child counts and scores and then discards them for `random.choice`. The uniform branch is
uniform because its weights are dead code -- *not* because a normalized sigmoid cancels out.
The earlier, stronger claim failed against the source and both the claim and its refutation
are recorded in the file.

### 3.4 SICA -- choose the base by a confidence lower bound

**Idea.** Do not build on the iteration with the highest mean; that high mean may be luck.
Take the **lower confidence bound of the best iteration** and build on the *newest* iteration
whose mean clears it.

**Transplanted.** `_ci_lower()` and `_select_base()` -- the same comparison and the same
"newest that clears the bound" tie-break, expressed against this platform's curve points
(`score_ci95[0]`) instead of SICA's `ArchiveAnalyzer`.

**Different.** The rule is used for what mode A can express: whether this round is worth
spending on at all, and which prior round is the fairest comparison. When nothing clears the
bound it reports `changed: false` rather than spending a model call to stand still.

### 3.5 AHE -- every edit is a falsifiable contract

**Idea.** Before measuring, the evolve agent pre-registers a `change_manifest` declaring
`predicted_fixes` and `risk_tasks` per change; the next iteration grades each declared change
against the observed per-task transition, and the verdict becomes an *instruction* to the
model. There is **no acceptance gate**.

```
HARMFUL              risk_hit > 0 and fixed == 0
MIXED                risk_hit > 0 and fixed > 0        # tested BEFORE EFFECTIVE
EFFECTIVE            fixed == predicted > 0
PARTIALLY_EFFECTIVE  0 < fixed < predicted
INEFFECTIVE          otherwise
```

**Transplanted.** `observed_diff` (fail→pass = fixed, pass→fail = regressed),
`evaluate_changes` (the five verdicts, their precedence, and `unattributed_regressions`),
`abandoned_levels` (the "same level for 2+ iterations ⇒ change level" half of the meta-rule,
computed rather than left for the model to notice), and the rollback **performed** rather
than requested: the candidate is built on `_harnessgrad/states/round-<n-2>`.

**Different.** With no reject step the rollback degrades to "different base + instruction",
and its granularity is the whole round rather than one change. `k=2` rollouts become `k=1`.
AHE's component taxonomy has no counterpart (our harness contract does not know AHE's mount
points), so the level travels only as a declared string.

**Found while porting.** `constraint_level` is **inert in AHE**: it appears exactly once in
the whole repository, in a prompt example, and no Python reads it. We record it and never
enforce it.

### 3.6 HarnessX -- the acceptance gate

**Idea.** Turn traces into harness updates and gate each round against the **historical
best** -- never against the last accepted round, or a loose tolerance drifts the baseline
down over many rounds. Equal scores do not dethrone the earliest holder, and a breach that a
passed-count noise guard calls noise is accepted *without* updating the best.

```
cost_delta_ratio = (round_cost - best_cost) / max(best_cost, 1e-3)     # 0 when best_cost == 0
score            = pass_rate - cost_weight · max(cost_delta_ratio, 0)
revert  ⟺  score < best_rate - tolerance
        ∧  |round_passed - best_passed| >= pass_count_noise_threshold
```

**Transplanted.** `_gate_decision` (the arithmetic above), the pre-registration
(`hypothesis_id`, `levers`, `predicted_affected`, `rollback_trigger`,
`expected_global_gain`, `regression_risk`, `cost_shift`) and the revert directive.

**Different.** The gate **cannot revert anything**: mode A accepts unconditionally. What the
port can do is (1) record `harnessx_gate` with the arithmetic and the reason, and (2) hand
back the historical best state (`_harnessgrad/states/round-<n>/`) as this round's candidate
and name it as the base the next round must build on. HarnessX restores `current_config =
best_cfg` inside its own loop; here the restore happens only because the platform happens to
measure the directory the method gave back. The shipping gate cannot run at all -- its replay
phase *executes* the candidate, which is the platform's job, not the method's. Two recipes
exist upstream with different rules (GAIA: tolerance 0.03, count guard, relative cost; tau2:
0.02, no guard, absolute cost); this port carries GAIA's and says so.

### 3.7 TTHE -- test-time harness evolution

**Idea.** Treat the executable harness as the state of a test-time adaptation: the LLM stays
frozen, no gold labels, and solver/proposer/judge are three roles of the same frozen model.
Per unlabeled batch: `G` fixed branches for `R` rounds, each branch editing only its own
previous harness and reading every active candidate's traces as peer evidence; a child
replaces its parent only if it imports, passes a frozen-solver/label-free audit, and leaves a
valid proposal card; after `R` rounds an agentic judge picks among **every** harness observed
in the batch, with the incoming incumbent always in the pool.

**Transplanted.** The branch-role schedule (`ROLES[round_index % 3]`, its period and
three-way rotation), `valid_proposal_card`, `read_evidence`/`validity_gate`, the "incumbent
is always acceptable" rollback semantics on every failure path, and the prompt's label-free
discipline (no gold, hints are not proof, every claim needs evidence).

**Different.** The `G×R` pool and the agentic judge are **not expressible**: the judge
re-runs and probes the database and is explicitly *not* `argmax(score)`, so rewriting it as
one would invent a decision TTHE does not make. The port keeps the gate's
"cannot regress" meaning instead: every failure path returns the incumbent with
`changed: false`. Rejection moves from "after executing the child, fall back to the parent"
to "before spending the round". The two label-free proxies (back-translation, metamorphic
INV/SENS fitness) need natural-language questions, hints, literal answer keys and a rewriter
model, none of which exists for a foreign harness. The proposer goes from a Claude Code CLI
with Read/Write/Bash and read-only DB probes to a single shared chat call.

---

## 4. What every port shares: seven constraints of this platform

1. **Who owns the loop.** Mode A exposes one pristine incumbent per round and measures one
   candidate; the original methods own their own loop, their own candidate count and their
   own trials.
2. **There is no reject step.** A method cannot veto a candidate the platform already
   measured. Every original gate therefore degrades to *a record plus a directive*, and the
   only way to "revert" is to hand back a state directory.
3. **No answer keys, no exam side.** Methods read per-task scores and traces of the side they
   are allowed to study (`INTERFACE.md` §2.3). DGM's gold patch, TTHE's label-free proxies and
   AHE's per-rollout data are all unavailable.
4. **No per-trial rewards.** RRSI's `δ` cannot be re-estimated from the record.
5. **No original logs or artifacts.** State is rebuilt from curve points plus
   `method_reported`; anything the original kept in `history.jsonl`-style files has to be
   re-declared.
6. **Only the newest 12 states are staged** (`STATES_KEPT`), so a rule that wants to build on
   an older best reports "state not staged" rather than guessing a path.
7. **The editor is a single-turn chat call** with JSON repair, not a tool-using agent. The
   `editor` is shared on purpose: it is the control for "the improver", so that a rule can be
   measured with the improver held constant.

---

## 5. What porting found in the originals

Reading someone's code closely enough to reimplement one rule from it is how the following
were found; each is recorded in the porting file rather than in a note somewhere else.

* **DGM**: `--update_archive` defaults to `keep_all`; the `best` branch takes the worst `k`.
* **HyperAgents**: two selectors, one of which discards the statistics it computed.
* **AHE**: `constraint_level` is inert.
* **HarnessX**: two recipes, two different gates; the port names which one it carries.
* **TTHE**: the module's own header declares its sibling modules ablations, not the live loop
  -- which is what decided which file to port.

And one inconsistency of ours, found the same way: `methods/tthe/run.py`'s docstring says
`changed: false` is how a method tells mode A to stop spending rounds, while every rejection
path in that file explicitly passes `stop=False`. The code is right -- rejecting a candidate
is not the same as being finished (`INTERFACE.md`, §`stop`) -- and the sentence is stale.

---

## 6. Why Meta-Harness has no port, and where it would belong instead

Meta-Harness (`stanford-iris-lab/meta-harness-tbench2-artifact`) releases **five files**: a
README, `agent.py`, a prompt template, a caching helper and a `pyproject.toml`. Its README
describes what it is in one paragraph: the agent "extends the Terminus-KIRA agent with
environment bootstrapping" -- before the agent loop starts it gathers a snapshot of the
sandbox (working directory, file listing, languages, package managers, memory) and injects it
into the initial prompt, saving the first few exploration turns. Then: *"The agent was
discovered through automated harness evolution. More details coming soon."*

Two independent reasons it cannot be a method here:

1. **There is no rule to port.** Searched over every published file, the words `score`,
   `verifier`, `reward`, `search`, `evolve`, `candidate`, `accept`, `select`, `mutat` and
   `benchmark` occur **zero** times. There is no acceptance statistic, no cost rule, no
   novelty term, no budget, no critic, no parent rule. The search process that produced it is
   not in the release.
2. **It is not a self-contained harness either.** Its interface is
   `run(instruction, environment: BaseEnvironment, context: AgentContext)`: the environment
   is a first-class argument, and its contribution *is* running commands inside that sandbox.
   Synthesizing a `BaseEnvironment` from a working directory is not possible -- it is
   harbor's sandbox abstraction -- so the adapter records
   `measurable_by_platform: false`, `trajectory_shape: single_point`, and
   `adapters/metaharness.py` says plainly that this one "needs to BE the agent inside
   harbor's sandbox".

**Where it would belong, if we want it.** Not in `methods/` -- it is not a rule. It is a
**harness**: the right home is `base_harness/<name>/`, and the portable part is its
*delta* over Terminus-KIRA (the environment snapshot injected into the first prompt), which
`adapters/metaharness.py:101-103` also records (`inherits: Terminus-KIRA / harbor's
Terminus-2`). That is a genuinely interesting candidate for one reason: our
`terminal_bench` dataset **is** Terminal-Bench 2.0, and the artifact's own README claims
**76.4%** on Terminal-Bench 2.0 -- so a port of the bootstrap onto our reference harness
would put a published, evolution-discovered harness on the same 89 tasks as our own
harnesses, measured by the platform. It is on the list as an experiment, not as an
integration.

---

## 7. How to check any of this

```bash
cd adapters && ./fetch.sh          # downloads every checkout to a sibling methods_src/
column -t SOURCES.md | less        # the digests each port was written against
python3 tools/validate_method.py --help
```

`adapters/SOURCES.md` records the repo, the branch, the file count and a content digest for
each method -- `c0e9bd2f3d16d6b5` for DGM, `1e617c7abdba1c4d` for SICA, and so on. A digest
that no longer matches means the port's citations should be re-read before its claims are
repeated.
