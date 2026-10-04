"""Two scoring policies the platform records, and why they are recorded rather than enforced.

Both come from Anthropic's automated-alignment-researcher work
(`alignment.anthropic.com/2026/automated-alignment-researchers/`), which states them as
evaluator-side rules:

* **The score is a geometric mean.** *"the overall score is the geometric mean of the scored
  benchmarks, so the lowest one binds and all must be lifted"*. An arithmetic mean lets a
  candidate buy a large gain on one task with a small loss on another; the geometric mean
  refuses that trade, which is the property a task set is *for*.
* **A capability floor can disqualify a candidate whatever its score.** *"a pass/fail check
  that disqualifies a method, whatever its score, if the trained model's 95% confidence
  interval on any capability benchmark falls entirely below the base model's"*.

**Neither is enforced here, and that is the design.** This platform has no acceptance rule
(`docs/framework_design.md` §1: the platform records the rule, it does not own it), and a
method cannot be told "do not keep what you already handed over". So both are computed and
written on the curve point, next to the mean:

    "score_geomean": 0.0,                     # the lowest task binds
    "capability_floor": {
        "baseline": "H0",                     # the point everything is compared against
        "violations": [{"task_id": …, "candidate": 0.0, "baseline": 1.0, "gap": -1.0,
                        "ci_entirely_below": true}],
        "checked": 4, "z": 1.96,
    }

A method whose acceptance rule wants Anthropic's gate has everything it needs on the point;
a method that ignores it is not silently overruled. The same split as the noise band: the
number is the platform's, the decision is the method's.

**The measured caveat about the geometric mean.** On a task set whose per-task scores are
binary, a geometric mean is 0 whenever any task fails -- which is every interesting round of
a harness-evolution run. That is not a defect of the formula, it is what "the lowest one
binds" means at the extreme; it becomes informative when task scores are continuous (a
`reward_file` check, or a set large enough that the score is a rate). It is recorded rather
than adopted as *the* score for that reason, and because changing `score` would make every
curve recorded before today incomparable with every curve after it (`INTERFACE.md` §3 is a
frozen contract).

Pure on purpose: dicts in, dicts out. No docker, no network, no filesystem.
"""
from __future__ import annotations

import math

#: The floor a zero is clamped to before the log, so one failed task gives a very small
#: positive number rather than an absolute zero. The number is deliberately tiny: it keeps
#: "the lowest binds" while leaving the result usable as a *number* (a zero cannot be
#: compared with another zero).
ZERO_FLOOR = 1e-6


def geomean(scores: list[float], *, floor: float = ZERO_FLOOR) -> float | None:
    """The geometric mean of per-task scores, with zeros clamped to `floor`.

    `None` for an empty list -- no tasks measured is not a score of zero, the same
    distinction `invalid` draws everywhere else (`INTERFACE.md` §2.5.7).
    """
    values = [float(s) for s in scores if isinstance(s, (int, float))]
    if not values:
        return None
    if any(v < 0 for v in values):
        # A negative per-task score is not something this platform produces, and a log of it
        # is a complex number that would raise deep inside `math`. Refused loudly.
        raise ValueError(f"a per-task score is negative, which the geometric mean cannot "
                         f"take: {min(values)}")
    logs = [math.log(max(v, floor)) for v in values]
    return math.exp(sum(logs) / len(logs))


def studied_per_task(point: dict) -> dict:
    """Per-task scores on the side a method was allowed to study.

    **The platform's copy of a rule that also exists in `methods/protocol.py`.** A method
    runs inside a sandbox that hides the platform, so it cannot import this; the platform
    cannot import a method either. Two copies of one rule is a cost, and it is the same cost
    `methods/apply.py` and `eval/candidate.py` already pay for the same reason -- with the
    same mitigation: a test asserts the two agree (`tests/quality/test_scoring_policies.py`).

    `score_kind` decides, and never a fallback that reaches the exam: `training` means the
    method read exactly these tasks' traces, so `per_task` *is* the diagnostic side; anything
    else (`exam`, or a record predating the field) means `train_score`'s side if it is there,
    and otherwise nothing when a split exists.
    """
    if point.get("score_kind") == "training":
        return dict(point.get("per_task") or {})
    if point.get("train_per_task"):
        return dict(point["train_per_task"])
    if not point.get("split"):
        return dict(point.get("per_task") or {})
    return {}


def floor_violations(candidate: dict, baseline: dict, *,
                     candidate_std: dict | None = None,
                     baseline_std: dict | None = None,
                     z: float = 1.96, tolerance: float = 0.0) -> dict:
    """Tasks where a candidate sits below the baseline, and whether it is below *significantly*.

    Anthropic's criterion is the strict one: the candidate is disqualified when its 95%
    confidence interval on a benchmark lies **entirely below** the base model's. That needs a
    spread, which this platform has only when a run used `--trials > 1`; with one sample the
    honest answer is a point comparison plus `ci_entirely_below: null` -- "we could not ask
    that question" rather than a `false` that reads like a passing grade.

    `tolerance` is subtracted from the baseline before comparing, so a caller can say "a
    tenth of a point below is not a violation" without editing the numbers. It is `0.0` by
    default because the policy this ports has no tolerance.
    """
    out = {"violations": [], "checked": 0, "z": z, "tolerance": tolerance,
           "ci_available": bool(candidate_std and baseline_std)}
    for task, base in sorted(baseline.items()):
        if task not in candidate:
            continue
        out["checked"] += 1
        got = float(candidate[task])
        base_value = float(base)
        gap = got - base_value
        if gap >= -tolerance:
            continue
        entry = {"task_id": task, "candidate": round(got, 4),
                 "baseline": round(base_value, 4), "gap": round(gap, 4)}
        # `z * se` with `se` from the *candidate's* spread across trials: the question is
        # whether this candidate is below a known baseline, not whether two noisy estimates
        # differ.
        std = (candidate_std or {}).get(task)
        if std is not None and z:
            upper = got + z * float(std)
            entry["candidate_ci_upper"] = round(upper, 4)
            entry["ci_entirely_below"] = bool(upper < base_value - tolerance)
        else:
            entry["ci_entirely_below"] = None
        out["violations"].append(entry)
    out["count"] = len(out["violations"])
    out["significant"] = sum(1 for v in out["violations"] if v["ci_entirely_below"] is True)
    return out
