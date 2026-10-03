"""The `services` contract: containers that run while the harness runs. §2.5.7.

Two kinds of test here, and the split is deliberate:

* **Validation** tests need no docker and assert that a contradictory or incomplete
  dataset is refused at load time. They are the cheap half and they cover most of the
  ways a dataset can be wrong.
* **Lifecycle** tests need docker and assert the things only a real container can
  demonstrate: that the harness reaches a service by the name it was told, that a
  service's recording is invisible to the harness and visible to the check, that a
  service that is not healthy makes a task **invalid** rather than zero, and that
  nothing is left behind.

Every property that matters has a test that it **can fail**. Two were written the other
way round first and both passed against a broken platform: `s01` graded "did the harness
call" by counting requests, which the platform's own health check satisfied; and the
service container was reachable by nothing, because its docker *name* was mangled and
the alias the harness was told had never been attached. Neither was visible from a
passing score.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import eval.container as container
import eval.services as services
from data import registry
from eval.runner import evaluate, run_one
from harnessgrad.environments import _env_record, _resolve_envs
from harnessgrad.records import _cost_of

ROOT = Path(__file__).resolve().parents[2]
DEMO_IMAGE = os.environ.get("HG_EXEC_DEMO_IMAGE",
                            "docker.m.daocloud.io/library/python:3.11-slim")
SERVICE_IMAGE = os.environ.get("HG_SERVICE_DEMO_IMAGE",
                               "harnessgrad-fake-service:demo")

#: A harness that does exactly what each task asks and reports what it saw. Self
#: contained, because inside the image there is no platform to borrow from.
HARNESS = '''\
import argparse, json, os, pathlib, sys, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--task"); ap.add_argument("--workdir")
a = ap.parse_args()
work = pathlib.Path(a.workdir)
tid = json.loads(pathlib.Path(a.task).read_text())["task_id"]
base = os.environ.get("HG_SVC_ORDERS_URL")
note = os.environ.get("HG_AGENT_PROBE", "solve")

if note != "idle" and base:
    def post(p, payload):
        req = urllib.request.Request(base + p, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    def get(p):
        return json.loads(urllib.request.urlopen(base + p, timeout=10).read())
    if tid == "s01":
        get("/orders")
    elif tid == "s02":
        post("/order", {"item": "widget", "qty": 3})
    elif tid == "s03":
        post("/order", {"item": "gadget", "qty": 2})
        (work / "order_count.txt").write_text(str(get("/orders")["order_count"]))
elif tid == "s04":
    (work / "ready.txt").write_text("yes\\n")

(work / "trace.jsonl").write_text(json.dumps({
    "usage": {"input_tokens": 7, "output_tokens": 3}}) + "\\n")
'''


def _require_docker() -> None:
    if container.available() is None:
        pytest.skip("no docker daemon: services cannot be exercised here")
    for ref in (DEMO_IMAGE, SERVICE_IMAGE):
        try:
            container.resolve_digest(ref)
        except container.ContainerUnavailable:
            pytest.skip(f"{ref!r} is not on the local daemon; import it first "
                        f"(tools/import_env.py)")


#: Resolved once per session, because `container.run` refuses an unpinned image and
#: resolving is a `docker inspect` per call.
_PINNED: dict[str, str] = {}


def _pin(ref: str) -> str:
    if ref not in _PINNED:
        _PINNED[ref] = container.resolve_digest(ref)
    return _PINNED[ref]


def _harness(tmp_path: Path) -> Path:
    repo = tmp_path / "svc_harness"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "agent.py").write_text(HARNESS)
    (repo / "harness.json").write_text(json.dumps({
        "name": "svc-probe", "version": "1", "entrypoint": "agent.py",
        "backend": "cli", "env_kinds": ["files", "exec"]}))
    return repo


def _service(**over) -> dict:
    svc = {"name": "orders", "image": _pin(SERVICE_IMAGE), "port": 8080,
           "health": {"argv": ["python3", "-c",
                               "import urllib.request as u;"
                               "u.urlopen('http://localhost:8080/health', timeout=2).read()"],
                      "timeout_s": 60},
           "record": True, "needs_seed": False, "needs_model": False}
    svc.update(over)
    return svc


def _env(services_list=None, **over) -> dict:
    spec = {"kind": "exec", "image": _pin(DEMO_IMAGE), "workdir": "/app",
            "network": "services", "placement": "inside",
            "services": services_list if services_list is not None else [_service()]}
    spec.update(over)
    return spec


def _evaluate(tmp_path: Path, env_spec: dict, *, setup=None, verifier=None,
              tasks=("s01",)) -> dict:
    env = {**os.environ, "HARNESSGRAD_WORK_ROOT": str(tmp_path / "work")}
    old = os.environ.copy()
    os.environ.update(env)
    try:
        return evaluate(
            _harness(tmp_path),
            [{"task_id": t, "goal": "do the thing"} for t in tasks],
            {t: "" for t in tasks}, {}, harness_sha="H",
            setups={t: setup for t in tasks} if setup else None,
            verifiers={t: verifier for t in tasks} if verifier else None,
            envs={t: env_spec for t in tasks},
            run_id="t", run_seed="seed", recordings_root=tmp_path / "recordings")
    finally:
        os.environ.clear()
        os.environ.update(old)


# ------------------------------------------------------------ validation ---

def test_services_without_a_service_network_are_refused():
    """The contradiction, both ways round. Silently upgrading one of them would leave
    the dataset describing a run that did not happen (§2.5.7)."""
    with pytest.raises(ValueError) as exc:
        registry._check_env({"kind": "exec", "image": "x", "network": "none",
                             "services": [_service()]}, "t", "ds")
    assert "network='services'" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        registry._check_env({"kind": "exec", "image": "x", "network": "services",
                             "services": []}, "t", "ds")
    assert "nothing to be reachable" in str(exc.value)


def test_a_service_name_must_be_a_dns_label():
    """It becomes a hostname the harness resolves and a path under /recordings, so it is
    refused rather than sanitized: a silently rewritten name is not the name the harness
    was told."""
    for bad in ("Orders", "has space", "-lead", "trail-", "a" * 64, ""):
        with pytest.raises(ValueError) as exc:
            registry._check_env({"kind": "exec", "image": "x", "network": "services",
                                 "services": [_service(name=bad)]}, "t", "ds")
        assert "DNS label" in str(exc.value), bad
    registry._check_env({"kind": "exec", "image": "x", "network": "services",
                         "services": [_service(name="mock-api-2")]}, "t", "ds")


def test_a_service_needs_a_health_check():
    """Required, not defaulted to "is the port open": a port that accepts a connection
    is not a service that is ready, and the score would be a race."""
    with pytest.raises(ValueError) as exc:
        spec = _service()
        del spec["health"]
        registry._check_env({"kind": "exec", "image": "x", "network": "services",
                             "services": [spec]}, "t", "ds")
    assert "health" in str(exc.value)
    assert "race" in str(exc.value)


def test_duplicate_service_names_are_refused():
    with pytest.raises(ValueError) as exc:
        registry._check_env({"kind": "exec", "image": "x", "network": "services",
                             "services": [_service(), _service()]}, "t", "ds")
    assert "two services named" in str(exc.value)


def test_a_service_needs_an_image_and_a_port():
    for bad in ({"image": ""}, {"port": 0}, {"port": 70000}, {"port": True},
                {"port": "8080"}):
        with pytest.raises(ValueError):
            registry._check_env({"kind": "exec", "image": "x", "network": "services",
                                 "services": [_service(**bad)]}, "t", "ds")


def test_an_unknown_service_field_is_refused():
    """A field that does nothing is worse than a missing one: the dataset reads as
    though it had asked for something."""
    with pytest.raises(ValueError) as exc:
        registry._check_env({"kind": "exec", "image": "x", "network": "services",
                             "services": [_service(replicas=3)]}, "t", "ds")
    assert "unknown field" in str(exc.value)


def test_services_are_refused_on_a_files_environment():
    """There is no boundary to put a service behind and no network namespace to keep it
    off the internet, so it is refused rather than half-supported."""
    with pytest.raises(ValueError) as exc:
        registry._check_env({"kind": "files", "services": [_service()]}, "t", "ds")
    assert "only an 'exec' environment" in str(exc.value)


def test_the_normalized_env_carries_the_services_through():
    normalized = registry._normalize_env(
        {"kind": "exec", "image": "x", "network": "services",
         "services": [_service(needs_seed=True)]})
    assert normalized["services"][0]["name"] == "orders"
    assert normalized["services"][0]["needs_seed"] is True
    assert normalized["services"][0]["health"]["timeout_s"] == 60


# ----------------------------------------------- the budgets stay separate ---

def test_the_environment_credential_never_reaches_the_harness():
    """A third budget needs a third exclusion, and the leak was on the path with no
    services at all: `harness_env()` forwards everything that is not the *method's*."""
    from eval.runner import harness_env

    old = os.environ.copy()
    os.environ.update({"HG_ENV_API_KEY": "env-secret", "HG_ENV_MODEL": "env-model",
                       "HG_METHOD_API_KEY": "method-secret",
                       "HG_AGENT_API_KEY": "agent-key"})
    try:
        env = harness_env()
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert "HG_ENV_API_KEY" not in env, "the environment's credential reached the harness"
    assert "HG_ENV_MODEL" not in env
    assert "HG_METHOD_API_KEY" not in env
    assert env["HG_AGENT_API_KEY"] == "agent-key"


