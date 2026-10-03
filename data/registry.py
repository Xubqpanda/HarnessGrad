"""Dataset registry. INTERFACE.md §8.1: the interface is dataset-agnostic.

A dataset may declare a train/eval split:

    SPLIT = {"train": [...task ids...], "eval": [...task ids...]}

`load_split` returns it, or `None` when the dataset does not declare one. It is
deliberately `None` and not a guess. Both obvious guesses are wrong in a way that
is invisible afterwards: treating every task as train hands the method the exam,
and treating every task as eval withholds the diagnostics the method is supposed
to work from. `None` means "this dataset cannot be used for the claim", and the
driver says so out loud rather than picking one.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

_VALID = ("train", "eval")


def load(name: str) -> tuple[list[dict], dict[str, str]]:
    """Return (tasks, scorable) for a dataset name."""
    module = importlib.import_module(f"data.{name}")
    return module.load()


def load_split(name: str) -> tuple[list[dict], dict[str, str], dict | None]:
    """Return (tasks, scorable, split). `split` is None when undeclared.

    Validates the declaration here rather than at the point of use, because the
    failure mode of a bad split is a run that completes and reports a number.
    Every task must land on exactly one side, and the sides must not overlap --
    an overlap means the method is scored on something it studied.
    """
    tasks, scorable = load(name)
    module = importlib.import_module(f"data.{name}")
    split = getattr(module, "SPLIT", None)
    if split is None:
        return tasks, scorable, None

    unknown = set(split) - set(_VALID)
    if unknown:
        raise ValueError(
            f"dataset {name!r}: SPLIT has unknown side(s) {sorted(unknown)}; "
            f"expected only {list(_VALID)}")

    ids = [t["task_id"] for t in tasks]
    known = set(ids)
    groups = {side: list(split.get(side, [])) for side in _VALID}
    for side, members in groups.items():
        missing = set(members) - known
        if missing:
            raise ValueError(
                f"dataset {name!r}: SPLIT[{side!r}] names unknown task(s) "
                f"{sorted(missing)}")

    overlap = set(groups["train"]) & set(groups["eval"])
    if overlap:
        raise ValueError(
            f"dataset {name!r}: task(s) {sorted(overlap)} are on both sides of "
            f"the split; a task the method studies cannot also be one it is "
            f"scored on")

    covered = set(groups["train"]) | set(groups["eval"])
    if covered != known:
        raise ValueError(
            f"dataset {name!r}: task(s) {sorted(known - covered)} are on neither "
            f"side of the split; they would be silently neither trained on nor "
            f"scored")

    empty = [side for side, members in groups.items() if not members]
    if empty:
        raise ValueError(f"dataset {name!r}: SPLIT has an empty side: {empty}")
    return tasks, scorable, groups


# --------------------------------------------------------------- environments ---

#: The environment kinds the platform knows how to provide. `files` is a directory on
#: the host and the harness runs beside it in a namespace; `exec` is a container and
#: the harness runs inside it (INTERFACE.md §2.5). Kept here rather than in the driver
#: because a dataset is where a kind is *named*, and the driver imports it.
ENV_KINDS = ("files", "exec")

NETWORKS = ("none", "bridge", "services")
PLACEMENTS = ("inside", "beside")

#: Fields an environment may carry. Unknown keys are refused rather than ignored: a
#: mistyped `work_dir` that silently did nothing would leave a task running in the
#: wrong place while the dataset said otherwise.
ENV_FIELDS = ("kind", "image", "workdir", "network", "placement", "services",
              "state", "cpus", "memory_mb", "agent_timeout_s", "verify_timeout_s")

#: Where a task's state lives (INTERFACE.md §2.5.9). `mount` is a host directory bound
#: at `workdir`; `container` is the container's own filesystem, snapshotted after the
#: harness stops so the check can run somewhere the harness never was.
STATES = ("mount", "container")

#: The default resource envelope. Declared per environment because the real task set
#: spans 1-4 cpus, 2-8 GB and 600-12000 s (measured), and a platform that ran those on
#: one default would report a harness failing a task it was never given time to attempt.
ENV_DEFAULTS = {"cpus": 2, "memory_mb": 4096,
                "agent_timeout_s": 300, "verify_timeout_s": 300}

#: A service's fields (INTERFACE.md §2.5.7). Every one has a consequence in
#: `eval/services.py`; none is decoration.
SERVICE_FIELDS = ("name", "image", "port", "health", "record", "needs_seed",
                  "needs_model")

#: A service name becomes a hostname on the task's internal network *and* a path
#: component under `/recordings`, so it is restricted to a DNS label. Refused rather
#: than sanitized: a name that is silently rewritten is not the name the harness was
#: told to call, and the harness would fail to resolve it for a reason nothing states.
SERVICE_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")

#: Where the task's files land inside an `exec` environment when the dataset does not
#: say. Duplicated from `eval/container.py` on purpose: the registry must be able to
#: state a normalized env without importing the container driver, which needs Docker.
DEFAULT_WORKDIR = "/app"


def _check_env(spec, tid: str, name: str) -> None:
    if not isinstance(spec, dict):
        raise ValueError(f"dataset {name!r}: ENV[{tid!r}] must be a dict")

    # Checked before the unknown-field rule so that naming a Dockerfile gets the
    # explanation rather than "unknown field 'dockerfile'". The two say very different
    # things: one is a typo, the other is a deliberate design boundary.
    if spec.get("dockerfile"):
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}] names a Dockerfile. Building is an "
            f"import step, not a run step, so a dataset does not reference one: "
            f"import it once and name the pinned image it produced (INTERFACE.md "
            f"§2.5.5).\n    python tools/import_env.py --dockerfile <path>")

    unknown = set(spec) - set(ENV_FIELDS)
    if unknown:
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}] has unknown field(s) {sorted(unknown)}; "
            f"known fields are {list(ENV_FIELDS)}")

    kind = spec.get("kind", "files")
    if kind not in ENV_KINDS:
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}] has kind {kind!r}; "
            f"known kinds are {list(ENV_KINDS)}")

    # Checked before the files/exec split so that a `files` env carrying `state` gets
    # the "only an exec environment uses this" message rather than being waved through.
    state = spec.get("state", "mount")
    if state not in STATES:
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}]['state'] must be one of {list(STATES)}, "
            f"got {state!r}")

    for field, minimum in (("cpus", 1), ("memory_mb", 64),
                           ("agent_timeout_s", 1), ("verify_timeout_s", 1)):
        value = spec.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or value < minimum:
            raise ValueError(
                f"dataset {name!r}: ENV[{tid!r}][{field!r}] must be a number >= "
                f"{minimum}, got {value!r}")

    if state == "container" and spec.get("services"):
        # Refused rather than half-supported. A recording is a **bind mount**, and
        # `docker commit` does not capture bind mounts -- so the check would look for
        # evidence that silently did not travel. Nothing in the real task set needs both
        # (measured: `mcp_servers` is empty for all 89), so this is a boundary, not a gap.
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}] asks for state='container' together with "
            f"services. A service's recording travels by bind mount and a container "
            f"snapshot does not capture one, so the check would read an empty recording "
            f"and the failure would look like the harness's. Not supported yet.")

    if kind == "files":
        extra = set(spec) - {"kind"}
        if extra:
            raise ValueError(
                f"dataset {name!r}: ENV[{tid!r}] is kind 'files' but carries "
                f"{sorted(extra)}, which only an 'exec' environment uses. A field "
                f"that does nothing is worse than a missing one: the dataset would "
                f"read as though it had asked for a container.")
        return

    image = spec.get("image")
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}] is kind 'exec' and needs a non-empty "
            f"`image`. Name an image that is already on the local daemon; this "
            f"platform does not pull or build during a run (INTERFACE.md §2.5.5).")

    workdir = spec.get("workdir", DEFAULT_WORKDIR)
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}]['workdir'] must be an absolute path "
            f"inside the container, got {workdir!r}. A relative one would resolve "
            f"against the image's WORKDIR and land the task somewhere the dataset "
            f"did not name.")

    network = spec.get("network", "none")
    if network not in NETWORKS:
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}]['network'] must be one of "
            f"{list(NETWORKS)}, got {network!r}")

    placement = spec.get("placement", "inside")
    if placement not in PLACEMENTS:
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}]['placement'] must be one of "
            f"{list(PLACEMENTS)}, got {placement!r}")

    services = spec.get("services") or []
    if not isinstance(services, list):
        raise ValueError(f"dataset {name!r}: ENV[{tid!r}]['services'] must be a list")
    seen: set[str] = set()
    for svc in services:
        _check_service(svc, tid, name)
        # Uniqueness here rather than at start time: two services with one name would
        # resolve to whichever container won the race, so the harness would reach an
        # environment the dataset did not describe and nothing would say so.
        if svc["name"] in seen:
            raise ValueError(
                f"dataset {name!r}: ENV[{tid!r}] declares two services named "
                f"{svc['name']!r}; a name is a hostname on the task's network, so "
                f"duplicates are ambiguous rather than merely untidy")
        seen.add(svc["name"])

    # Both directions of the same contradiction. Declaring services with no network to
    # reach them on, and asking for a network with nothing to put on it, are equally
    # likely to be a typo -- and silently upgrading one of them would leave the dataset
    # describing a run that did not happen (INTERFACE.md §2.5.7).
    if services and network != "services":
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}] declares services but sets "
            f"network={network!r}. A service is reachable only on the task's internal "
            f"network, so this is a contradiction; write network='services'.")
    if network == "services" and not services:
        raise ValueError(
            f"dataset {name!r}: ENV[{tid!r}] sets network='services' but declares no "
            f"services. There is nothing to be reachable, and the network would only "
            f"be an isolation the dataset did not ask for.")


def _check_service(svc, tid: str, name: str) -> None:
    where = f"dataset {name!r}: ENV[{tid!r}]['services']"
    if not isinstance(svc, dict):
        raise ValueError(f"{where} entries must be dicts")

    unknown = set(svc) - set(SERVICE_FIELDS)
    if unknown:
        raise ValueError(
            f"{where} has unknown field(s) {sorted(unknown)}; known fields are "
            f"{list(SERVICE_FIELDS)}")

    svc_name = svc.get("name")
    if not isinstance(svc_name, str) or not SERVICE_NAME_RE.match(svc_name):
        raise ValueError(
            f"{where}: service name {svc_name!r} must be a DNS label -- lowercase "
            f"letters, digits and internal hyphens. It becomes a hostname the harness "
            f"resolves and a directory under /recordings.")

    image = svc.get("image")
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            f"{where}: service {svc_name!r} needs a non-empty `image`, pinned to a "
            f"digest by the same rule as the task's (INTERFACE.md §2.5.4). A service "
            f"is a container and never a host process (§2.5.8), so that a harness "
            f"cannot read or kill the thing grading its interaction.")

    port = svc.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError(
            f"{where}: service {svc_name!r} needs `port` as an integer in 1..65535, "
            f"got {port!r}. It is the port the harness is told to call.")

    # Required, not defaulted to "is the port open". A port that accepts a connection
    # is not a service that is ready, and the whole point of the check is that the
    # harness must not start against a service that is still initialising -- a score
    # decided by a race is not a measurement (INTERFACE.md §2.5.7).
    health = svc.get("health")
    if not isinstance(health, dict):
        raise ValueError(
            f"{where}: service {svc_name!r} needs a `health` check. Without one the "
            f"platform cannot tell a service that is ready from one that accepts a "
            f"connection and is not, and the task's score becomes a race it runs "
            f"against its own infrastructure. Shape: "
            f"{{'argv': [...], 'timeout_s': 30}}")
    argv = health.get("argv")
    if not isinstance(argv, list) or not argv or not all(
            isinstance(a, str) for a in argv):
        raise ValueError(
            f"{where}: service {svc_name!r} `health` needs `argv` as a non-empty list "
            f"of strings, run inside the service container")
    timeout = health.get("timeout_s", 60)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(
            f"{where}: service {svc_name!r} `health` timeout_s must be positive, "
            f"got {timeout!r}")

    for flag in ("record", "needs_seed", "needs_model"):
        if not isinstance(svc.get(flag, False), bool):
            raise ValueError(
                f"{where}: service {svc_name!r} `{flag}` must be a bool, got "
                f"{svc.get(flag)!r}")


def _normalize_env(spec: dict) -> dict:
    """The canonical form, so two spellings of one environment compare equal."""
    if spec.get("kind", "files") == "files":
        return {"kind": "files"}
    services = []
    for svc in spec.get("services") or []:
        services.append({
            "name": svc["name"], "image": svc["image"], "port": svc["port"],
            "health": {"argv": list(svc["health"]["argv"]),
                       "timeout_s": svc["health"].get("timeout_s", 60)},
            "record": svc.get("record", False),
            "needs_seed": svc.get("needs_seed", False),
            "needs_model": svc.get("needs_model", False),
        })
    return {"kind": "exec",
            "image": spec["image"],
            "workdir": spec.get("workdir", DEFAULT_WORKDIR),
            "network": spec.get("network", "none"),
            "placement": spec.get("placement", "inside"),
            "services": services,
            "state": spec.get("state", "mount"),
            **{f: spec.get(f, d) for f, d in ENV_DEFAULTS.items()}}


def load_envs(name: str) -> dict[str, dict]:
    """Resolve every task's environment: `ENV[tid]`, else `DEFAULT_ENV`, else `files`.

    A separate function rather than a fourth return value of `load_tasks`, because
    every existing dataset and test calls that one and unpacking three values; a
    dataset that says nothing about environments must be unaffected by this feature
    existing, and the cheapest proof of that is that none of them changed.
    """
    tasks, _ = load(name)
    module = importlib.import_module(f"data.{name}")
    default = getattr(module, "DEFAULT_ENV", None) or {"kind": "files"}
    declared = dict(getattr(module, "ENV", None) or {})

    if not isinstance(default, dict):
        raise ValueError(f"dataset {name!r}: DEFAULT_ENV must be a dict")
    _check_env(default, "<default>", name)

    ids = {t["task_id"] for t in tasks}
    unknown = set(declared) - ids
    if unknown:
        raise ValueError(f"dataset {name!r}: ENV names unknown task(s) {sorted(unknown)}")

    envs = {}
    for tid in ids:
        spec = declared.get(tid, default)
        _check_env(spec, tid, name)
        envs[tid] = _normalize_env(spec)
    return envs


# ------------------------------------------------------- tasks and verifiers ---

#: The verifier kinds the platform knows how to run. Unknown kinds are refused at
#: load time rather than at scoring time: a dataset that only fails once a run has
#: been paid for is a dataset nobody can debug.
VERIFIER_KINDS = ("answer", "command")

#: Verifier fields. `reward_file` is Terminal-Bench's convention: the check writes the
#: score into a file and exits 0 either way, so the exit code carries nothing
#: (INTERFACE.md §2.5.9). Parsed as a number, so a fraction is a score.
VERIFIER_FIELDS = ("kind", "argv", "cwd", "inputs", "pass_when", "stdout",
                   "timeout_s", "expected", "env", "reward_file")

#: Keys that must never appear in a task dict, because `run_one` writes the whole
#: dict to `task.json` and hands it to the harness. Putting the grade or the initial
#: state in there would hand the harness what it is being measured against.
#:
#: `env` is here for consistency rather than secrecy: an environment is declared in
#: the module-level `ENV`/`DEFAULT_ENV` like `SETUP` and `VERIFY` are, so one that
#: appears inside a task would be silently *not* the environment that ran, while the
#: harness read it as though it were.
FORBIDDEN_IN_TASKS = ("verify", "verifier", "setup", "expected", "scorable", "env")


def _check_no_secrets(task: dict, name: str) -> None:
    leaked = [k for k in task if k in FORBIDDEN_IN_TASKS]
    if leaked:
        raise ValueError(
            f"dataset {name!r}: task {task.get('task_id')!r} carries {leaked}, which the "
            f"harness would receive verbatim: every task dict is written to task.json "
            f"and passed on the command line. The grade and the initial state belong in "
            f"VERIFY and SETUP (INTERFACE.md §2.4).")


def _check_setup(spec, tid: str, name: str) -> None:
    if not isinstance(spec, dict):
        raise ValueError(f"dataset {name!r}: SETUP[{tid!r}] must be a dict")
    files = spec.get("files") or []
    if not isinstance(files, list):
        raise ValueError(f"dataset {name!r}: SETUP[{tid!r}]['files'] must be a list")
    for entry in files:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise ValueError(
                f"dataset {name!r}: SETUP[{tid!r}] has an entry without a `path`")
        # Escape refused, not clamped. A `../` that silently became `.` would set up a
        # task whose inputs are not where the dataset said they were.
        parts = Path(str(entry["path"])).parts
        if str(entry["path"]).startswith("/") or ".." in parts:
            raise ValueError(
                f"dataset {name!r}: SETUP[{tid!r}] path {entry['path']!r} escapes the "
                f"working directory")
        if "content" not in entry and "from" not in entry:
            raise ValueError(
                f"dataset {name!r}: SETUP[{tid!r}] entry {entry['path']!r} has neither "
                f"`content` nor `from`")


def _check_verifier(spec, tid: str, name: str) -> None:
    if not isinstance(spec, dict):
        raise ValueError(f"dataset {name!r}: VERIFY[{tid!r}] must be a dict")
    kind = spec.get("kind")
    if kind not in VERIFIER_KINDS:
        raise ValueError(
            f"dataset {name!r}: VERIFY[{tid!r}] has kind {kind!r}; "
            f"known kinds are {list(VERIFIER_KINDS)}")
    if kind == "answer" and not isinstance(spec.get("expected"), str):
        raise ValueError(f"dataset {name!r}: VERIFY[{tid!r}] needs a string `expected`")
    if kind == "command":
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv or not all(
                isinstance(a, str) for a in argv):
            raise ValueError(
                f"dataset {name!r}: VERIFY[{tid!r}] needs `argv` as a non-empty list of "
                f"strings")
        for entry in (spec.get("inputs") or []):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"dataset {name!r}: VERIFY[{tid!r}] inputs must be dicts")
            # An input names either a path relative to the workdir (`path`, the original
            # contract) or an absolute location inside the container (`dst`), because a
            # check that expects its tests at `/tests` cannot get them from a relative
            # path (INTERFACE.md §2.5.9).
            target = entry.get("dst") or entry.get("path")
            if not target:
                raise ValueError(
                    f"dataset {name!r}: VERIFY[{tid!r}] input has neither `path` nor "
                    f"`dst`")
            if entry.get("dst") and not str(entry["dst"]).startswith("/"):
                raise ValueError(
                    f"dataset {name!r}: VERIFY[{tid!r}] input `dst` must be absolute, "
                    f"got {entry['dst']!r}; it is a path inside the container")
            if "content" not in entry and "from" not in entry:
                raise ValueError(
                    f"dataset {name!r}: VERIFY[{tid!r}] input {target!r} has neither "
                    f"`content` nor `from`")
        reward = spec.get("reward_file")
        if reward is not None and (not isinstance(reward, str)
                                   or not reward.startswith("/")):
            raise ValueError(
                f"dataset {name!r}: VERIFY[{tid!r}]['reward_file'] must be an absolute "
                f"path inside the container, got {reward!r}")
        if reward and spec.get("pass_when", "exit0") != "exit0":
            raise ValueError(
                f"dataset {name!r}: VERIFY[{tid!r}] declares both `reward_file` and "
                f"`pass_when`; the reward comes from the file, so `pass_when` would be "
                f"a second answer to the same question")
        if spec.get("pass_when", "exit0") not in ("exit0", "exit0_stdout"):
            raise ValueError(
                f"dataset {name!r}: VERIFY[{tid!r}] pass_when must be 'exit0' or "
                f"'exit0_stdout'")


def load_tasks(name: str) -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
    """Return (tasks, setups, verifiers) — everything needed to run and grade.

    `SCORABLE` is not a second mechanism: a dataset that declares it and no `VERIFY`
    gets `{"kind": "answer", "expected": ...}` for each task, which is precisely what
    the platform did before verifiers existed. The equivalence is deliberate, so that
    adding this layer changed nothing for the datasets that already worked.
    """
    tasks, scorable = load(name)
    module = importlib.import_module(f"data.{name}")
    setups = dict(getattr(module, "SETUP", None) or {})
    declared = getattr(module, "VERIFY", None)

    for task in tasks:
        _check_no_secrets(task, name)

    ids = {t["task_id"] for t in tasks}

    if declared is None:
        missing = [t["task_id"] for t in tasks if t["task_id"] not in scorable]
        if missing:
            raise ValueError(
                f"dataset {name!r}: no VERIFY declared and SCORABLE is missing "
                f"{missing}; those tasks would have no grade at all")
        verifiers = {tid: {"kind": "answer", "expected": scorable[tid]} for tid in ids}
    else:
        unknown = set(declared) - ids
        if unknown:
            raise ValueError(
                f"dataset {name!r}: VERIFY names unknown task(s) {sorted(unknown)}")
        absent = ids - set(declared)
        if absent:
            raise ValueError(
                f"dataset {name!r}: task(s) {sorted(absent)} have no VERIFY entry")
        verifiers = {}
        for tid, spec in declared.items():
            _check_verifier(spec, tid, name)
            verifiers[tid] = {"kind": "answer", "expected": scorable.get(tid, ""),
                              **spec} if spec.get("kind") == "answer" else dict(spec)

    unknown_setup = set(setups) - ids
    if unknown_setup:
        raise ValueError(f"dataset {name!r}: SETUP names unknown task(s) "
                         f"{sorted(unknown_setup)}")
    for tid, spec in setups.items():
        _check_setup(spec, tid, name)

    return tasks, setups, verifiers
