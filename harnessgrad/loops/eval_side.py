"""The eval side: measure the state a train run left behind, and evolve nothing.

A run covers exactly one side (§2.6). This schedule takes a harness state out of a finished
train run's record, measures it on the tasks that side holds, and writes one point. There is
no method here and no rounds: an exam that can be retaken is not an exam. The join back to
the train point (`evaluated_from.harness_sha`) is what makes the train/eval pair
reconstructible from two records rather than assumed from two runs started close together.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import time

from ckpt.git_state import commit_state
from eval.console import CONSOLE as console
from eval.runner import evaluate
from harnessgrad.environments import _env_record
from harnessgrad.environments import _report_harness_failed
from harnessgrad.environments import _report_invalid
from harnessgrad.identity import _identity
from harnessgrad.records import _cost_of
from harnessgrad.records import _curve_point
from harnessgrad.records import _require_untampered
from harnessgrad.records import _save_traces
from harnessgrad.records import _save_verdicts
from harnessgrad.records import _scored
from harnessgrad.records import _write_curve

def _eval_source(args, runs_root: Path) -> tuple[Path, int]:
    """The harness state an `--side eval` run measures, and which round it was.

    An eval run has no evolution in it: it takes a state a **train** run produced and
    puts a number on it. The state is `round<N>_base/` inside that run's record, which
    is a complete harness tree -- measured: 16 files including `harness.json` -- so the
    rest of the run (staging, the manifest, the header, the sandbox) works on it with no
    special case at all.

    Refuses with the rounds that *are* available, because "round 3 of a run that stopped
    at 1" is a typo a reader can fix in one second and a silent fallback would hide.
    """
    if not args.from_run:
        raise SystemExit(
            "--side eval measures a harness state, so it needs --from-run <train run id>. "
            "There is nothing to evaluate otherwise: an eval run does not evolve anything.")
    src = Path(runs_root) / args.from_run
    if not src.is_dir():
        raise SystemExit(f"no run {args.from_run!r} under {runs_root}")
    # `state_after_round<N>` is the current name and `round<N>_base` is the older one:
    # records made before states were kept in the run directory used that, and an eval
    # run should be able to point at them too. Both are complete harness trees.
    def _rounds(pattern: str) -> dict[int, Path]:
        out: dict[int, Path] = {}
        for p in src.glob(pattern):
            if not p.is_dir():
                continue
            digits = "".join(c for c in p.name if c.isdigit())
            if digits:
                out[int(digits)] = p
        return out

    found = _rounds("state_after_round*") or _rounds("round*_base")
    if not found:
        raise SystemExit(
            f"run {args.from_run!r} keeps no harness state to evaluate. Every round "
            f"should leave one as `state_after_round<N>/`; a record without one predates "
            f"that, and there is nothing to measure.")
    want = args.from_round if args.from_round is not None else max(found)
    if want not in found:
        raise SystemExit(
            f"run {args.from_run!r} has no round {want}; it has {sorted(found)}")
    return found[want], want

def _eval_from_record(runs_root: Path, run_id: str, round_no: int) -> dict:
    """What the train run's own record says about the state being measured.

    Read rather than recomputed: the link has to survive on the record, and a reader of
    the eval run should be able to follow it to the train run's point without running
    anything. `harness_sha` is the join key and it is on both points already.
    """
    curve = Path(runs_root) / run_id / "curve.jsonl"
    if not curve.exists():
        return {"run_id": run_id, "round": round_no}
    for line in reversed(curve.read_text().splitlines()):
        if not line.strip():
            continue
        try:
            p = json.loads(line)
        except json.JSONDecodeError:
            continue
        if p.get("round") == round_no:
            return {"run_id": run_id, "round": round_no,
                    "harness_sha": (p.get("identity") or {}).get("harness_sha"),
                    "train_score": p.get("score") if p.get("side") != "eval" else None}
    return {"run_id": run_id, "round": round_no}

def _run_eval_side(*, work: Path, man: dict, run_id: str, run_dir: Path, tasks: list[dict],
                   scorable: dict, setups, verifiers, envs, side_ids, split, args,
                   eval_from: dict | None, improver: dict | None = None) -> int:
    """Measure one harness state on the exam side. INTERFACE.md §2.6.

    No method, no rounds, one point: an eval run does not evolve anything, so there is
    nothing for a method to do and nothing to iterate. It exists so that a train run can
    stop carrying the exam at all -- the eval traces are then not a field this platform
    withholds from the method channel, they are a side that is not in the train run.

    The state is staged and committed like any harness, so the point carries the
    `harness_sha` of what was measured and a reader can join it to the train run's point
    for the same round. That join is the train-vs-eval pair, reconstructed from two
    records instead of living on one point.
    """
    cache: dict = {}
    cumulative = {"evaluation_trials": 0, "generation_tokens": 0,
                  "env_generation_tokens": 0, "wall_clock_s": 0.0}
    sha = commit_state(work, f"eval of {args.from_run} round "
                             f"{(eval_from or {}).get('round')}")
    t0 = time.time()
    res = evaluate(work, tasks, scorable, cache, harness_sha=sha, sandbox=args.sandbox,
                   setups=setups, verifiers=verifiers, envs=envs, run_id=run_id,
                   run_seed=os.environ["HARNESSGRAD_SEED"],
                   recordings_root=run_dir / "recordings", round_no=0)
    _require_untampered(res)
    _report_invalid(res, args.dataset)
    _report_harness_failed(res, args.dataset)

    scored = _scored(res, side_ids)
    if not scored:
        console.note(
            f"refusing to write a point: none of the {len(tasks)} eval tasks produced a "
            f"scoreable result, so there is no exam score to record.", level="error")
        return 2

    cumulative["evaluation_trials"] = len(tasks)
    cumulative["wall_clock_s"] = time.time() - t0
    cumulative["env_generation_tokens"] = sum(
        (res.get("env_usage") or {}).get(k, 0) for k in ("input", "output"))

    point = _curve_point(
        run_id=run_id, round_index=0,
        identity=_identity(man, sha, args.mode, improver=improver,
                           declared=res.get("identity_declared")),
        sampling={"policy": "all", "task_ids": list(scored),
                  "selection_basis_round": 0, "selected_within": "eval"},
        scores=[res["per_task"][t] for t in scored], task_ids=scored,
        train_scores=None, train_ids=None, split=split,
        env_record=_env_record(envs, scored),
        invalid=res.get("invalid"),
        harness_failed=res.get("harness_failed"),
        model_gateway=res.get("model_gateway"),
        harness_runtime=res.get("harness_runtime"),
        cost=_cost_of(res, len(tasks), cumulative["wall_clock_s"]),
        cumulative=cumulative, label="exam", side="eval",
        evaluated_from=eval_from)
    point["measured_by_platform"] = True
    point["verifier_kinds"] = sorted({(verifiers.get(t) or {}).get("kind", "answer")
                                      for t in scored})
    _save_verdicts(run_dir, 0, res, [t["task_id"] for t in tasks])
    _save_traces(run_dir, 0, res.get("traces") or {}, None, split)
    _write_curve(run_dir / "curve.jsonl", [point])
    console.results([point], split=split)
    return 0