def test_services_receive_the_environment_budget_and_the_harness_does_not():
    """Two prefixes, two audiences (§2.5.7). One prefix with two audiences would make
    "may the harness see this" a question answered by knowing which keys exist."""
    old = os.environ.copy()
    os.environ.update({"HG_ENV_MODEL": "sim-model", "HG_ENV_API_KEY": "sim-key",
                       "HG_ENV_SEED": "should-not-be-read-from-here"})
    try:
        svc_env = services.service_env([_service(needs_model=True, needs_seed=True)],
                                       task_id="s01", run_seed="R")
        harness_facing = services.harness_env([_service()])
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert svc_env["HG_ENV_MODEL"] == "sim-model"
    assert svc_env["HG_ENV_API_KEY"] == "sim-key"
    assert svc_env["HG_ENV_SEED"] == services.task_seed("R", "s01")
    assert svc_env["HG_ENV_SEED"] != "should-not-be-read-from-here", (
        "the run's seed must be derived, not inherited from the host")
    assert list(harness_facing) == ["HG_SVC_ORDERS_URL"]


def test_the_task_seed_is_derived_and_reproducible():
    assert services.task_seed("R", "a") == services.task_seed("R", "a")
    assert services.task_seed("R", "a") != services.task_seed("R", "b")
    assert services.task_seed("R", "a") != services.task_seed("S", "a")


