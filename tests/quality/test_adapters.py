"""Every adapter must be reachable through the platform's own entrypoint.

This is the test that was missing when nine adapters were described as "done".
They had three different `to_trajectory` signatures and the entrypoint supplied a
fixed set of keywords, so two thirds of them could not be called through the
platform at all. Each worked when invoked directly from Python, which is why the
gap survived nine rounds of adapter work: **the adapters were tested the way they
were written, not the way they are used.**

The uniform contract is `adapters/context.py`. This checks the property that
matters -- not that a signature matches, but that a caller who knows only the
platform's protocol can get a trajectory out of every adapter.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = ROOT / "adapters"
METHODS = ROOT.parent / "methods_src"
RRSI = ROOT.parent / "rrsi-run"

#: (adapter, method checkout, extra environment). An adapter whose checkout is
#: absent is skipped rather than failed: the sources are other people's
#: repositories, fetched by `adapters/fetch.sh`, and a platform test must not
#: require them to be present.
#: A reader-adapter also needs a *run* to read, not just a checkout. SICA and
#: HyperAgents ship no run artifacts, so their cases are skipped unless a run
#: directory is supplied -- the test must not require the machine to have one.
# `Path("")` is the CURRENT DIRECTORY, not an absent path, so an empty default
# would make the skip condition below never fire and the test would run against
# the repository root. `None` is what "not supplied" has to look like.
def _opt_path(var: str) -> "Path | None":
    v = os.environ.get(var)
    return Path(v) if v else None


SICA_RUNS = _opt_path("HG_SICA_RUNS")
HA_RUNS = _opt_path("HG_HYPERAGENTS_RUNS")

#: (adapter, method checkout, extra env, does it read a run directory?)
#:
#: The fourth field is not decoration. "`HG_RUN_ROOT` is absent" means two
#: different things -- an adapter that does not read runs, and a reader-adapter
#: whose runs were not supplied -- and conflating them skipped five adapters that
#: were working perfectly.
CASES = [
    ("rrsi", RRSI, {"HG_RUN_ROOT": str(RRSI / "runs" / "coding")}, True),
    ("dgm", METHODS / "dgm", {"HG_RUN_ROOT": str(METHODS / "dgm")}, True),
    ("sica", None, {"HG_RUN_ROOT": str(SICA_RUNS) if SICA_RUNS else None}, True),
    ("hyperagents", None, {"HG_RUN_ROOT": str(HA_RUNS) if HA_RUNS else None,
                           "HG_OPTIONS": '{"domain": "polyglot"}'}, True),
    ("tthe", METHODS / "tthe", {"HG_OPTIONS": '{"domain": "text_to_sql"}'}, False),
    ("ahe", METHODS / "ahe", {}, False),
    ("mac", METHODS / "mac", {}, False),
    ("harnessx", METHODS / "harnessx", {"HG_OPTIONS": '{"example": "coding"}'}, False),
    ("metaharness", METHODS / "metaharness", {}, False),
]


def test_the_uniform_contract_is_what_the_adapters_implement():
    """`to_trajectory` on every adapter, discovered rather than listed: a hand-kept
    list drifts, and the drift looks like a broken adapter."""
    import importlib
    import inspect

    sys.path.insert(0, str(ADAPTERS))
    found = sorted(p.stem for p in ADAPTERS.glob("*.py")
                   if p.stem not in ("entrypoint", "context", "__init__"))
    assert len(found) == 9, f"expected nine adapters, found {found}"

    bad = []
    for name in found:
        fn = getattr(importlib.import_module(name), "to_trajectory", None)
        if fn is None:
            bad.append(f"{name}: no to_trajectory")
            continue
        params = set(inspect.signature(fn).parameters)
        # `call()` in context.py can satisfy any of these shapes; what it cannot
        # do is guess a parameter name it has never seen.
        known = {"repo", "run_root", "run_dir", "out_dir", "domain", "example",
                 "solution"}
        unknown = params - known
        if unknown:
            bad.append(f"{name}: unrecognised parameters {sorted(unknown)}")
    assert not bad, ("the uniform contract cannot call these adapters; add the "
                     "parameter to adapters/context.py::call: " + "; ".join(bad))


@pytest.mark.parametrize("name,repo,extra,needs_runs", CASES,
                         ids=[c[0] for c in CASES])
def test_every_adapter_is_reachable_through_the_platform_entrypoint(
        name, repo, extra, needs_runs, tmp_path):
    """End to end: entrypoint subprocess -> adapter -> a trajectory file."""
    if repo is not None and not Path(repo).is_dir():
        pytest.skip(f"method checkout absent: {repo}")
    if needs_runs:
        run_root = extra.get("HG_RUN_ROOT")
        if run_root is None:
            pytest.skip(
                "this reader-adapter needs a run directory and the method ships "
                "none; set HG_SICA_RUNS / HG_HYPERAGENTS_RUNS to exercise it"
            )
        if not Path(run_root).is_dir():
            pytest.skip(f"no run artifacts at {run_root!r}")

    traj_path = tmp_path / "trajectory.json"
    env = dict(os.environ, HG_ADAPTER=name,
               **{k: v for k, v in extra.items() if v is not None})
    if repo is not None:
        env["HG_REPO"] = str(repo)
    request = json.dumps({
        "platform_api_version": "0.1.0",
        "workspace": str(tmp_path),
        "trajectory_out": str(traj_path),
    })

    proc = subprocess.run(
        [sys.executable, str(ADAPTERS / "entrypoint.py")],
        input=request, capture_output=True, text=True, timeout=300, env=env,
    )
    assert proc.returncode == 0, (
        f"{name} could not be called through the platform entrypoint:\n"
        f"{proc.stderr[-800:]}"
    )
    assert traj_path.exists(), f"{name} wrote no trajectory"

    traj = json.loads(traj_path.read_text())
    assert traj.get("steps"), f"{name} produced no steps"
    assert traj.get("trajectory_shape"), (
        f"{name} must declare the shape of its trajectory: two of these methods "
        "produce trees, and a consumer that assumes a sequence will draw a lineage "
        "that never existed"
    )
