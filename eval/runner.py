"""Execute a harness on a task set. Framework-owned (INTERFACE.md §0)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import eval.container as container_mod
import eval.harness_runtime as harness_runtime
import eval.modelgate as modelgate_mod
import eval.progress as progress_mod
import eval.sandbox as sandbox_mod
import eval.services as services_mod
from eval.services import label
from ckpt.git_state import STATE_DIRNAME
from eval.integrity import PLATFORM_ROOT, diff, snapshot


def harness_env() -> dict:
    """The environment a harness runs in: the agent's model, and not the method's.

    The two are configured separately on purpose (`.env.example`), and separate
    configuration is not separation until something enforces it. Without this the
    harness inherits `HG_METHOD_API_KEY` and can spend the method's quota, or be
    pointed at the method's endpoint -- so a harness that misbehaves under
    measurement would look like a harness problem when it is a plumbing one.

    The reverse direction is deliberately left open: a method is allowed to see how
    the harness will be run, because it is being asked to reason about the harness.
    One-way isolation is the useful half, and it is the half that keeps the two
    budgets from being confused for each other.

    `HG_ENV_*` is stripped here for the same reason, and it closed a real leak: this
    function forwards everything that is not the *method's*, so the moment a third
    budget existed for the environment, a `files` task's harness inherited the
    environment's credential. Services only exist on `exec` -- where
    `container_env`'s allowlist already excluded it -- so the leak would have been on
    the path with no services at all, which is exactly where nobody looks for one.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HG_METHOD_", "HG_ENV_", "HARNESSGRAD_SEED"))}
    return env


def _last_identity(trace: str) -> dict | None:
    """The harness's own statement of what ran it, if it made one.

    A harness reads its model configuration from wherever it likes -- our `HG_AGENT_*`
    convention, a CLI's own config file, a flag. The platform only knows the first of
    those, so left to itself it would record `HG_AGENT_MODEL` for a harness that
    ignored it. **This project has already shipped that bug once**: a curve point said
    `agent_model: mock` while a live model answered every task.

    So the harness may declare it, and when it does the declaration is what goes on
    the record -- see `INTERFACE.md` §3. A declaration that disagrees with the
    platform's own environment is not resolved silently in either direction; both
    values are recorded.

    Shape, one JSON object per line in the trace:

        {"harness_identity": {"agent_model": "...", "agent_backend": "...",
                              "harness": "claude-code 2.0.1"}}
    """
    found = None
    for line in trace.splitlines():
        line = line.strip()
        if not line or "harness_identity" not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("harness_identity"), dict):
            found = event["harness_identity"]      # last one wins
    return found


def _last_usage(trace: str) -> dict | None:
    """The `usage` block a harness wrote into its trace, if it wrote one.

    Last one wins: a harness may report usage incrementally as it goes, and the final
    line is the total. Absent means absent -- a harness that reports nothing gets no
    token count rather than a zero, because a zero that means "not reported" is
    indistinguishable from a real zero and would silently disable a cost rule.
    """
    found = None
    for line in trace.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("usage"), dict):
            found = record["usage"]
    return found


def _materialize(workdir: Path, entries, platform_root: Path,
                 base: Path | None = None) -> list[str]:
    """Write a dataset's files into `workdir`. Returns the relative paths written.

    Used twice per task, and the two uses are the point of the whole design:

    * **before** the harness runs, to set the task up;
    * **again before verification**, to overwrite anything the harness changed. A task
      whose grade is "run these tests" must not be gradeable by editing the tests, and
      re-writing them is the only way that holds when the harness was free to write
      into the same directory.

    Paths are refused rather than clamped if they escape `workdir`: a `../` silently
    turning into `.` would set up a task whose inputs are not where the dataset said.
    """
    written: list[str] = []
    for entry in entries or []:
        rel = str(entry.get("path") or "")
        if not rel:
            continue
        target = (workdir / rel).resolve()
        if not str(target).startswith(str(workdir.resolve()) + os.sep) \
                and target != workdir.resolve():
            raise ValueError(f"setup path {rel!r} escapes the working directory")
        if "content" in entry and entry["content"] is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(entry["content"]))
        elif "from" in entry:
            source = Path(str(entry["from"]))
            if not source.is_absolute():
                source = (base or platform_root) / source
            if not source.exists():
                raise ValueError(f"setup source {source} does not exist")
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        else:
            continue
        written.append(rel)
    return written