# ------------------------------------------------------------- mount rules ---

def test_the_harness_is_given_no_view_of_a_recording(tmp_path):
    """The rule that makes a recording evidence. Not read-only: *absent*."""
    specs = [_service()]
    recordings = {"orders": tmp_path / "rec"}
    assert services.mounts_for("harness", specs, recordings) == []


def test_the_check_is_given_the_recording_read_only(tmp_path):
    mounts = services.mounts_for("verifier", [_service()], {"orders": tmp_path / "rec"})
    joined = " ".join(mounts)
    assert f"dst={services.RECORDINGS_ROOT}/orders" in joined
    assert "readonly" in joined, (
        "a check that can write its own evidence has a verdict that depends on it")


def test_an_unknown_audience_is_refused_rather_than_defaulted(tmp_path):
    with pytest.raises(ValueError):
        services.mounts_for("someone-else", [_service()], {})


# --------------------------------------------------------------- lifecycle ---

def test_a_harness_reaches_the_service_by_the_name_it_was_told(tmp_path):
    """The whole point, and the bug that made it worth a test.

    The docker *name* is platform-mangled -- it has to be unique on the daemon -- so the
    name the harness was given has to be attached as a network alias. Without it the
    harness gets `Name or service not known` for a service that is running and healthy.
    """
    _require_docker()
    spec = _env()
    running = services.start(spec["services"], recordings_root=tmp_path / "rec",
                             run_id="t", task_id="s01", run_seed="R")
    try:
        told = services.harness_env(spec["services"])
        assert told == {"HG_SVC_ORDERS_URL": "http://orders:8080"}
        proc = container.run(
            ["python3", "-c", "import urllib.request as u;"
                              "print(u.urlopen('http://orders:8080/orders', timeout=5).read().decode())"],
            image=_pin(DEMO_IMAGE), task_dir=tmp_path, task_mount="/app",
            network=running.network, timeout_s=120)
        assert proc.returncode == 0, proc.stderr
        assert "order_count" in proc.stdout
    finally:
        running.stop()


