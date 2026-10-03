"""Terminal-Bench-shaped tasks: state in the container, verified from a snapshot.

INTERFACE.md §2.5.9. This is the third task shape the platform supports, after a host
directory (`mount`) and a service-backed directory. It exists because Terminal-Bench
puts the task in the container's own filesystem -- measured: the image ships `/app`
empty, the instruction says to write `/app/ars.R`, and the check reads `/app`.

The tests come in the order the design is risky:

1. the contract refuses what it does not support (no docker needed);
2. the snapshot carries the harness's work and the check runs somewhere the harness
   never was;
3. **the oracle control passes.** That one is first among the docker tests in spirit:
   without it, a reward of 0 from a real harness is ambiguous -- is the harness weak, or
   is the check unreachable? Three real bugs were found with it, one of which would have
   made *every* Terminal-Bench task score zero for a reason nothing recorded.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import eval.container as container
from data import registry

ROOT = Path(__file__).resolve().parents[2]
TB_ROOT = Path(os.environ.get("HG_TERMINAL_BENCH",
                              "/mnt/20t/xubuqiang/Study/terminal-bench-2"))


def _require_tb() -> None:
    if container.available() is None:
        pytest.skip("no docker daemon")
    if not TB_ROOT.is_dir():
        pytest.skip(f"no Terminal-Bench checkout at {TB_ROOT}")


# ------------------------------------------------------------ validation ---

def test_a_container_state_task_that_also_asks_for_services_is_refused():
    """A recording travels by bind mount and a container snapshot does not capture one,
    so the check would read an empty recording and blame the harness for it."""
    with pytest.raises(ValueError) as exc:
        registry._check_env(
            {"kind": "exec", "image": "x", "state": "container",
             "network": "services",
             "services": [{"name": "api", "image": "y", "port": 1,
                           "health": {"argv": ["true"], "timeout_s": 1}}]},
            "t", "ds")
    assert "recording" in str(exc.value)


def test_an_unknown_state_is_refused():
    with pytest.raises(ValueError) as exc:
        registry._check_env({"kind": "exec", "image": "x", "state": "somewhere"},
                            "t", "ds")
    assert "state" in str(exc.value)


def test_resources_and_timeouts_must_be_positive_numbers():
    for bad in ({"cpus": 0}, {"memory_mb": 0}, {"agent_timeout_s": -1},
                {"verify_timeout_s": 0}, {"cpus": True}):
        with pytest.raises(ValueError):
            registry._check_env({"kind": "exec", "image": "x", **bad}, "t", "ds")


def test_the_resource_defaults_are_filled_in():
    env = registry._normalize_env({"kind": "exec", "image": "x"})
    assert env["state"] == "mount"
    assert (env["cpus"], env["memory_mb"]) == (2, 4096)
    assert (env["agent_timeout_s"], env["verify_timeout_s"]) == (300, 300)


def test_reward_file_and_pass_when_together_are_refused():
    """Two answers to one question."""
    with pytest.raises(ValueError) as exc:
        registry._check_verifier(
            {"kind": "command", "argv": ["true"], "reward_file": "/logs/r.txt",
             "pass_when": "exit0_stdout", "stdout": "x"}, "t", "ds")
    assert "reward_file" in str(exc.value)


def test_a_reward_file_must_be_absolute():
    with pytest.raises(ValueError):
        registry._check_verifier(
            {"kind": "command", "argv": ["true"], "reward_file": "reward.txt"}, "t", "ds")


def test_an_input_dst_must_be_absolute():
    with pytest.raises(ValueError) as exc:
        registry._check_verifier(
            {"kind": "command", "argv": ["true"],
             "inputs": [{"dst": "tests", "from": "/x"}]}, "t", "ds")
    assert "absolute" in str(exc.value)


def test_an_input_with_neither_path_nor_dst_is_refused():
    with pytest.raises(ValueError):
        registry._check_verifier(
            {"kind": "command", "argv": ["true"], "inputs": [{"from": "/x"}]}, "t", "ds")


# --------------------------------------------------- the real task set ---

def test_the_adapter_reads_the_real_task_set():
    """The mapping, checked against the checkout rather than against a fixture."""
    _require_tb()
    import importlib
    tb = importlib.import_module("data.terminal_bench")
    tasks, _ = tb.load()
    if not tasks:
        pytest.skip("no Terminal-Bench tasks selected")
    assert len(tasks) == len(tb.SETUP) == len(tb.VERIFY) == len(tb.ENV)
    for t in tasks:
        tid = t["task_id"]
        assert t["goal"].strip(), f"{tid} has an empty goal"
        env = tb.ENV[tid]
        assert env["state"] == "container", tid
        assert env["image"], tid
        # Every real task ships an image tag; the driver pins it and refuses to start
        # without a digest, so this is the only place the tag is allowed to appear.
        assert not env["image"].startswith("/"), tid
        assert tb.VERIFY[tid]["reward_file"] == tb.REWARD_FILE
        assert tb.VERIFY[tid]["inputs"][0]["dst"] == "/tests"


def test_no_task_is_excluded_by_a_stale_metadata_flag():
    """**This test exists because the opposite assertion was there first.**

    Two tasks carry `metadata.custom_docker_compose = True` and were excluded on the
    strength of it. They are single-container: the whole checkout contains **zero**
    docker-compose files, both ship a self-contained `Dockerfile`, and both carry the
    line `# Fields moved from docker-compose.yaml`. The flag was stale and the exclusion
    quietly removed two tasks from a benchmark.

    So the assertion is now that the flag does *not* drive exclusion, and that every task
    on disk is loaded unless it is named in `EXCLUDED` with a reason.
    """
    _require_tb()
    import importlib
    import tomllib
    tb = importlib.import_module("data.terminal_bench")

    on_disk = {d.name for d in TB_ROOT.iterdir()
               if d.is_dir() and (d / "task.toml").is_file()}
    flagged = {d.name for d in TB_ROOT.iterdir()
               if (d / "task.toml").is_file()
               and (tomllib.loads((d / "task.toml").read_text()).get("metadata") or {})
                   .get("custom_docker_compose")}
    assert flagged, ("no task carries the flag any more; if Terminal-Bench cleaned its "
                     "metadata up, this test has stopped testing anything")
    assert not (flagged & set(tb.EXCLUDED)), (
        f"a stale metadata flag is excluding tasks again: {sorted(flagged & set(tb.EXCLUDED))}")
    assert set(tb.EXCLUDED) <= on_disk, "EXCLUDED names tasks that are not there"

    loaded = {t["task_id"] for t in tb.load()[0]}
    if not tb.ONLY:
        assert loaded == on_disk - set(tb.EXCLUDED), (
            f"{len(on_disk)} on disk, {len(loaded)} loaded, "
            f"{sorted(on_disk - loaded - set(tb.EXCLUDED))} missing without a reason")
        assert len(loaded) == 89, f"{len(loaded)} tasks loaded, expected all 89"


def test_the_importer_attempts_a_mirror_and_reports_what_it_tried(tmp_path):
    """`--mirror` is the path a host without registry access depends on, so it gets a
    test that reaches the code rather than one that only imports the module.

    This is not hypothetical: the first version of the mirror support read
    `os.environ` in a module that never imported `os`, and **every one of 67 images
    failed instantly** with `NameError` while the log said only `FAIL`. Importing the
    module was the test that existed, and it passed.
    """
    if container.available() is None:
        pytest.skip("no docker daemon")
    sys.path.insert(0, str(ROOT / "tools"))
    import import_env

    # A port nothing listens on, so the attempt fails fast and for the right reason.
    with pytest.raises(SystemExit) as exc:
        import_env.ensure_local("example.invalid/img:1", allow_pull=True,
                                mirrors=["127.0.0.1:1"])
    said = str(exc.value)
    assert "127.0.0.1:1" in said, f"the mirror that was tried is not named: {said}"


def test_both_checkout_layouts_are_found(tmp_path):
    """Terminal-Bench 2 puts tasks at the repository root; **2.1** puts them under
    `tasks/`. Both are found, so `HG_TERMINAL_BENCH` can point at either.

    Built as a synthetic checkout rather than by requiring both clones to be present:
    the property is "the adapter looks in both places", and that is testable with two
    directories and a `task.toml`.
    """
    import importlib
    import tomllib
    tb = importlib.import_module("data.terminal_bench")

    for layout, sub in (("root", ""), ("tasks/", "tasks")):
        root = tmp_path / layout.replace("/", "_")
        home = root / sub if sub else root
        home.mkdir(parents=True)
        task = home / "a-task"
        task.mkdir()
        (task / "task.toml").write_text(
            'schema_version = "1.1"\n'
            '[environment]\ndocker_image = "example/img:1"\n'
            'cpus = 1\nmemory_mb = 1024\nallow_internet = true\n'
            '[agent]\ntimeout_sec = 60.0\n[verifier]\ntimeout_sec = 60.0\n')
        (task / "instruction.md").write_text("do the thing\n")
        (task / "tests").mkdir()
        (task / "tests" / "test.sh").write_text("#!/bin/bash\n")

        old = tb.ROOT
        try:
            tb.ROOT = root
            dirs = tb._task_dirs()
            assert [d.name for d in dirs] == ["a-task"], (layout, dirs)
            task_dict, _, verifier, env = tb._load_one(dirs[0])
            assert task_dict["goal"] == "do the thing"
            assert env["state"] == "container"
            assert env["network"] == "bridge"
            assert verifier["reward_file"] == tb.REWARD_FILE
        finally:
            tb.ROOT = old


def test_a_checkout_with_no_tasks_says_where_it_looked(tmp_path):
    """A wrong path is the most likely way to use this adapter, so the error has to name
    both places rather than only the one that was assumed."""
    import importlib
    tb = importlib.import_module("data.terminal_bench")
    old = tb.ROOT
    try:
        tb.ROOT = tmp_path / "empty"
        (tmp_path / "empty").mkdir()
        with pytest.raises(ValueError) as exc:
            tb._task_dirs()
        assert "tasks" in str(exc.value) and str(tb.ROOT) in str(exc.value)
    finally:
        tb.ROOT = old


def test_the_split_is_derived_and_stable():
    """Terminal-Bench declares no split; the platform imposes one so that a method cannot
    study its exam (§2.3). Derived from the task id so it does not reshuffle when the
    task set changes."""
    _require_tb()
    import importlib
    tb = importlib.import_module("data.terminal_bench")
    if tb.SPLIT is None:
        pytest.skip("HG_TB_SPLIT=none")
    assert tb._is_eval("abc") == tb._is_eval("abc")
    ids = [t["task_id"] for t in tb.load()[0]]
    assert sorted(tb.SPLIT["train"] + tb.SPLIT["eval"]) == sorted(ids)
    assert tb.SPLIT["train"] and tb.SPLIT["eval"], "one side is empty"


# ------------------------------------------------------ the mechanism ---

#: Tasks whose *reference solution* completes here, so their check is **verified
#: reachable** — **at the time it was measured, on this host, with whatever network it
#: had then.** That qualifier is not hedging: `cancel-async-tasks` passed, then failed
#: minutes later, and the reason was `failed to download https://github.com/astral-sh/
#: uv/...` — the check fetches `uv` from GitHub releases and this host's containers
#: cannot always reach GitHub. A "verified" list is a measurement with a timestamp, and
#: `_environmental()` below is how the test tells that apart from a check that is
#: genuinely unreachable.
#:
#: **Measured, not inferred** — and that distinction is the whole point:
#:
#: The first version of this list was guessed from `solve.sh` size and whether it
#: installed a heavy toolchain, and the oracle then showed only **two of the five**
#: actually work. The three that do not, and why:
#:
#:   build-pmars                `apt-get install dpkg-dev=1.22.21` -- that version is gone
#:                              from the archive, so the solution fails and the check
#:                              correctly reports it. Terminal-Bench's own solutions pin
#:                              apt versions that drift.
#:   configure-git-webserver    expects `/etc/ssh/sshd_config`, which the image does not
#:                              have; the solution completes only partly.
#:   count-dataset-tokens       downloads from `huggingface.co`, which this host cannot
#:                              reach.
#:
#: None of the three is a platform defect: in each case the check ran, read its reward
#: from the file, and reported the truth. What they mean is narrower and more useful —
#: **on this host, two of five Terminal-Bench tasks have a check we can demonstrate is
#: reachable, and the rest cannot be verified here.** A score of 0 from a real harness on
#: one of the other three is uninterpretable until that is fixed.
ORACLE_VERIFIED = ("cancel-async-tasks", "dna-assembly")


#: Names/ids of failures that mean *the host could not fetch the check's own tooling*,
#: not that the check is unreachable. Kept as an explicit allowlist rather than "any
#: error containing 'network'", so that a failure which does not match still fails the
#: test -- the control has to be able to fail for the reason it exists.
_ENVIRONMENTAL = (
    "failed to download https://github.com",
    # The check's own test runner, fetched at verify time by `test.sh`:
    # `curl -LsSf https://astral.sh/uv/install.sh | sh`, then `uvx`. When that fetch
    # fails, `uvx` is simply absent and pytest never runs -- measured on
    # cancel-async-tasks. It only became visible when the platform started keeping the
    # check's **stderr**: the `command not found` line is on stderr, while stdout held
    # apt's progress, so this control used to fail with no explanation at all.
    "uvx: command not found",
    "Temporary failure resolving",
    "Could not resolve host",
    "Network is unreachable",
    "connection timed out",
)


def _environmental(verdict: dict) -> str | None:
    """The host-side fetch failure behind a 0, if that is what it was."""
    detail = (verdict or {}).get("detail") or ""
    for marker in _ENVIRONMENTAL:
        if marker in detail:
            return marker
    return None


def _one_ready_task():
    """A task whose image is local **and** whose solution is measured to complete here.

    `import_env.py` is the only thing allowed to pull, so a test that pulled would be
    testing something the platform forbids during a run.
    """
    _require_tb()
    import importlib
    tb = importlib.import_module("data.terminal_bench")
    by_id = {t["task_id"]: t for t in tb.load()[0]}
    for tid in ORACLE_VERIFIED:
        if tid not in by_id:
            continue
        env = tb.ENV[tid]
        try:
            container.resolve_digest(env["image"])
        except container.ContainerUnavailable:
            continue
        return by_id[tid], tb.SETUP[tid], tb.VERIFY[tid], env
    pytest.skip("none of the verified task images is local; run "
                "tools/import_env.py for one of " + ", ".join(ORACLE_VERIFIED))


def test_the_oracle_control_passes(tmp_path):
    """**The control that makes every other number on this dataset mean something.**

    `base_harness/oracle` runs the dataset's own reference solution and does nothing
    else. If it does not score, the dataset's check is not reachable and no harness's
    score on it means anything -- which is exactly the ambiguity this removes. It found
    three real bugs here: a `docker cp` path that landed the task one directory too deep,
    a `mkdir` issued to a container that was not running yet, and a verifier with no
    network while all 89 real checks install their own test dependencies at verify time.
    """
    from eval.runner import run_one

    task, setup, verifier, env = _one_ready_task()
    tid = task["task_id"]
    import importlib
    tb = importlib.import_module("data.terminal_bench")
    if not (TB_ROOT / tid / "solution").is_dir():
        pytest.skip(f"{tid} ships no solution for the oracle")

    # The oracle is handed the solution through the ordinary SETUP path, which is what
    # the adapter does under HG_TB_ORACLE=1.
    old = os.environ.copy()
    os.environ.update({"HG_TB_ORACLE": "1", "HG_TB_TASKS": tid,
                       "HG_AGENT_BACKEND": "mock",
                       "HARNESSGRAD_WORK_ROOT": str(tmp_path / "work")})
    try:
        resolved, problem = __import__("driver")._resolve_envs({tid: env})
        assert problem is None, problem
        setup = {"files": [{"path": ".oracle",
                            "from": str(TB_ROOT / tid / "solution")}]}
        result = run_one((ROOT / "base_harness" / "oracle"), task, sandbox=True,
                         setup=setup, verifier=verifier, env=resolved[tid],
                         run_id="oracle-test", run_seed="s",
                         recordings_root=tmp_path / "recordings")
    finally:
        os.environ.clear()
        os.environ.update(old)

    verdict = result.get("verdict") or {}
    # A check that could not fetch its *own* tooling says nothing about whether the
    # platform can reach it, so it is skipped loudly rather than counted as a failure.
    # Anything else **fails**: that is the whole job of this control.
    why = _environmental(verdict)
    if why is not None:
        pytest.skip(
            f"the host could not fetch the check's own tooling ({why!r}), so this run "
            f"measured the network and not the platform. The oracle is verified "
            f"conditionally, not permanently.")
    assert verdict.get("score") == 1.0, (
        f"the oracle did not pass {tid}, so its check is not reachable and no harness's "
        f"score on this dataset means anything yet: "
        f"{json.dumps(verdict, ensure_ascii=False)[:800]}")


def test_a_container_state_run_leaves_nothing_behind(tmp_path):
    """A snapshot image per task is a disk leak waiting to happen, and it is the platform
    that made it."""
    from eval.runner import run_one

    task, setup, verifier, env = _one_ready_task()
    import importlib
    tb = importlib.import_module("data.terminal_bench")
    if not (TB_ROOT / task["task_id"] / "solution").is_dir():
        pytest.skip("no solution to run")

    before = set(container.docker(
        "images", "--format", "{{.Repository}}:{{.Tag}}").stdout.split())
    old = os.environ.copy()
    os.environ.update({"HG_AGENT_BACKEND": "mock",
                       "HARNESSGRAD_WORK_ROOT": str(tmp_path / "work")})
    try:
        resolved, _ = __import__("driver")._resolve_envs({task["task_id"]: env})
        run_one((ROOT / "base_harness" / "oracle"), task, sandbox=True,
                setup={"files": [{"path": ".oracle",
                                  "from": str(TB_ROOT / task["task_id"] / "solution")}]},
                verifier=verifier, env=resolved[task["task_id"]],
                run_id="leak-test", run_seed="s",
                recordings_root=tmp_path / "recordings")
    finally:
        os.environ.clear()
        os.environ.update(old)

    after = set(container.docker(
        "images", "--format", "{{.Repository}}:{{.Tag}}").stdout.split())
    leaked = sorted(a for a in after - before if a.startswith("hg-snapshot"))
    assert leaked == [], f"a snapshot survived: {leaked}"
    left = container.docker("ps", "-a", "--filter", "name=hg-leak-test",
                            "--format", "{{.Names}}").stdout.split()
    assert left == [], f"a container survived: {left}"


def test_the_image_interpreter_wins_when_it_has_one():
    """§2.5.3's property, preserved where it can be: inside the environment the
    interpreter is the image's. The platform injects its own only when the image has
    none -- measured: a real Terminal-Bench image is `ubuntu:24.04` with no `python3`."""
    if container.available() is None:
        pytest.skip("no docker daemon")
    for image, expected in (("docker.m.daocloud.io/library/python:3.11-slim", True),
                            ("alexgshaw/cancel-async-tasks:20251031", None)):
        try:
            container.resolve_digest(image)
        except container.ContainerUnavailable:
            continue
        found = container.image_python(image)
        if expected is True:
            assert found, f"{image} has a python but the probe missed it"
        elif expected is None:
            # Whatever this image has, the contract is that the probe answers the same
            # way twice -- the cache is not allowed to change the answer.
            assert container.image_python(image) == found


def test_a_directory_input_lands_at_the_destination_even_when_it_already_exists(tmp_path):
    """**`docker cp <dir> <ctr>:<dst>` 在 `dst` 已存在时会嵌套,而 `dst` 存不存在不是平台说了算。**

    被测的容器是从 snapshot 建的,而 snapshot 是 **harness 自己的文件系统**。实测
    (`break-filter-js-from-html`):那只 harness 在探测题目里的过滤器时跑了
    `mkdir -p /tests && cp /app/filter.py /tests/filter.py` —— 完全正当 —— 于是检查的输入落到了
    `/tests/tests/test.sh`,检查死在 `bash: /tests/test.sh: No such file or directory`,
    平台把这一笔记成了 **harness 的 0**。同一轮另一只没碰 `/tests` 的 harness 就正常验证,
    所以它看起来像"这道题时好时坏"。

    钉住的是**平台那一行选择**(`contents=`),不是 `docker cp` 本身:我在真容器上量过四种组合,
    `src/.` 两种情况下都对。平台这一侧一旦有人把 `contents=` 去掉,这条就红。
    """
    import eval.container as container_mod
    from eval import runner as runner_mod

    calls: list[tuple] = []

    def fake_cp_in(name, source, dest, **kw):
        calls.append((name, str(source), str(dest), kw))

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test.sh").write_text("#!/bin/bash\n")
    single = tmp_path / "one.txt"
    single.write_text("x\n")
    spec = {"inputs": [{"dst": "/tests", "from": str(directory)},
                       {"dst": "/app/one.txt", "from": str(single)}]}

    real = container_mod.cp_in
    container_mod.cp_in = fake_cp_in
    try:
        runner_mod._copy_verifier_inputs("ctr", spec, tmp_path, "/app")
    finally:
        container_mod.cp_in = real

    assert calls[0] == ("ctr", str(directory), "/tests", {"contents": True}), calls
    assert calls[1] == ("ctr", str(single), "/app/one.txt", {"contents": False}), calls
