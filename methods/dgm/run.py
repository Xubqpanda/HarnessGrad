#!/usr/bin/env python3
"""DGM's archive rule, as a HarnessGrad method.

Why this file exists
--------------------
DGM's contribution is not the edit -- a coding agent makes that -- it is **the
archive and how a parent is drawn from it**. From its own loop (`DGM_outer.py`):

    score_child_prop:  p(c) ∝ sigmoid(10*(score_c - 0.5)) * 1/(1 + children_c)

A commit's weight rises with its score and falls with how many children it has
already produced, so the search is pushed away from a single lucky ancestor. The
archive keeps every variant and names its `parent_commit`, which is what makes
"children" a computable quantity at all.

What is transplanted
--------------------
* The parent-selection distribution above (`DGM_outer.py:91-100`), with one task
  set in place of a domain list, so a candidate's score is that round's score.
* The child-count penalty `1/(1+children)` (`DGM_outer.py:70-72` defines the
  count; `:91-100` applies it).
* The retention gate `keep_better`: a variant enters the archive iff
  `score >= initial_score - noise_leeway` (`DGM_outer.py:178-185`).
* The shape of entry selection -- pick *which failure to target* before editing
  (`DGM_outer.py:111-148`), rather than editing blind.

What is not transplanted
------------------------
DGM's experiment scaffolding: Docker containers, git-commit-per-variant with
chained `model_patch.diff` replay, the SWE-bench/Polyglot harnesses, and the o1
diagnosis pipeline. The platform supplies all of it -- a task set, execution,
scoring and a record of every round -- so reimplementing it here would be
reimplementing the platform.

What is not reproduced, and two findings worth carrying
-------------------------------------------------------
1. **`--update_archive` defaults to `keep_all`, not `keep_better`**
   (`DGM_outer.py:232`). Our own read-only adapter states the opposite
   (`adapters/dgm.py:145-149`). With `keep_all` the archive is not a filter at
   all: every compiled variant enters, and the only thing that ever leaves the
   parent pool is a commit whose metadata will not load. Both are implemented
   here and the choice is recorded, because a run that silently picks one and
   claims the other is a false claim about someone else's method.
2. **`best` mode is inverted in the shipped code.** `sorted(candidates, key=score)`
   followed by `[:k]` takes the *ascending* sort, i.e. the worst k
   (`DGM_outer.py:101-106`). We do not port that branch.
3. **DGM's strongest input does not exist here.** Its improvement prompt carries
   the gold patch, the private test patch and the test log
   (`prompts/self_improvement_prompt.py:95-106`). The platform's traces exclude
   expected answers on purpose, so this port is **DGM minus its most informative
   signal**, and the copy of DGM we can run is materially weaker than the
   published one. Saying so is part of porting it honestly.

The `noise_leeway` of 0.1 is an absolute accuracy on SWE-bench, where a round
submits hundreds of instances. On a small task set the same 0.1 is many interval
widths wide, so it is a *configured* constant here (see `HG_DGM_NOISE_LEEWAY`),
not a reproduced statistic.

What mode A costs this method
-----------------------------
The platform measures one candidate per round and accepts it unconditionally, so
DGM's parallel children (`selfimprove_size`, sampled with replacement) collapse
to one child per round. What survives is the archive and the sampling rule; what
does not is the branching. The method also cannot decline a candidate -- it can
only choose its parent and record what the archive rule would have retained.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import editor                                                  # noqa: E402
import protocol                                                # noqa: E402

#: DGM's `--eval_noise` default (`DGM_outer.py:238`). Absolute, and calibrated on
#: a benchmark whose rounds submit hundreds of instances -- see the docstring.
NOISE_LEEWAY = float(os.environ.get("HG_DGM_NOISE_LEEWAY", "0.1"))

#: `keep_better` or `keep_all`. Default matches the shipped CLI default
#: (`DGM_outer.py:232`), not the adapter's description of it.
RETAIN = os.environ.get("HG_DGM_RETAIN", "keep_all")

#: The logistic's steepness and centre (`DGM_outer.py:91-100`).
STEEP, CENTRE = 10.0, 0.5

DGM_SYSTEM = """You are improving an agent harness, acting as DGM's self-modification step.

You will be shown one harness and the record of running it on a set of tasks, plus a
specific failure the archive has chosen to target this round.

Reply with JSON only:

  {"files": [{"path": "<relative path>", "content": "<complete new file contents>"}],
   "hypothesis": "<one sentence: what you changed and why>"}

Rules:
- `path` is relative to the harness root. `harness.json` may not be changed.
- `content` must be the COMPLETE new file, not a diff. This may create a new file.
- Generalise: a change that only helps the task you were shown is not an improvement.
  Never hardcode a task's literal value.