def test_a_service_that_is_not_healthy_refuses_the_task(tmp_path):
    """**The negative control for the whole invalid category.**

    A service that is listening but whose own health check never passes must produce an
    `invalid` task -- attributed to the dataset and the service -- and not a zero. A
    zero here is indistinguishable from a weak harness, which is the distinction this
    platform exists to preserve.
    """
    _require_docker()
    spec = _env([_service(health={"argv": ["false"], "timeout_s": 3})])
    result = _evaluate(tmp_path, spec)
    # Nothing was scored, so the caller has to be able to see that it was not.
    assert "s01" not in result["per_task"], (
        f"an unmeasurable task was given a score: {result['per_task']}")
    assert result["invalid"]["s01"]["stage"] == "health"
    assert result["invalid"]["s01"]["service"] == "orders"
    assert "not a harness result" in result["invalid"]["s01"]["detail"]


def test_the_same_task_scores_when_its_service_is_healthy(tmp_path):
    """The other half: the test above is only a measurement if the healthy case passes.

    Same task, same harness, same everything except the health command -- so the
    difference in outcome is the health check and nothing else.
    """
    _require_docker()
    result = _evaluate(tmp_path, _env(), verifier={
        "kind": "command", "argv": [
            "python3", "-c",
            "import json,pathlib,sys;"
            "p=pathlib.Path('/recordings/orders/harnessgrad.json');"
            "d=json.loads(p.read_text()) if p.exists() else {};"
            "sys.exit(0 if any(r.get('path')=='/orders' for r in d.get('received',[])) else 1)"]})
    assert result["invalid"] == {}, result["invalid"]
    assert result["per_task"] == {"s01": 1.0}


def test_the_health_check_actually_waits(tmp_path):
    """A service that binds its socket and is not ready yet.

    `tools/fake_service.py` answers `/health` with 503 for ~1.5s after it starts
    listening. A platform that probed the port instead of running the check would return
    immediately and the harness would race the service.
    """
    _require_docker()
    started = time.monotonic()
    running = services.start([_service()], recordings_root=tmp_path / "rec",
                             run_id="t", task_id="s01", run_seed="R")
    try:
        assert time.monotonic() - started >= 1.0, (
            "the service reported healthy sooner than it can be ready, so the health "
            "check is not being run")
    finally:
        running.stop()


def test_nothing_is_left_behind_after_teardown(tmp_path):
    """Containers carry `--rm`; networks do not."""
    _require_docker()
    running = services.start(_env()["services"], recordings_root=tmp_path / "rec",
                             run_id="teardown-run", task_id="s01", run_seed="R")
    network, names = running.network, list(running.containers.values())
    assert running.stop() == [], "teardown reported a problem with a healthy service"

    left = container.docker(
        "network", "ls", "--filter", "label=harnessgrad.run=teardown-run",
        "--format", "{{.Name}}").stdout.split()
    assert left == [], f"a network survived teardown and would be inherited: {left}"
    for cid in names:
        gone = container.docker("inspect", cid)
        # `docker inspect` says "no such object" while `docker rm` says "No such
        # container"; matching one exact string made this fail against a container that
        # was already gone. Match the fact, not docker's wording for it.
        assert gone.returncode != 0 and "no such" in gone.stderr.lower(), (
            f"a service container survived teardown: {gone.stdout[:200]}")


def test_a_start_that_fails_does_not_leave_a_network(tmp_path):
    """A failed start must clean up after itself, or the *next* task inherits a network
    it did not create and the failure looks like isolation that is not there."""
    _require_docker()
    with pytest.raises(services.ServiceUnavailable):
        services.start([_service(health={"argv": ["false"], "timeout_s": 2})],
                       recordings_root=tmp_path / "rec", run_id="failed-run",
                       task_id="s01", run_seed="R")
    left = container.docker(
        "network", "ls", "--filter", "label=harnessgrad.run=failed-run",
        "--format", "{{.Name}}").stdout.split()
    assert left == [], f"a failed start left a network behind: {left}"


