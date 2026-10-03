#!/usr/bin/env python3
"""AHE's per-round decision rule, as a HarnessGrad method.

Why this file exists
--------------------
AHE (Agentic Harness Engineering) reads like "an agent edits a harness and keeps
whatever scores better". The code says something narrower and more specific, and
that is what is transplanted here. One AHE iteration is:

1. **Pre-registration before measurement.** The evolve agent writes
   `change_manifest.json` at the experiment root, with one entry per change
   declaring `predicted_fixes` and `risk_tasks`
   (`agents/evolve_agent/evolve_prompt.md:195-218`).
2. **Attribution after measurement.** The next iteration loads the previous
   manifest (`evolve.py:2200-2219`, called at `evolve.py:4380`) and grades each
   declared change against the observed per-task transition
   (`evaluate_changes`, `evolve.py:2239-2329`):

       HARMFUL              risk hit > 0 and nothing predicted was fixed
       MIXED                both a predicted fix and a risk hit
       EFFECTIVE            fixed == predicted > 0
       PARTIALLY_EFFECTIVE  0 < fixed < predicted
       INEFFECTIVE          otherwise                        (`evolve.py:2294-2303`)

   Note the precedence, which is easy to get wrong: MIXED is tested *before*
   EFFECTIVE, so a change that fixed everything it promised and also regressed a
   declared risk task is MIXED, not EFFECTIVE.
3. **The verdict becomes an instruction.** `evolve.py:2837-2843` renders
   KEEP / IMPROVE / ROLLBACK+PIVOT, and the component-level meta-rule is stated
   in the prompt: *"If the same failure class persists across 2+ iterations
   despite fixes at one component level, that level may be the wrong choice.
   Rollback the ineffective change and re-approach from a different component
   level"* (`evolve_prompt.md:103`; also `:172-175`).

What is transplanted
--------------------
* `observed_diff()` -- fail->pass = `flipped`, pass->fail = `regressed`, exactly
  AHE's task transition classification (`evolve.py:842-843`, `:886-889`). AHE's
  pass threshold is `reward >= 1.0` (`evolve.py:634`); HarnessGrad's
  per-task scores are 0.0/1.0 (`eval/runner.py:168-172`), so `PASS_THRESHOLD = 1.0` maps
  exactly and there is nothing to calibrate.
* `evaluate_changes()` -- AHE's attribution, including its verdict precedence and
  its `unattributed_regressions` (`evolve.py:2318-2320`).
* `abandoned_levels()` -- the 2+-iterations-at-one-level half of the meta-rule
  (`evolve_prompt.md:103`), computed from the same attribution over all past
  rounds instead of being left for the model to notice in prose.
* The rollback: AHE's own instruction is *"restore files that need rollback to
  that version"* (`evolve.py:2860-2861`). Mode A's expression of that is to
  build this round's candidate on `_harnessgrad/states/round-<n-2>` -- the state
  before the harmful change -- so the rollback is actually performed rather than
  merely requested. `states/` exists on this channel for exactly this
  (`driver.py:134-140`).
* The stop rule: `target_metric >= target_pass_rate` (`evolve.py:4395-4421`),
  whose default is 0.95 (`configs/base.yaml:10`).

What is not transplanted
------------------------
* AHE's component taxonomy and mount points (`evolve_prompt.md:86-97`). The
  harness here is whatever repository the platform was handed; the level is
  carried only as the declared string `constraint_level`.
* **`constraint_level` is inert in AHE, and it stays inert here.** It appears
  exactly once in the whole repository, in a prompt example
  (`evolve_prompt.md:213`), and no Python reads it (`grep -rn constraint_level
  --include=*.py` over AHE returns nothing). This method *records* it -- including
  copying it into the platform's `edit_kind`, which is the field that exists for
  "the method's own word for what kind of change this was" (`INTERFACE.md` §4.6)
  -- and never enforces it.
* AHE's experiment scaffolding: `init_workspace`, the `runs/iteration_NNN/` tree,
  best-of-N variants, the agent-debugger QA pass, the git tag/commit per change.
  None of it is the decision rule, and the platform already supplies the loop.
* `perform_auto_rollback` (`evolve.py:2418-2454`) is deliberately **not** ported
  as a per-round mechanism: it is called only on resume (`evolve.py:4164-4172`)
  to restore the workspace to the start iteration, i.e. it is a resumption
  device, not an acceptance gate.

What mode A cannot express, stated plainly
------------------------------------------
Mode A hands one pristine incumbent, measures one candidate per round, and
carries it forward unconditionally. Therefore:

* **The cross-iteration attribution itself is expressible** -- it only needs the
  next round's per-task results, and mode A stages every curve point as history.
  That is the part this file ports.
* **AHE's k-rollout aggregation is not.** AHE marks a task as passed only when
  *all* k rollouts pass (`evolve.py:602`, `:650-670`) and uses the pass@1
  estimator as the headline metric (`:695`). The platform records exactly one
  score per task per round, so there is neither a per-task rollout count nor a
  pass@k signal to attribute against. One trial per task is the degenerate k=1
  case, which is the only case AHE itself runs without rollout data.
* **AHE's full tool-using editor is not.** The evolve agent reads analysis
  reports and edits the workspace with tools; this method is the shared one-shot
  editor (`methods/editor.py`), so it gets one model call per round.
* **Best-of-N parallel variants are not.** One candidate per round means no
  cross-variant comparison.
* **There is no acceptance gate, and none is invented.** The rollback in AHE is
  *decided by the model* and performed by it copying files back -- `evolve.py:4377`
  says the attribution is "report only, rollback decided by evolve agent" -- and
  the loop carries the workspace forward after every iteration. The only
  quantitative judgement is the attribution verdict. The mechanical base swap in
  `choose_edit_dir()` is a rollback, not an acceptance test: it does not gate
  anything, and the platform still measures whatever candidate comes out.

Two consequences worth naming, because they change what a curve from this method
means. First, the platform has no `exception` state: AHE separates
`exception -> pass` (infra recovery, `evolve.py:844-845`) from a real flip, but a
HarnessGrad timeout is simply a 0.0, so infra noise can register as a flip.
Second, mode A's round is the atomic unit of change, so a HARMFUL verdict rolls
back the *whole* previous round, while AHE rolls back one declared change at a
time.

A correction to our own adapter
-------------------------------
`adapters/ahe.py:193-198` records AHE's acceptance rule as *"keep the candidate
if PASS@1 beats the incumbent's best-so-far (Algorithm 1 line 14)"*. That claim
is not in this code. No branch in the main loop rejects or reverts a workspace on
score; the loop runs to `max_iterations` or the target pass rate, and the only
rollback is the resume-time one plus the model-executed attribution rollback
above. The adapter's provenance field should be corrected, but this port does not
touch it.

The honest summary: **the decision rule is AHE's; the loop, the editor and the k
are the platform's.** A run of this method is evidence about AHE's attribution
rule, not a reproduction of AHE's results.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import editor                                              # noqa: E402
import protocol                                            # noqa: E402

#: AHE passes a task when `reward >= 1.0` (`evolve.py:634`). HarnessGrad's
#: per-task scores are 0.0/1.0 (`eval/runner.py:168-172`), so this is an identity,
#: not a calibration.
PASS_THRESHOLD = 1.0

#: `target_pass_rate` default (`configs/base.yaml:10`), read at `evolve.py:4153`
#: and compared at `evolve.py:4396`. The environment variable is AHE's own config
#: knob transplanted to the method's config surface.
DEFAULT_TARGET_PASS_RATE = 0.95

#: `evolve.py:2837-2843`, verbatim. The verdict drives the instruction, and the
#: mapping is AHE's, not ours.
SUGGESTED_ACTION = {
    "EFFECTIVE": "Keep",
    "PARTIALLY_EFFECTIVE": "Keep, continue monitoring",
    "MIXED": "Keep effective parts, rollback harmful parts",
    "INEFFECTIVE": "Rollback or redesign",
    "HARMFUL": "Must rollback",
}

#: `evolve_prompt.md:201-218`, AHE's `change_manifest.json` shape. `constraint_level`
#: is declared prose and is recorded, never enforced (see the module docstring).
MANIFEST_SCHEMA = """{
  "iteration": <this round's number>,
  "changes": [
    {
      "id": "chg-1",
      "type": "new|improvement|rollback",
      "description": "What was changed and why",
      "files": ["relative/to/harness/file.py"],
      "failure_pattern": "The failure class this addresses",
      "predicted_fixes": ["task-id-a", "task-id-b"],
      "risk_tasks": ["task-id-c"],
      "constraint_level": "middleware|tool_impl|tool_desc|skill|prompt",
      "why_this_component": "Why this component level was chosen over alternatives"
    }
  ]
}"""

AHE_SYSTEM = """You are the evolution step of AHE (Agentic Harness Engineering), \
improving an agent harness. Every change must be traceable to specific failure \
evidence: which tasks failed, what the root cause was, and which tasks the fix \
might put at risk (evolve_prompt.md:17-25).