def _verifier_env(workdir: Path) -> dict:
    """A minimal environment for the check.

    Deliberately **not** `harness_env()`: the verifier has no business holding the
    agent's API key, and a grade that can call a model is a grade that can vary
    between runs.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(workdir), "LANG": os.environ.get("LANG", "C.UTF-8"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return env


def _verifier_env_container() -> dict:
    """The check's environment inside a container.

    No `PATH`: the image has its own and the host's names directories that do not
    exist inside. `HOME` and `TMPDIR` point at the container's tmpfs, not at the task
    mount, because the task mount is bound **read-only** for the check -- a check that
    writes into the tree it is grading is one whose verdict depends on its own
    leftovers. `PYTHONDONTWRITEBYTECODE` is kept for the same reason.

    And, as on the host: no agent credentials, because a grade that can call a model
    is a grade that can vary between runs.
    """
    return {"HOME": "/tmp", "TMPDIR": "/tmp",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONDONTWRITEBYTECODE": "1"}


def run_one(repo: Path, task: dict, timeout_s: int = 300,
            sandbox: bool = True, setup: dict | None = None,
            verifier: dict | None = None,
            verify_timeout_s: int = 300,
            env: dict | None = None,
            run_id: str = "", run_seed: str = "",
            recordings_root: Path | None = None) -> dict:
    """Run the harness on one task in a throwaway dir, off the platform's disk.

    Returns {task_id, answer, trace, exit_code}. A non-zero exit is recorded as
    a harness failure rather than raised: one broken task must not lose a whole
    paid round.

    `sandbox=True` runs the subject inside its own mount namespace, so the platform
    is not on its disk at all. Paths are bound at their host locations, so the
    repository path, `--workdir`, and anything the harness writes into its trace
    keep the same spelling they would have had unsandboxed -- a translated path is
    a place where a harness's own record stops matching the filesystem it ran on.

    It does **not** restrict the network or take away the agent's API key: a harness
    that cannot reach a model cannot be measured. See `eval/sandbox.py`.
    """
    # Resolve before the subprocess switches cwd: a relative repo path would
    # otherwise be interpreted against the sandbox directory and the harness
    # would silently fail to launch, scoring zero for the wrong reason.
    repo = Path(repo).resolve()
    workdir = Path(tempfile.mkdtemp(prefix="hg-task-"))
    # The environment is a property of the task (INTERFACE.md §2.5). `files` is the
    # absence of a declaration, not a separate code path invented for old datasets.
    env_spec = env or {"kind": "files"}
    is_exec = env_spec.get("kind") == "exec"
    # Inside a container the task is mounted at the dataset's `workdir` (default
    # `/app`), so the harness is told the path it can actually see. Outside one the
    # task *is* the host temporary directory. This is the only path translation in
    # the runner, and it exists because the dataset names a location in the
    # environment rather than on the host.
    task_mount = env_spec.get("workdir") or container_mod.DEFAULT_WORKDIR
    # Declared per environment: the real task set spans 600-12000 s, and the module
    # default of 300 would report a harness failing a task it was never given time to
    # attempt (INTERFACE.md §2.5.9).
    timeout_s = int(env_spec.get("agent_timeout_s") or timeout_s)
    verify_timeout_s = int(env_spec.get("verify_timeout_s") or verify_timeout_s)
    # Read before the dispatch: both state kinds need it, and it used to be computed
    # further down in the `mount` path only.
    _manifest = json.loads((repo / "harness.json").read_text())
    runtime_paths = tuple(Path(p).expanduser()
                          for p in (_manifest.get("runtime_paths") or []))
    if is_exec and env_spec.get("state") == "container":
        return _run_one_container_state(
            repo, task, env_spec=env_spec, setup=setup, verifier=verifier,
            timeout_s=timeout_s, verify_timeout_s=verify_timeout_s,
            run_id=run_id, run_seed=run_seed, recordings_root=recordings_root,
            runtime_paths=runtime_paths)
    service_specs = list(env_spec.get("services") or [])
    running = None
    # The internal network, whoever created it: `services.start` when there are
    # services, the model gateway when there are not. Tracked separately from `running`
    # so teardown knows which of the two it owns.
    network_name: str | None = None
    owns_network = False
    model_gateway = None
    held = progress_mod.task_of()
    phases = progress_mod.phases_for(held)
    try:
        # The task's initial state, before the harness sees anything. It used to be an
        # empty directory, which is why the old contract could only express "answer a
        # question": a dataset had nowhere to put the thing the task is *about*.
        with phases("setup"):
            if setup:
                _materialize(workdir, setup.get("files"), PLATFORM_ROOT)

            task_file = workdir / "task.json"
            task_file.write_text(json.dumps(task))

        # Services come up before the harness and are health-checked before it starts
        # (INTERFACE.md §2.5.7, steps 1-3). A harness that starts against a service
        # that is still initialising does not produce a bad measurement, it produces a
        # measurement of a race -- so the failure is refused here rather than scored.
        if service_specs:
            try:
                running = services_mod.start(
                    service_specs,
                    # A caller that names no root gets a throwaway one beside the task
                    # directory: an in-process caller (a test) wants the lifecycle
                    # exercised, not a directory in somebody's run tree.
                    recordings_root=(recordings_root
                                     or workdir.parent / f".recordings-{run_id or 'run'}"),
                    run_id=run_id or "run", task_id=task["task_id"],
                    run_seed=run_seed or "0")
                network_name = running.network
            except services_mod.ServiceUnavailable as exc:
                return _invalid(task, exc, stage=exc.stage, service=exc.service)

        manifest = json.loads((repo / "harness.json").read_text())
        entry = manifest["entrypoint"]
        # Trees this harness needs in order to run at all — a CLI it drives, a
        # runtime it was built against. Declared, not inferred: the platform cannot
        # know where somebody's agent CLI is installed.
        runtime_paths = tuple(Path(p).expanduser()
                              for p in (manifest.get("runtime_paths") or []))
        # Inside a container the entrypoint is still `<repo>/<entry>` because the
        # harness tree is mounted at its own host path; only the task's paths move,
        # because the dataset named a location *in the environment*.
        #
        # The interpreter is **the platform's own** for `exec`, mounted in read-only,
        # rather than whatever `python` the image happens to have -- which may be
        # nothing (measured: the real Terminal-Bench image has no `python3` at all).
        # `files` keeps the bare name, because there the host's PATH is the right answer
        # and changing it would change what every existing dataset measures.
        task_arg = (container_mod.to_container(task_file, workdir, task_mount)
                    if is_exec else str(task_file))
        workdir_arg = task_mount if is_exec else str(workdir)
        # The image's interpreter when it has one (§2.5.3), the platform's when it does
        # not. `files` keeps the bare name: there the host's PATH is the right answer and
        # changing it would change what every existing dataset measures.
        image_python = container_mod.image_python(env_spec["image"]) if is_exec else None
        python = image_python or (sys.executable if is_exec else "python")
        argv = [python, str(repo / entry), "--task", task_arg,
                "--workdir", workdir_arg]

        # The harness's own dependencies, from a directory outside the task's
        # environment, resolved for **this** interpreter's Python version. `None` is the
        # ordinary case: nobody configured one, and the run then behaves exactly as it
        # did before this existed. See `eval/harness_runtime.py` for why this is not a
        # derived image and not the platform's interpreter.
        overlay = harness_runtime.lookup(
            repo, python, image=(env_spec["image"] if is_exec else None),
            image_digest=(env_spec.get("image_digest") if is_exec else None))
        if overlay:
            # Only the paths are collected here; the environment is applied below, where
            # `child_env` exists. It used to be written here, one screen above its own
            # definition -- a `NameError` on the one branch nobody had configured yet,
            # which is how it survived: `overlay` is `None` unless a harness runtime is
            # declared, and then the run that declares one is the run that crashes.
            runtime_paths = tuple(runtime_paths) + harness_runtime.extra_paths(overlay)

        # The harness's own writable scratch space, inside its otherwise read-only
        # tree. Exported so a harness need not know the platform's layout, and
        # stated in the environment rather than passed as an argument because the
        # entrypoint contract (INTERFACE.md §1.2) is `--task` and `--workdir` only.
        state_dir = repo / STATE_DIRNAME
        # Created on demand, not just at staging time: a workspace committed before
        # `.state/` existed would otherwise make every run refuse to start, and the
        # sandbox needs a real directory to mount over. Empty and gitignored, so it
        # cannot move a sha.
        state_dir.mkdir(exist_ok=True)

        # A writable HOME, and the XDG directories, per task.
        #
        # This is what makes an agent CLI usable as a harness at all. The sandbox
        # starts from an empty root -- only /usr, /bin, /sbin, /lib*, /etc and the
        # trees it is told to bind exist inside -- so the inherited `HOME`, pointing at
        # a real `/home/<user>` that does not exist there, is a path a CLI cannot even
        # `mkdir` under. Every one of them (Claude Code, Codex, OpenCode, ...) writes
        # session state, config and caches under HOME, and dies immediately if it
        # cannot.
        #
        # Under `workdir` rather than somewhere of its own, because `workdir` is
        # already bound read-write and is already a per-task temporary directory: the
        # isolation this needs is the isolation it already has, and inventing a second
        # mount for it would be a second thing to get wrong. Named with the platform's
        # prefix so it cannot be mistaken for a task's own file.
        #
        # The same directory is created for a container, where it is reached as
        # `<workdir>/.harnessgrad-home` through the task bind mount -- so the two
        # environments agree on where a CLI keeps its state without agreeing on how
        # that path is spelled on the host.
        home = workdir / ".harnessgrad-home"
        (home / "tmp").mkdir(parents=True, exist_ok=True)

        if is_exec:
            # An allowlist, and container-side HOME/XDG/TMPDIR: forwarding the host
            # values would name directories that do not exist inside the image.
            child_env = container_mod.container_env(task_mount)
        else:
            child_env = harness_env()
            child_env.update({
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                # A CLI's scratch files should not land in the directory the task is
                # about, where a verifier would then be reading them.
                "TMPDIR": str(home / "tmp"),
            })
        child_env["HARNESSGRAD_STATE"] = str(state_dir)
        if overlay:
            child_env.update(harness_runtime.env_for(overlay))

        # Publish the agent's model onto the task's network. A containerised harness
        # cannot reach a model on the host's loopback -- measured, every obvious route
        # fails -- and the wrong fix is `--network bridge`, which would give the task the
        # internet and silently change what it is (`eval/modelgate.py`).
        if is_exec:
            base = child_env.get("HG_AGENT_BASE_URL")
            if base and os.environ.get("HG_AGENT_BACKEND", "mock") != "mock" \
                    and modelgate_mod.is_host_local(base):
                if network_name is None:
                    network_name = services_mod.create_network(
                        run_id or "run", task["task_id"])
                    owns_network = True
                target, path = modelgate_mod.split(base)
                model_gateway = modelgate_mod.ModelGateway(
                    bind_host=services_mod.gateway_ip(network_name),
                    target=target, path=path).start()
                # The harness is handed the published address, and nothing else about
                # the mechanism: `HG_AGENT_*` is the contract it already reads.
                child_env["HG_AGENT_BASE_URL"] = model_gateway.base_url

        # How the harness finds the services, and the whole of what it is told about
        # them. `HG_SVC_*` rather than `HG_ENV_*`: the environment's own model,
        # credential and seed go to the service containers only (§2.5.7).
        if running is not None:
            child_env.update(services_mod.harness_env(service_specs))

        # On a `services` environment the harness joins the task's internal network,
        # which is how "reachable by name, not on the internet" is delivered. Passing
        # `env_spec["network"]` straight through would ask docker for a network called
        # `services`, which does not exist.
        # With a gateway published, the harness joins the internal network so it can
        # reach it. Otherwise the declared value stands.
        network = (network_name if network_name is not None
                   else env_spec.get("network", "none"))

        # Everything the harness needs to run at all, plus the platform's interpreter.
        # The platform's interpreter is mounted only when it is going to be used, so an
        # image that brings its own does not also get the host's Python on its disk.
        runtime = ((tuple(runtime_paths)
                    + harness_runtime.extra_paths(overlay)
                    + (() if image_python else container_mod.interpreter_paths()))
                   if is_exec else ())

        def launch():
            if is_exec:
                # `runtime_paths` is deliberately not applied: it exists because a
                # host CLI lives outside `/usr`, and inside a container the runtime is
                # the image's. Mounting a host tree in would defeat the point of
                # having an image at all.
                return container_mod.run(
                    argv, image=env_spec["image"], task_dir=workdir,
                    task_mount=task_mount, harness_root=repo,
                    state_dirs=(state_dir,), cwd=task_mount, env=child_env,
                    timeout_s=timeout_s, network=network,
                    # Stated rather than omitted: the harness gets **no** view of a
                    # service's recording, and the empty list is that decision.
                    readonly_paths=runtime,
                    extra_mounts=tuple(services_mod.mounts_for(
                        "harness", service_specs,
                        running.recordings if running else {})))
            if not sandbox:
                return subprocess.run(argv, capture_output=True, text=True,
                                      timeout=timeout_s, cwd=workdir, env=child_env)
            # `repo` is `<work_root>/<run-id>/workspace`, so the work root -- the one
            # tree that must stay visible -- is two levels up. Read from the
            # platform's own environment rather than guessed from the run id.
            work_root = Path(os.environ.get("HARNESSGRAD_WORK_ROOT")
                             or repo.parent.parent)
            return sandbox_mod.run(argv, platform=PLATFORM_ROOT,
                                   work_root=work_root, task_dir=workdir,
                                   env=child_env, timeout_s=timeout_s, cwd=workdir,
                                   repo=repo, state_dirs=(state_dir,),
                                   runtime_paths=runtime_paths)

        try:
            with phases("agent"):
                proc = launch()
            exit_code, stderr = proc.returncode, proc.stderr[-2000:]
        except subprocess.TimeoutExpired:
            exit_code, stderr = -1, f"timeout after {timeout_s}s"
        except sandbox_mod.SandboxUnavailable as exc:
            # Not swallowed. A run that asked for a sandbox and silently got none
            # produces numbers that mean something other than what they say.
            raise SystemExit(f"refusing to run: sandbox requested but unavailable: "
                             f"{exc}. Pass --no-sandbox to accept that a harness "
                             f"can reach the platform.")

        with phases("collect"):
            answer_path = workdir / "answer.txt"
            answer = answer_path.read_text() if answer_path.exists() else ""
            trace_path = workdir / "trace.jsonl"
            trace = trace_path.read_text() if trace_path.exists() else ""

        # The harness has exited. Everything below grades what it left behind, from
        # outside, with the artifacts frozen -- see INTERFACE.md §2.2 and §2.4.
        with phases("verify"):
            verdict = _verify(workdir, verifier, answer, sandbox=sandbox,
                              timeout_s=verify_timeout_s, env=env_spec,
                              service_specs=service_specs, running=running)

        # The environment's own spend, read from the recordings **before teardown**:
        # the containers are about to be removed and their evidence with them.
        env_usage = running.usage() if running is not None else {}

        return {"task_id": task["task_id"], "answer": answer,
                "trace": trace, "exit_code": exit_code, "stderr": stderr,
                "verdict": verdict, "env_usage": env_usage,
                "harness_runtime": harness_runtime.record(
                    overlay, platform_interpreter=(image_python is None)),
                "model_gateway": ({"published": True,
                                   "target": f"{model_gateway.target[0]}:{model_gateway.target[1]}"}
                                  if model_gateway is not None else None),
                "identity": _last_identity(trace)}
    finally:
        # Step 8, and in a `finally` for the reason the table in §2.5.7 gives: a leaked
        # network would be inherited by the next task, where it would look like
        # isolation that is not there. `stop()` never raises.
        if model_gateway is not None:
            model_gateway.stop()
        if owns_network and network_name is not None:
            leaked_net = services_mod.remove_network(network_name)
            if leaked_net:
                print(f"warning: could not remove the model gateway network for "
                      f"{task['task_id']}: {leaked_net}", file=sys.stderr)
        if running is not None:
            leaked = running.stop()
            if leaked:
                # Not raised. The task's result is the platform's report about the
                # *task*, and a teardown problem after it is a fact about the run.
                print(f"warning: could not fully tear down services for "
                      f"{task['task_id']}: {leaked}", file=sys.stderr)
        shutil.rmtree(workdir, ignore_errors=True)


def _run_one_container_state(repo: Path, task: dict, *, env_spec: dict, setup,
                             verifier, timeout_s: int, verify_timeout_s: int,
                             run_id: str, run_seed: str,
                             recordings_root: Path | None,
                             runtime_paths: tuple[Path, ...] = ()) -> dict:
    """Run a task whose state is the container's own filesystem. INTERFACE.md §2.5.9.

    The order is the design, so it is spelled out here in the same steps the contract
    uses. What makes it worth its extra moving parts is step 6-7: the check's inputs and
    its reward directory are created **after** the harness has stopped, so the harness
    never had access to either -- which is what keeps §2.2's invariants true even though
    the task state travels by snapshot rather than by re-materialization.
    """
    repo = Path(repo).resolve()
    task_mount = env_spec.get("workdir") or container_mod.DEFAULT_WORKDIR
    tid = task["task_id"]
    name = label(f"hg-{run_id or 'run'}-{tid}")
    snapshot_tag = f"hg-snapshot:{label(f'{run_id or "run"}-{tid}')}"
    snapshot = None
    created = False
    network_name = None
    gateway = None
    answer = trace = ""
    #: The task this work belongs to, and the phases it reports. `None` outside a run
    #: (a unit test, a direct call), in which case every phase below is a no-op.
    held = progress_mod.task_of()
    phases = progress_mod.phases_for(held)

    # A throwaway host directory for what has to be copied in or read back out. Not a
    # bind mount: that is the whole point of this state kind.
    with tempfile.TemporaryDirectory(prefix="hg-cstate-") as tmp:
        staging = Path(tmp)
        try:
            # Step 1. The harness tree read-only at its own host path, and its scratch
            # space as a **tmpfs** -- so nothing the containerised harness writes as root
            # lands on the host, and the scratch is not committed into the graded
            # snapshot either.
            state_dir = repo / STATE_DIRNAME
            state_dir.mkdir(exist_ok=True)

            with phases("create"):
                # The internal network is created unconditionally: it is where the model
                # gateway is published, and a harness with no model is not being measured.
                network_name = services_mod.create_network(run_id or "run", tid)
                # The harness's own dependencies, resolved for this image's Python
                # version and mounted read-only from outside the image. It is a **bind
                # mount**, which is what keeps it out of the graded snapshot: measured,
                # `docker commit` records the mount target as an empty directory and
                # nothing under it, so the check's fresh container never sees these
                # packages. See `eval/harness_runtime.py`.
                image_python_here = container_mod.image_python(env_spec["image"])
                overlay = harness_runtime.lookup(
                    repo, image_python_here or sys.executable,
                    image=env_spec["image"],
                    image_digest=env_spec.get("image_digest"))

                container_mod.create(
                    name, image=env_spec["image"],
                    limits=container_mod.limits_from(env_spec), network=network_name,
                    extra_mounts=tuple(
                        ["--mount", f"type=bind,src={repo},dst={repo},readonly"]
                        + [a for pth in (tuple(runtime_paths)
                                         + harness_runtime.extra_paths(overlay)
                                         + (() if image_python_here
                                            else container_mod.interpreter_paths()))
                           for a in ("--mount",
                                     f"type=bind,src={pth},dst={pth},readonly")]),
                    # The harness's scratch and its HOME/XDG/TMPDIR are tmpfs, so nothing
                    # the platform created for its own bookkeeping ends up inside the
                    # snapshot the check grades. The snapshot should be the harness's work,
                    # not the platform's plumbing.
                    tmpfs=(str(state_dir),
                           container_mod.env_home(task_mount)))
            created = True
            if env_spec.get("network") == "bridge":
                container_mod.network_connect(name, "bridge")

            with phases("prepare"):
                # Step 2. SETUP, copied in rather than mounted.
                if setup and setup.get("files"):
                    _materialize(staging / "setup", setup.get("files"), PLATFORM_ROOT)
                    container_mod.cp_in(name, staging / "setup", task_mount,
                                        contents=True)

                manifest = json.loads((repo / "harness.json").read_text())
                entry = manifest["entrypoint"]
                task_file = staging / "task.json"
                task_file.write_text(json.dumps(task))
                container_mod.cp_in(name, task_file, f"{task_mount}/task.json")
                # No `mkdir` for the scratch or the home directory: they are `--tmpfs`
                # mounts, and docker creates their mount points at **create** time. The
                # first version called `mkdir_in` here, which uses `docker exec` -- and a
                # container that has not been started cannot be exec'd. Measured:
                # `container ... is not running`.

                image_python = container_mod.image_python(env_spec["image"])
                argv = [image_python or sys.executable, str(repo / entry), "--task",
                        f"{task_mount}/task.json", "--workdir", task_mount]

                child_env = container_mod.container_env(task_mount)
                child_env["HARNESSGRAD_STATE"] = str(state_dir)
                # Prepended, not replacing: a harness that sets its own `PYTHONPATH` is not
                # the platform's to overwrite.
                child_env.update(harness_runtime.env_for(overlay))

                # Step 3's precondition: the model has to be reachable from inside, and the
                # wrong fix is giving the task the internet it did not ask for.
                base = child_env.get("HG_AGENT_BASE_URL")
                if base and os.environ.get("HG_AGENT_BACKEND", "mock") != "mock" \
                        and modelgate_mod.is_host_local(base):
                    target, path = modelgate_mod.split(base)
                    bind = services_mod.gateway_ip(network_name)
                    gateway = modelgate_mod.ModelGateway(
                        bind_host=bind, target=target, path=path).start()
                    child_env["HG_AGENT_BASE_URL"] = gateway.base_url

                container_mod.start(name)

            # Step 3.
            exit_code, stderr = None, ""
            with phases("agent"):
                try:
                    proc = container_mod.exec_in(name, argv, cwd=task_mount,
                                                 env=child_env, timeout_s=timeout_s)
                    exit_code, stderr = proc.returncode, proc.stderr[-2000:]
                except subprocess.TimeoutExpired:
                    exit_code, stderr = -1, f"timeout after {timeout_s}s"

            with phases("snapshot"):
                # Step 4. Stopping the whole container is what makes "nothing the harness
                # started is still running" structural rather than best-effort, and a
                # stopped container is still committable.
                container_mod.stop(name)
                snapshot = container_mod.commit(name, snapshot_tag)

                # What the harness produced, read back out of the snapshot. `cp_out` is a
                # best-effort read: a harness that wrote no trace is a fact to record.
                if container_mod.cp_out(name, f"{task_mount}/trace.jsonl",
                                        staging / "trace.jsonl"):
                    trace = (staging / "trace.jsonl").read_text()
                if container_mod.cp_out(name, f"{task_mount}/answer.txt",
                                        staging / "answer.txt"):
                    answer = (staging / "answer.txt").read_text()

            # Steps 5-9.
            with phases("verify"):
                verdict = _verify_container_state(
                    snapshot, verifier, task_mount=task_mount, staging=staging,
                    timeout_s=verify_timeout_s, run_id=run_id or "run", task_id=tid,
                    network=env_spec.get("network", "none"),
                    limits=container_mod.limits_from(env_spec))

            return {"task_id": tid, "answer": answer, "trace": trace,
                    "exit_code": exit_code, "stderr": stderr, "verdict": verdict,
                    "env_usage": {}, "identity": _last_identity(trace),
                    "harness_runtime": harness_runtime.record(
                        overlay, platform_interpreter=(image_python_here is None)),
                    "model_gateway": ({"published": True,
                                       "target": f"{gateway.target[0]}:{gateway.target[1]}"}
                                      if gateway is not None else None)}
        finally:
            # Teardown is a phase like any other, and it is the one that used to be
            # invisible while it took minutes: removing one image per task plus the
            # network, with nothing printed. It runs inside the task's context, so it
            # reports even when the path above raised.
            with phases("teardown"):
                if gateway is not None:
                    gateway.stop()
                if snapshot is not None:
                    container_mod.remove_image(snapshot)
                if created:
                    container_mod.remove_container(name)
                if network_name is not None:
                    services_mod.remove_network(network_name)


#: How much of a check's own output goes into its verdict.
#:
#: 600 was the number, and it was measured to be too small for a reason worth writing
#: down: the real task set reaches its test runner by `apt-get install`-ing it, so the
#: last 600 characters of stdout can be package-manager noise printed *after* the test
#: report -- and the report is the only part that says which check failed and why. A
#: method reads this field to decide what to change; a verdict it cannot learn from is a
#: round it cannot use. Measured on `cancel-async-tasks`: the whole detail was
#: `Setting up libcurl4 ... Processing triggers for libc-bin`, and the improver's round-2
#: edit had to guess.
VERDICT_STDOUT_TAIL = 2000
VERDICT_STDERR_TAIL = 800
VERDICT_FAILED_TESTS = 8
VERDICT_DETAIL_LIMIT = 6000


#: The egress proxy the platform's **checks** may use, if the operator configured one.
#:
#: Not for the harness, and the difference matters. A harness reaches its model through the
#: platform -- the model gateway when the endpoint is host-local -- and pushing those calls
#: through an HTTP proxy breaks them. A *check* is different: it runs after the harness has
#: stopped, calls no model, and on this host 83 of 89 Terminal-Bench checks bootstrap their
#: own test runner with `curl https://astral.sh/uv/... | sh`. When that fetch cannot leave
#: the machine, the check never runs -- and a check that never ran was recorded as a task
#: the harness failed, which is the wrong attribution and a wasted round.
#:
#: Measured on this host: `astral.sh` is unreachable directly (403 in a container, TLS
#: handshake timeout from the shell) while `pypi.org` is fine; with
#: `HG_EGRESS_PROXY=http://172.17.0.1:7890` the task's own `test.sh` runs to completion and
#: writes its `ctrf.json`. Unset, nothing about a run changes.
EGRESS_PROXY_ENV = "HG_EGRESS_PROXY"


def _check_env() -> dict[str, str]:
    """What the check's process is told about how to leave the machine, if anything.

    All four spellings, because the tools disagree: `apt` reads the lowercase ones, `curl`
    prefers the uppercase, and `uv` honours both. `NO_PROXY` keeps anything local (a
    service, a package mirror on the bridge) from being pushed through the proxy.
    """
    proxy = (os.environ.get(EGRESS_PROXY_ENV) or "").strip()
    if not proxy:
        return {}
    local = "localhost,127.0.0.1,::1"
    return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy,
            "https_proxy": proxy, "NO_PROXY": local, "no_proxy": local}


def _read_json(path: Path) -> dict | None:
    """Parse a file another program wrote. `None` on anything unexpected."""
    try:
        loaded = json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _test_report_failures(report: dict | None) -> tuple[list[str], str]:
    """Failing tests from a CTRF report, and a one-line summary of the run.

    A check may write a machine-readable report beside its reward file (the real task
    set passes `--ctrf /logs/verifier/ctrf.json` to pytest). That report is the
    difference between `reward 0` and
    `test_out_html_bypasses_filter: the XSS bypass failed`, and only the second is
    something a method can act on.

    Parsed defensively: it is another project's format, and a report this cannot read
    must not take the verdict down with it. An empty list is a fact too -- it means the
    report was there and nothing failed.
    """
    results = (report or {}).get("results")
    if not isinstance(results, dict):
        return [], ""
    summary = results.get("summary") if isinstance(results.get("summary"), dict) else {}
    counts = " ".join(
        f"{summary[key]} {name}" for key, name in
        (("passed", "passed"), ("failed", "failed"), ("skipped", "skipped"))
        if isinstance(summary.get(key), int) and summary[key])
    failures: list[str] = []
    tests = results.get("tests")
    for test in tests if isinstance(tests, list) else []:
        if not isinstance(test, dict) or test.get("status") != "failed":
            continue
        name = str(test.get("name") or "?")
        # The assertion, not the summary. `pytest-json-ctrf` fills `message` with
        # "The test failed in the call phase due to an assertion error" -- true, and
        # useless to a method deciding what to change. The assertion itself is in
        # `trace`, on the `E ` lines pytest prints.
        assertion = ""
        for line in str(test.get("trace") or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("E ") and len(stripped) > 2:
                assertion = " ".join(stripped[2:].split())
        if not assertion:
            lines = str(test.get("message") or "").splitlines()
            assertion = " ".join(lines[0].split()) if lines else ""
        failures.append(f"{name}: {assertion[:240]}" if assertion else name)
    if len(failures) > VERDICT_FAILED_TESTS:
        rest = len(failures) - VERDICT_FAILED_TESTS
        failures = failures[:VERDICT_FAILED_TESTS] + [f"... and {rest} more"]
    return failures, counts


def _check_output_detail(out: str, err: str) -> str:
    """Both of the check's streams, labelled and bounded.

    `stderr` used to be dropped entirely, which is where a check that *could not run*
    says so: measured on `break-filter-js-from-html`, `uvx: command not found` went to
    stderr while stdout held apt's progress, so the verdict read as a failed task rather
    than a check that never tested anything.
    """
    parts = []
    if out and out.strip():
        parts.append(f"stdout {out.strip()[-VERDICT_STDOUT_TAIL:]}")
    if err and err.strip():
        parts.append(f"stderr {err.strip()[-VERDICT_STDERR_TAIL:]}")
    return " | ".join(parts)


def _verify_container_state(snapshot: str, verifier, *, task_mount: str,
                            staging: Path, timeout_s: int, run_id: str,
                            task_id: str, network: str = "none",
                            limits: dict | None = None) -> dict:
    """Steps 5-9: a fresh container from the snapshot, the check's inputs copied in
    afterwards, and the score read from the file the check writes.

    The container is fresh, so nothing the harness left running is present. The inputs go
    in at step 6 and the reward directory at step 7, both after the harness stopped --
    which is why this is a re-materialization and not a snapshot of the harness's view.
    """
    spec = verifier or {"kind": "answer", "expected": None}
    kind = spec.get("kind", "answer")
    name = label(f"hg-verify-{run_id}-{task_id}")
    created = False
    try:
        # The environment's network, not `none`. Measured: the real task set installs
        # its own test dependencies at verification time -- all 89 `test.sh` files
        # `apt-get install` or `pip install` before running anything -- so a check given
        # no network cannot run at all, and reports a reward of 0 that looks exactly like
        # a harness that failed the task. The oracle control is what found this.
        container_mod.create(name, image=snapshot, limits=limits or {},
                            network=network, extra_mounts=())
        created = True

        _copy_verifier_inputs(name, spec, staging, task_mount)

        container_mod.start(name)

        # Step 7. The reward directory exists only now -- and it is created *after*
        # `start` because `mkdir_in` is a `docker exec`, which needs a running
        # container. The ordering that matters is the one the contract states (after
        # the harness has stopped); this is only about which docker verb can do it.
        reward_file = spec.get("reward_file")
        if reward_file:
            container_mod.mkdir_in(name, str(Path(reward_file).parent), "0755")
        argv = [str(a) for a in spec.get("argv") or []]
        if not argv:
            return {"kind": kind, "passed": False,
                    "detail": "a command verifier with state='container' needs `argv`"}
        try:
            proc = container_mod.exec_in(name, argv,
                                         cwd=spec.get("cwd") or task_mount,
                                         env=_check_env() or None,
                                         timeout_s=timeout_s)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return {"kind": kind, "passed": False,
                    "detail": f"the verifier timed out after {timeout_s}s"}

        # Step 9. `reward_file` first: the real task set writes 1/0 into a file and exits
        # 0 either way, so the exit code is not a fallback, it is noise.
        if reward_file:
            got = staging / "reward.txt"
            if not container_mod.cp_out(name, reward_file, got):
                return {"kind": kind, "passed": False,
                        "detail": f"the check wrote no {reward_file}; exit {code}, "
                                  + (_check_output_detail(out, err)
                                     or "and printed nothing at all")}
            raw = got.read_text().strip()
            try:
                score = float(raw)
            except ValueError:
                return {"kind": kind, "passed": False,
                        "detail": f"{reward_file} held {raw!r}, which is not a number"}
            # The check's own output goes in the detail, because with a `reward_file`
            # the exit code says nothing and a bare "reward 0" leaves a reader with no
            # way to tell a wrong answer from a check that could not run at all. It
            # cost a full debugging round to learn that: the oracle solved the task,
            # the reward was still 0, and the reason was only in this output.
            #
            # **The report comes first, and the streams after it.** A method reads the
            # detail to decide what to change, so the part that names the failing check
            # has to survive the truncation; raw output is evidence of last resort.
            detail = [f"reward {score:g} from {reward_file} (exit {code})"]
            report_path = spec.get("evidence_file")
            if report_path:
                got_report = staging / Path(str(report_path)).name
                if container_mod.cp_out(name, str(report_path), got_report):
                    failures, counts = _test_report_failures(_read_json(got_report))
                    detail.append(f"check_report {report_path}"
                                  + (f" ({counts})" if counts else ""))
                    detail.extend(f"FAILED {line}" for line in failures)
                    if not failures:
                        detail.append("check_report lists no failing test")
                else:
                    # Not a policy change -- the score stays what the check wrote -- but
                    # the fact has to be on the record: a reward of 0 written by a check
                    # whose tests never ran is not a failed harness.
                    detail.append(f"the check wrote no {report_path}: its tests did "
                                  f"not run, so this 0 is the check's setup, not the "
                                  f"harness's answer")
            detail.append(_check_output_detail(out, err))
            return {"kind": kind, "passed": bool(score),
                    "score": score,
                    "detail": " | ".join(p for p in detail if p)[:VERDICT_DETAIL_LIMIT]}

        pass_when = spec.get("pass_when", "exit0")
        passed = code == 0
        detail = f"exit {code}"
        if pass_when == "exit0_stdout":
            want = str(spec.get("stdout", ""))
            passed = passed and out.strip() == want.strip()
            detail = f"exit {code}, stdout {out.strip()[:80]!r} vs {want[:80]!r}"
        if not passed and out.strip():
            detail += f" | {out.strip()[-200:]}"
        return {"kind": kind, "passed": passed, "detail": detail}
    finally:
        if created:
            container_mod.remove_container(name)


def _input_source(entry: dict, staging: Path) -> Path:
    """The host path behind a verifier input, for `docker cp`."""
    if "content" in entry and entry["content"] is not None:
        written = staging / f"input-{abs(hash(entry.get('dst') or entry.get('path')))}"
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(str(entry["content"]))
        return written
    source = Path(str(entry["from"]))
    if not source.is_absolute():
        source = PLATFORM_ROOT / source
    if not source.exists():
        raise ValueError(f"verifier input source {source} does not exist")
    return source


def _copy_verifier_inputs(name: str, spec: dict, staging: Path, task_mount: str) -> None:
    """Step 6: put a check's inputs where the check expects them.

    `dst` is a container path; `path` remains the original relative-to-the-workdir form, so
    nothing that worked before changes. Split out of `_verify_container_state` so the
    *decision* below can be tested without a docker daemon -- the decision is what was wrong.
    """
    for entry in spec.get("inputs") or []:
        if entry.get("dst"):
            # **`src/.`, not `src`.** `docker cp <dir> <ctr>:<dst>` *nests* when `dst`
            # already exists, and whether it exists is not the platform's to assume: the
            # container is built from the snapshot, which is the **harness's** filesystem.
            # Measured (`break-filter-js-from-html`): the harness ran
            # `mkdir -p /tests && cp /app/filter.py /tests/filter.py` while probing the
            # task's filter -- its own business -- so the check's inputs landed at
            # `/tests/tests/test.sh`, the check died with
            # `bash: /tests/test.sh: No such file or directory`, and the platform recorded
            # that as a task the *harness* failed. In the same round a harness that
            # happened not to touch `/tests` verified normally, which is what made this
            # look like a flaky task rather than a staging bug.
            #
            # `src/.` copies the *contents* into the destination and is correct whether or
            # not it exists (measured both ways on a real container). A file input keeps
            # the plain form: `docker cp <file> <ctr>:<dst>` overwrites rather than nests.
            source = _input_source(entry, staging)
            container_mod.cp_in(name, source, str(entry["dst"]),
                                contents=source.is_dir())
        else:
            _copy_relative_input(name, entry, staging, task_mount)


def _copy_relative_input(name: str, entry: dict, staging: Path,
                         task_mount: str) -> None:
    """An input with `path` (not `dst`): materialize it into the container's workdir."""
    rel = str(entry["path"])
    target = staging / "rel"
    _materialize(target, [entry], PLATFORM_ROOT)
    container_mod.cp_in(name, target / rel, f"{task_mount}/{rel}")