def test_the_service_and_the_check_share_a_network_and_the_harness_cannot_see_out(
        tmp_path):
    """`--internal` is what makes "reachable, and not on the internet" true rather than
    hoped for."""
    _require_docker()
    running = services.start(_env()["services"], recordings_root=tmp_path / "rec",
                             run_id="t", task_id="s01", run_seed="R")
    try:
        proc = container.run(
            ["python3", "-c",
             "import socket,urllib.request\n"
             "try:\n"
             "    urllib.request.urlopen('http://orders:8080/orders', timeout=5)\n"
             "    print('service=reachable')\n"
             "except Exception as e:\n"
             "    print('service=unreachable', type(e).__name__)\n"
             "try:\n"
             "    urllib.request.urlopen('http://1.1.1.1', timeout=4)\n"
             "    print('internet=reachable')\n"
             "except Exception as e:\n"
             "    print('internet=unreachable', type(e).__name__)\n"
             "try:\n"
             "    socket.gethostbyname('pypi.org'); print('dns=resolves')\n"
             "except Exception as e:\n"
             "    print('dns=fails', type(e).__name__)\n"],
            image=_pin(DEMO_IMAGE), task_dir=tmp_path, task_mount="/app",
            network=running.network, timeout_s=180)
        assert "service=reachable" in proc.stdout, proc.stdout + proc.stderr
        assert "internet=unreachable" in proc.stdout, proc.stdout
        assert "dns=fails" in proc.stdout, proc.stdout
    finally:
        running.stop()


# ------------------------------------------------------------ the record ---

def test_the_record_carries_the_services_and_their_digests(tmp_path):
    """§2.5.4 one level down: a task with a mock API is a different task from one
    without, and a service image retagged is a different task again."""
    envs = registry.load_envs("verify_service")
    resolved, problem = _resolve_envs(envs)
    assert problem is None, problem
    record = _env_record(resolved, ["s01", "s03"])
    assert record["network"] == "services"
    assert [s["name"] for s in record["services"]] == ["orders"]
    assert record["services"][0]["image_digest"].startswith("sha256:")
    # No service in this dataset needs one, so both are null -- a fact about the run
    # rather than a missing field.
    assert record["model"] is None and record["seed"] is None


def test_the_record_says_which_services_if_any_are_missing():
    """`[]` and not an absent key: "no services" has to be a fact."""

    assert _env_record(registry.load_envs("verify_demo"), ["v01"])["services"] == []


def test_needs_seed_and_needs_model_decide_whether_the_record_is_null(tmp_path):

    envs = registry.load_envs("verify_service")
    envs = {tid: {**spec, "services": [{**spec["services"][0],
                                        "needs_seed": True, "needs_model": True}]}
            for tid, spec in envs.items()}
    resolved, _ = _resolve_envs(envs)
    old = os.environ.copy()
    os.environ.update({"HARNESSGRAD_SEED": "abc123", "HG_ENV_MODEL": "sim-model"})
    try:
        record = _env_record(resolved, ["s01"])
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert record["seed"] == "abc123"
    assert record["model"] == "sim-model"


def test_the_environment_spend_is_reported_separately_from_the_harness(tmp_path):
    """§2.5.7: folding it in does not weaken a cost-aware rule, it feeds it a number
    its formula is not about."""

    res = {"tokens": {"input": 100, "output": 50, "calls": 2, "tasks_reported": 1},
           "env_usage": {"input": 7, "output": 3, "calls": 1, "services_reported": 1}}
    cost = _cost_of(res, 1, 1.0)
    assert cost["harness_tokens"] == 150
    assert cost["env_generation_tokens"] == 10
    assert cost["harness_tokens"] != 160, "the environment's spend was folded in"

    # And "not reported" is not zero.
    silent = _cost_of({"tokens": {}, "env_usage": {"services_reported": 0}},
                             1, 1.0)
    assert silent["env_generation_tokens"] is None
    assert silent["harness_tokens"] is None