Reply with JSON only:

  {"files": [{"path": "<relative path>", "content": "<complete new file contents>"}],
   "hypothesis": "<one sentence: what you changed and why>",
   "changes": [ <AHE change_manifest entries, see below> ]}

The `changes` list is AHE's change manifest. It is a PRE-REGISTRATION: it is written
before the platform measures this candidate, and the next iteration grades it against
the tasks that actually flipped.

""" + MANIFEST_SCHEMA + """

Rules:
- `path` is relative to the harness root. `harness.json` may not be changed.
- `content` must be the COMPLETE new file, not a diff. This may create a new file.
- `predicted_fixes` and `risk_tasks` must be ids from the scored task set named in
  the request. A HARMFUL verdict (a declared risk task regressed and nothing was
  fixed) forces a rollback, so do not declare risks carelessly -- but do declare
  them, because an undeclared regression becomes an unattributed one.
- `constraint_level` is one of middleware|tool_impl|tool_desc|skill|prompt. It is
  recorded as your declaration; nothing enforces it.
- If the evidence does not support any change, reply {"no_change": "<reason>"}.
"""


# --------------------------------------------------------- AHE's statistics ---

def _as_list(value) -> list:
    """AHE indexes `predicted_fixes`/`risk_tasks` as lists. A model that returns a
    bare string should not be iterated character by character, so it is wrapped;
    that is a robustness deviation from `evolve.py:2277-2278`, recorded here."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _is_pass(value) -> bool:
    """AHE's `reward >= 1.0` (`evolve.py:632-636`). `bool` is excluded because it
    is an `int` in Python and a JSON `true` is not a score.

    The platform's per-task scores are 0.0/1.0 today (`eval/runner.py:168-172`),
    so this is exact. A dataset that ever reports partial credit would be read as
    "anything below full credit is a fail" -- which is AHE's own threshold applied
    unchanged, not a new one invented here."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and float(value) >= PASS_THRESHOLD)


def observed_diff(before: dict, after: dict) -> dict:
    """The task transition AHE's attribution reads: fail->pass and pass->fail.

    This is `compute_iteration_diff`'s classification (`evolve.py:842-843`,
    `:886-889`) restricted to the two lists `evaluate_changes` consults. AHE also
    classifies `exception -> pass` as `infra_recovered` rather than `flipped`; the
    platform records no exception state, so a timeout is a 0.0 and can enter
    `flipped`. That is a real difference in what may be attributed, and it is
    named rather than papered over.

    This reads the **train** side, and that is the right side for attribution
    rather than a concession. The question attribution answers is "did my change
    do what I predicted", which is mechanistic and belongs on the tasks the method
    studies. The eval side answers "did it generalise", and the platform withholds
    its per-task breakdown from the channel precisely so a method cannot iterate
    against the specific questions it is scored on. Its aggregate score is still
    reported -- that is the reward signal, and without one no method can climb.
    """
    # `studied_per_task`, not `train_per_task`: §2.6 put a train run's own per-task
    # scores in `per_task` and emptied `train_*` (on a single-side run it would be a
    # copy). Reading the old field here returned `{}` on every train run, so `shared`
    # was always empty and attribution silently reported that nothing had moved.
    b = protocol.studied_per_task(before)
    a = protocol.studied_per_task(after)
    shared = sorted(set(b) & set(a))
    flipped, regressed, stable_pass, stable_fail = [], [], [], []
    for tid in shared:
        was, now = _is_pass(b[tid]), _is_pass(a[tid])
        if not was and now:
            flipped.append(tid)
        elif was and not now:
            regressed.append(tid)
        elif was and now:
            stable_pass.append(tid)
        else:
            stable_fail.append(tid)
    return {
        "flipped": flipped,
        "regressed": regressed,
        "stable_pass": stable_pass,
        "stable_fail": stable_fail,
        "compared": shared,
        # Tasks AHE would call `exception`: present on one side only. Listed so a
        # reader can see why they are in neither transition list.
        "only_before": sorted(set(b) - set(a)),
        "only_after": sorted(set(a) - set(b)),
    }


def evaluate_changes(manifest: dict, diff: dict) -> dict:
    """AHE's change attribution, `evolve.py:2239-2329`.

    Ported branch for branch, including the precedence that makes MIXED win over
    EFFECTIVE when a declared risk task regressed (`evolve.py:2294-2299`). The
    only change is that `predicted_fixes`/`risk_tasks` are coerced to lists so a
    malformed reply is graded as an empty declaration instead of being iterated
    as a string.

    Returns AHE's structure: `change_evaluations` (one entry per declared change,
    with `actually_fixed`, `still_failed`, `risk_realized`, `hit_rate`, `verdict`),
    `unattributed_regressions` (`evolve.py:2318-2320`) and a one-line `summary`.
    """
    changes = manifest.get("changes") or []
    flipped_set = set(diff.get("flipped") or [])
    regressed_set = set(diff.get("regressed") or [])

    all_predicted: set = set()
    all_risk: set = set()
    evaluations = []
    for chg in changes:
        if not isinstance(chg, dict):
            continue
        chg_id = str(chg.get("id") or "unknown")
        predicted = _as_list(chg.get("predicted_fixes"))
        risks = _as_list(chg.get("risk_tasks"))
        all_predicted.update(predicted)
        all_risk.update(risks)

        actually_fixed = [t for t in predicted if t in flipped_set]
        still_failed = [t for t in predicted if t not in flipped_set]
        risk_realized = [t for t in risks if t in regressed_set]
        n_fixed, n_predicted, n_risk_hit = (len(actually_fixed), len(predicted),
                                            len(risk_realized))

        # The order is AHE's (`evolve.py:2294-2303`), and it matters: a declared
        # risk hit demotes a fully-fixed change from EFFECTIVE to MIXED.
        if n_risk_hit > 0 and n_fixed == 0:
            verdict = "HARMFUL"
        elif n_risk_hit > 0 and n_fixed > 0:
            verdict = "MIXED"
        elif n_fixed == n_predicted and n_predicted > 0:
            verdict = "EFFECTIVE"
        elif n_fixed > 0:
            verdict = "PARTIALLY_EFFECTIVE"
        else:
            verdict = "INEFFECTIVE"

        evaluations.append({
            "change_id": chg_id,
            "description": chg.get("description", ""),
            "files": chg.get("files", []),
            "predicted_fixes": predicted,
            "actually_fixed": actually_fixed,
            "still_failed": still_failed,
            "predicted_risks": risks,
            "risk_realized": risk_realized,
            "hit_rate": f"{n_fixed}/{n_predicted}" if n_predicted > 0 else "0/0",
            "verdict": verdict,
        })

    attributed = all_predicted | all_risk
    unattributed = [t for t in regressed_set if t not in attributed]
    return {
        "evaluating_iteration": manifest.get("iteration", 0),
        "change_evaluations": evaluations,
        "unattributed_regressions": sorted(unattributed),
        "summary": ", ".join(f"{e['change_id']}: {e['verdict']}" for e in evaluations),
    }


def manifest_of(point: dict) -> dict | None:
    """The pre-registration recorded on a curve point, if any.

    AHE loads the previous manifest from an experiment-root file and guards
    against staleness by checking `iteration` (`evolve.py:2200-2219`). Here the
    manifest is written into `method_reported` of the point it belongs to, so it
    cannot be read a round late and no such check is needed.

    One reading of a manifest is *which round declared it*, and that is recorded:
    the manifest in point `i` was declared by the method call that produced point
    `i`, so it predicts the transition `i-1 -> i`.
    """
    reported = point.get("method_reported") or {}
    manifest = reported.get("ahe_manifest")
    if isinstance(manifest, dict) and isinstance(manifest.get("changes"), list):
        # An empty manifest is returned rather than hidden: AHE's loader finds the
        # file and `evaluate_changes` then produces no evaluations
        # (`evolve.py:2267`), which reads differently from "no manifest at all"
        # (`evolve.py:4389-4390`). Both readings are kept.
        return manifest
    return None


def attribution_history(history: list[dict]) -> list[dict]:
    """Grade every manifest the curve holds, oldest first.

    This reconstructs AHE's per-iteration attribution series: for each point `i`
    that carries a manifest, compare `history[i-1]` with `history[i]`
    (`evolve.py:4371-4384`). `records[-1]` is therefore *this* round's grading of
    the immediately preceding round, and the earlier records are what the
    2+-iterations rule reads.
    """
    records = []
    for i in range(1, len(history)):
        diff = observed_diff(history[i - 1], history[i])
        manifest = manifest_of(history[i])
        records.append({
            "round": history[i].get("round"),
            "manifest": manifest,
            "diff": diff,
            "evidence": evaluate_changes(manifest, diff) if manifest else None,
        })
    return records


def abandoned_levels(records: list[dict]) -> dict[str, list[str]]:
    """The 2+-iterations-at-one-level rule, computed (`evolve_prompt.md:103`).

    "If the same failure class persists across 2+ iterations despite fixes at one
    component level, that level may be the wrong choice." Operationalised as: the
    same `(failure_pattern, constraint_level)` pair was declared in at least two
    distinct rounds and never earned EFFECTIVE. Returns
    `{failure_pattern: [levels to stop retrying]}`.

    AHE leaves this to the model reading `evolution_history.md`; computing it is
    how the rule survives a one-shot editor that cannot browse history. A change
    with no `failure_pattern` cannot participate -- a real gap, and the reason the
    system prompt asks for the field.
    """
    tried: dict[str, dict[str, dict]] = {}
    for record in records:
        manifest = record.get("manifest") or {}
        by_id = {str(e.get("change_id")): e
                 for e in ((record.get("evidence") or {}).get("change_evaluations") or [])}
        for chg in manifest.get("changes") or []:
            if not isinstance(chg, dict):
                continue
            pattern = str(chg.get("failure_pattern") or "").strip()
            level = str(chg.get("constraint_level") or "").strip()
            if not pattern or not level:
                continue
            entry = tried.setdefault(pattern, {}).setdefault(
                level, {"rounds": set(), "effective": 0})
            # A set, not a list: the same round declaring the same pattern twice
            # is one iteration, and the rule counts iterations
            # (`evolve_prompt.md:103`).
            entry["rounds"].add(record.get("round"))
            if (by_id.get(str(chg.get("id"))) or {}).get("verdict") == "EFFECTIVE":
                entry["effective"] += 1
    return {
        pattern: sorted(level for level, s in levels.items()
                        if len(s["rounds"]) >= 2 and s["effective"] == 0)
        for pattern, levels in tried.items()
        if any(len(s["rounds"]) >= 2 and s["effective"] == 0 for s in levels.values())
    }


# --------------------------------------------------------- state selection ---

def _state_dir(base: Path, round_no) -> str | None:
    """A previous state's directory, from the index the platform wrote
    (`driver.py:162-179`). Never guessed from a path: a missing state is reported,
    not resolved to an empty directory."""
    import json
    index = editor.channel(base) / "states" / "index.json"
    if not index.exists():
        return None
    try:
        data = json.loads(index.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return (data.get(f"round-{round_no}") or {}).get("harness_dir")


def choose_edit_dir(base: Path, history: list[dict],
                    record: dict | None) -> tuple[Path, str]:
    """Which harness this round edits, and why.

    AHE rolls back by restoring the ineffective change's files to the previous
    iteration's version (`evolve.py:2860-2861`). At mode A's granularity -- one
    candidate per round -- the previous round *is* the harmful change, so the
    faithful rollback is to build the candidate on the state two rounds back
    (`history[-2]`), which is exactly what `states/` stages for a method
    (`driver.py:134-140`).

    Only HARMFUL triggers this. INEFFECTIVE's own instruction is "rollback **or**
    redesign" (`evolve.py:2841`), so the model decides and the base stays the
    incumbent. A rollback that cannot be staged is reported and the incumbent is
    used, rather than failing the round over a platform-side window.
    """
    verdicts = [e.get("verdict")
                for e in ((record or {}).get("evidence") or {}).get("change_evaluations", [])]
    if "HARMFUL" not in verdicts:
        return base, ""
    if len(history) < 2:
        return base, "a rollback was requested but no earlier state is on the curve"
    before = history[-2]
    staged = _state_dir(base, before.get("round"))
    if staged and Path(staged).is_dir():
        return Path(staged), (f"the HARMFUL change was rolled back: this round edits "
                              f"round-{before.get('round')}, the state before it")
    return base, (f"a rollback to round-{before.get('round')} was requested but that "
                  f"state is not staged; restore the harmful change's files yourself")


# ------------------------------------------------------------- the prompt ---

def render_attribution(record: dict | None) -> str:
    """AHE's auto-generated attribution report (`evolve.py:2825-2861`)."""
    if record is None:
        return ("## Previous iteration change attribution report\n"
                "No earlier round carries a change manifest yet, so AHE's "
                "attribution step has nothing to grade (`evolve.py:4379-4380`).")
    if not record.get("manifest"):
        return (f"## Previous iteration change attribution report\n"
                f"Round {record.get('round')} declared no `change_manifest`, so "
                f"AHE's attribution step is skipped (`evolve.py:4389-4390`).")
    evidence = record.get("evidence") or {}
    evaluations = evidence.get("change_evaluations") or []
    if not evaluations:
        return (f"## Previous iteration change attribution report\n"
                f"Round {record.get('round')}'s manifest declared no changes, so "
                f"there is nothing to attribute (`evolve.py:2267`).")
    lines = [
        "## Previous iteration change attribution report (auto-generated)",
        "Use this report to decide whether to roll back the previous iteration's "
        "changes. HARMFUL and INEFFECTIVE changes are the ones to prioritize for "
        "rollback (`evolve.py:2829-2830`).",
        "",
        "| Change | Predicted Fixes | Actually Fixed | Regressions | Verdict | Suggested Action |",
        "|--------|-----------------|----------------|-------------|---------|------------------|",
    ]
    for e in evaluations:
        n_pred = len(e.get("predicted_fixes") or [])
        n_fixed = len(e.get("actually_fixed") or [])
        lines.append(
            f"| {e.get('change_id')}: {str(e.get('description') or '')[:40]} "
            f"| {n_fixed}/{n_pred} "
            f"| {', '.join(str(t) for t in (e.get('actually_fixed') or [])[:5]) or '-'} "
            f"| {', '.join(str(t) for t in (e.get('risk_realized') or [])[:5]) or '-'} "
            f"| {e.get('verdict')} | {SUGGESTED_ACTION.get(e.get('verdict'), '?')} |")
    for e in evaluations:
        if e.get("still_failed"):
            lines.append(f"- {e.get('change_id')} predicted but not fixed: "
                         f"{e['still_failed']}")
    unattr = evidence.get("unattributed_regressions") or []
    if unattr:
        lines.append(
            f"- Unattributed regression tasks: {unattr} -- these were in no change's "
            f"predicted_fixes or risk_tasks, so they may be interaction effects of "
            f"several changes (`evolve.py:2855-2858`).")
    lines.append(
        f"- Rollback method: restore the files of a rolled-back change to their "
        f"round-{int(record.get('round') or 0) - 1} contents; the state is staged "
        f"under `_harnessgrad/states/` (`evolve.py:2860-2861`).")
    return "\n".join(lines)


