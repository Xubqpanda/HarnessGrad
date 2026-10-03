# What the methods actually look like

Three method repositories were read (not run) in order to find the common shape
the platform has to accommodate. The findings below are quoted from the source.
Adapters are written only after this file, so that the interface is derived from
the artifacts rather than guessed.

Sources, all fetched as tarballs from `codeload.github.com` (plain `git clone`
times out from this host):

| method | repo | branch | files |
| --- | --- | --- | --- |
| DGM | `jennyzzt/dgm` | `main` | 1650 |
| SICA | `MaximeRobeyns/self_improving_coding_agent` | **`master`** | 141 |
| HyperAgents | `facebookresearch/Hyperagents` | `main` | 145 |

A note on reaching them: plain `git clone` from this host times out, and
`api.github.com` is rate-limited. `https://codeload.github.com/<owner>/<repo>/tar.gz/refs/heads/<branch>`
works, but the branch name must be checked — SICA's default is `master`, not
`main`, and guessing `main` yields a 404 that looks like a missing repository.

---

## Why adapters instead of hoping one interface fits

These three are not variations on one design. They disagree about the most basic
question the platform has to answer: **where is the boundary between the artifact
being improved and the thing doing the improving?**

The three answers:

| method | the method lives | the artifact lives | consequence |
| --- | --- | --- | --- |
| **RRSI** | *outside* the editable surface | `third_party/harbor_terminus2` | improving the harness cannot change the improver |
| **DGM** | *beside* the artifact, as one variant in an archive | one variant per run id | there is no single "the harness" |
| **SICA** | *inside* the artifact | the whole workdir, including `base_agent/` | improving the harness **is** the method |
| **HyperAgents** | *declared as a separate file beside the artifact* | `task_agent.py` + `meta_agent.py` | both are patchable, and each can be rolled back alone |

A platform that assumes any one of these will mis-measure the other two. That is
the argument for adapters, and it is also the argument for the platform staying
ignorant of method internals: the platform cannot tell these apart from the
inside, and should not try.

---

## Shape 1 — RRSI: loop-owning, boundary outside

Already integrated in intent elsewhere; recorded here for contrast.

* `domains/coding/adapter.py:108` — `harness_path = "../../third_party/harbor_terminus2"`.
  The editable surface is pinned to one directory that does not contain the
  search code.
* `rrsi.py:61-62` — the search-rule parameters (`T`, `k`, `delta`, `beta0`, ...)
  are a `OVERRIDES` list consumed as **command-line arguments** (`rrsi.py:70`),
  i.e. reachable by a human, not by the search loop.
* Artifact for the platform: `runs/<domain>/frontier.json` with a `trajectory`
  array of `{t, S, C, commit, job}`, plus `history.jsonl` carrying a
  `component` tag per edit.

**Platform reading**: one repo, one path in it, state = commit. The method can
never appear in `editable_surface_touched`, which is exactly the reading we want
to report.

---

## Shape 2 — DGM: archive of variants, no single lineage

* `DGM_outer.py:19-35` — the run starts from `archive = ['initial']` and reloads
  `metadata['archive']` from a previous run; the archive is a **list of run ids**.
* `DGM_outer.py:331` — state is persisted as `{"archive": archive, ...}`.
* Each variant carries its own `metadata.json`:
  `initial/metadata.json` has `run_id` and
  `overall_performance = {accuracy_score, total_resolved_instances,
  total_submitted_instances, total_unresolved_ids, ...}` — **a score per variant,
  already present**.
* `DGM_outer.py:174-190` — `update_archive(..., method='keep_all'|'keep_better',
  noise_leeway=0.1)`. The `keep_better` rule compares against
  `get_original_score(output_dir) - noise_leeway`, i.e. **a hand-set noise
  tolerance is part of the acceptance rule**.
* `DGM_outer.py:192-196` — `get_full_eval_threshold` decides which variants get
  the expensive full evaluation, using `swe_bench/subsets/{small,medium,big}.json`.

**Platform reading**: a "state" here is a *variant*, and the run is a **tree, not
a sequence**. The curve is well defined (score per variant, and DGM already
records it), but "the harness at round t" is not: a round produces several
children from several parents.

This is the first genuine interface gap, and it is about the **shape of the
trajectory**, not about scoring. The platform's `Trajectory` is a flat list of
steps; for an archive method the honest rendering is either the best-so-far curve
or a family of curves. Both are supported by a flat list plus a `label`, but the
platform must say which one it is drawing rather than implying a lineage.

---

## Shape 3 — SICA: the method is a file in the artifact

* `base_agent/__main__.py:150-160` — the meta-improvement prompt is explicit about
  the target: *"make the coding agent system placed in `/home/sandbox/workdir`
  better at writing code"*.