- If the evidence does not support any change, reply {"no_change": "<reason>"}.
"""


def _score(point: dict) -> float:
    """The number the archive ranks on.

    Never the exam: ranking parents on `score` when `score` is the exam is the leak the
    split exists to prevent (`INTERFACE.md` §2.3). Which field holds the diagnostic
    score is decided by `score_kind` (§2.6) and written once in
    `protocol.studied_score` -- this used to try `train_score` and fall back to `score`,
    which gave the right number for the wrong reason and would read the exam on any
    point where `score` is the exam.
    """
    return protocol.studied_score(point) or 0.0


def _sigmoid(x: float) -> float:
    # Clamped: `10*(score-0.5)` overflows for |x| beyond ~700, and a score is not
    # bounded to [0,1] in principle, so the guard is not cosmetic.
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + pow(2.718281828459045, -x))


def _initial_score(history: list[dict]) -> float:
    return _score(history[0]) if history else 0.0


def _archive(history: list[dict]) -> list[dict]:
    """What may be a parent, under the chosen retention rule.

    `keep_all` keeps every round with a usable number. `keep_better` keeps only
    those within `NOISE_LEEWAY` of the *initial* score -- DGM's gate compares
    against the original, never against the best archive member
    (`DGM_outer.py:178-185`), so an underperforming child inside the leeway stays
    in the pool forever.
    """
    usable = [p for p in history if isinstance(p.get("score"), (int, float))]
    if RETAIN == "keep_all":
        return usable
    floor = _initial_score(history) - NOISE_LEEWAY
    return [p for p in usable if _score(p) >= floor]


def _child_counts(history: list[dict]) -> dict[int, int]:
    """How many later rounds declared each round as their parent.

    The platform's mode A is a straight chain, so this count would be a constant
    1 for every round but the last and the penalty would be inert. It is not
    inert because *this method declares its own parent* in
    `method_reported.dgm_parent`, and `method_reported` round-trips into the next
    round's history -- so the count measures the lineage this method actually
    built, including the rounds where it chose to branch backwards. That is also
    why the declaration has to be recorded even when the round changes nothing.
    """
    counts: dict[int, int] = {}
    for point in history:
        declared = (point.get("method_reported") or {}).get("dgm_parent")
        if isinstance(declared, int):
            counts[declared] = counts.get(declared, 0) + 1
    return counts


def select_parent(history: list[dict], round_index: int) -> tuple[dict, dict]:
    """The rule. Returns (chosen point, a report of the whole table).

    Sampled rather than argmax'd, because that is what `random.choices(...,
    weights=...)` does (`DGM_outer.py:91-100`) -- taking the argmax would silently
    turn a diversification rule into a greedy one, which is the difference the
    child-count term exists to make.
    """
    archive = _archive(history)
    if not archive:
        return (history[-1] if history else {}), {"archive": [], "rule": "empty"}

    children = _child_counts(history)
    weights = {}
    for point in archive:
        round_no = point.get("round", 0)
        weights[round_no] = (_sigmoid(STEEP * (_score(point) - CENTRE))
                             * 1.0 / (1.0 + children.get(round_no, 0)))
    total = sum(weights.values())
    if total <= 0:
        # Every candidate is at the logistic's floor. Uniform is what DGM falls
        # back to when the weights do not normalize (`DGM_outer.py:107-109`).
        weights = {r: 1.0 for r in weights}
        total = float(len(weights))

    rng = random.Random(round_index * 7919)          # recorded seed; see INTERFACE.md
    pick = rng.random() * total
    chosen, running = archive[-1], 0.0
    for point in archive:
        running += weights[point.get("round", 0)]
        if pick <= running:
            chosen = point
            break

    best = max(archive, key=_score)
    report = {
        "rule": "DGM score_child_prop: sigmoid(10*(s-0.5)) / (1+children)",
        "retain": RETAIN,
        "noise_leeway": NOISE_LEEWAY,
        "archive": [p.get("round") for p in archive],
        "child_counts": {str(k): v for k, v in sorted(children.items())},
        "weights": {str(r): round(w, 6) for r, w in sorted(weights.items())},
        "selected_parent": chosen.get("round"),
        "best_round": best.get("round"),
        "chose_something_other_than_best": chosen.get("round") != best.get("round"),
        "seed": round_index * 7919,
    }
    return chosen, report


def choose_target(parent: dict) -> tuple[str, str]:
    """Which failure this round targets -- the shape of `DGM_outer.py:111-148`.

    DGM chooses between four concrete entries: `solve_empty_patches` (>=10% of
    tasks produced no patch), `solve_stochasticity`, `solve_contextlength`, and
    otherwise a uniformly random unresolved task. The last three are read out of
    SWE-bench's logs and **have no counterpart in a trace that carries no
    expected answer** -- so what is ported is the shape (decide the target before
    editing, from the parent's per-task record) and not DGM's specific entries.

    The record read here is the **train** side. The platform withholds the eval
    side's per-task breakdown from the channel on purpose: a method that can see
    which exam questions are failing can iterate against those specific tasks. Its
    aggregate score is still reported, because a method with no reward signal at
    all is a random walk, not a fairer experiment.
    """
    per_task = protocol.studied_per_task(parent)
    if not per_task:
        return "no per-task record on the parent; edit from the trace alone", "unspecified"
    failing = [tid for tid, s in sorted(per_task.items()) if not s]
    if not failing:
        return "the parent passes every task it is shown", "none"
    share = len(failing) / max(len(per_task), 1)
    if share >= 0.5:
        return (f"{len(failing)}/{len(per_task)} tasks fail -- this is the broad "
                f"failure mode, not a corner case"), "broad-failure"
    return (f"target the {len(failing)} failing task(s): "
            f"{', '.join(failing[:6])}"), "narrow-failure"


def build_prompt(base: Path, req: dict, parent: dict, target: str,
                 report: dict) -> str:
    sources = editor.load_sources(base)
    traces = editor.load_traces(base, limit=6, order=editor.failing_first(base))
    return (
        f"DGM round {req.get('round_index', 1)}. "
        f"The archive chose round {parent.get('round')} as this round's parent "
        f"(score {_score(parent):.3f} on the tasks you can see; "
        f"the best archive member is round {report.get('best_round')}).\n\n"
        f"## What to target\n{target}\n\n"
        f"## What the harness did on the parent\n{traces}\n\n"
        "## The parent's sources\n"
        + "\n\n".join(f"### {k}\n```\n{v}\n```" for k, v in sources.items())
        + "\n\nPropose one change to this parent, or say no_change."
    )


def main() -> int:
    req = editor.read_request()
    editor.require_api(req)
    base = Path(req["base_harness"])
    history = editor.load_history(base)
    round_index = int(req.get("round_index", 1))

    parent, report = select_parent(history, round_index)
    target, target_kind = choose_target(parent)

    # Build on the archive's choice, not on the incumbent. This is the whole
    # point: with only the incumbent's files a method cannot express any rule
    # whose contribution is *which state to build on*, and eight published
    # methods collapse into the same hill-climb.
    parent_dir = _state_dir(base, parent.get("round"))
    if not parent_dir or not Path(parent_dir).is_dir():
        return editor.report(
            req, method="dgm", harness_dir=base, changed=False,
            hypothesis=f"the archive chose round {parent.get('round')} but its state "
                       f"is not staged", edit_kind="dgm_rule",
            method_reported={"dgm": report, "target": target},
            acceptance_rule=report.get("rule", "n/a"), label="DGM: selected state unavailable")

    report["target"] = target
    report["target_kind"] = target_kind

    dest = editor.candidate_path(req)
    try:
        reply = editor.ask(build_prompt(Path(parent_dir), req, parent, target, report),
                           system=DGM_SYSTEM, base=base)
    except SystemExit:
        raise
    except Exception as exc:                                   # noqa: BLE001
        return editor.fail(req, method="dgm", exc=exc, base=base,
                           state={"dgm": report, "dgm_parent": parent.get("round")})

    edits = [] if "no_change" in reply else reply.get("files")
    changed, files, note = editor.apply_edits(Path(parent_dir), dest, edits)
    if "no_change" in reply:
        hypothesis = str(reply["no_change"])[:200]
    elif not changed:
        hypothesis = f"unusable proposal: {note}"
    else:
        hypothesis = str(reply.get("hypothesis", ""))[:200]

    return editor.report(
        req, method="dgm", harness_dir=dest, changed=changed, files=files,
             # 没有停止规则:由平台的 --rounds 封顶,不是由编辑器的不作为。
             stop=False,
        hypothesis=hypothesis, edit_kind="dgm_rule",
        method_reported={"dgm": report,                   # carries dgm_parent
                         "dgm_parent": parent.get("round"),
                         "target": target},
        acceptance_rule=("DGM's archive is maintained by keep_better/keep_all inside "
                         "the method; the platform's measurement decides the score"),
        label=(f"[DGM parent r{parent.get('round')}] " + hypothesis[:60]) if changed
              else f"DGM: no edit (parent r{parent.get('round')})")


def _state_dir(base: Path, round_no) -> str | None:
    """Where the platform staged a previous round's harness.

    `states/index.json` is read rather than the directory guessed, because a state
    that could not be materialized is deliberately absent from the index -- a
    method that guessed the path would find an empty directory and report a
    failure the platform caused.
    """
    import json
    index = editor.channel(base) / "states" / "index.json"
    if not index.exists():
        return None
    try:
        data = json.loads(index.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return (data.get(f"round-{round_no}") or {}).get("harness_dir")


if __name__ == "__main__":
    raise SystemExit(main())