#: 形如"harness 自己调模型时连不上"的失败痕迹。
#:
#: 为什么要单独认这一类:实测(2026-10-02,`loop-terminal_bench-submit-1`)两道题的
#: harness 死在 `RuntimeError: 3 attempts failed, last: Connection error.`,而平台给它们
#: 记的是 **0.0** —— 也就是把一次**网关故障**记成了"harness 很弱"。§2.5.7 的全部主张
#: 就是"测不了的题"和"测出来是 0 的题"必须分开,这一条正是它要防的那种混淆。
#:
#: 机制已经复现过(见 `eval/modelgate.py` 的说明):vLLM 的 uvicorn 默认 5 秒关掉空闲
#: keep-alive,而网关把上游的 EOF 半关到客户端,于是 harness 隔十几秒的下一次调用
#: 复用了一条死连接 —— 只要中间跑过一条几十秒的命令就会踩到。
_PROVIDER_FAILURES = (
    "APIConnectionError", "APITimeoutError", "Connection error",
    "RemoteDisconnected", "Connection refused", "Connection reset",
    "connect timeout", "Server disconnected",
)


def _provider_failure(result: dict) -> dict | None:
    """harness 是不是**因为连不上模型端点**而死的。`None` 表示不是。

    判据刻意很窄,只在这种情况成立:

      * harness 以**非零退出码**结束 —— 它自己没把这次故障吸收掉;
      * stderr 的**尾部**有客户端连接/超时的痕迹 —— 尾部,不是全文:一个连上又掉了、
        但自己重试成功的 harness 会在中间留下同样的字眼,而那是它做对了。

    退出码 0 的一律不算:它跑完了,分数就是它该得的分数。
    """
    if not result.get("exit_code"):            # None(无效)或 0 都不算
        return None
    tail = (result.get("stderr") or "")[-1500:]
    for marker in _PROVIDER_FAILURES:
        if marker in tail:
            # 摘出那一行,放进记录里 —— 读者要的是"哪一句"。
            line = next((l.strip() for l in reversed(tail.splitlines())
                         if marker in l), marker)
            return {"marker": marker, "line": line[:240],
                    "exit_code": result.get("exit_code")}
    return None


