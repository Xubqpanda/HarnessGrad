"""Mode A: the platform owns the round loop and calls the method once per round.

One round is one exchange -- hand the method the current harness, take back the one
candidate it names, measure it with the platform's own scorer, and continue from what was
measured. The method is external so that the same method can be applied to any base harness,
and so two methods can be compared from one starting point.

The loop is where "a round that did not complete is not a point" is enforced: the edit and
its measurement both have to happen, or the step is retried and eventually recorded as a
failed step rather than drawn on the curve.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import shlex
import shutil
import sys
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
from harnessgrad.methods import _touched_paths
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

def _run_mode_a(args, work, man, run_id, mode, curve, h0_sha, base, tasks,
                task_ids, scorable, sampling, cache, cumulative,
                train_ids=None, split=None, setups=None,
                verifiers=None, verifier_kinds=None, envs=None,
                improver=None) -> list[dict]:
    """Mode A: the platform owns the loop and calls an EXTERNAL method each round.

    One round = hand the method the current harness, take back the one candidate
    it produced, measure it, and continue from there. The method is external for
    the same reason it is in mode B: so the same method can be applied to any base
    harness, and so two methods can be compared from one starting point.

    Mode A and mode B differ only in who owns the schedule. Here the platform
    decides how many rounds to run and what each starts from; the method decides
    what one step is. A method that owns its own search -- RRSI with its annealed
    budget, DGM with its archive -- runs in mode B, because re-wiring its
    schedule would change the thing being measured.
    """
    argv = args.method_entrypoint
    if not argv:
        print("mode A needs --method-entrypoint pointing at an external method",
              file=sys.stderr)
        return curve
    if isinstance(argv, str):
        argv = shlex.split(argv)

    # The method contract's version is the platform's to declare. A harness has
    # nothing to do with methods any more, so asking a harness for it would be
    # asking the wrong artifact -- and after round 1 the workspace holds a
    # candidate anyway.
    api_version = PLATFORM_API_VERSION
    run_dir = Path(args.runs_root) / run_id
    # Where the method's inputs go: outside the platform **and outside the work root**.
    #
    # Two moves, for two different reasons, and both were forced by trying to sandbox
    # a method:
    #
    #   * out of the run directory, because that is inside the platform and hiding the
    #     platform would have hidden the method's own inputs;
    #   * out of the work root, because the sandbox re-binds the work root read-only
    #     *after* opening the writable hole -- and a read-only bind of a parent wins
    #     over a writable bind of its child. Measured: a method got `Read-only file
    #     system` writing its own trajectory.
    #
    # A temporary directory has neither property, and `task_dir` re-binds it after the
    # hides, which is the same mechanism the harness's own working directory uses.
    method_root = Path(tempfile.mkdtemp(prefix=f"hg-method-{run_id}-")).resolve()

    #: Rounds whose step did not finish -- the edit sequence never applied. Recorded
    #: rather than only printed: "3 rounds but 2 steps" is a fact a reader comparing two
    #: curves needs, and the console's scrollback is not a record.
    failed_steps: list[dict] = []

    #: How many times **one step** may be attempted before the run gives up on it.
    #:
    #: A step is two phases (propose, then apply) and **only a step that finished both
    #: advances the counter** -- `r` is always `steps_done + 1`, so the curve's rounds
    #: stay contiguous and the method's own round index keeps matching the record.
    #:
    #: Measured, and the reason this is a retry rather than a `break`: a 5-round run
    #: ended after one round because a single reply came back as prose instead of JSON.
    #: `editor.fail` deliberately passes `stop=False` for exactly this reason -- "ending
    #: a run on one transient blip would cost a method the rest of its budget" -- and a
    #: driver that broke out of the loop contradicted the method's own intent.
    #:
    #: **Per step, not per run.** The first version of this bound the *total* attempts at
    #: `rounds * STEP_ATTEMPTS` while the comment claimed it was per step. Measured: a run
    #: whose step 4 kept hitting `APITimeoutError` had spent 1h31m and was still going,
    #: because steps 1-3 needed one attempt each and left the whole remaining quota to one
    #: stuck step. A budget that one step can consume is not a budget for the step.
    STEP_ATTEMPTS = 3
    steps_done, step_attempts = 0, 0
    while steps_done < args.rounds:
        r = steps_done + 1
        step_attempts += 1
        if step_attempts > STEP_ATTEMPTS:
            console.note(
                f"step {r}: gave up after {STEP_ATTEMPTS} attempts", level="error")
            console.round_line(r, "gave up", level="error")
            failed_steps.append({
                "round": r,
                "why": f"no completion in {STEP_ATTEMPTS} attempts; the run stops here "
                       f"because a step that cannot finish is not a step"})
            break
        prior = curve[-1]
        # The whole curve so far, not just the incumbent: a method whose contribution
        # is its selection rule needs the series, and the cheap way to give it is the
        # points already recorded rather than a new mechanism.
        #
        # Traces, but only the train half. Everything about the split is
        # bookkeeping except this line: a method that can read an eval trace can
        # edit the harness against the tasks it is about to be scored on, and the
        # curve that results is indistinguishable from improvement.
        _stage_for_method(work, prior, base["traces"], history=curve,
                          task_ids=train_ids, run_dir=run_dir,
                          tasks=tasks, setups=setups, verifiers=verifiers,
                          result=base)

        base_dir = (method_root / f"round{r}_base").resolve()
        if base_dir.exists():
            shutil.rmtree(base_dir)
        shutil.copytree(work, base_dir, ignore=shutil.ignore_patterns(".git"))

        method_ws = (method_root / f"round{r}_ws").resolve()
        method_ws.mkdir(parents=True, exist_ok=True)
        traj_path = (method_root / f"round{r}_trajectory.json").resolve()

        t_gen = time.time()
        # The method call is the other long silence in a round, and it is the one where
        # "is it thinking or is it dead?" is hardest to answer from outside: the method
        # is an external process the platform does not log. A phase record here is the
        # difference between a 20-minute blank and "RRSI is still generating".
        with console.reporter.phase("agent", round_no=r, task_id="method",
                                   budget_s=args.method_timeout):
            reply = _call_method(work, argv, {
                "platform_api_version": api_version,
                "mode": "A",
                "base_harness": str(base_dir),
                "workspace": str(method_ws),
                "round_index": r,
                "incumbent_score": prior["score"],
                "task_ids": task_ids,
                "train_task_ids": list(train_ids or []),
                "trajectory_out": str(traj_path),
                # **The improver the platform resolved, by path as well as by name.**
                # A method cannot look it up for itself: its sandbox hides the platform,
                # so `tools/improver.py` is not on its disk and neither is
                # `improvers/skill.md` (which travels in the channel). Handing it over
                # is also what keeps the record honest -- this is the same resolution the
                # curve point names, so the method cannot run a different one.
                "improver": _improver_identity(improver),
            }, args.method_timeout, sandboxed=args.sandbox,
                scratch=method_root, cwd=method_ws, readonly=(base_dir,),
                runtime=_improver_runtime(improver), improver=improver)
        gen_s = time.time() - t_gen
        cumulative["wall_clock_s"] += gen_s
        cumulative["generation_tokens"] += int(reply.get("generation_tokens") or 0)
        # Before anything is decided about this round: the method's own log is evidence
        # either way, and the directory holding it is deleted when the round ends.
        _keep_method_logs(method_ws, run_dir, r)

        if "_error" in reply:
            # The stderr tail goes in the *note*, not only in the `detail` event beside
            # it. Both are recorded, but a reader of `console.log` sees the notes and not
            # the details -- measured on the first `--improver codex` run, whose three
            # failures read as a bare `exit 1` in the log while the reason
            # ("no usable improver") sat one event away.
            stderr = " ".join((reply.get("_stderr") or "").split())
            why = reply["_error"] + (f" -- {stderr[-400:]}" if stderr else "")
            console.note(f"round {r}: method entrypoint failed: {why}", level="error")
            console.detail(**{k: reply.get(k) for k in ("_stderr", "_stdout")})
            console.round_line(r, "entrypoint failed", level="error",
                               note=stderr[-120:])
            failed_steps.append({"round": r, "why": f"entrypoint failed: {why}"[:400]})
            continue
        if not traj_path.exists():
            console.note(f"round {r}: method wrote no trajectory", level="error")
            console.round_line(r, "no trajectory", level="error")
            failed_steps.append({"round": r,
                                 "why": "the method wrote no trajectory"})
            continue

        step = (json.loads(traj_path.read_text()).get("steps") or [{}])[0]
        src = step.get("harness_dir")
        if not src or not Path(src).is_dir():
            console.note(f"round {r}: method named no readable harness_dir "
                         f"({src!r})", level="error")
            console.round_line(r, "no readable harness_dir", level="error")
            failed_steps.append({"round": r,
                                 "why": f"no readable harness_dir ({src!r})"})
            continue

        # **A step is two phases, and a half-finished step is not a measurement.**
        #
        # `editor.fail` writes a trajectory whose one step points at the pristine base and
        # carries `method_reported.error`. Measuring that used to produce an ordinary
        # curve point whose label said "method error" and whose score was the base
        # harness's -- a number about a candidate the method never produced. Measured:
        # `UnparseableReply` (the model answered with prose instead of the JSON envelope)
        # was recorded as `round 1  method error  train 0.000`, which reads as a weak
        # harness rather than as a lost round.
        step_error = ((step.get("method_reported") or {}).get("error")
                      or step.get("error"))
        if step_error:
            console.note(f"round {r}: the step did not finish -- {step_error}",
                         level="error")
            console.round_line(r, "step failed", level="error", note=str(step_error)[:120])
            failed_steps.append({"round": r, "why": str(step_error)[:400]})
            continue

        # The channel belongs in the method's input tree and nowhere else. `base_dir` is
        # already a copy of it, so `work` -- the tree that gets committed and measured --
        # can be rid of it immediately rather than at the swap. Before this, a run whose
        # last attempt failed ended with the goal and the verifier's gold sitting in its
        # final workspace.
        shutil.rmtree(work / "_harnessgrad", ignore_errors=True)

        # Swap the candidate in and measure it as the platform's own.
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

        # **The platform re-checks the candidate, because the method is not trusted.**
        # The method validates its own output too (`methods/editor.py`), which is what
        # lets it re-propose before reporting -- but a check performed by the thing being
        # measured is a claim, and this is the platform's own. A candidate that is not a
        # harness is recorded as **unmeasured** rather than scored: there is no
        # measurement to report, and a `0.000` here would be indistinguishable from a
        # harness that ran and was weak (§2.5.7's distinction, one level up).
        candidate_problems = candidate.validate(work, base_manifest=man)
        has_manifest = not candidate_problems
        sha = commit_state(work, f"round {r}: {step.get('label', 'method step')}")
        _save_round_diff(run_dir, r, work, curve[-1]["identity"]["harness_sha"], sha)
        _save_round_state(run_dir, r, work)
        t0 = time.time()
        res = (evaluate(work, tasks, scorable, cache, harness_sha=sha, jobs=args.jobs,
                        trials=args.trials,
                        sandbox=args.sandbox, setups=setups, verifiers=verifiers,
                    envs=envs, run_id=run_id, run_seed=os.environ["HARNESSGRAD_SEED"],
                    recordings_root=run_dir / "recordings",
                    round_no=r)
               if has_manifest else None)
        _require_untampered(res)
        _report_invalid(res, args.dataset)
        _report_harness_failed(res, args.dataset)
        dt = time.time() - t0
        if res is not None:
            cumulative["evaluation_trials"] += len(tasks)
            cumulative["env_generation_tokens"] += (res.get("env_usage") or {}).get(
                "input", 0) + (res.get("env_usage") or {}).get("output", 0)
        cumulative["wall_clock_s"] += dt

        point = _curve_point(
            run_id=run_id, round_index=r,
            identity=_identity(man, sha, mode,
                               method_model=reply.get("method_model") or "n/a",
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
            cost=_cost_of(res, len(tasks), dt + gen_s,
                          step.get("claimed_cost")),
            cumulative=cumulative,
            n_trials=(res or {}).get("trials"), score_std=(res or {}).get("score_std"),
            per_task_std=(res or {}).get("per_task_std"),
            label=step.get("label", ""),
            hypothesis=reply.get("hypothesis"),
            edits_applied=reply.get("files"),
            candidate_problems=candidate_problems,
            method_reported=step.get("method_reported") or {})
        point["edit_kind"] = step.get("edit_kind")
        point["measured_by_platform"] = res is not None
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
        point["verifier_kinds"] = verifier_kinds
        # Against the **previous round**, not against H0. Measured: with H0 as the
        # baseline, rounds 4 and 5 reported `touched: ["agent.py"]` while their tree was
        # byte-identical to round 3's -- a reader concludes "the method edited something"
        # from a round where nothing moved. `_save_round_diff` already got this right
        # (`from:` is the previous point's tree); this field did not, and it is the one
        # the method-facing page quotes as "what moved this round".
        point["editable_surface_touched"] = _touched_paths(
            work, curve[-1]["identity"]["harness_sha"], sha)
        curve.append(point)
        _write_curve(run_dir / "curve.jsonl", curve)
        # The step finished both phases, so it counts -- and the next step starts with a
        # fresh attempt budget rather than inheriting this step's leftovers.
        steps_done += 1
        step_attempts = 0

        if res is not None:
            base = res
            _save_verdicts(run_dir, r, res, task_ids)
            _save_traces(run_dir, r, res.get("traces") or {}, train_ids, split)
        # `changed: false` was doing two jobs, and mode A conflated them:
        #
        #   "I have nothing more to say"   a method that is finished
        #   "I reject this candidate"      a method whose contribution IS a filter
        #
        # The second must not end the run. RRSI screen-rejects a candidate and then
        # retries after bounded repair; TTHE's rollback gate keeps the incumbent and
        # waits for the next batch. Under the old rule both lost the rest of their
        # budget to a single rejection -- measured: `methods/rrsi` and
        # `methods/tthe` stopped after one method round on a base harness scoring
        # 0.0, while every eager method got three. A platform whose promise is a
        # fair comparison cannot score cautious methods on fewer rounds than eager
        # ones.
        #
        # `stop` defaults to `not changed`, which is exactly the old behaviour, so
        # nothing that predates the field changes meaning. A method that rejects a
        # candidate and wants its next round says so with `stop: false`.
        changed = bool(reply.get("changed", True))
        stop = bool(reply.get("stop", not changed))
        # A method may say why in `note`, and the default has to stay neutral.
        # The driver cannot tell "I rejected this candidate" (RRSI's leakage screen
        # dropping a proposal) from "I had nothing to propose" (DGM's editor
        # declining) -- and calling both "rejected" told the reader something the
        # method never said. Reported as a confusing message on a DGM run.
        note = reply.get("note")
        if note is not None:
            note = str(note)[:160]
        elif stop:
            note = "no change reported" if not changed else "method asked to stop"
        elif not changed:
            note = "no change this round; continuing"
        console.round_line(r, step.get("label") or "method step", point, note=note)
        if stop:
            break

    reset_hard(work, h0_sha)
    shutil.rmtree(method_root, ignore_errors=True)
    _write_run_meta(
        Path(args.runs_root) / run_id, trajectory_shape="sequence",
        curve_drawn="per_round",
        # Mode A: the platform owns the schedule, so nobody nominates a step.
        nominated=None,
        failed_steps=failed_steps,
        rounds_measured=len([p for p in curve if p.get("measured_by_platform")]),
        final_score=curve[-1].get("score") if curve else None,
        # Both sides of the last measured point. `final_score` alone is the exam
        # only, and a run whose train side moved while this did not is the finding
        # most worth being able to see from the run directory.
        final_train_score=curve[-1].get("train_score") if curve else None,
        split=split,
        wall_clock_s=round(cumulative["wall_clock_s"], 1))
    return curve
