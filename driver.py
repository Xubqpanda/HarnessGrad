#!/usr/bin/env python3
"""The sampling loop. Framework-owned (INTERFACE.md §2).

    H0 evaluate -> curve point 0 -> trainer edits -> commit -> evaluate -> ...
                                                              -> curve point t

Two execution modes, because real methods come in two shapes (trainers/base.py):

    A  framework-driven   the platform owns the round loop and calls improve()
    B  method-driven      the method runs its own loop and returns a Trajectory

Both write the same `curve.jsonl`, so curves from either mode land on one axis.

The Trainer never sees this file. Everything it is allowed to know is staged
into <workspace>/_harnessgrad/ before each call.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shlex
import shutil
import sys
import time
from pathlib import Path

import data.registry as datasets
from tools.improver import forbidden_reason
from tools.improver import load_config as load_improvers
from tools.improver import resolve as resolve_improver
from ckpt.git_state import commit_state, manifest, stage_workspace
from eval.console import CONSOLE as console
from eval.metrics import select_task_set
from eval.integrity import PLATFORM_ROOT, workspace_is_outside_platform
from eval.runner import evaluate
import eval.container as container
import eval.modelgate as modelgate
import eval.sandbox as sandbox
import eval.services as services_mod


# ------------------------------------------------------------------- layers ---
#
# The implementation lives in `harnessgrad/`, layered so the dependencies point one way
# (`harnessgrad/__init__.py` draws the picture). This file is the entrypoint: argparse, the
# wiring, and the order in which the platform refuses things -- the name that INTERFACE.md,
# `tools/serve_ui.py` and every recorded run's argv use, so it stays where it is.
#
# Only what this file itself wires up is imported. Everything else is reached where it
# lives (`from harnessgrad.loops.mode_a import ...`), so the layer a change belongs to is
# visible in the import line rather than flattened through the entrypoint.
from harnessgrad import FRAMEWORK_VERSION
from harnessgrad.environments import (
    _check_env_kinds, _env_record, _narrow_to_run, _report_harness_failed,
    _report_invalid, _required_env_kinds, _resolve_envs, _warn_missing_overlay)
from harnessgrad.identity import _identity
from harnessgrad.loops.eval_side import _eval_from_record, _eval_source, _run_eval_side
from harnessgrad.loops.mode_a import _run_mode_a
from harnessgrad.loops.mode_b import _run_mode_b
from harnessgrad.methods import PlatformTampered, _check_method_entrypoint
from harnessgrad.records import (
    _cost_of, _curve_point, _require_untampered, _save_round_state, _save_traces,
    _save_verdicts, _scored, _write_curve, _write_run_meta)


# --------------------------------------------------------- model config ---

def load_env(path: Path) -> dict:
    """Load `.env` into the environment the harness will run in.

    The harness reads its model configuration from the environment, so somebody
    has to put it there. Doing it in the driver rather than in the harness keeps
    the harness free of a dotenv dependency and means a method that edits the
    harness cannot accidentally change how the model is reached.

    Real environment variables win: an explicit `HG_AGENT_MODEL=... ./driver.py`
    must not be silently overridden by a file on disk.
    """
    if not path.exists():
        return {}
    loaded = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if not key:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def describe_model() -> dict:
    """What the platform will record as `agent_model`, and whether it is real.

    Reported at the top of a run because the alternative is discovering three
    rounds later that every score came from the mock stand-in.
    """
    backend = os.environ.get("HG_AGENT_BACKEND", "mock")
    model = os.environ.get("HG_AGENT_MODEL") or None
    has_key = bool(os.environ.get("HG_AGENT_API_KEY"))
    return {
        "backend": backend,
        "model": model,
        "credentials": has_key,
        "live": backend != "mock" and bool(model) and has_key,
    }


# The curve table is rendered by `eval/console.py`, next to the header and the
# round lines, so that the human text and `events.jsonl` cannot drift apart. It
# used to live here and carried a `sel` column that read `?` on every row of
# every run this platform has produced, because mode A has no selection step to
# report. A column that is constant noise teaches the reader to skip the table,
# which is the opposite of what a table is for.


# ----------------------------------------------------------------- main ---

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", default="loop")
    ap.add_argument("--dataset", default="demo")
    ap.add_argument("--sampling", default="all",
                    help="all | below_X | above_X | random_N  (chosen on H0)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--mode", default="A", choices=["A", "B"],
                    help="A=platform drives the loop (default); B=the method runs "
                         "its own loop and is reached through its own entrypoint")
    ap.add_argument("--method-timeout", type=int, default=86400,
                    help="mode B: seconds before the method's own loop is killed")
    # **哪个改进器。** 方法自带改进器时这三个开关是无害的;用一种"把方法拆成规则+
    # 改进器"的方法(如 `methods/codex`)时,它们决定谁来做那一步 —— 而解析出来的
    # 版本和哈希会进每个曲线点(INTERFACE.md §4.49)。放在命令行而不是只读配置:
    # 一次实验"用哪个改进器"正是最该被显式记录、最不该靠环境变量碰运气的东西。
    ap.add_argument("--improver", default=None,
                    help="improvers/improvers.json 里的名字,或一个可执行文件的路径")
    ap.add_argument("--improver-model", default=None, help="覆盖改进器自己的模型")
    ap.add_argument("--improver-effort", default=None,
                    help="codex 系:low|medium|high|xhigh")
    ap.add_argument("--method-entrypoint", default=None,
                    help="mode B only: argv for the method's own runner, e.g. "
                         "'python /path/to/rrsi_wrap.py'. Defaults to "
                         "harness.json's improve_entrypoint.")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--tasks", default=None,
                    help="a comma-separated subset of task ids to run, instead of the "
                         "whole side. Refused if any id is not in the dataset: silently "
                         "dropping one would produce a curve whose task count does not "
                         "match what was asked for, which is the kind of difference "
                         "nobody notices until they compare two runs.")
    ap.add_argument("--side", choices=("train", "eval"), default="train",
                    help="which side of the dataset's split this run is about. A run "
                         "evaluates ONE side: a train run evolves the harness and shows "
                         "the method the traces of the tasks it is scored on, and an "
                         "eval run measures one state of a train run on the exam. They "
                         "are separate runs so that the exam's traces are not in the "
                         "train run at all -- an absence, not a withheld field.")
    ap.add_argument("--from-run", default=None,
                    help="--side eval: the train run whose harness state to measure")
    ap.add_argument("--from-round", type=int, default=None,
                    help="--side eval: which round of that run (default: its last)")
    ap.add_argument("--seed", default=None,
                    help="the environment's seed (INTERFACE.md §2.5.7). A per-task "
                         "seed is derived from it and given to services that declare "
                         "`needs_seed`; recorded on every curve point. Drawn at random "
                         "when omitted, and re-supplyable so a recorded run can be "
                         "reproduced.")
    ap.set_defaults(sandbox=True)
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--no-sandbox", dest="sandbox", action="store_false",
                    help="run the harness WITHOUT a mount namespace. A sandboxed "
                         "harness cannot see the platform at all; without one it "
                         "can read the driver and .env, and only the hash check in "
                         "eval/integrity.py stands between it and the scorer. "
                         "Numbers are not comparable across the two modes.")
    ap.add_argument("--work-root", default="../harnessgrad_work",
                    help="where harness working trees live; must be OUTSIDE the "
                         "platform tree, or a harness can reach the platform")
    args = ap.parse_args()

    runs_root = Path(args.runs_root)
    eval_from = None
    if args.side == "eval":
        # The "harness" of an eval run is a state a train run produced. Pointing
        # `harness_src` at it before anything else means staging, the manifest, the
        # header, the sandbox and the method channel all work on it unchanged -- there
        # is no second code path for "a harness that came out of a run".
        try:
            harness_src, _round = _eval_source(args, runs_root)
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            return 2
        eval_from = _eval_from_record(runs_root, args.from_run, _round)
    else:
        harness_src = Path(__file__).parent / "base_harness" / args.harness
        if not harness_src.is_dir():
            print(f"no such harness: {harness_src}", file=sys.stderr)
            return 2

    env_file = Path(__file__).parent / ".env"
    load_env(env_file)
    model = describe_model()
    # The project's model rule, enforced at the door next to the credential check: the
    # harness's model is a run-level choice and the same rule covers it. `improvers.json`
    # holds the list and the reason (`tools/improver.py:forbidden_reason`).
    banned = forbidden_reason(load_improvers(), model.get("model") or "")
    if banned:
        print(f"refusing to run: {banned}", file=sys.stderr)
        return 2
    if model["backend"] != "mock" and not model["live"]:
        missing = [n for n, ok in (("HG_AGENT_MODEL", model["model"]),
                                   ("HG_AGENT_API_KEY", model["credentials"]))
                   if not ok]
        print(f"backend is {model['backend']!r} but {', '.join(missing)} "
              f"{'is' if len(missing) == 1 else 'are'} unset; every score would "
              f"come from a harness that cannot reach a model. See .env.example.",
              file=sys.stderr)
        return 2

    tasks_all, scorable, split = datasets.load_split(args.dataset)
    # The task's initial state, and the grade. Both are platform-side only: `TASKS`
    # is what the harness receives, `SETUP`/`VERIFY` never leave this process except
    # as the files SETUP produces (INTERFACE.md §2.4).
    _, setups, verifiers = datasets.load_tasks(args.dataset)
    # Where each task runs (INTERFACE.md §2.5). Resolved and pinned here, before any
    # evaluation, because §2.5.4's rule is "no digest, no start": a tag that cannot be
    # pinned must stop the run at the door rather than produce a curve nobody can
    # reproduce.
    envs = datasets.load_envs(args.dataset)

    # **A run is about one side, and the narrowing happens here**, before round 0 and
    # before the workspace is staged. That placement is the design: the other side is
    # never loaded, never evaluated, and its traces are never written, so "the method
    # cannot see the exam" stops being a field this platform withholds and becomes a
    # side that is not in the run at all. The same upgrade as hiding the platform by not
    # mounting it, rather than by checking what was read.
    side_ids = list(split[args.side]) if split else [t["task_id"] for t in tasks_all]

    # An explicit subset, applied *after* the side so a request can never pull a task
    # from the other side by naming it -- the side is the run's identity, not a filter
    # the caller can override.
    if args.tasks:
        asked = [t.strip() for t in args.tasks.split(",") if t.strip()]
        unknown = sorted(set(asked) - set(side_ids))
        if unknown:
            print(f"--tasks names {unknown}, which are not on the {args.side} side of "
                  f"{args.dataset!r}. That side has {sorted(side_ids)}",
                  file=sys.stderr)
            return 2
        side_ids = [t for t in side_ids if t in set(asked)]

    tasks_all = [t for t in tasks_all if t["task_id"] in set(side_ids)]
    if not tasks_all:
        print(f"dataset {args.dataset!r} has no tasks on side {args.side!r}",
              file=sys.stderr)
        return 2

    # **The narrowing has to reach the three side tables, not just the task list.**
    #
    # `load_envs` and `load_tasks` read the *whole* dataset, so `envs`, `setups` and
    # `verifiers` each carry an entry for every task, including the other side. Until
    # this call existed, `--tasks` narrowed only `tasks_all`, and the preflight below
    # still resolved every image in the dataset: a one-task train run was refused
    # because an *eval* task's image was missing. Measured: `--side train --tasks
    # break-filter-js-from-html` refused over `alexgshaw/mteb-leaderboard:20251031`,
    # an image the run would never touch -- and the read of that refusal is "it ignored
    # my --tasks", which is worse than the refusal itself.
    setups, verifiers, envs = _narrow_to_run(setups, verifiers, envs, side_ids)
    verifier_kinds = sorted({v.get("kind", "answer") for v in verifiers.values()})
    mode = args.mode

    run_id = args.run_id or (
        f"{args.harness}-{args.dataset}-{args.sampling}-m{mode}")
    run_dir = Path(args.runs_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # From here on the run narrates itself through the console, which writes the
    # human text and `events.jsonl` together.
    console.attach(run_dir / "events.jsonl")

    # **Which improver this run's edits came from, resolved before anything is paid for.**
    #
    # A method is a decision rule *and* an improver, and the platform used to record only
    # the rule's model. Resolved here rather than at the first method call so that a
    # machine with no usable improver refuses at the door -- the same reason images are
    # pinned above: a run that discovers its tooling is missing three rounds in has
    # already spent the rounds.
    #
    # A refusal is only for a run that *needs* an improver and cannot find one. The
    # reference methods carry their own (`methods/editor.py` calls `HG_METHOD_*` directly),
    # so an unresolvable improver is a warning plus an empty identity field, not a stop --
    # and `improver` stays empty so no curve point can claim an improver it did not use.
    #
    # Held in a local and handed to the loops that write points. It used to be a module
    # global here that `_identity` read from the other side of the call stack: a curve
    # point's improver field then depended on a mutable global, and "what does this point
    # claim" could not be answered without running the program that produced it.
    improver: dict = {}
    # The request travels through the environment, because that is how the improver
    # itself is configured (`HG_IMPROVER*` are what `methods/codex` reads) -- resolving
    # and executing must not disagree about which one was chosen.
    if args.improver:
        os.environ["HG_IMPROVER_NAME"] = args.improver
    if args.improver_model:
        os.environ["HG_IMPROVER_MODEL"] = args.improver_model
    if args.improver_effort:
        os.environ["HG_IMPROVER_EFFORT"] = args.improver_effort
    try:
        improver = resolve_improver(load_improvers(), version="",
                                    requested=args.improver,
                                    model_override=args.improver_model)
    except Exception as exc:                                    # noqa: BLE001
        improver = {"ok": False, "reason": f"could not read the improver config: {exc}"}
    if improver.get("ok"):
        console.note(
            f"improver {improver['name']} {improver['version']}"
            + (f" · model {improver['model']}" if improver.get("model") else "")
            + f" · sha {improver['sha256'][:12]}"
            + ("  ← declared MODIFIED" if improver.get("modified") else ""))
    else:
        console.note(
            f"no improver resolved ({improver.get('reason')}); a method that carries its "
            f"own improver still runs, but this run carries no improver identity",
            level="warn")
        improver = {}

    # **The capability check goes first, and the order is the whole point.**
    #
    # Both of these refuse before anything is paid for, so either order is "correct" in
    # the sense that nothing runs. They are not equivalent to a reader: a harness that
    # declares it cannot operate on this environment is **unfixable by importing
    # anything**, while a missing image is fixed by one command. Reported the other way
    # round -- which is what happened, because the image resolution was moved up here
    # for `run_meta.json`'s sake and the gate was left behind -- a user whose harness
    # could never work was told to import an image, imported it, came back, and was told
    # to import the next one. Measured: two UI runs, two different missing images, and
    # the real reason (the harness declares `files` only) never appeared.
    #
    # Read from `harness_src` rather than the staged `work`, because staging happens
    # later and nothing has copied anything yet. The two are the same bytes at this
    # point: `stage_workspace` is a plain copy.
    # A harness that declares dependencies, running where none were provided.
    #
    # A warning rather than a refusal, because it is not always a problem: a `files`
    # dataset runs the harness on the platform's interpreter, which may already have
    # them. What it must not be is silent -- measured, `loop` on Terminal-Bench died on
    # its first model call and the run reported nothing but `score 0.000`, which reads
    # exactly like a method that decided not to edit.
    _warn_missing_overlay(harness_src, envs, args.dataset)

    man_at_door = manifest(harness_src)
    problem = _check_env_kinds(man_at_door, _required_env_kinds(envs))
    if problem:
        console.note(f"refusing to run: {problem}", level="error")
        return 2

    # Pin the images here, before anything describes the run. The first version of this
    # resolved them further down, next to the `env_kinds` gate -- and the header and
    # `run_meta.json` were both written before it, so they reported `image_digest: null`
    # and named the image's *tag* while every curve point carried the digest. Two records
    # of one run disagreeing about what it ran in is the exact failure §2.5.4 exists to
    # prevent.
    envs, env_problem = _resolve_envs(envs)
    if env_problem:
        console.note(f"refusing to run: {env_problem}", level="error")
        return 2

    # The container boundary is built once, at the door, for the same reason the bwrap
    # one is: a boundary that cannot be built must stop the run before it is paid for,
    # and must never degrade into running on the host.
    if "exec" in _required_env_kinds(envs):
        version = container.available()
        if version is None:
            console.note(
                "refusing to run: this dataset needs an `exec` environment but no "
                "docker daemon is reachable. Starting the run anyway would either "
                "score zero on every task or fall back to the host, and both look "
                "like a weak harness.", level="error")
            return 2
        for image in sorted({spec["image"] for spec in envs.values()
                             if spec.get("kind") == "exec"}):
            # `PLATFORM_ROOT`, not `PLATFORM`: the latter is the tuple of names that
            # makes up the platform, and passing it here crashed every `exec` run with
            # a `tuple / str` TypeError before a single container started.
            check = container.self_check(PLATFORM_ROOT, image)
            if not check["ok"]:
                console.note(f"refusing to run: container boundary for {image!r} "
                             f"does not hold: {check['detail']}", level="error")
                return 2
            console.note(f"container boundary holds (docker {version}): "
                         f"{check['detail']}", level="info")

    if split is None:
        console.note(
            f"dataset {args.dataset!r} declares no train/eval split: every task is "
            f"scored and the method is shown traces for all of them. The run is "
            f"still measured, but a score from it cannot support a claim about "
            f"generalisation -- the method studied its own exam.",
            level="warn")

    # The harness's workspace is deliberately NOT under the platform's own tree.
    # It used to be `runs/<id>/workspace`, which put it two `..` from the driver,
    # the scorer and `.env` -- measured: a harness could read all three and write
    # anywhere in the platform directory. A subprocess cannot walk out of a tree it
    # was never placed inside, so this is the half of isolation that actually holds
    # without a container. The run record stays under `runs/`; only the working
    # tree moves.
    #
    # Checked before anything is created, so a refusal leaves no partial state
    # inside the platform for the next run to trip over.
    work = (Path(args.work_root) / run_id / "workspace").resolve()
    if not workspace_is_outside_platform(work, PLATFORM_ROOT):
        print(f"refusing to run: the workspace {work} is inside the platform "
              f"({PLATFORM_ROOT}). A harness there can read the driver and .env "
              f"and write anywhere in the platform. Pass --work-root outside it.",
              file=sys.stderr)
        return 2

    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    curve_path = run_dir / "curve.jsonl"

    # The sandbox is verified before the first paid call, not once per task. A
    # namespace that cannot be built stops the run at the door; the alternative is
    # a run that quietly measured something other than what its metadata claims.
    sandbox_state = {"requested": args.sandbox, "active": False,
                     "plan_version": sandbox.PLAN_VERSION}
    if args.sandbox:
        report = sandbox.self_check(PLATFORM_ROOT, work.parent.parent)
        if not report["ok"]:
            console.note(f"refusing to run: {report['detail']}", level="error")
            return 2
        sandbox_state["active"] = True
        sandbox_state["detail"] = report["detail"]
        sandbox_line = "bwrap active — the harness cannot see this directory"
    else:
        sandbox_line = ("OFF — the harness can read the driver and .env; only the "
                        "hash check stands between it and the scorer")
        console.note(
            "running with --no-sandbox: the harness can read the platform and "
            ".env, and only the hash check stands between it and the scorer",
            level="warn")

    # A run whose tasks are in containers must not describe itself as a bwrap run.
    # The two are different boundaries with different properties, and the header is
    # the one line a reader takes the run's conditions from -- this exact class of
    # misdescription is what `agent_model` already cost once.
    env_kinds = _required_env_kinds(envs)
    if "exec" in env_kinds:
        images = sorted({str(spec.get("image_digest") or spec.get("image"))
                         for spec in envs.values() if spec.get("kind") == "exec"})
        where = f"exec — {len(images)} image digest(s)" if len(images) > 1 \
            else f"exec — {images[0][:26] if images else 'unresolved'}"
        svc_names = sorted({svc["name"] for spec in envs.values()
                            for svc in (spec.get("services") or [])})
        if svc_names:
            # Named, not counted: "a mock API was reachable" and "a database was
            # reachable" are different tasks, and the header is where a reader takes
            # the run's conditions from (§2.5.4).
            where += f" + services[{', '.join(svc_names)}]"
        # `args.sandbox`, **not** `sandbox`: the bare name in this module is
        # `import eval.sandbox as sandbox`, so a truthiness test on it tests a *module
        # object* and is always true. Measured: this condition silently never fired, so
        # the header named the container boundary and left out that the host-side one
        # was off. It is the same shadowing that produced `'bool' object has no
        # attribute 'SandboxUnavailable'` in Step 3, which is why it is spelled out
        # here instead of left to look obvious.
        if not args.sandbox:
            # `--no-sandbox` does not disable the container: the container *is* the
            # task's environment (INTERFACE.md §2.5.5), so removing it would not make
            # the task run unisolated, it would make the task not run at all. Saying
            # so beats a line that reads as "no boundary anywhere".
            where += " (host-side bwrap OFF for the method)"
        sandbox_line = where

    # `run_one` needs the work root to know which tree to leave visible. Exported
    # rather than passed through every layer: it is a property of this run, and the
    # runner is the only reader.
    os.environ["HARNESSGRAD_WORK_ROOT"] = str(work.parent.parent)
    # The environment's seed is a run-level fact, and it has to exist before any
    # environment is described. `secrets` rather than `random`: this seeds an
    # environment a method may be trying to fit to, and a predictable seed is a
    # predictable environment.
    os.environ["HARNESSGRAD_SEED"] = args.seed or secrets.token_hex(8)
    if any(services_mod.requires_model(spec.get("services") or [])
           for spec in envs.values()):
        # An environment-side model is a third budget. Refused when a service needs one
        # and none is configured, rather than passed as an empty string: a simulator
        # that silently has no model produces a task that fails for a reason nothing
        # records, and §2.5.7 requires `env.model` to be non-null exactly when a
        # service declared it needs one.
        if not os.environ.get("HG_ENV_MODEL"):
            console.note(
                "refusing to run: a service declares needs_model but HG_ENV_MODEL is "
                "unset. The environment's model is its own budget -- configure it in "
                ".env (HG_ENV_MODEL, HG_ENV_API_KEY) rather than reusing the agent's.",
                level="error")
            return 2

    _write_run_meta(
        run_dir, run_id=run_id, mode=mode, harness=args.harness,
        dataset=args.dataset, sampling_policy=args.sampling,
        workspace=str(work), work_root=str(work.parent.parent),
        platform_root=str(PLATFORM_ROOT),
        sandbox=sandbox_state,
        # The run-level view of §2.5.4, beside the per-point one. With no task ids this
        # falls back to every distinct environment the dataset declares, which is the
        # right granularity for a run: a reader should be able to tell what kind of
        # world this run measured in without reading the curve.
        env=_env_record(envs, []),
        # The declared split, not the selected tasks: a reader of the run record
        # needs to know what the dataset said the two sides were, before any
        # sampling policy narrowed it.
        split=split,
        split_declared=split is not None,
        agent_model=model["model"] or None,
        agent_backend=model["backend"],
        framework_version=FRAMEWORK_VERSION,
        method_entrypoint=args.method_entrypoint,
        # The run's first write: drop anything a previous run left under this id.
        fresh=True,
    )

    stage_workspace(harness_src, work)
    man = manifest(work)
    # `_check_env_kinds` already ran, against this same manifest read from the source --
    # see the note beside `envs, env_problem = _resolve_envs(envs)` for why it has to be
    # the first refusal rather than this one.

    # A containerised harness whose model is not on this host needs the internet to
    # reach it, and a task that did not ask for the internet must not silently get it --
    # that would change what the task *is* (§2.5.4). Refused rather than attempted: the
    # harness would have no model, so every score would be a zero that means nothing,
    # and it would look like a weak harness.
    if "exec" in _required_env_kinds(envs) \
            and os.environ.get("HG_AGENT_BACKEND", "mock") != "mock":
        base_url = os.environ.get("HG_AGENT_BASE_URL") or ""
        if base_url and not modelgate.is_host_local(base_url):
            offline = sorted(tid for tid, spec in envs.items()
                             if spec.get("kind") == "exec"
                             and spec.get("network") != "bridge")
            if offline:
                console.note(
                    f"refusing to run: the agent's model is at {base_url!r}, which is "
                    f"not on this host, so a containerised harness needs the internet "
                    f"to reach it. Task(s) {offline} are 'exec' with network != "
                    f"'bridge', so the harness would have no model at all. Either set "
                    f"network='bridge' and accept that the task is a networked task, or "
                    f"point HG_AGENT_BASE_URL at a model on this host.", level="error")
                return 2

    console.header(
        run_id=run_id, harness=man["name"], version=man["version"], mode=mode,
        dataset=args.dataset, n_tasks=len(tasks_all),
        model=model["model"], backend=model["backend"], live=model["live"],
        sandbox=sandbox_line, split=split, argv=sys.argv, side=args.side)

    if args.side == "eval":
        return _run_eval_side(
            work=work, man=man, run_id=run_id, run_dir=run_dir, tasks=tasks_all,
            scorable=scorable, setups=setups, verifiers=verifiers, envs=envs,
            side_ids=side_ids, split=split, args=args, eval_from=eval_from,
            improver=improver)

    cache: dict = {}
    # `env_generation_tokens` is its own axis rather than part of `generation_tokens`:
    # the x-axis is compute, and an environment that spends on every task adds a
    # constant per round. Adding it to the *method's* number would move the axis by an
    # amount the method did not spend (INTERFACE.md §2.5.7).
    cumulative = {"evaluation_trials": 0, "generation_tokens": 0,
                  "env_generation_tokens": 0, "wall_clock_s": 0.0}

    # ---- round 0: score H0 on everything, then fix the task set from it ----
    t0 = time.time()
    base = evaluate(work, tasks_all, scorable, cache, harness_sha="H0",
                    sandbox=args.sandbox, setups=setups, verifiers=verifiers,
                    envs=envs, run_id=run_id, run_seed=os.environ["HARNESSGRAD_SEED"],
                    recordings_root=run_dir / "recordings")
    _require_untampered(base)
    _report_invalid(base, args.dataset)
    _report_harness_failed(base, args.dataset)
    if not base["per_task"]:
        # Nothing was measurable, so there is no curve to draw. Refused rather than
        # recorded as a point: `mean([])` is 0.0, and a zero here would say "the
        # harness scored nothing" about a run in which the harness never ran
        # (INTERFACE.md §2.5.7).
        console.note(
            f"refusing to run: none of the {len(tasks_all)} tasks in "
            f"{args.dataset!r} produced a scoreable result. The curve would have to "
            f"report a score for a task set that was never measured.", level="error")
        return 2
    cumulative["evaluation_trials"] += len(tasks_all)
    cumulative["env_generation_tokens"] += (base.get("env_usage") or {}).get(
        "input", 0) + (base.get("env_usage") or {}).get("output", 0)
    cumulative["wall_clock_s"] += time.time() - t0

    # The sampling policy chooses what the run is *scored* on, so it selects
    # inside the eval side. It deliberately does not reach into the train side: a
    # method handed a random 2 of its 40 diagnostics is a different, worse
    # experiment than the one the policy was written to describe. The cost of a
    # split is therefore all of train, every round -- which is the honest price
    # of the method not being shown the exam.
    # The run's own side. `tasks_all` was narrowed to it before round 0, so the policy
    # selects within it and `within` can never reach across -- the cross-side leak this
    # guards against is not a rule here, it is an absence of the other side.
    task_ids = select_task_set(base["per_task"], args.sampling,
                               within=(side_ids or None))
    if not task_ids:
        console.note(f"sampling policy {args.sampling!r} selected no tasks on H0",
                     level="error")
        return 2
    # A train run's scored set **is** its diagnostics set: the method is shown the tasks
    # it is scored on, which is what training means and is exactly why the exam has to be
    # a different run. An eval run has no method at all (see the `--side eval` path), so
    # this is only ever populated for `train`.
    train_ids = list(task_ids) if args.side == "train" else []
    tasks = [t for t in tasks_all if t["task_id"] in set(task_ids + train_ids)]
    sampling = {"policy": args.sampling, "task_ids": task_ids,
                "selection_basis_round": 0,
                "selected_within": args.side if split else "all"}
    if split and len(task_ids) < 8:
        # Not a warning about the platform, a warning about the number the reader is
        # about to see. `task_ids` is what *this run* scores -- on a train run that is
        # the train side, on an eval run the exam -- so the wording has to stay
        # side-neutral. It used to say "eval side is N tasks" while printing the run's
        # own task count: a one-task train run announced "eval side is 1 tasks" about a
        # 37-task exam, which reads as a measurement of the wrong thing.
        #
        # A 2-task run moves in steps of 0.5, and a 0.5 step read as a result is the
        # most expensive mistake this console can help someone avoid.
        console.note(
            f"this run scores {len(task_ids)} tasks — the score moves in steps of "
            f"{1.0 / len(task_ids):.3f}", level="warn")

    h0_sha = commit_state(work, "H0")
    # Round 0's state too: "measure H0 on the exam" is the baseline every later eval is
    # read against, and it needs a state to measure exactly like any other round.
    _save_round_state(run_dir, 0, work)
    curve = [_curve_point(
        run_id=run_id, round_index=0,
        identity=_identity(man, h0_sha, mode, improver=improver,
                            declared=base.get("identity_declared")),
        sampling=sampling,
        scores=[base["per_task"][t] for t in _scored(base, task_ids)],
        task_ids=_scored(base, task_ids),
        train_scores=[base["per_task"][t] for t in _scored(base, train_ids)],
        train_ids=_scored(base, train_ids), split=split,
        invalid=base.get("invalid"),
        harness_failed=base.get("harness_failed"),
        model_gateway=base.get("model_gateway"),
        harness_runtime=base.get("harness_runtime"),
        env_record=_env_record(envs, task_ids),
        cost=_cost_of(base, len(tasks_all), cumulative["wall_clock_s"]),
        cumulative=cumulative, label="H0 (base harness)")]
    # The base harness is always evaluated by the platform, so its point is a
    # platform measurement like any other. Leaving the field unset made the
    # exported record claim the score was the method's -- the opposite of true.
    curve[0]["measured_by_platform"] = True
    # The verifier's *kind*, never its spec. This point is written into the method's
    # channel, so an argv or an expected answer here would hand a method the grade it
    # is being measured against -- the same leak the eval traces are withheld for.
    curve[0]["verifier_kinds"] = verifier_kinds
    console.round_line(0, "H0 (base harness)", curve[0])
    # Written now, not only at the end. A run that is interrupted used to leave the
    # *previous* run's curve.jsonl on disk, so the console showed a curve belonging
    # to a different method than the one in run_meta -- read as "I picked RRSI and
    # it ran DGM". The record has to be durable at the moment it is produced.
    _write_curve(curve_path, curve)
    _save_verdicts(run_dir, 0, base, [t["task_id"] for t in tasks])
    _save_traces(run_dir, 0, base.get("traces") or {}, train_ids, split)

    # 在分派之前挡住相对的入口路径。放在 main 里,因为 `_run_mode_a/b` 返回的是
    # 曲线,不是退出码 —— 第一次把检查放进 mode 函数时,`return 2` 让 main 拿一个
    # 整数当曲线去迭代,报的是 "TypeError: 'int' object is not iterable" 而不是
    # 那句本该出现的拒绝理由。
    if args.method_entrypoint:
        problem = _check_method_entrypoint(shlex.split(args.method_entrypoint))
        if problem:
            console.note(f"refusing to run:\n  {problem}", level="error")
            return 2

    if mode == "A":
        curve = _run_mode_a(args, work, man, run_id, mode, curve, h0_sha,
                            base, tasks, task_ids, scorable, sampling, cache,
                            cumulative, train_ids, split, setups, verifiers,
                            verifier_kinds, envs, improver=improver)
    else:
        curve = _run_mode_b(args, work, man, run_id, mode, curve, h0_sha,
                            run_dir, base, tasks, task_ids, scorable, sampling, cache,
                            cumulative, train_ids, split, verifier_kinds,
                            setups, verifiers, envs, improver=improver)

    _write_curve(curve_path, curve)
    console.results(curve, split=split)
    console.artifact("curve", curve_path)
    console.artifact("workspace", work)
    console.artifact("events", run_dir / "events.jsonl")
    return 0


if __name__ == "__main__":
    # A tampered run has no partial value to salvage: the curve it printed would
    # be the attacker's self-assessment. Exit non-zero and say so.
    try:
        sys.exit(main())
    except PlatformTampered:
        sys.exit(3)