def _invalid(task: dict, exc, *, stage: str, service: str | None) -> dict:
    """A task the platform could not measure, as opposed to one it measured as zero.

    INTERFACE.md §2.5.7: the result is **not** a `per_task` score and **not** a zero,
    and it is attributed to the dataset and the service by name. The distinction is the
    entire reason this platform exists — a broken mock API and a weak harness must not
    be the same number — and it is independently attested: JarvisBench reports that
    "harness or provider failures are marked invalid rather than assigned a score of
    zero" (`INTERFACE.md` §2.5.7).
    """
    return {"task_id": task["task_id"], "invalid": {
                "stage": stage, "service": service, "detail": str(exc)},
            "answer": "", "trace": "", "exit_code": None, "stderr": "",
            "env_usage": {}, "model_gateway": None, "identity": None}


def _verify(workdir: Path, verifier: dict | None, answer: str,
            *, sandbox: bool, timeout_s: int,
            env: dict | None = None,
            service_specs: list[dict] | None = None,
            running=None) -> dict:
    """Decide whether the task was completed. Returns {kind, passed, detail}.

    `answer` is the legacy path and stays byte-for-byte what it was: stripped,
    lowercased, compared to `expected`. A dataset that declares only `SCORABLE`
    reaches exactly this branch, which is what makes adding verifiers a change that
    did not touch any existing dataset's numbers.
    """
    spec = verifier or {"kind": "answer", "expected": None}
    kind = spec.get("kind", "answer")
    env_spec = env or {"kind": "files"}
    is_exec = env_spec.get("kind") == "exec"
    task_mount = env_spec.get("workdir") or container_mod.DEFAULT_WORKDIR
    service_specs = list(service_specs or [])

    if kind == "answer":
        expected = spec.get("expected")
        passed = bool(expected is not None
                      and answer.strip().lower() == str(expected).strip().lower())
        return {"kind": "answer", "passed": passed,
                "detail": f"answer {answer.strip()[:80]!r} vs {str(expected)[:80]!r}"}

    if kind != "command":
        raise ValueError(f"unknown verifier kind {kind!r}")

    # Re-materialize the check's inputs, overwriting whatever the harness left there.
    try:
        _materialize(workdir, spec.get("inputs"), PLATFORM_ROOT)
    except (ValueError, OSError) as exc:
        return {"kind": "command", "passed": False,
                "detail": f"could not stage the verifier's inputs: {exc}"}

    argv = [str(a) for a in spec["argv"]]
    cwd = workdir / spec["cwd"] if spec.get("cwd") else workdir

    try:
        if is_exec:
            # A **fresh** container from the same image, sharing only the task bind
            # mount -- see `eval/container.run` for why the check does not reuse the
            # harness's container. Its `cwd` and its `argv` are container paths, and
            # it is handed no host environment at all.
            #
            # With services, the check **joins the task's internal network and they are
            # still up** (INTERFACE.md §2.5.7 step 6): §2.2 already allows a check that
            # needs a database in a particular state, and a check that needs to ask the
            # mock API what it was sent has the same shape. `running` being None here
            # would be a caller bug -- a service task whose check silently could not
            # reach its service would fail the task for a reason nothing records.
            proc = container_mod.run(
                argv, image=env_spec["image"], task_dir=workdir,
                task_mount=task_mount, cwd=container_mod.to_container(
                    cwd, workdir, task_mount),
                env=_verifier_env_container(),
                timeout_s=timeout_s,
                network=(running.network if running is not None
                         else env_spec.get("network", "none")),
                task_dir_writable=False,
                extra_mounts=tuple(services_mod.mounts_for(
                    "verifier", service_specs,
                    running.recordings if running else {})))
        elif sandbox:
            work_root = Path(os.environ.get("HARNESSGRAD_WORK_ROOT")
                             or workdir.parent.parent)
            proc = sandbox_mod.run(argv, platform=PLATFORM_ROOT, work_root=work_root,
                                   task_dir=workdir, env=_verifier_env(workdir),
                                   timeout_s=timeout_s,
                                   cwd=cwd, repo=workdir, task_dir_writable=False)
        else:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout_s, cwd=cwd,
                                  env=_verifier_env(workdir))
        code, out = proc.returncode, proc.stdout
    except FileNotFoundError as exc:
        return {"kind": "command", "passed": False,
                "detail": f"the verifier command does not exist: {exc}"}
    except subprocess.TimeoutExpired:
        return {"kind": "command", "passed": False,
                "detail": f"the verifier timed out after {timeout_s}s"}

    pass_when = spec.get("pass_when", "exit0")
    passed = code == 0
    detail = f"exit {code}"
    if pass_when == "exit0_stdout":
        want = str(spec.get("stdout", ""))
        passed = passed and out.strip() == want.strip()
        detail = f"exit {code}, stdout {out.strip()[:80]!r} vs {want[:80]!r}"
    if not passed and out.strip():
        detail += f" | {out.strip()[-200:]}"
    return {"kind": "command", "passed": passed, "detail": detail}


