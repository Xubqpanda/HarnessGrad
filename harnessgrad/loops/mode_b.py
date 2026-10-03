"""Mode B: the method runs its own loop and hands back complete harness directories.

The method lives outside the harness. It is given a pristine copy of the base harness and a
scratch directory, and it writes a trajectory naming one **complete harness directory** per
step; the platform swaps each named directory into a pristine base and measures it.

A directory rather than a commit id, because the method's states live in the method's own
version control and the platform cannot resolve them -- the first version accepted a name,
measured whatever the working tree happened to hold, and scored two distinct harnesses the
same while the curve looked plausible. A method that hands over a directory cannot have that
harness become something else by the time it is measured.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import shlex
import shutil
import tempfile
import time

from ckpt.git_state import commit_state
from ckpt.git_state import reset_hard
from eval.console import CONSOLE as console
from eval.runner import evaluate
from harnessgrad import PLATFORM_API_VERSION
from harnessgrad.channel import _stage_for_method
from harnessgrad.environments import _env_record
from harnessgrad.environments import _report_harness_failed
from harnessgrad.environments import _report_invalid
from harnessgrad.identity import _identity, _improver_identity, _improver_runtime
from harnessgrad.methods import _call_method
from harnessgrad.records import _cost_of
from harnessgrad.records import _curve_point
from harnessgrad.records import _keep_method_logs
from harnessgrad.records import _require_untampered
from harnessgrad.records import _save_round_diff
from harnessgrad.records import _save_round_state
from harnessgrad.records import _save_traces
from harnessgrad.records import _save_verdicts
from harnessgrad.records import _scored
from harnessgrad.records import _write_curve
from harnessgrad.records import _write_run_meta
import eval.candidate as candidate

def _run_mode_b(args, work, man, run_id, mode, curve, h0_sha, run_dir, base,
                tasks, task_ids, scorable, sampling, cache,
                cumulative, train_ids=None, split=None,
                verifier_kinds=None, setups=None, verifiers=None,
                envs=None, improver=None) -> list[dict]:
    """Mode B: an EXTERNAL method produces candidate harnesses; we measure them.

    The method lives outside the harness. It is handed a pristine copy of the
    base harness and a scratch directory, and it writes a trajectory naming one
    **complete harness directory** per step. The platform then swaps each named
    harness in and measures it with its own scorer, on its own fixed task set.

    Why this shape and not "the method edits our harness in place": the platform
    must be able to measure what the method actually produced. When a method
    edited the workspace directly, a step that named an earlier state was
    measured against the working tree, which still held the later one -- two
    distinct harnesses scored identically and the curve looked plausible. A
    method that hands over a directory cannot have that failure.
    """
    budget = {"rounds": args.rounds, "generation_tokens": None,
              "wall_clock_s": None, "note": "caps only; mode B owns its rhythm"}

    argv = args.method_entrypoint
    if not argv:
        console.note("mode B needs --method-entrypoint pointing at an external "
                     "method", level="error")
        return curve
    if isinstance(argv, str):
        argv = shlex.split(argv)

    # A pristine copy of the base harness, so a method cannot accidentally (or
    # deliberately) be measured against the state it left behind.
    # See `_run_mode_a` for why this is a temporary directory rather than a path under
    # the run directory or the work root.
    method_root = Path(tempfile.mkdtemp(prefix=f"hg-method-{run_id}-")).resolve()
    base_dir = method_root / "base_harness"
    if base_dir.exists():
        shutil.rmtree(base_dir)
    shutil.copytree(work, base_dir, ignore=shutil.ignore_patterns(".git"))

    method_ws = method_root / "workspace"
    method_ws.mkdir(parents=True, exist_ok=True)
    traj_path = method_root / "trajectory.json"

    # Mode B stages the channel too, and until now it did not: `_stage_for_method` was
    # called from the mode A loop alone, so a mode B method was handed a base harness
    # with no `_harnessgrad/` at all -- no traces, no `round.json`, no history, none of
    # what §4.7 promises it. The method runs its whole search in one call, so there is
    # one incumbent and no earlier rounds: `curve[-1]` is that incumbent and `curve` is
    # the (single) point it can see.
    _stage_for_method(work, curve[-1], base.get("traces") or {},
                      history=[], task_ids=train_ids, run_dir=run_dir,
                      tasks=tasks, setups=setups, verifiers=verifiers,
                      result=base)

    t_gen = time.time()
    reply = _call_method(work, argv, {
        "platform_api_version": PLATFORM_API_VERSION,
        "mode": "B",
        "base_harness": str(base_dir),
        "workspace": str(method_ws),
        "task_ids": task_ids,
        "train_task_ids": list(train_ids or []),
        "budget": budget,
        "trajectory_out": str(traj_path),
        # See `_run_mode_a`: the improver travels with the request, because a method
        # cannot resolve it from inside its own sandbox.
        "improver": _improver_identity(improver),
    }, args.method_timeout, sandboxed=args.sandbox,
        scratch=method_root, cwd=method_ws, readonly=(base_dir,),
        runtime=_improver_runtime(improver), improver=improver)
    gen_s = time.time() - t_gen
    cumulative["wall_clock_s"] += gen_s
    cumulative["generation_tokens"] += int(reply.get("generation_tokens") or 0)
    _keep_method_logs(method_ws, run_dir, 0)

    if "_error" in reply:
        stderr = " ".join((reply.get("_stderr") or "").split())
        why = reply["_error"] + (f" -- {stderr[-400:]}" if stderr else "")
        console.note(f"mode B entrypoint failed: {why}", level="error")
        console.detail(**{k: reply.get(k) for k in ("_stderr", "_stdout")})
        return curve
    if not traj_path.exists():
        console.note(f"mode B: method reported success but wrote no trajectory to "
                     f"{traj_path}", level="error")
        return curve

    traj = json.loads(traj_path.read_text())
    steps = traj.get("steps") or []
    if not steps:
        console.note("mode B: trajectory has no steps", level="error")
        return curve

    shape = traj.get("trajectory_shape", "sequence")
    curve_drawn = traj.get("curve_drawn", "per_step")
    acceptance = (traj.get("provenance") or {}).get("acceptance_rule")
    console.note(f"trajectory {shape}, curve drawn {curve_drawn}")

    nominated = traj.get("nominated")
    nominated = len(steps) if nominated is None else nominated

    failed_steps: list[dict] = []

    for i, step in enumerate(steps, start=1):
        label = step.get("label", "")
        src = step.get("harness_dir")
        if not src or not Path(src).is_dir():
            console.note(f"mode B: step {i} ({label!r}) names no harness_dir the "
                         f"platform can read; skipping rather than guessing",
                         level="error")
            continue

        # Swap the candidate harness in and measure it as the platform's own.
        reset_hard(work, h0_sha)
        for child in list(work.iterdir()):
            if child.name == ".git":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        shutil.copytree(Path(src), work, dirs_exist_ok=True)
        # **The channel is never part of a measured tree.** `src` is method-supplied, so
        # it can name anything -- and on the error path `editor.fail` names the pristine
        # `base_dir`, which is a copy of `work` taken *after* staging and therefore
        # carries `_harnessgrad/`. Measured: a failed round put the channel back into
        # `work`, and from there into `state_after_round<N>` -- which is the tree an eval
        # run starts from. The harness runs inside `work`, so that put the instruction and
        # the verifier's gold inside the subject's own reach; §4.7's boundary is the
        # channel **to the method**, not a directory the artifact can read.
        shutil.rmtree(work / "_harnessgrad", ignore_errors=True)

        # See `_run_mode_a`: the platform re-checks the candidate it is about to measure.
        candidate_problems = candidate.validate(work, base_manifest=man)
        has_manifest = not candidate_problems
        # A half-finished step is not a measurement, in mode B as in mode A.
        step_error = ((step.get("method_reported") or {}).get("error")
                      or step.get("error"))
        if step_error:
            console.note(f"step {i}: the step did not finish -- {step_error}",
                         level="error")
            console.round_line(i, "step failed", level="error",
                               note=str(step_error)[:120])
            failed_steps.append({"round": i, "why": str(step_error)[:400]})
            continue

        sha = commit_state(work, f"step {i}: {label or 'method step'}")
        _save_round_diff(run_dir, i, work, curve[-1]["identity"]["harness_sha"], sha)
        _save_round_state(run_dir, i, work)
        t0 = time.time()
        if has_manifest:
            res = evaluate(work, tasks, scorable, cache, harness_sha=sha,
                           sandbox=args.sandbox, setups=setups,
                           verifiers=verifiers, envs=envs, run_id=run_id,
                           run_seed=os.environ["HARNESSGRAD_SEED"],
                           recordings_root=run_dir / "recordings", round_no=i)
        else:
            res = None
        _require_untampered(res)
        # mode A 的循环里,这两件事都算了出来却没有对任何人说过。invalid 与
        # harness_failed 是同一个毛病:「平台知道了一个事实,然后不告诉读的人」。
        _report_invalid(res, args.dataset)
        _report_harness_failed(res, args.dataset)
        dt = time.time() - t0
        cumulative["wall_clock_s"] += dt
        if res is not None:
            cumulative["evaluation_trials"] += len(tasks)
            cumulative["env_generation_tokens"] += (res.get("env_usage") or {}).get(
                "input", 0) + (res.get("env_usage") or {}).get("output", 0)
            _save_verdicts(run_dir, i, res, task_ids)
            _save_traces(run_dir, i, res.get("traces") or {}, train_ids, split)

        point = _curve_point(
            run_id=run_id, round_index=i,
            identity=_identity(man, sha, mode,
                               declared=(res or {}).get("identity_declared"),
                               improver=improver),
            sampling=sampling,
            scores=[res["per_task"][t] for t in _scored(res, task_ids)],
            task_ids=_scored(res, task_ids),
            train_scores=[res["per_task"][t] for t in _scored(res, train_ids)],
            train_ids=_scored(res, train_ids),
            invalid=(res or {}).get("invalid"),
            harness_failed=(res or {}).get("harness_failed"),
            model_gateway=(res or {}).get("model_gateway"),
            harness_runtime=(res or {}).get("harness_runtime"),
            split=split,
            env_record=_env_record(envs, task_ids if res else []),
            cost=_cost_of(res, len(tasks), dt,
                          step.get("claimed_cost", {}).get("generation_tokens")),
            cumulative=cumulative,
            selection={
                "selected_on_reported_set": traj.get("selected_on_reported_set"),
                "selection_pool_size": traj.get("selection_pool_size"),
            },
            label=label + (" [nominated]" if i == nominated else ""),
            hypothesis=step.get("hypothesis") or (reply or {}).get("hypothesis"),
            edits_applied=step.get("files") or (reply or {}).get("files"),
            candidate_problems=candidate_problems,
            method_reported=step.get("method_reported") or {})
        point["edit_kind"] = step.get("edit_kind")
        point["measured_by_platform"] = res is not None
        point["verifier_kinds"] = verifier_kinds
        if res is None:
            # Named problems, not "no harness.json": a candidate can be rejected for a
            # manifest that no longer parses, an entrypoint that is gone, Python that does
            # not compile, or a widened `env_kinds`. The old single sentence covered one
            # of those four, so the other three reached a reader as `score 0.000`.
            point["unmeasured"] = {
                "reason": "the candidate harness is not one the platform can measure, so "
                          "there is no measurement to report",
                "problems": candidate_problems,
                "harness_dir": src,
            }
        curve.append(point)
        _write_curve(run_dir / "curve.jsonl", curve)
        console.round_line(i, label, point,
                           note="nominated" if i == nominated else None)

    reset_hard(work, h0_sha)
    shutil.rmtree(method_root, ignore_errors=True)
    _write_run_meta(
        run_dir, platform_tampered=None, mode="B", trajectory_shape=shape,
        failed_steps=failed_steps,
        curve_drawn=curve_drawn, acceptance_rule=acceptance, replay=False,
        method_entrypoint=argv, base_harness=str(base_dir),
        split=split,
        final_score=curve[-1].get("score") if curve else None,
        final_train_score=curve[-1].get("train_score") if curve else None,
        # 1-based, matching `INTERFACE.md`'s example and the `[nominated]` marker the
        # loop above prints. It was read from the trajectory and then never recorded,
        # so `tools/export_harnesses.py` -- which reads it from run_meta -- always saw
        # None and always exported the last checkpoint. The method's nomination has
        # therefore never actually been honoured; the feature was silently inert.
        nominated=nominated,
        wall_clock_s=round(gen_s, 1))
    return curve