def render_pivot(record: dict | None,
                 abandoned: dict[str, list[str]]) -> str:
    """The rollback/pivot half: AHE's KEEP/IMPROVE/ROLLBACK+PIVOT plus the
    component-level rule (`evolve_prompt.md:103`, `:172-175`)."""
    by_id: dict[str, dict] = {}
    if record and record.get("manifest"):
        for chg in record["manifest"].get("changes") or []:
            if isinstance(chg, dict):
                by_id[str(chg.get("id"))] = chg
    evaluations = ((record or {}).get("evidence") or {}).get("change_evaluations") or []
    lines = []
    for e in evaluations:
        chg = by_id.get(str(e.get("change_id"))) or {}
        level = chg.get("constraint_level")
        pattern = chg.get("failure_pattern")
        n_fixed = len(e.get("actually_fixed") or [])
        n_pred = len(e.get("predicted_fixes") or [])
        if e.get("verdict") == "HARMFUL":
            tail = (f"then re-approach failure class {pattern!r} from a different "
                    f"component level -- not {level!r} (`evolve_prompt.md:103`)."
                    if level else
                    f"then re-approach the same failure pattern from a different "
                    f"component level (`evolve_prompt.md:103`).")
            lines.append(
                f"- {e.get('change_id')} is HARMFUL: {len(e.get('risk_realized') or [])} "
                f"declared risk task(s) regressed and {n_fixed}/{n_pred} predicted "
                f"task(s) were fixed. Its files must be rolled back, {tail}")
        elif e.get("verdict") == "INEFFECTIVE":
            lines.append(
                f"- {e.get('change_id')} is INEFFECTIVE: roll it back or redesign it. "
                f"If the same failure class has now persisted at {level or 'this'} "
                f"level for 2+ iterations, that level is the wrong choice -- "
                f"re-approach from a different component level "
                f"(`evolve_prompt.md:103`, `:175`).")
        elif e.get("verdict") == "MIXED":
            lines.append(
                f"- {e.get('change_id')} is MIXED: keep its effective parts and roll "
                f"back the parts that regressed {e.get('risk_realized') or []} "
                f"(`evolve.py:2840`).")
    for pattern, levels in sorted(abandoned.items()):
        lines.append(
            f"- Failure class {pattern!r} was addressed at "
            f"{', '.join(repr(level) for level in levels)} in 2+ iterations and never "
            f"became EFFECTIVE; do not "
            f"retry it there. Roll back and re-approach from a different component "
            f"level (`evolve_prompt.md:103`).")
    if not lines:
        return ("## Rollback / pivot instruction\n"
                "No change was judged HARMFUL or INEFFECTIVE, so there is no rollback "
                "or pivot instruction this round (`evolve.py:2837-2843`).")
    return ("## Rollback / pivot instruction (AHE's component-level rule)\n"
            + "\n".join(lines))