def _harness_failure(result: dict) -> dict | None:
    """Why a harness left nothing to read, when it left nothing.

    A task can be **measured** and still say nothing about the harness: the check reads
    the artifact, finds it wrong, and records a 0.0 -- and that 0.0 is a real score. But
    if the harness *also* wrote no trace, then no method has anything to read while the
    curve reports a number, and those two situations are indistinguishable on the curve.

    Measured, and the reason this exists: a harness that needed a package the task image
    did not have died on its first model call with `ModuleNotFoundError`, exit code 1.
    The trace was 0 bytes, the score was 0.000, and the run reported *nothing* -- the
    exit code and stderr were computed here and then dropped, so "the method decided not
    to edit" was the only reading available. A harness that cannot start is a fact about
    the run and belongs in the record.

    Every harness the platform ships writes `trace.jsonl`, so an empty one is always
    abnormal; a harness that simply chose to write nothing is worth knowing about too.
    Deliberately **not** folded into `invalid`: the task was measured, so it keeps its
    score. This is the attribution, not a reason to drop the measurement (§2.5.7).
    """
    if (result.get("trace") or "").strip():
        return None
    return {"exit_code": result.get("exit_code"),
            "stderr": (result.get("stderr") or "")[-1500:]}


def _reporter():
    """The reporter an evaluation should narrate through.

    The context first, then the console's own. Both are needed and neither is
    redundant:

    * the **context** is how a task's phases reach the code that runs them
      (`eval/container.py` beats while it waits inside a single `docker exec`);
    * the **console** is what makes that context exist at all when the driver has not
      set one. Measured: the driver's own calls to `evaluate` (`driver.py` round 0, the
      mode B loop, the eval side) run on the main thread with no context set, and the
      first version of this put the progress events *only* in the context -- so a real
      run reported the method call's phases and nothing at all for the four tasks it
      spent the hour on. A run that narrates its shortest wait and hides its longest
      is worse than one that narrates neither.
    """
    from eval.console import CONSOLE
    return progress_mod.current() or CONSOLE.reporter


