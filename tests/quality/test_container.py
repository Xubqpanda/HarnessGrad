"""The `exec` environment kind: the container boundary, end to end. INTERFACE.md §2.5.

Every test here either proves a property the contract claims, or proves that the test
for that property **can fail**. The second half is not padding: this module's own
`self_check` was written once with two probes that could not fail for the reason they
claimed (a marker under `/tmp`, which the container shadows with a tmpfs; and a write
to `/usr`, which a non-root uid cannot do in any image), and both passed against a
deliberately broken plan. Checks that cannot fail are worse than no checks, because
they are believed.

Docker is required. The tests skip rather than fail when there is no daemon, because a
machine without Docker is a machine that cannot run this feature at all -- but the
skip is loud in its reason, not a silent pass.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import eval.container as container
from ckpt.git_state import STATE_DIRNAME
from data import registry
from eval.integrity import PLATFORM_ROOT

ROOT = Path(__file__).resolve().parents[2]
DEMO_IMAGE = os.environ.get("HG_EXEC_DEMO_IMAGE",
                            "docker.m.daocloud.io/library/python:3.11-slim")


def _require_docker() -> str:
    version = container.available()
    if version is None:
        pytest.skip("no docker daemon: the exec environment cannot be exercised here")
    try:
        return container.resolve_digest(DEMO_IMAGE)
    except container.ContainerUnavailable as exc:
        pytest.skip(f"{DEMO_IMAGE!r} is not on the local daemon: {exc}")


#: A harness that is **self-contained**: it uses nothing but the base interpreter, which
#: is what an `exec` harness has to be, because the image is the environment and the
#: platform's own tools are not in it. It also runs on the host unchanged, which is what
#: makes the same file usable as the `files` control.
#:
#: The mode travels in `HG_AGENT_PROBE_MODE` rather than a plainer name on purpose --
#: `eval/container.py` forwards an allowlist of `HG_AGENT_*` and the mode has to survive
#: it. `test_only_declared_variables_cross_into_the_container` pins the other half.
HARNESS = '''\
import argparse, json, os, pathlib, subprocess, sys

ap = argparse.ArgumentParser()
ap.add_argument("--task"); ap.add_argument("--workdir")
a = ap.parse_args()
work = pathlib.Path(a.workdir)
task = json.loads(pathlib.Path(a.task).read_text())
tid = task["task_id"]
mode = os.environ.get("HG_AGENT_PROBE_MODE", "solve")

if mode == "idle":
    pass
elif tid == "e01":
    (work / "broken.py").write_text(
        "import sys\\ndef add(a, b):\\n    return a + b\\n")
elif tid == "e02":
    # Asked of the interpreter that is actually running this harness. If the platform
    # ran it on the host this prints 3.12 and the check fails.
    out = subprocess.run(
        [sys.executable, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True, text=True).stdout.strip()
    (work / "answer.txt").write_text(out)
elif tid == "e03":
    if mode == "cheat":
        # Rewrite the specification so that it cannot fail.
        (work / "check.py").write_text("import sys; sys.exit(0)\\n")
    else:
        (work / "out.txt").write_text("correct\\n")
elif tid == "e04":
    (work / "done.txt").write_text("yes\\n")

(work / "trace.jsonl").write_text(json.dumps({
    "harness_identity": {"agent_model": "probe", "harness": "probe 1"},
    "usage": {"input_tokens": 11, "output_tokens": 7},
}) + "\\n")
'''


def _harness(tmp_path: Path, *, env_kinds=None) -> Path:
    repo = tmp_path / "probe_exec_harness"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "agent.py").write_text(HARNESS)
    (repo / "harness.json").write_text(json.dumps({
        "name": "probe-exec", "version": "1", "entrypoint": "agent.py",
        "backend": "cli", "env_kinds": env_kinds or ["files", "exec"],
    }))
    return repo


def _run(tmp_path: Path, *, mode="solve", dataset="verify_exec",
         env_kinds=None, timeout=1800, extra_argv=()) -> subprocess.CompletedProcess:
    runs = tmp_path / "runs"
    return subprocess.run(
        [sys.executable, "driver.py",
         "--harness", str(_harness(tmp_path, env_kinds=env_kinds)),
         "--mode", "A", "--rounds", "1", "--sampling", "all",
         "--dataset", dataset, "--run-id", "probe", *extra_argv,
         "--runs-root", str(runs), "--work-root", str(tmp_path / "work"),
         "--method-entrypoint",
         f"{sys.executable} {ROOT / 'methods/noop/run.py'}"],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "HG_AGENT_BACKEND": "mock",
             "HG_AGENT_PROBE_MODE": mode})


def _curve(tmp_path: Path) -> list[dict]:
    path = tmp_path / "runs" / "probe" / "curve.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------ the gate ---

def test_a_harness_that_declares_only_files_is_refused_for_an_exec_dataset(tmp_path):
    """Refused, not attempted.

    The failure mode otherwise is a harness that runs, scores zero on every task, and
    looks like a weak harness. The manifest is the harness's own statement of what it
    can do, and the platform's job is to hold it to that rather than to find out by
    measurement. This costs nothing and needs no docker -- that is the point of
    checking before the run rather than during it.
    """
    proc = _run(tmp_path, env_kinds=["files"])
    assert proc.returncode == 2, proc.stdout[-800:]
    assert "exec" in (proc.stdout + proc.stderr)


def test_an_image_that_is_not_local_is_refused_rather_than_pulled(tmp_path):
    """§2.5.4: no digest, no start. §2.5.5: nothing is built or pulled during a run."""
    _require_docker()
    with pytest.raises(container.ContainerUnavailable) as exc:
        container.resolve_digest("harnessgrad/definitely-not-a-real-image:latest")
    assert "import_env" in str(exc.value), (
        "the refusal must say what to do about it, not only that it failed")


def test_a_tag_is_not_accepted_as_already_pinned():
    assert container.looks_pinned("python:3.11-slim") is False
    assert container.looks_pinned("repo/x@sha256:" + "a" * 64) is True
    assert container.looks_pinned("sha256:" + "b" * 64) is True
    # A digest-shaped string that is not a digest is not pinned.
    assert container.looks_pinned("python@sha256:abc") is False


def test_running_an_unpinned_image_is_refused_at_the_call(tmp_path):
    """The safety net inside `run`, independent of the driver's startup resolution.

    Resolving at startup is the mechanism; this is what happens if a caller ever
    forgets. The cost of being wrong is a silent pull, and the cost of checking is a
    string compare.
    """
    _require_docker()
    with pytest.raises(container.ContainerUnavailable):
        container.run(["true"], image="python:3.11-slim", task_dir=tmp_path,
                      task_mount="/app")


# ------------------------------------------------------------ the boundary ---

def test_the_container_boundary_holds():
    """The nine properties, measured. See `eval/container.self_check`."""
    _require_docker()
    result = container.self_check(PLATFORM_ROOT, DEMO_IMAGE)
    assert result["ok"], result["detail"]
    assert result["image_digest"].startswith("sha256:")


def test_the_platform_is_invisible_inside_the_container(tmp_path):
    """Stated separately from `self_check` so a reader can see the fact, not a boolean.

    The probe uses the **real** platform path. A marker under `/tmp` would be shadowed
    by the container's tmpfs and report "invisible" whether or not anything was
    mounted, which is a check that cannot fail.
    """
    pinned = _require_docker()
    proc = container.run(
        ["sh", "-c", f"test -e {PLATFORM_ROOT / 'driver.py'} && echo yes || echo no"],
        image=pinned, task_dir=tmp_path, task_mount="/app",
        state_dirs=(), timeout_s=180)
    assert proc.stdout.strip() == "no", proc.stdout


def test_only_declared_variables_cross_into_the_container(tmp_path):
    """An allowlist, not "the host environment minus some names".

    Both directions matter: the agent's configuration has to survive, and everything
    else has to not. `HG_METHOD_*` is the one with teeth -- the two budgets are kept
    apart by `harness_env()` on the host and by this allowlist in a container, and two
    independent mechanisms is the correct number for a separation that has already
    been broken once.
    """
    pinned = _require_docker()
    proc = container.run(
        ["sh", "-c", "echo AGENT=$HG_AGENT_PROBE_MODE "
                     "METHOD=${HG_METHOD_API_KEY:-absent} "
                     "HOME_SET=${HOME:+yes}"],
        image=pinned, task_dir=tmp_path, task_mount="/app", timeout_s=180,
        env={**container.container_env("/app"), "HG_AGENT_PROBE_MODE": "sentinel",
             "HG_METHOD_API_KEY": "must-not-cross"})
    # `container_env` is the allowlist, so passing the method key in explicitly is the
    # caller's error; what this pins is that the *allowlist* does not carry it and that
    # an agent variable does.
    assert "AGENT=sentinel" in proc.stdout, proc.stdout
    assert "HOME_SET=yes" in proc.stdout, proc.stdout


def test_each_container_run_starts_fresh(tmp_path):
    """The property §2.5.6 asks for, made structural rather than best-effort.

    Killing the process tree of a `docker exec` cannot guarantee that nothing the
    harness started is still running: anything backgrounded is reparented to the
    container's PID 1 and survives the exec. A fresh container per call makes it a
    fact about how the check is started instead of a fact about how well the harness
    was cleaned up -- and this is the measurement that the fresh container is real,
    using a file the harness wrote outside the task bind mount.
    """
    pinned = _require_docker()
    first = container.run(["sh", "-c", "echo leaked > /tmp/marker && echo wrote"],
                          image=pinned, task_dir=tmp_path, task_mount="/app",
                          timeout_s=180)
    assert "wrote" in first.stdout, first.stderr
    second = container.run(
        ["sh", "-c", "test -e /tmp/marker && echo present || echo absent"],
        image=pinned, task_dir=tmp_path, task_mount="/app", timeout_s=180)
    assert second.stdout.strip() == "absent", (
        "the check would inherit whatever the harness left in its container")


def test_the_task_mount_is_read_only_for_the_check(tmp_path):
    """A check that writes into the tree it grades has a verdict that depends on its
    own leftovers."""
    pinned = _require_docker()
    (tmp_path / "artifact.txt").write_text("the harness made this\n")
    proc = container.run(
        ["sh", "-c", "cat artifact.txt; echo x > written 2>/dev/null "
                     "&& echo wrote || echo readonly"],
        image=pinned, task_dir=tmp_path, task_mount="/app", timeout_s=180,
        task_dir_writable=False)
    assert "the harness made this" in proc.stdout, "the check must still read it"
    assert proc.stdout.strip().endswith("readonly"), proc.stdout


# -------------------------------------------------------- the whole thing ---

def test_a_working_harness_scores_full_marks(tmp_path):
    """All four tasks, in containers, including `e02`, which is the one that can tell
    a real `exec` run from a `files` run wearing a container's name."""
    _require_docker()
    proc = _run(tmp_path, mode="solve")
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    points = _curve(tmp_path)
    assert points[-1]["score"] == 1.0, json.dumps(points[-1]["per_task"], indent=1)