def build_prompt(base: Path, edit_dir: Path, req: dict, history: list[dict],
                 record: dict | None, abandoned: dict[str, list[str]],
                 rollback_note: str) -> str:
    """The edit request, framed by the attribution verdict.

    The verdict is not decoration: it is the part of AHE that decides what this
    round is allowed to be. The traces come from the channel (they are staged in
    the harness directory, `INTERFACE.md` §4.7); the sources come from whichever
    state this round edits, which after a rollback is an earlier state.
    """
    incumbent = history[-1] if history else {}
    scored = list(req.get("task_ids") or [])
    shown = list(req.get("train_task_ids") or protocol.studied_task_ids(incumbent))
    traces = editor.load_traces(base, limit=6, order=editor.failing_first(base))
    sources = editor.load_sources(edit_dir)

    where = ("the incumbent" if edit_dir == base
             else f"round-{history[-2].get('round')} (rolled back)")
    parts = [
        f"AHE iteration {req.get('round_index', 1)}. The incumbent harness scored "
        f"{incumbent.get('score')} on the scored task set.",
        "",
        "## The scored task set (what the attribution grades)",
        f"Scored task ids: {scored or '(none named)'}",
        f"Train task ids whose traces you are shown: {shown or '(none)'}",
        "`predicted_fixes` and `risk_tasks` must name ids from the scored set; the "
        "attribution only sees those (`evolve.py:2283-2285`).",
        "",
        render_attribution(record),
        "",
        render_pivot(record, abandoned),
    ]
    if rollback_note:
        parts += ["", f"Rollback: {rollback_note}."]
    parts += [
        "",
        f"## What the harness did (scored-set diagnostics)\n{traces}",
        "",
        f"## Sources of {where}",
        "\n\n".join(f"### {k}\n```\n{v}\n```" for k, v in sources.items()),
        "",
        "Propose one change, declare its manifest, or say no_change.",
    ]
    return "\n".join(parts)