def evaluate(repo: Path, tasks: list[dict], scorable: dict[str, str],
             cache: dict | None = None, harness_sha: str = "",
             sandbox: bool = True, setups: dict | None = None,
             verifiers: dict | None = None,
             envs: dict | None = None,
             run_id: str = "", run_seed: str = "",
             recordings_root: Path | None = None,
             round_no: int = 0) -> dict:
    """Run a harness over a task set and score it.

    `scorable` maps task_id -> expected answer. Scoring lives here, not in the
    Trainer, because a subject that scores itself destroys the comparison.

    Two functions so that the whole evaluation -- not just each task's own phases --
    runs inside one reporting context, which is what lets the container client beat
    while it waits on a `docker exec`. The signature and the body are the same as
    before; only the `with` is new.
    """
    with progress_mod.reporting(_reporter()):
        return _evaluate(repo, tasks, scorable, cache=cache, harness_sha=harness_sha,
                         sandbox=sandbox, setups=setups, verifiers=verifiers,
                         envs=envs, run_id=run_id, run_seed=run_seed,
                         recordings_root=recordings_root, round_no=round_no)


def _evaluate(repo: Path, tasks: list[dict], scorable: dict[str, str],
              cache: dict | None = None, harness_sha: str = "",
              sandbox: bool = True, setups: dict | None = None,
              verifiers: dict | None = None,
              envs: dict | None = None,
              round_no: int = 0,
              run_id: str = "", run_seed: str = "",
              recordings_root: Path | None = None) -> dict:
    cache = cache if cache is not None else {}
    per_task, traces, verdicts = {}, {}, {}
    #: Tasks the platform could not measure, as opposed to ones it measured as zero.
    #: Kept out of `per_task` entirely (INTERFACE.md §2.5.7): a task that never ran is
    #: not a task that scored nothing, and putting a `0.0` here is the specific lie
    #: this whole mechanism exists to prevent.
    invalid: dict[str, dict] = {}
    #: Tasks the platform *did* measure but whose harness left no trace, with the exit
    #: code and the tail of its stderr. Kept apart from `invalid` on purpose: an invalid
    #: task is absent from `per_task` because there is no measurement, while these keep
    #: their score. Same split the platform makes everywhere else -- the attribution is
    #: not a reason to throw the number away.
    harness_failed: dict[str, dict] = {}
    #: What each harness **printed**, per task, kept whether it failed or not.
    #:
    #: `harness_failed` is the narrow version of this: it fires only when the trace is
    #: empty, and it answers "did the harness start?". A task that scored 0 with a
    #: non-empty trace still has a question attached to it ("it ran, so what did it
    #: say?"), and the answer was in `result["stderr"]` and thrown away. Measured: four
    #: of five tasks at 0.0 on `loop-terminal_bench-26452c` with no explanation anywhere
    #: in the run; the explanation had to be reconstructed by hand from the traces.
    harness_output: dict[str, dict] = {}
    #: Which interpreter ran the harness on each task and where its own dependencies
    #: came from (`eval/harness_runtime.py`). Distinct records, because the version is a
    #: property of the task's image and one run may cross several.
    runtime_records: list[dict] = []
    declarations: list[dict] = []
    tampered: dict[str, list[str]] = {}
    env_usage = {"input": 0, "output": 0, "calls": 0, "services_reported": 0}
    #: Whether the platform had to publish the agent's model onto the task's network.
    #: A fact about the measurement: a containerised harness that could not reach a
    #: model would score zero for a reason that is not about the harness.
    model_gateway: dict | None = None

    # The harness is arbitrary code the platform did not write, and it runs with
    # the platform on disk. One pair of hashes around the whole task set, not one
    # pair per task: a 400-task benchmark would otherwise hash the platform 400
    # times, and the cost of the check would scale with the cost of the run it is
    # protecting. Granularity is not lost -- the report names files, and which
    # task touched them is not something a failing run needs.
    before = snapshot(PLATFORM_ROOT)

    for task_index, task in enumerate(tasks, start=1):
        tid = task["task_id"]
        key = f"{harness_sha}:{tid}"
        if key in cache:
            per_task[tid] = cache[key]["score"]
            traces[tid] = cache[key]["trace"]
            if cache[key].get("harness_failed"):
                harness_failed[tid] = cache[key]["harness_failed"]
            cached_record = cache[key].get("harness_runtime")
            if cached_record and cached_record not in runtime_records:
                runtime_records.append(cached_record)
            if cache[key].get("harness_output"):
                harness_output[tid] = cache[key]["harness_output"]
            if cache[key].get("verdict"):
                verdicts[tid] = cache[key]["verdict"]
            continue

        # One task, one record, and the phases inside it belong to it. Where this
        # context is set, `eval/container.py` starts beating while it waits, so a
        # task that spends 40 minutes inside a single `docker exec` stops being
        # indistinguishable from a dead driver. See `eval/progress.py`.
        # `round_no` is what makes the event stream readable: every phase of every task
        # used to be labelled round 0, including the candidate rounds, so a log could not
        # say which round a task belonged to. The traces and verdicts were stored per
        # round all along; only the live record had lost the number.
        with progress_mod.task_progress(tid, round_no=round_no, index=task_index,
                                        total=len(tasks)) as held:
            try:
                result = run_one(repo, task, sandbox=sandbox,
                                 setup=(setups or {}).get(tid),
                                 verifier=(verifiers or {}).get(tid),
                                 env=(envs or {}).get(tid),
                                 run_id=run_id, run_seed=run_seed,
                                 recordings_root=recordings_root)
            except BaseException as exc:
                # The record says the task died and why; the exception still travels.
                held.failed(f"{type(exc).__name__}: {exc}")
                raise

            # A task the platform could not measure never reaches scoring. It is *not*
            # cached either: the cache key is `(harness_sha, tid)` and the reason this
            # task failed was not the harness, so caching it would carry a dataset bug
            # into every later round and hide the round where it was introduced.
            if result.get("invalid"):
                invalid[tid] = result["invalid"]
                held.failed(str(result["invalid"].get("detail") or "")[:200])
                continue

            # The score the platform computed for this task. Set on the holder so the
            # task's closing record carries it: a panel showing "task 3/5 done" without
            # the number is a progress bar; with it, it is the round forming.
            held.score = float((result.get("verdict") or {}).get(
                "score", 1.0 if (result.get("verdict") or {}).get("passed") else 0.0))

        # The score is the *platform's* verdict on what the harness left behind, and
        # the harness's exit code deliberately does not enter it: a run that crashed
        # but produced a working artifact completed the task. See INTERFACE.md §2.2.
        verdict = result.get("verdict") or {}
        # `score` first: a `reward_file` check returns a number and the platform must
        # use it, not collapse it to "did it pass". A fractional reward is a score
        # (INTERFACE.md §2.5.9), and discarding it would silently turn a 0.5 into a 1.
        score = float(verdict.get("score",
                                  1.0 if verdict.get("passed") else 0.0))
        per_task[tid] = score
        verdicts[tid] = verdict
        traces[tid] = result["trace"]
        # A harness that left no trace is recorded next to its score rather than
        # instead of it. See `_harness_failure` for what this looked like when it was
        # dropped: a crashed harness and a harness that chose not to edit were the same
        # reading, and the run said nothing either way.
        failure = _harness_failure(result)
        # Both ends. A tail alone lost the harness's own prints on the one run that
        # needed them most: measured on an eval run of `sanitize-git-repo`, 6 KB of
        # `openai` retry traceback filled the capture completely and the first thing the
        # harness said was gone. The head is where a harness states what it is doing.
        raw = result.get("stderr") or ""
        harness_output[tid] = {"exit_code": result.get("exit_code"),
                               "stderr": (raw if len(raw) <= 8000
                                          else raw[:2000] + "\n...\n" + raw[-6000:])}
        if failure:
            harness_failed[tid] = failure
        runtime_record = result.get("harness_runtime")
        if runtime_record and runtime_record not in runtime_records:
            runtime_records.append(runtime_record)
        for key_name in ("input", "output", "calls"):
            value = (result.get("env_usage") or {}).get(
                {"input": "input_tokens", "output": "output_tokens",
                 "calls": "calls"}[key_name])
            if isinstance(value, int):
                env_usage[key_name] += value
        env_usage["services_reported"] += (result.get("env_usage") or {}).get(
            "services_reported", 0)
        if result.get("model_gateway"):
            model_gateway = result["model_gateway"]
        declared = result.get("identity")
        if declared:
            declarations.append(declared)
        cache[key] = {"score": score, "trace": result["trace"],
                      "harness_failed": failure,
                      "harness_output": harness_output.get(tid),
                      # The verdict travels with the score. Without this, a cached round
                      # writes `score 0.0, kind null, detail ""` -- the exact "a number
                      # with no explanation" this record exists to prevent. Measured on
                      # `loop-terminal_bench-41133` round 5: all five verdict files were
                      # empty because the round was entirely cache hits.
                      "verdict": verdicts.get(tid) or {},
                      "harness_runtime": result.get("harness_runtime")}

    # What the harness spent. Read from the traces rather than asked for: a harness
    # that reports its own cost can under-report, and every harness already writes
    # `usage` into its trace because the contributor spec requires it. A method's
    # acceptance rule may be cost-aware (RRSI's is: it permits a candidate to spend
    # more tokens only in proportion to the score it gains), and a cost rule fed a
    # constant zero is not a weak rule, it is no rule -- measured: the platform
    # reported `generation_tokens: 0` for every point, so `Delta C` was always 0 and
    # `beta0 + beta1 * Delta S` was satisfied by every candidate.
    tokens = {"input": 0, "output": 0, "calls": 0, "tasks_reported": 0}
    for trace in traces.values():
        usage = _last_usage(trace)
        if usage is None:
            continue
        tokens["tasks_reported"] += 1
        for key, field in (("input", "input_tokens"), ("output", "output_tokens")):
            value = usage.get(field)
            if isinstance(value, int):
                tokens[key] += value
        if isinstance(usage.get("calls"), int):
            tokens["calls"] += usage["calls"]

    # A cached task never ran, so a fully-cached evaluation reports nothing --
    # correct: the platform did not execute anything this time.
    tampered = {k: v for k, v in diff(before, snapshot(PLATFORM_ROOT)).items() if v}
    # What the harness said it was. One declaration is the normal case; more than one
    # distinct one means the harness changed what ran it mid-run, which is a fact a
    # reader needs rather than something to average away.
    identity_declared = None
    identity_variants: list[dict] = []
    for declared in declarations:
        if declared not in identity_variants:
            identity_variants.append(declared)
    if identity_variants:
        identity_declared = identity_variants[0]

    return {"per_task": per_task, "traces": traces, "tampered": tampered,
            "verdicts": verdicts,
            "invalid": invalid,
            "harness_failed": harness_failed,
            "harness_output": harness_output,
            "harness_runtime": runtime_records,
            "env_usage": env_usage,
            "model_gateway": model_gateway,
            "identity_declared": identity_declared,
            "identity_variants": identity_variants,
            "tokens": tokens}