* `base_agent/__main__.py:113` — `async def improve_agent(workdir, logdir,
  best_iter, current_iter)`: **the working directory handed to the improver is the
  agent's own source**.
* `base_agent/agent_change_log.md` — a markdown change log with
  `| Iteration | Change Name | Was Successful? (pending/yes/no) |`,
  seeded at iteration 0 with "Base Agent". A `Feature Description` /
  `Feature Outcome` template is filled in by the agent itself.

**Platform reading**: the artifact and the improver are the same directory. So
`editable_surface_touched` is *everything*, and the boundary question ("can the
method change its own search rule?") is answered **yes by construction** — not
because SICA crossed a line, but because it never drew one.

This is the shape that makes a "real RSI mode" meaningless as a declaration: the
structural property that distinguishes SICA from RRSI is visible in the diff, and
invisible in any self-description.

---

## Shape 4 — HyperAgents: the two files are named

This is the most interesting shape for the platform's boundary design, because
HyperAgents does not merely permit self-modification — it **names the boundary in
the patch machinery**.

* `utils/gl_utils.py:416` —
  `process_meta_patch_files(meta_patch_files, output_dir, reset_task_agent=False, reset_meta_agent=False)`
  filters a submitted patch by filename:
  `filter_patch_by_files(patch_str, ["task_agent.py"])` when resetting the task
  agent, `["meta_agent.py"]` when resetting the meta agent.
* So one patch can touch both files, and either can be reverted independently.
  **The improver and the improved are separately addressable artifacts.**
* `generate_loop.py:440, 461, 466` — `run_meta_agent` is a runtime flag, and the
  loop distinguishes "meta agent succeeded" from "eval ran".

**And yet HyperAgents reports that it stops short of the outer loop.** Its
Limitations say components of the open-ended exploration loop — *"parent
selection, evaluation protocols"* — **remain fixed**, and that they *"cannot alter
the outer process that determines which agents are selected or how they are
evaluated"*, adding that keeping these fixed is what buys stability and safety.

**Platform reading**: this is the case that settles the boundary design. A system
can make its meta-level source patchable, name the boundary explicitly in its
tooling, and still hold selection and evaluation outside. The distinction between
"real RSI" and "not" is therefore not a property a method can declare — it is a
property of *which paths moved*, which is exactly what
`editable_surface_touched` records. Four methods, four different answers, and all
four are visible in the diff.

---

## What this means for the interface

Confirmed by all three:

1. **State is `(repo, path_in_repo, commit)`**, not a standalone repository.
   RRSI pins a subdirectory; SICA is the whole repo; DGM names a variant id whose
   snapshot lives under `<run_id>/`.
2. **The trajectory is not always a sequence.** DGM's is a tree. The platform must
   expose *which* curve it is drawing.
3. **The acceptance rule is always hand-set and always different** — RRSI has a
   calibrated noise band, DGM a `noise_leeway=0.1` compared against the original
   score. The platform must record the rule, never own it.
4. **Where a scored snapshot lives varies — but not in the way that matters.**
   DGM's scored artifacts sit at `<output_dir>/<run_id>/` (`metadata.json` plus
   `logs/` and `predictions/`), which looks at first like a state that lives
   *outside* the repository and therefore cannot be named by a commit. Checking
   the actual contents disproves this: that directory holds **evaluation output
   only**, and the harness code — `coding_agent.py`, `llm_withtools.py`,
   `tools/`, `prompts/` — sits at the repository root and is tracked by git,
   exactly as `process_meta_patch_files` assumes when it filters patches by
   `task_agent.py` / `meta_agent.py`.

   So both adapted methods reduce to the same thing, differing only in how much
   of the repository is in scope:

   | method | `path_in_repo` |
   | --- | --- |
   | RRSI | `third_party/harbor_terminus2/` |
   | DGM | `.` (repository root) |

   That reading was **wrong**, and the correction matters. Checking where a
   snapshot *lives* answered a different question from how the platform *gets*
   it. The predicted change was withdrawn on the strength of that probe and then
   had to be reinstated once the trace path was actually executed: a step naming
   an earlier commit was measured against the working tree, which still held the
   *later* state, so two distinct harnesses scored identically.

   **The interface did change: a step now carries the harness files it produced**
   (`INTERFACE.md` §4.5). The episode is kept here because it is the clearest
   available evidence for a rule this project already had -- *do not change the
   interface until it has been exercised* -- and because it shows the rule
   cutting the other way: a probe can disprove a change that is in fact needed.

   What survives from the probe is the only part that was ever about *shape*: all
   four methods reduce to `(repo, path_in_repo, commit)`, differing in how much
   of the repository is in scope.
