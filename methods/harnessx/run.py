#!/usr/bin/env python3
"""HarnessX's per-round decision rule, as a HarnessGrad method.

What is transplanted
--------------------
The **acceptance gate** HarnessX applies to the round it has just measured
(`recipe/gaia_evolver/run.py:1174-1257`, mirrored in
`recipe/gaia_evolver/run_meta.py:403-431`):

    cost_delta_ratio = (round_cost - best_cost) / max(best_cost, 1e-3)  # 0 when best_cost == 0
    score            = round_pass_rate - cost_weight * max(cost_delta_ratio, 0)
    revert iff score < best_rate - tolerance
           and abs(round_passed - best_passed) >= pass_count_noise_threshold

* It compares against the **historical best, never the last accepted round**
  (`run.py:1186-1189`), so a loose tolerance cannot drift the baseline downward
  over many rounds; only a strictly higher score displaces the holder
  (`run.py:1246-1255` -- "equal-score rounds do not dethrone the earliest holder").
* A breach the absolute passed-count guard calls noise is **accepted but does not
  update best** (`run.py:1227-1236`), so the next round still compares against the
  same historical high.
* Defaults: GAIA `tolerance 0.03` (`run.py:465`), `pass_count_noise_threshold 3`
  (`recipe/gaia_evolver/defaults.py:23`), `cost_weight 0.0` (`run.py:479`).
  tau2 keeps only the score comparison -- `tolerance 0.02`, `cost_weight 0.0`
  (`recipe/tau2_evolver/defaults.py:36-37`), **no** pass-count guard, an absolute
  rather than relative cost delta (`recipe/tau2_evolver/run.py:1179`), and the
  vocabulary `accept`/`reject` -- so this port carries the richer GAIA kernel and
  says which one it is.

The **pre-registration** is transplanted too: before measurement the meta-agent
declares `hypothesis_id`, `levers`, `predicted_affected`, `rollback_trigger`,
`expected_global_gain`, `regression_risk`, `cost_shift`
(`harnessx/meta_harness/workspace/skills/journal/SKILL.md:40-51`); the orchestrator
grades `predicted_affected` against what actually flipped afterwards
(`harnessx/meta_harness/journal.py:605-667`, `recipe/gaia_evolver/run.py:827-905`).
The lever vocabulary is exactly `{configuration, control, action, instruction}`
(`journal.py:57`, `skills/analyze/SKILL.md:119-124`), and every candidate owes the
"why this lever and not the adjacent one" argument (`analyze/SKILL.md:447`), so
the reply schema asks for it.

What is not transplanted
------------------------
* The **shipping gate** cannot run here at all. Its replay phase *executes* the
  candidate (`meta_harness/replay.py:64-151`), and running a candidate is the
  platform's job, not the method's. The other phases are engine-specific too:
  canonicalize needs `HarnessConfig.from_yaml_file`, the hook-contract check needs
  `MultiHookProcessor`, and the novelty/evidence policy reads HarnessX's own
  journal and `candidates.md` (`meta_harness/validate_workflow.py:857-869`).
* The **edit** itself. HarnessX's contribution is the gate, not the diagnosis, so
  the diagnosis is delegated to the shared editor (`editor.ask`), exactly as
  `sica_ci` does.
* HarnessX's YAML artifact shape and its processor/tool vocabulary. Neither exists
  in this platform's harness contract (`INTERFACE.md` §1.1).

**Not reproduced:** HarnessX's own loop. It runs a multi-round experiment with a
resident meta-agent, its own task sampling, its own scoring and its own rollback;
none of that is reimplementable here without reimplementing the platform. What runs
is one platform round at a time, with the gate transplanted onto the platform's
curve points. **A run of this method is evidence about HarnessX's gate, not a
reproduction of HarnessX's scores.**

What mode A cannot express -- and the weakening that follows
------------------------------------------------------------
Mode A hands a method one pristine incumbent, takes one candidate per round, and
**accepts it unconditionally**: there is no reject step anywhere in the loop. The
gate therefore cannot actually revert anything, and this is a real weakening, not a
faithful revert. All the port can express is:

1. a **record** -- `method_reported["harnessx_gate"]`, carrying the arithmetic
   (rate, cost delta, adjusted score, count delta), the decision and the reason;
2. a **directive** -- on a revert the round's candidate is the historically best
   state copied in verbatim from `_harnessgrad/states/round-<n>/`, and the record
   names that directory as the base the next round must build on
   (`method_reported["harnessx_revert_directive"]`).

HarnessX restores `current_config = best_cfg` inside its own loop
(`recipe/gaia_evolver/run.py:813-820`); here the restore takes effect only because
the platform happens to measure the directory this method hands back. The platform
still measures one candidate per round and cannot be told "do not".

If the best round's state was not materialized -- `states/index.json` keeps only the
newest `STATES_KEPT = 12` points (`driver.py:47`, `driver.py:157-179`) -- the port
records the directive with `state_available: false` and stops (`changed: false`),
rather than edit on top of a base the gate just rejected.

Two smaller divergences, both forced by the record rather than chosen:

* **Cost is not USD.** HarnessX's `round_cost` is dollars; a HarnessGrad curve point
  records trials/tokens/wall-clock (`driver.py:296-306`). The cost term reads
  `cost.harness_tokens` (the harness's own per-round spend) by default, and the
  record names the field it read. With the published default `cost_weight 0.0` the
  term is zero either way.
* **`passed` means a full pass.** HarnessX counts binary passes against a small task
  set; here a task counts only when its `per_task` score is `>= 1.0`. On a graded
  (fractional) task set the absolute count guard would mean something slightly
  different, so the record carries both the count and the rate for every point.

Tunables (environment variables, defaults from the source above):

    HARNESSX_TOLERANCE                  0.03   GAIA --regression-tolerance
    HARNESSX_COST_WEIGHT                0.0    GAIA --cost-weight
    HARNESSX_PASS_COUNT_NOISE_THRESHOLD 3      GAIA PASS_COUNT_NOISE_THRESHOLD
    HARNESSX_COST_FIELD                 harness_tokens
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import editor                                              # noqa: E402


#: The four levers HarnessX allows (`harnessx/meta_harness/journal.py:57`,
#: `workspace/skills/analyze/SKILL.md:119-124`). Recorded as declared, never used to
#: reject an edit: the platform records `edit_kind`, it does not police it.
LEVERS = ("configuration", "control", "action", "instruction")

#: What the record says the method's acceptance rule is. Kept next to the code that
#: implements it, because `editor.report` copies it into every trajectory.
ACCEPTANCE_RULE = (
    "HarnessX best-so-far gate (recipe/gaia_evolver/run.py:1210-1257): "
    "score = pass_rate - cost_weight*max(cost_delta_ratio,0); revert iff "
    "score < best_rate - tolerance AND |passed_delta| >= pass_count_noise_threshold; "
    "a noise-level breach is accepted but does not update best. Mode A has no reject "
    "step, so a revert is handed back as the best state plus a directive"
)


# ------------------------------------------------------------- tunables ---

def _tunables() -> dict:
    """The gate's knobs, read from the environment with the source's defaults.

    Read per call, not at import time, so a test (or an operator) can change one
    without reloading the module. A malformed value falls back to the default rather
    than crashing the round: a method that dies on a typo'd env var reports nothing.
    """
    def _float(name: str, default: str) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return float(default)

    def _int(name: str, default: str) -> int:
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            return int(default)

    return {
        "tolerance": _float("HARNESSX_TOLERANCE", "0.03"),
        "cost_weight": _float("HARNESSX_COST_WEIGHT", "0.0"),
        "pass_count_noise_threshold": _int("HARNESSX_PASS_COUNT_NOISE_THRESHOLD", "3"),
        "cost_field": os.environ.get("HARNESSX_COST_FIELD", "harness_tokens"),
    }


# ------------------------------------------------------- the gate kernel ---

def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _passed(point: dict) -> int | None:
    """HarnessX's `round_passed`: how many of the scored tasks the round passed.

    HarnessX counts tasks whose binary pass is `1.0`. The platform withholds the
    eval side's per-task breakdown from the method channel -- a method that can see
    which exam questions fail can iterate against those specific tasks -- so the
    count is reconstructed from the aggregate instead: `score * n`, where `n` is how
    many tasks the score averages over. On the 0.0/1.0 scores this platform produces
    that is exact, and it is exact for the same reason HarnessX's own pass is binary.

    Returns `None` when the point does not say how many tasks were scored, or when
    the platform produced no score at all, so the caller can tell "zero passes" from
    "no count available". Those are different states and the noise guard must not
    read the second as the first.
    """
    if not point.get("measured_by_platform", True):
        return None
    # `n_passed` is the platform's own count and is exact for any scoring scheme.
    exact = point.get("n_passed")
    if isinstance(exact, int):
        return exact
    # Fallback for a point written before the field existed. Reconstructing the
    # count from the mean is exact only for 0/1 scores; a fractionally-graded
    # dataset would round, which is why the field exists rather than the fallback.
    task_ids = (point.get("sampling") or {}).get("task_ids")
    if not isinstance(task_ids, list) or not task_ids:
        return None
    score = _num(point.get("score"))
    if score is None:
        return None
    return int(round(score * len(task_ids)))


def _round_cost(point: dict, cost_field: str) -> float:
    """The round's cost in the field the platform actually records.

    HarnessX reads USD. A HarnessGrad point records no USD, so the closest analogue
    is `cost.harness_tokens` -- the *harness's* own per-round spend, not the method's
    -- and the record names the field. `cost_weight` defaults to 0.0 in both
    published recipes, so this only matters when someone turns the term on.
    """
    cost = point.get("cost")
    value = cost.get(cost_field) if isinstance(cost, dict) else None
    if value is None and isinstance(cost, dict):
        value = cost.get("method_generation_tokens")
    return _num(value, 0.0)


def _gate_decision(point: dict, best: dict | None, *, tolerance: float,
                   cost_weight: float, pass_count_noise_threshold: int,
                   cost_field: str) -> dict:
    """One call of `_score_and_gate` -- the transplanted kernel.

    `best` is `{rate, cost, round, passed}` or `None`. Returns the gate record plus
    the updated `best` and, on a revert, the round to restore. The two-condition rule
    is the source's: a score breach alone never reverts, because a 1-2 task flip on a
    small set is eval stochasticity, not regression (`run.py:1191-1199`).
    """
    rate = _num(point.get("score"))
    cost = _round_cost(point, cost_field)
    passed = _passed(point)
    record: dict = {
        "graded_round": point.get("round"),
        "round_rate": rate,
        "round_cost": cost,
        "round_passed": passed,
    }

    if best is None:
        new_best = {"rate": rate, "cost": cost,
                    "round": point.get("round"), "passed": passed}
        record.update(
            decision="ACCEPTED",
            reason="first round — no prior to compare against",
            best_round=None, best_rate=None, best_passed=None,
            count_delta=None, cost_delta_ratio=0.0, adjusted_score=rate,
            updated_best_round=new_best["round"], reverted_to_round=None,
            noise_level=False, pass_count_available=passed is not None,
        )
        return {**record, "best": new_best, "revert_to": None}

    best_rate = _num(best.get("rate"))
    best_cost = _num(best.get("cost"))
    # The source's guard: a zero baseline cost means no ratio (run.py:1219), not a
    # division by the 1e-3 floor.
    cost_delta_ratio = ((cost - best_cost) / max(best_cost, 1e-3)
                        if best_cost else 0.0)
    adjusted = rate - cost_weight * max(cost_delta_ratio, 0.0)
    best_passed = best.get("passed")
    count_delta = (None if (passed is None or best_passed is None)
                   else abs(passed - best_passed))
    record.update(
        best_round=best.get("round"), best_rate=best_rate, best_passed=best_passed,
        count_delta=count_delta, cost_delta_ratio=cost_delta_ratio,
        adjusted_score=adjusted, pass_count_available=count_delta is not None,
    )

    if adjusted < best_rate - tolerance:
        # The score check failed. The count check decides whether it is signal.
        if count_delta is None or count_delta < pass_count_noise_threshold:
            why = (f"score {adjusted:.3f} < R{best.get('round')} {best_rate:.3f} - "
                   f"tolerance {tolerance:.3f} but "
                   + ("no passed-task count is available"
                      if count_delta is None
                      else f"|Δpassed|={count_delta} < threshold "
                           f"{pass_count_noise_threshold}")
                   + " — noise-level, accepted without updating best")
            record.update(decision="ACCEPTED", reason=why, noise_level=True,
                          updated_best_round=best.get("round"),
                          reverted_to_round=None)
            return {**record, "best": best, "revert_to": None}
        why = (f"score {adjusted:.3f} < R{best.get('round')} {best_rate:.3f} - "
               f"tolerance {tolerance:.3f} AND |Δpassed|={count_delta} >= threshold "
               f"{pass_count_noise_threshold} — revert to R{best.get('round')}")
        record.update(decision="REVERTED", reason=why, noise_level=False,
                      updated_best_round=best.get("round"),
                      reverted_to_round=best.get("round"))
        return {**record, "best": best, "revert_to": best.get("round")}

    new_best = ({"rate": rate, "cost": cost,
                 "round": point.get("round"), "passed": passed}
                if adjusted > best_rate else best)
    record.update(
        decision="ACCEPTED",
        reason=(f"score {adjusted:.3f} >= R{best.get('round')} {best_rate:.3f} - "
                f"tolerance {tolerance:.3f}"),
        noise_level=False, updated_best_round=new_best.get("round"),
        reverted_to_round=None,
    )
    return {**record, "best": new_best, "revert_to": None}


def _replay_best(points: list[dict], **tun) -> tuple[dict | None, list[dict]]:
    """Rebuild HarnessX's `best_so_far` by replaying the gate over the curve.

    The gate is path-dependent once `cost_weight > 0` (a round's adjusted score is
    measured against the best's cost), so `max(history, key=score)` is *not* the same
    quantity. Replaying the kernel is the faithful reconstruction, and it is cheap.
    """
    best: dict | None = None
    steps: list[dict] = []
    for point in points:
        record = _gate_decision(point, best, **tun)
        best = record["best"]
        steps.append({
            "round": record["graded_round"],
            "decision": record["decision"],
            "adjusted_score": record["adjusted_score"],
            "best_round": record["best_round"],
            "updated_best_round": record["updated_best_round"],
        })
    return best, steps


def _grade(history: list[dict], tun: dict) -> dict:
    """The gate record for the round just measured, plus the replayed best.

    At mode A round `r` the staged `history` is the whole curve so far, so
    `history[-1]` is the round the platform just measured and `history[:-1]` is what
    HarnessX's `best_so_far` had seen when it graded it.
    """
    tun = {k: tun[k] for k in ("tolerance", "cost_weight",
                               "pass_count_noise_threshold", "cost_field")}
    if len(history) < 2:
        last_round = history[-1].get("round") if history else None
        return {
            "decision": "NO_GATE",
            "reason": ("no measured candidate to grade yet — this is the baseline; "
                       "HarnessX's first round only establishes best"),
            "tunables": dict(tun),
            "graded_round": None, "round_rate": None, "round_passed": None,
            "round_cost": None, "best_round": last_round,
            "best_rate": _num(history[-1].get("score")) if history else None,
            "best_passed": None, "count_delta": None, "cost_delta_ratio": 0.0,
            "adjusted_score": None, "updated_best_round": last_round,
            "reverted_to_round": None, "noise_level": False,
            "pass_count_available": False, "history_steps": [],
        }

    best, steps = _replay_best(history[:-1], **tun)
    record = _gate_decision(history[-1], best, **tun)
    out = {k: v for k, v in record.items() if k not in ("best", "revert_to")}
    out["tunables"] = dict(tun)
    out["history_steps"] = steps
    return out


# ------------------------------------------------- previous state + revert ---

def _state_dir(base: Path, round_no) -> str | None:
    """Where the platform staged a previous round's harness.

    `states/index.json` is read rather than the directory guessed, because a state
    that could not be materialized is deliberately absent from the index -- a method
    that guessed the path would find an empty directory and report a failure the
    platform caused (`driver.py:168-172`).
    """
    index = editor.channel(base) / "states" / "index.json"
    if not index.exists():
        return None
    try:
        data = json.loads(index.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return (data.get(f"round-{round_no}") or {}).get("harness_dir")


def _revert_directive(base: Path, best_round, state_dir: str | None) -> dict:
    """The revert, expressed as everything mode A can actually say.

    A record plus a named base directory: the platform will not honour a reject, so
    the only way to "restore best" is to hand the best state back as this round's
    candidate and tell the next round where it came from.
    """
    relative = f"{editor.CHANNEL}/states/round-{best_round}"
    available = bool(state_dir) and Path(state_dir).is_dir()
    return {
        "action": "revert",
        "base_round": best_round,
        "base_state": relative,
        "base_harness_dir": str(state_dir) if available else None,
        "state_available": available,
        "text": (f"build on the historically best state at `{relative}`, because the "
                 f"last round's candidate was reverted by HarnessX's gate"),
    }


def _tree_digest(root: Path) -> dict[str, str]:
    """Content hash of every harness file, ignoring git and the method channel."""
    out: dict[str, str] = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if ".git" in rel.parts or editor.CHANNEL in rel.parts:
            continue
        try:
            out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def _same_tree(a: Path, b: Path) -> bool:
    """Whether two harness directories hold byte-identical files.

    Needed on the revert path: if the state the gate chose for us *is* the incumbent
    (possible when an earlier revert already restored it), reporting `changed: true`
    would mint a checkpoint that differs from its parent by nothing -- the exact
    thing `protocol.emit`'s docstring warns makes a control curve move.
    """
    return _tree_digest(a) == _tree_digest(b)


# ------------------------------------------------------ the editor facade ---

HARNESSX_SYSTEM = """You are the meta-agent of HarnessX, whose acceptance gate \
decides whether the round you are about to propose survives.

You will be shown the harness's source and the record of running it on a set of \
tasks. Propose ONE concrete change that is likely to flip failing tasks without \
breaking passing ones.

Before the platform measures your proposal you must pre-register the hypothesis, \
in HarnessX's schema. Reply with JSON only:

  {"files": [{"path": "<relative path>", "content": "<complete new file contents>"}],
   "hypothesis": "<one sentence: what you changed and why>",
   "hypothesis_id": "h_<short slug>",
   "levers": ["configuration" | "control" | "action" | "instruction"],
   "predicted_affected": ["<task id>", "..."],
   "rollback_trigger": "<observable signal that means revert this next round>",
   "expected_global_gain": "<cluster-level upside, not a single-task anecdote>",
   "regression_risk": "<what could break outside predicted_affected>",
   "cost_shift": "<expected token/cost movement>",
   "lever_argument": "<why this lever and not the adjacent one>"}

The four levers are HarnessX's and they mean what they mean there:
- configuration: kwargs on an existing component, when its tuning is off;
- control: a new mechanical hook around the loop (guard, parse, sanitise);
- action: a new tool, when the agent has no way to take a class of action;
- instruction: a prompt/template edit, when capability and control are fine but the \
agent does not know when or in what order to use them.
Every candidate owes the "why this lever and not the adjacent one" argument.

Rules:
- `path` is relative to the harness root. `harness.json` may not be changed.
- `content` must be the COMPLETE new file, not a diff. This may create a new file.
- `predicted_affected` lists the task ids you claim will flip F->T (or, for a \
preservative change, the passing ids you claim to protect). Over-predicting is \
graded against you.
- If the evidence supports no change, reply {"no_change": "<reason>"}.
"""


def _preregistration(reply: dict) -> dict:
    """HarnessX's journal frontmatter, as declared by the model before measurement.

    Recorded verbatim plus one derived flag. `levers_valid` is derived because
    HarnessX's `append_entry` refuses unknown levers (`journal.py:155-157`); here the
    platform records rather than polices, so an invalid declaration is reported, not
    silently dropped.
    """
    levers = reply.get("levers")
    if not isinstance(levers, list):
        levers = []
    levers = [str(lever) for lever in levers]
    return {
        "hypothesis_id": reply.get("hypothesis_id"),
        "levers": levers,
        "levers_valid": bool(levers) and all(lever in LEVERS for lever in levers),
        "predicted_affected": reply.get("predicted_affected"),
        "rollback_trigger": reply.get("rollback_trigger"),
        "expected_global_gain": reply.get("expected_global_gain"),
        "regression_risk": reply.get("regression_risk"),
        "cost_shift": reply.get("cost_shift"),
        "lever_argument": reply.get("lever_argument"),
        "source": "reply",
    }


def _previous_preregistration(history: list[dict]) -> dict | None:
    """The graded round's own prediction, read back from the curve point.

    `_curve_point` copies the method's `method_reported` onto the point
    (`driver.py:850-851`), which is what makes it readable next round -- the platform
    records the prediction beside the measurement, and grading is a separate act.
    """
    if not history:
        return None
    reported = history[-1].get("method_reported")
    if isinstance(reported, dict):
        value = reported.get("harnessx_preregistration")
        if isinstance(value, dict):
            return {**value, "source": "previous_round_record"}
    return None


def build_prompt(base: Path, request: dict, gate: dict, revert_directive: dict | None,
                 prereg: dict | None) -> str:
    """The edit is made in the light of the gate's verdict, not independently of it."""
    sources = editor.load_sources(base)
    traces = editor.load_traces(base, limit=6, order=editor.failing_first(base))
    lines = [
        f"HarnessX evolve round {request.get('round_index', 1)}. The harness scored "
        f"{request.get('incumbent_score')} on the task set.",
        "",
        "## HarnessX's gate on the round just measured",
        f"- decision: {gate.get('decision')}",
        f"- {gate.get('reason')}",
    ]
    if gate.get("best_round") is not None:
        lines.append(f"- historical best: round {gate.get('best_round')} at "
                     f"{gate.get('best_rate')} (compared against the best, never the "
                     f"last accepted round)")
    if revert_directive:
        lines.append(f"- {revert_directive.get('text')}")
    if prereg:
        lines.append(f"- the round's pre-registered hypothesis was "
                     f"`{prereg.get('hypothesis_id')}` on levers "
                     f"{prereg.get('levers')}")
    lines += [
        "",
        f"## What the harness did last round\n{traces}",
        "",
        "## Current harness sources",
        "\n\n".join(f"### {name}\n```\n{body}\n```"
                    for name, body in sources.items()),
        "",
        "Propose one change and pre-register it, or say no_change.",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------- main ---

def _reported(gate: dict, prereg: dict | None, directive: dict | None,
              **extra) -> dict:
    """The one shape every exit path writes, so no path can forget the gate."""
    return {
        "harnessx_gate": gate,
        "harnessx_preregistration": prereg,
        "harnessx_revert_directive": directive,
        **extra,
    }


def main() -> int:
    request = editor.read_request()
    editor.require_api(request)

    base = Path(request["base_harness"])
    history = editor.load_history(base)
    tun = _tunables()
    gate = _grade(history, tun)
    graded_prereg = _previous_preregistration(history)

    # ---- the revert branch: the only way mode A can express "reject" ----------
    if gate["decision"] == "REVERTED":
        best_round = gate["reverted_to_round"]
        state_dir = _state_dir(base, best_round)
        directive = _revert_directive(base, best_round, state_dir)
        if not directive["state_available"]:
            return editor.report(
                request, method="harnessx", harness_dir=base, changed=False,
                stop=False,
                hypothesis=(f"HarnessX gate reverted round {gate.get('graded_round')} "
                            f"to R{best_round}, but that state is not staged; refusing "
                            f"to build on a base the gate rejected"),
                edit_kind="harnessx_revert",
                method_reported=_reported(gate, graded_prereg, directive),
                acceptance_rule=ACCEPTANCE_RULE,
                label=f"HarnessX gate: revert to R{best_round} (state not staged)")

        dest = editor.candidate_path(request)
        editor.apply_edits(Path(directive["base_harness_dir"]), dest, [])
        same = _same_tree(Path(directive["base_harness_dir"]), base)
        return editor.report(
            request, method="harnessx", harness_dir=dest, changed=not same,
            hypothesis=(f"HarnessX gate reverted round {gate.get('graded_round')}: "
                        f"{gate.get('reason')}")[:300],
            edit_kind="harnessx_revert",
            method_reported=_reported(
                gate, graded_prereg, directive,
                candidate_equals_incumbent=same),
            acceptance_rule=ACCEPTANCE_RULE,
            label=f"HarnessX gate: revert to R{best_round}")

    # ---- the accept branch: spend the editor on a new candidate ---------------
    dest = editor.candidate_path(request)
    try:
        reply = editor.ask(
            build_prompt(base, request, gate, None, graded_prereg),
            system=HARNESSX_SYSTEM, base=base)
    except SystemExit:
        raise
    except Exception as exc:                                   # noqa: BLE001
        # `editor.fail` now carries the method's own record and continues the run.
        # Both matter here: the gate's arithmetic is what the next round grades
        # against, and a transient 500 is not a decision by this method.
        return editor.fail(
            request, method="harnessx", exc=exc, base=base,
            state={"harnessx_gate": gate,
                   "harnessx_preregistration": _preregistration({})})

    prereg = _preregistration(reply)

    if "no_change" in reply:
        editor.apply_edits(base, dest, [])
        return editor.report(
            request, method="harnessx", harness_dir=dest, changed=False,
            stop=False,
            hypothesis=str(reply["no_change"])[:200], edit_kind="harnessx_rule",
            method_reported=_reported(gate, prereg, None),
            acceptance_rule=ACCEPTANCE_RULE,
            label="HarnessX gate: accepted, no edit")

    changed, files, note = editor.apply_edits(base, dest, reply.get("files"))
    if not changed:
        return editor.report(
            request, method="harnessx", harness_dir=dest, changed=False,
            stop=False,
            hypothesis=f"unusable proposal: {note}", edit_kind="harnessx_rule",
            method_reported=_reported(gate, prereg, None, rejected=note),
            acceptance_rule=ACCEPTANCE_RULE,
            label="HarnessX gate: accepted, unusable proposal")

    lever_tag = "+".join(prereg["levers"]) if prereg["levers_valid"] else "harnessx_rule"
    return editor.report(
        request, method="harnessx", harness_dir=dest, changed=True, files=files,
        hypothesis=str(reply.get("hypothesis", ""))[:300],
        edit_kind=f"harnessx:{lever_tag}",
        method_reported=_reported(gate, prereg, None),
        acceptance_rule=ACCEPTANCE_RULE,
        extra={"preregistered_hypothesis": prereg.get("hypothesis_id")},
        label=(f"[HarnessX {lever_tag}] "
               + str(reply.get("hypothesis", ""))[:50]))


if __name__ == "__main__":
    raise SystemExit(main())