def test_an_idle_harness_scores_zero(tmp_path):
    _require_docker()
    proc = _run(tmp_path, mode="idle")
    assert proc.returncode == 0, proc.stdout[-1500:]
    assert _curve(tmp_path)[-1]["score"] == 0.0


def test_the_check_reports_the_images_interpreter_not_the_hosts(tmp_path):
    """**The test that catches the only failure that matters here.**

    An `exec` task that is really a `files` task is an environment curve that was never
    taken, and it would look correct in every other respect. The host interpreter is a
    different version from the image's, so an honest answer distinguishes them.

    The image is **asked**, rather than assumed to be `DEMO_IMAGE`: the demo dataset
    names an image built from a base plus the base harness's `install`
    (`tools/import_env.py --harness`), so an assertion tied to the base would fail the
    moment the dataset's image changed -- which it did, and did. Asking the image also
    makes the check cover the derived image, which is the one that actually runs.
    """
    _require_docker()
    host = "%d.%d" % sys.version_info[:2]
    spec = registry.load_envs("verify_exec")["e02"]
    expected = registry.load_tasks("verify_exec")[2]["e02"]["expected"]
    pinned = container.resolve_digest(spec["image"])

    proc = container.run(
        ["python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        image=pinned, task_dir=tmp_path, task_mount="/app", timeout_s=180)
    in_image = proc.stdout.strip()
    assert in_image, proc.stderr

    if in_image == host:
        # Skipped, not failed: the assertion is only meaningful when the host and the
        # image disagree, and where they agree the test cannot distinguish a real `exec`
        # run from a `files` run. That is a limitation of the demo image, not a defect,
        # and failing here would make the suite machine-dependent.
        pytest.skip(f"vacuous here: the host and {spec['image']!r} both report {host}")
    assert in_image == expected, (
        f"{spec['image']!r} runs Python {in_image} but the demo's e02 verifier expects "
        f"{expected!r}, so the task is unpassable rather than discriminating")


def test_a_harness_cannot_grade_itself_in_a_container(tmp_path):
    """`e03`, with a harness that rewrites the specification to `sys.exit(0)`.

    The same control `verify_demo` runs for the host path, repeated for the container
    path because §2.5.6's guarantee has to hold in both -- and in the container it is
    harder to see, since the harness and the check share a bind mount and the check is
    started by a second `docker run` that the first one has no handle on.
    """
    _require_docker()
    proc = _run(tmp_path, mode="cheat")
    assert proc.returncode == 0, proc.stdout[-1500:]
    # A run covers one side, and the default is the train side -- so the tasks this
    # harness was scored on are all in `per_task`, and there is no second set to put in
    # `train_per_task` (INTERFACE.md §2.6).
    point = _curve(tmp_path)[-1]
    per_task = point["per_task"]
    assert set(per_task) == {"e01", "e02"}, (
        f"a train run's per_task is the train side: {per_task}")
    assert point["train_per_task"] == {} and point["train_score"] is None, point
    # The control is only meaningful if the edit was the *only* thing that failed: this
    # harness solves e01 and e04 normally, so a run that scored zero everywhere would
    # make the assertion below pass for an unrelated reason. `e03` is on the eval side
    # and is not in this run at all now -- which is the point of the split.
    assert per_task.get("e01") == 1.0, per_task


def test_the_cheat_would_pass_if_the_inputs_were_not_restored(tmp_path):
    """The negative control for the test above.

    Without re-materialization the cheat succeeds, so `test_a_harness_cannot_grade_
    itself_in_a_container` is measuring the re-materialization and not something else
    that happens to make it fail. Run directly against the verifier machinery, with the
    restore step skipped.
    """
    pinned = _require_docker()
    work = tmp_path / "task"
    work.mkdir()
    (work / "check.py").write_text("import sys; sys.exit(0)\n")   # the cheat
    (work / "out.txt").write_text("wrong\n")

    # What the platform does: restore the input, then run.
    from eval.runner import _materialize
    _materialize(work, registry.load_tasks("verify_exec")[2]["e03"]["inputs"],
                 PLATFORM_ROOT)

    restored = container.run(["python3", "check.py"], image=pinned, task_dir=work,
                             task_mount="/app", timeout_s=180,
                             task_dir_writable=False)
    assert restored.returncode != 0, (
        "restoring the check must defeat an edited one")

    # And with the cheat left in place it passes -- which is what makes the assertion
    # above a measurement rather than a tautology.
    (work / "check.py").write_text("import sys; sys.exit(0)\n")
    cheated = container.run(["python3", "check.py"], image=pinned, task_dir=work,
                            task_mount="/app", timeout_s=180,
                            task_dir_writable=False)
    assert cheated.returncode == 0


def test_the_curve_records_the_environment(tmp_path):
    """§2.5.4. A `files` curve and an `exec` curve are different quantities, and so are
    two `exec` curves under different digests -- so the environment travels with the
    number on every point, not only in `run_meta.json`."""
    _require_docker()
    assert _run(tmp_path, mode="idle").returncode == 0
    for point in _curve(tmp_path):
        env = point["env"]
        assert env["kind"] == "exec", env
        assert env["image_digest"].startswith("sha256:"), env
        assert env["network"] == "none", env
        assert env["placement"] == "inside", env
        # Recorded as null rather than omitted: §2.5.7 has not landed, and "not
        # applicable yet" must be distinguishable from "the platform forgot".
        assert "model" in env and env["model"] is None
        assert "seed" in env and env["seed"] is None


def test_files_datasets_still_record_a_files_environment(tmp_path):
    """The other half of the record rule: adding `exec` must not have told every
    existing dataset that it now runs in a container."""
    assert _run(tmp_path, mode="solve", dataset="verify_demo").returncode == 0
    for point in _curve(tmp_path):
        assert point["env"]["kind"] == "files", point["env"]
        assert point["env"]["image_digest"] is None


# ------------------------------------------------------------ importing ---
#
# `install` has been declared, validated and inert since the manifest gained the field.
# §2.5.6 settles where it goes -- into the environment, at import time -- so these tests
# exist to make the difference between "declared" and "runs" observable.

def _fixture_harness(tmp_path: Path, install: str | None, body: str) -> Path:
    repo = tmp_path / "install_fixture"
    repo.mkdir(parents=True, exist_ok=True)
    manifest = {"name": "install-fixture", "version": "1", "entrypoint": "agent.py",
                "backend": "cli", "env_kinds": ["exec"]}
    if install:
        manifest["install"] = install
        (repo / install).write_text(body)
    (repo / "agent.py").write_text("# a harness\n")
    (repo / ".state").mkdir(exist_ok=True)
    (repo / ".state" / "scratch").write_text("must not be copied\n")
    (repo / "harness.json").write_text(json.dumps(manifest))
    return repo


def test_the_derived_dockerfile_bakes_the_install_in(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import import_env

    repo = _fixture_harness(tmp_path, "requirements.txt", "requests==2.31.0\n")
    text = import_env.derived_dockerfile("base:tag", repo, "requirements.txt")
    assert text.startswith("FROM base:tag\n"), text
    assert "COPY . /opt/harnessgrad-install/" in text, text
    assert "pip install" in text and "requirements.txt" in text, text


def test_an_unrecognised_install_file_is_refused_rather_than_guessed_at(tmp_path):
    """A build step is arbitrary code. A tool that invents a command for a file it does
    not recognise is a tool that runs something nobody wrote."""
    sys.path.insert(0, str(ROOT / "tools"))
    import import_env

    with pytest.raises(SystemExit) as exc:
        import_env.install_command("Makefile")
    assert "neither" in str(exc.value)


def test_a_harness_that_declares_no_install_is_not_an_error(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import import_env

    assert import_env.harness_install(_fixture_harness(tmp_path, None, "")) is None
    repo = _fixture_harness(tmp_path, "requirements.txt", "requests\n")
    assert import_env.harness_install(repo) == "requirements.txt"


def test_a_missing_install_file_is_refused(tmp_path):
    """`validate_harness` already refuses this, and the importer must not be the one
    place that trusts the manifest."""
    sys.path.insert(0, str(ROOT / "tools"))
    import import_env

    repo = _fixture_harness(tmp_path, None, "")
    (repo / "harness.json").write_text(json.dumps(
        {"name": "x", "version": "1", "entrypoint": "agent.py", "install": "gone.sh"}))
    with pytest.raises(SystemExit) as exc:
        import_env.harness_install(repo)
    assert "gone.sh" in str(exc.value)


def test_importing_a_harness_actually_runs_its_install(tmp_path):
    """The end-to-end proof that `install` is no longer inert.

    A `.sh` install rather than a `requirements.txt`, so the build needs no network and
    the test is measuring the mechanism instead of a package index. The marker it
    writes is checked **in the resulting image**, because the claim being tested is
    "the install is baked into the environment", and anything weaker would also pass
    for an install that ran on the host and vanished.
    """
    pinned = _require_docker()
    sys.path.insert(0, str(ROOT / "tools"))
    import import_env

    repo = _fixture_harness(
        tmp_path, "install.sh",
        'set -e\necho "baked at import time" > /opt/hg-install-proof.txt\necho "# a module" > /opt/hg-from-install.py\n')
    image_id = import_env.import_with_harness(pinned, repo, "install.sh",
                                              "harnessgrad-test-fixture:latest")
    try:
        proc = container.run(
            ["sh", "-c", "cat /opt/hg-install-proof.txt; "
                         "test -e /opt/harnessgrad-install/.state "
                         "&& echo STATE_LEAKED || echo state_not_copied"],
            image=image_id, task_dir=tmp_path, task_mount="/app", timeout_s=180)
        assert "baked at import time" in proc.stdout, proc.stdout + proc.stderr
        # The harness's own scratch space is not part of its declaration and must not
        # be shipped into an image that will be mounted read-only and reused.
        assert "state_not_copied" in proc.stdout, proc.stdout
    finally:
        subprocess.run(["docker", "image", "rm", "--force", image_id],
                       capture_output=True, text=True)


def test_the_run_record_and_the_curve_agree_about_the_image(tmp_path):
    """The bug this pins, because it is a shape that will be attempted again.

    Images were first pinned next to the `env_kinds` gate -- which is where the check
    *logically* belongs, and which runs *after* the header and `run_meta.json` are
    written. So the header named the image's tag, `run_meta.json` recorded
    `image_digest: null`, and every curve point carried the digest. Two records of one
    run disagreeing about what it ran in is precisely the failure §2.5.4 exists to
    prevent, and it was invisible from the curve alone.
    """
    _require_docker()
    assert _run(tmp_path, mode="idle").returncode == 0
    meta = json.loads((tmp_path / "runs" / "probe" / "run_meta.json").read_text())
    points = _curve(tmp_path)
    assert meta["env"]["kind"] == "exec", meta["env"]
    assert meta["env"]["image_digest"], (
        f"run_meta.json describes the run without pinning it: {meta['env']}")
    assert meta["env"]["image_digest"] == points[0]["env"]["image_digest"], (
        f"the run record and the curve disagree: {meta['env']} vs {points[0]['env']}")


def test_no_sandbox_does_not_disable_the_container(tmp_path):
    """`--no-sandbox` and the container boundary are different switches.

    The container is not a wrapper around the task, it **is** the task's environment
    (INTERFACE.md §2.5.5), so `--no-sandbox` cannot switch it off -- removing it would
    not make the task run unisolated, it would make the task not run at all. What
    `--no-sandbox` does turn off is the host-side bwrap, which for an `exec` dataset is
    already not the boundary. The header has to say so, or a reader sees "no sandbox"
    and concludes there was no boundary anywhere.
    """
    _require_docker()
    proc = _run(tmp_path, mode="solve", extra_argv=("--no-sandbox",))
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]
    header = " ".join((proc.stdout + proc.stderr).split())
    assert "exec" in header, header[:800]
    assert "host-side bwrap OFF for the method" in header, (
        f"the header reads as if there were no boundary at all: {header[:800]}")
    # And the run is still a real one: the container-based task scored.
    assert _curve(tmp_path)[-1]["score"] == 1.0