# ------------------------------------------------------------- the manifest ---

def build_manifest(reply: dict, round_index: int, written: list[str]) -> dict:
    """This round's pre-registration, in AHE's `change_manifest.json` shape.

    AHE's evolve agent writes the manifest itself (`evolve_prompt.md:195-218`),
    so the fields are taken from the model's reply. A reply that declares nothing
    still yields a manifest -- empty, and marked `declared: false` -- because the
    next round must be able to tell "no prediction was made" from "the method
    never ran". Grading it produces no `change_evaluations` and leaves every
    regression unattributed (`evolve.py:2267`, `:2318-2320`); that is a reading of
    the round, not a missing measurement.
    """
    changes: list[dict] = []
    items = reply.get("changes") if isinstance(reply.get("changes"), list) else []
    declared = bool(items)
    if not items:
        flat_keys = {"predicted_fixes", "risk_tasks", "constraint_level",
                     "failure_pattern"}
        if any(k in reply for k in flat_keys):
            items, declared = [reply], True
    for i, chg in enumerate(items):
        if not isinstance(chg, dict):
            continue
        changes.append({
            "id": str(chg.get("id") or f"chg-{i + 1}"),
            "type": chg.get("type"),
            "description": str(chg.get("description") or ""),
            "files": _as_list(chg.get("files")) or list(written),
            "failure_pattern": chg.get("failure_pattern"),
            # The pre-registration, written before the platform measures.
            "predicted_fixes": _as_list(chg.get("predicted_fixes")),
            "risk_tasks": _as_list(chg.get("risk_tasks")),
            # Declared prose, recorded and never enforced (see module docstring).
            "constraint_level": chg.get("constraint_level"),
            "why_this_component": chg.get("why_this_component"),
        })
    manifest = {"iteration": round_index, "changes": changes,
                "declared": bool(declared and changes)}
    if not manifest["declared"]:
        manifest["note"] = ("the model declared no change manifest this round, so "
                            "the next iteration has no prediction to grade")
    return manifest