def test_the_improvers_own_split_reaches_the_point():
    """方法自己那个账本:一个总数加一个拆分。总数给规则,拆分给"为什么这么贵"。

    实测过一个总数不够用:一轮 24 KB 的 harness 源码进、200 token 出,总数涨的时候
    看不出是"读得多"还是"想得多",而这两件事的修法完全不同。
    """
    cost = _cost_of({"tokens": {"input": 10, "output": 5, "tasks_reported": 1}}, 1, 1.0,
                    {"generation_tokens": 2000, "method_input_tokens": 1800,
                     "method_output_tokens": 200, "method_model_calls": 2,
                     "method_model": "m-real"})
    assert cost["method_generation_tokens"] == 2000
    assert cost["generation_tokens"] == 2000
    assert (cost["method_generation_input"], cost["method_generation_output"]) == (1800, 200)
    assert (cost["method_model_calls"], cost["method_model"]) == (2, "m-real")

    # 老调用方传裸数字,和"根本没有方法"两种老记录都不能坏。
    assert _cost_of(None, 0, 0.0, 7)["method_generation_tokens"] == 7
    nothing = _cost_of(None, 0, 0.0)
    assert nothing["method_generation_tokens"] is None
    assert nothing["generation_tokens"] == 0
    assert nothing["method_generation_input"] is None, "没测到不是 0"


# ------------------------------------------------- the two driver refusals ---

def _run_driver(tmp_path: Path, dataset: str, *, extra_env=None, extra_argv=(),
                ) -> subprocess.CompletedProcess:
    runs = tmp_path / "runs"
    env = {**os.environ, "HG_AGENT_BACKEND": "mock"}
    env.pop("HG_ENV_MODEL", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "driver.py", "--harness", str(_harness(tmp_path)),
         "--mode", "A", "--rounds", "0", "--sampling", "all", "--dataset", dataset,
         "--run-id", "probe", "--runs-root", str(runs),
         "--work-root", str(tmp_path / "work"), *extra_argv,
         "--method-entrypoint",
         f"{sys.executable} {ROOT / 'methods/noop/run.py'}"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800, env=env)


def _said(proc) -> str:
    """Everything the run printed, whitespace-normalized.

    Both halves, because the console sends errors and warnings to stderr and the rest
    to stdout -- and normalized, because it wraps its own output at a fixed width, so a
    phrase that reads as one line in the source arrives split across two. Asserting on
    raw stdout made two of these tests fail against messages that had been printed
    correctly.
    """
    return " ".join((proc.stdout + proc.stderr).split())


def test_a_service_needing_a_model_stops_the_run_when_none_is_configured(tmp_path):
    """A third budget has to be configured, not inherited.

    A simulator that silently has no model produces a task that fails for a reason
    nothing records, and §2.5.7 requires `env.model` to be non-null exactly when a
    service declared it needs one -- so the platform cannot proceed with neither.
    """
    _require_docker()
    proc = _run_driver(tmp_path, "broken_service")
    assert proc.returncode == 2, _said(proc)[-1200:]
    assert "needs_model but HG_ENV_MODEL is unset" in _said(proc), _said(proc)[-800:]


def test_a_run_where_nothing_is_scoreable_is_refused_not_recorded_as_zero(tmp_path):
    """**The negative control for `invalid`.**

    Every task's service is never healthy, so there is no measurement. `mean([])` is
    `0.0`, and writing that would say "the harness scored zero" about a run in which the
    harness never ran. The run must refuse and name the dataset-side cause.
    """
    _require_docker()
    proc = _run_driver(tmp_path, "broken_service",
                       extra_env={"HG_ENV_MODEL": "sim-model-for-this-test"})
    assert proc.returncode == 2, _said(proc)[-2000:]
    # `broken_service` splits 1/1, and a run covers one side, so the refusal is about
    # one task rather than two (INTERFACE.md §2.6).
    assert "none of the 1 tasks" in _said(proc), _said(proc)[-1500:]
    assert "never-ready" in _said(proc), (
        f"the refusal must name the dataset-side cause: {_said(proc)[-1200:]}")
    # And nothing was written that a reader could mistake for a result.
    curve = tmp_path / "runs" / "probe" / "curve.jsonl"
    assert not curve.exists() or not [
        line for line in curve.read_text().splitlines() if line.strip()], (
        "a curve point was written for a task set that was never measured")


def test_a_healthy_service_run_names_the_service_in_its_header(tmp_path):
    """The header is where a reader takes the run's conditions from, and "a mock API
    was reachable" is a condition."""
    _require_docker()
    proc = _run_driver(tmp_path, "verify_service", extra_env={"HG_AGENT_PROBE": "idle"})
    assert proc.returncode == 0, _said(proc)[-1500:]
    assert "services[orders]" in _said(proc), _said(proc)[:1500]


def test_the_declared_environment_reaches_the_curve(tmp_path):
    _require_docker()
    assert _run_driver(tmp_path, "verify_service",
                       extra_env={"HG_AGENT_PROBE": "idle"}).returncode == 0
    point = json.loads((tmp_path / "runs" / "probe" / "curve.jsonl")
                       .read_text().splitlines()[-1])
    assert point["env"]["services"][0]["name"] == "orders"
    assert point["env"]["services"][0]["image_digest"].startswith("sha256:")
    assert point["env"]["network"] == "services"
    assert point["env"]["seed"] is None, "no service asked for a seed"
    assert point["cost"]["env_generation_tokens"] is None, (
        "the service reported no usage, and that is not the same as reporting zero")


# ------------------------------------------------------- the model gateway ---
#
# A containerised harness cannot reach a model on the host's loopback, and the wrong
# fix is `--network bridge` -- that would give the task the internet and silently change
# what it is. `eval/modelgate.py` publishes the endpoint on the network the task already
# has. These tests pin the load-bearing fact and the wiring, in that order.

def test_a_host_local_url_is_recognised_and_a_remote_one_is_not():
    """The decision that selects the mechanism. A remote endpoint is *not* published:
    the container has no route to it, and pretending otherwise would produce a gateway
    that forwards into nothing."""
    from eval import modelgate

    for local in ("http://127.0.0.1:8001/v1", "http://localhost:8001/v1",
                  "http://0.0.0.0:8001/v1", "http://host.docker.internal:8001/v1"):
        assert modelgate.is_host_local(local), local
    for remote in ("https://api.deepseek.com/v1", "http://10.0.0.5:8001/v1",
                   "https://example.com"):
        assert not modelgate.is_host_local(remote), remote


def test_the_gateway_preserves_the_path():
    """`/v1` is part of an OpenAI-compatible endpoint. Dropping it produces a 404 that
    reads like a wrong model rather than a wrong URL."""
    from eval import modelgate

    target, path = modelgate.split("http://127.0.0.1:8001/v1")
    assert target == ("127.0.0.1", 8001)
    assert path == "/v1"
    assert modelgate.split("http://127.0.0.1:8001")[1] == ""


def test_a_container_reaches_the_host_on_the_internal_gateway(tmp_path):
    """**The load-bearing fact, measured both ways.**

    A container on an `--internal` network can reach a host process bound to that
    network's gateway address, and **still cannot reach the internet**. That
    combination is the whole design: the harness gets its model without the task
    becoming a networked task. Without the first half there is nothing to publish onto;
    without the second half, `--network bridge` would have been the easier answer and a
    strictly worse one.

    Kept separate from the end-to-end gateway test below because the two fail for
    different reasons: this one is about the network, that one is about the platform
    using it.
    """
    _require_docker()
    import http.server
    import threading

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                           # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"from-the-host")

        def log_message(self, *a):
            pass

    net = services.create_network("gwtest", "t01")
    server = None
    try:
        gw_ip = services.gateway_ip(net)
        server = http.server.HTTPServer((gw_ip, 0), _H)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        proc = container.run(
            ["python3", "-c",
             "import urllib.request as u\n"
             f"print(u.urlopen('http://{gw_ip}:{port}/', timeout=5).read().decode())\n"
             "try:\n"
             "    u.urlopen('http://1.1.1.1', timeout=4); print('internet=up')\n"
             "except Exception:\n"
             "    print('internet=down')\n"],
            image=_pin(DEMO_IMAGE), task_dir=tmp_path, task_mount="/app",
            network=net, timeout_s=180)
        assert "from-the-host" in proc.stdout, proc.stdout + proc.stderr
        assert "internet=down" in proc.stdout, (
            f"the internal network reaches the internet: {proc.stdout}")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        services.remove_network(net)