def _target_pass_rate() -> float:
    """AHE's `target_pass_rate` (`configs/base.yaml:10`), overridable the way any
    AHE config value is."""
    raw = os.environ.get("HG_AHE_TARGET_PASS_RATE")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_TARGET_PASS_RATE


def _edit_kind(manifest: dict, changed: bool) -> str:
    """AHE's own word for the change, made machine-readable.

    `INTERFACE.md` §4.6 records `constraint_level` as the example of a vocabulary
    that exists only in AHE's prose and that no code reads. It is carried here as
    the declared level, recorded and never interpreted by this method.
    """
    if changed:
        for chg in manifest.get("changes") or []:
            level = chg.get("constraint_level")
            if level:
                return str(level)
        return "ahe_change"
    return "ahe_rule"


AHE_RULE_TEXT = (
    "AHE has no acceptance gate: every iteration is carried forward "
    "(`evolve.py:4377`), the attribution is report-only, and the rollback is "
    "performed by the model restoring files (`evolve.py:2860-2861`). The only "
    "verdict is the attribution's EFFECTIVE/MIXED/HARMFUL/... judgement "
    "(`evolve.py:2294-2303`); the platform's measurement decides the score.")


# ------------------------------------------------------------------ main ---

def main() -> int:
    request = editor.read_request()
    editor.require_api(request)

    base = Path(request["base_harness"])
    round_index = int(request.get("round_index", 1))
    history = editor.load_history(base)
    records = attribution_history(history)
    # The newest record grades the immediately preceding round -- the manifest
    # that round declared (`evolve.py:4371-4384`).
    record = records[-1] if records else None
    abandoned = abandoned_levels(records)
    edit_dir, rollback_note = choose_edit_dir(base, history, record)
    target = _target_pass_rate()

    incumbent = history[-1] if history else {}
    incumbent_score = incumbent.get("score")
    if not isinstance(incumbent_score, (int, float)):
        incumbent_score = request.get("incumbent_score")

    verdicts = {str(e.get("change_id")): e.get("verdict")
                for e in ((record or {}).get("evidence") or {}).get("change_evaluations", [])}
    rule_report = {
        "rule": "AHE: pre-registered predictions graded by per-task attribution; "
                "the verdict drives KEEP/IMPROVE/ROLLBACK+PIVOT",
        "round": round_index,
        "target_pass_rate": target,
        "previous_round": (record or {}).get("round"),
        "verdicts": verdicts,
        "suggested_actions": {k: SUGGESTED_ACTION.get(v, "?") for k, v in verdicts.items()},
        "abandoned_levels": abandoned,
        "rollback": rollback_note or None,
        "edited_from": str(edit_dir),
        "pass_threshold": PASS_THRESHOLD,
    }

    # AHE's stop rule, `evolve.py:4395-4421`: the loop breaks at the target pass
    # rate rather than evolving again. `changed: false` is how mode A is told the
    # same thing (`INTERFACE.md` §4.55); spending a model call here would be
    # spending it to stand still.
    if isinstance(incumbent_score, (int, float)) and incumbent_score >= target:
        stopped = {"iteration": round_index, "changes": [], "declared": False,
                   "stopped": True,
                   "note": f"not declared: the incumbent scored {incumbent_score} >= "
                           f"target {target} (`evolve.py:4396`)"}
        return editor.report(
            request, method="ahe", harness_dir=base, changed=False,
            hypothesis=(f"stopped by AHE's target rule: incumbent score "
                        f"{incumbent_score} >= target pass rate {target}"),
            edit_kind="ahe_stop",
            method_reported={"ahe_rule": rule_report, "ahe_manifest": stopped,
                             "ahe_attribution": (record or {}).get("evidence")},
            acceptance_rule=AHE_RULE_TEXT,
            extra={"ahe_rule": rule_report["rule"]},
            label=f"AHE: stop at target ({incumbent_score} >= {target})")

    dest = editor.candidate_path(request)
    try:
        reply = editor.ask(
            build_prompt(base, edit_dir, request, history, record, abandoned,
                         rollback_note),
            system=AHE_SYSTEM, base=base)
    except SystemExit:
        raise
    except Exception as exc:                                   # noqa: BLE001
        return editor.fail(request, method="ahe", exc=exc, base=base,
                           state={"ahe_manifest": record})

    if "no_change" in reply:
        manifest = build_manifest({}, round_index, [])
        editor.apply_edits(edit_dir, dest, [])
        return editor.report(
            request, method="ahe", harness_dir=dest, changed=False,
                                                     stop=False,
            hypothesis=str(reply["no_change"])[:200], edit_kind="ahe_rule",
            method_reported={"ahe_rule": rule_report, "ahe_manifest": manifest,
                             "ahe_attribution": (record or {}).get("evidence")},
            acceptance_rule=AHE_RULE_TEXT,
            extra={"ahe_rule": rule_report["rule"]},
            label="AHE: no change")

    changed, files, note = editor.apply_edits(edit_dir, dest, reply.get("files"))
    manifest = build_manifest(reply, round_index, files)
    if not changed:
        manifest["note"] = f"the proposal was unusable: {note}"
    hypothesis = (f"unusable proposal: {note}" if not changed
                  else str(reply.get("hypothesis", ""))[:300])

    return editor.report(
        request, method="ahe", harness_dir=dest, changed=changed, files=files,
        hypothesis=hypothesis,
        edit_kind=_edit_kind(manifest, changed),
        method_reported={"ahe_rule": rule_report, "ahe_manifest": manifest,
                         "ahe_attribution": (record or {}).get("evidence")},
        acceptance_rule=AHE_RULE_TEXT,
        extra={"ahe_rule": rule_report["rule"]},
        label=(f"[AHE {manifest['changes'][0].get('id') if manifest['changes'] else '-'}]"
               f" {hypothesis[:50]}") if changed else "AHE: unusable proposal")


if __name__ == "__main__":
    raise SystemExit(main())