def test_a_containerised_harness_gets_its_model_and_not_the_internet(tmp_path):
    """The end-to-end version, through `run_one`: the run records `model_gateway`, the
    harness's base URL was rewritten to the published address, and the internet is still
    unreachable from inside."""
    _require_docker()
    import http.server
    import threading
    from eval import modelgate

    # A stand-in model on the host's loopback, which is what the container cannot reach
    # directly -- so passing this test cannot be an accident of the host being reachable.
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                           # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    loopback = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = loopback.server_address[1]
    threading.Thread(target=loopback.serve_forever, daemon=True).start()

    harness = tmp_path / "gl_harness"
    harness.mkdir()
    (harness / "harness.json").write_text(json.dumps({
        "name": "gl", "version": "1", "entrypoint": "agent.py", "backend": "cli",
        "env_kinds": ["exec"]}))
    (harness / "agent.py").write_text(
        "import argparse, json, os, pathlib, urllib.request\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--task'); ap.add_argument('--workdir')\n"
        "a = ap.parse_args()\n"
        "w = pathlib.Path(a.workdir)\n"
        "base = os.environ['HG_AGENT_BASE_URL']\n"
        "seen = urllib.request.urlopen(base, timeout=10).read().decode()\n"
        "try:\n"
        "    urllib.request.urlopen('http://1.1.1.1', timeout=4)\n"
        "    net = 'internet'\n"
        "except Exception:\n"
        "    net = 'offline'\n"
        "(w / 'answer.txt').write_text(f'{base}|{seen}|{net}')\n"
        "(w / 'trace.jsonl').write_text('{}\\n')\n")

    old = os.environ.copy()
    os.environ.update({"HG_AGENT_BACKEND": "openai",
                       "HG_AGENT_BASE_URL": f"http://127.0.0.1:{port}",
                       "HARNESSGRAD_WORK_ROOT": str(tmp_path / "work")})
    try:
        result = run_one(
            harness, {"task_id": "g01", "goal": "call the model"}, sandbox=True,
            env={"kind": "exec", "image": _pin(DEMO_IMAGE), "workdir": "/app",
                 "network": "none", "placement": "inside", "services": []},
            run_id="gl", run_seed="s", recordings_root=tmp_path / "rec")
    finally:
        loopback.shutdown()
        loopback.server_close()
        os.environ.clear()
        os.environ.update(old)

    assert result["answer"].startswith("http://"), result["answer"]
    base, seen, net = result["answer"].split("|")
    assert seen == "ok", f"the harness did not reach the model: {result['answer']}"
    assert net == "offline", "the task silently gained the internet"
    assert "127.0.0.1" not in base, (
        f"the harness was handed the loopback address, which it cannot reach: {base}")
    assert result["model_gateway"]["published"] is True
    assert result["model_gateway"]["target"] == f"127.0.0.1:{port}"
    # The gateway's own account of the call that just went through it. Byte counts are the
    # ones that cannot be faked by a gateway that only *thinks* it forwarded something:
    # the harness's request went up and the reply came back.
    gw = result["model_gateway"]
    assert gw["connections"] == 1, gw
    assert gw["bytes_to_upstream"] > 0 and gw["bytes_to_client"] > 0, gw
    assert gw["upstream_connect_failed"] == 0 and gw["read_errors"] == [], gw


def test_a_remote_model_with_an_offline_network_is_refused(tmp_path):
    """A containerised harness whose model is not on this host needs the internet, and a
    task that did not ask for the internet must not silently get it. Refused rather than
    attempted: the harness would have no model, so every score would be a zero that
    means nothing and would look like a weak harness."""
    _require_docker()
    proc = _run_driver(tmp_path, "verify_exec",
                       extra_env={"HG_AGENT_BACKEND": "openai",
                                  "HG_AGENT_BASE_URL": "https://api.example.com/v1",
                                  "HG_AGENT_API_KEY": "x"})
    assert proc.returncode == 2, _said(proc)[-1500:]
    assert "not on this host" in _said(proc), _said(proc)[-1200:]
