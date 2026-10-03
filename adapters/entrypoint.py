#!/usr/bin/env python3
"""Run any external adapter as a mode-B entrypoint.

One entrypoint for all nine adapters, because the platform should not need to know
which method it is running -- and because nine scripts with three different calling
conventions is nine scripts, not nine integrations.

    HG_ADAPTER=harnessx HG_REPO=/path/to/harnessx HG_OPTIONS='{"example":"coding"}' \
        python adapters/entrypoint.py

Environment:
    HG_ADAPTER   which adapter module to load (required)
    HG_REPO      the method's checkout, for adapters that read one
    HG_RUN_ROOT  where a reader-adapter finds the method's runs
    HG_OPTIONS   JSON object of method-specific keys, e.g. {"domain": "..."}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _as_paths(options: dict, keys: tuple[str, ...]) -> None:
    """Turn string values for path-shaped options into Paths, in place."""
    for k in keys:
        v = options.get(k)
        if isinstance(v, str) and v:
            options[k] = Path(v).expanduser()


def main() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    got = req.get("platform_api_version")
    if got != "0.1.0":
        print(f"entrypoint: platform_api_version {got!r} != '0.1.0'", file=sys.stderr)
        return 2

    from context import AdapterRequest, call

    name = os.environ.get("HG_ADAPTER")
    if not name:
        print("entrypoint: set HG_ADAPTER to an adapter module name",
              file=sys.stderr)
        return 2
    try:
        module = __import__(name)
    except ImportError as exc:
        print(f"entrypoint: no adapter named {name!r}: {exc}", file=sys.stderr)
        return 2

    # The platform tells us where its scratch is; the adapter compiles states
    # under it rather than into a location of its own choosing.
    run_dir = Path(req.get("workspace") or "/tmp/hg-adapter").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    options = dict(req.get("options") or {})
    if raw := os.environ.get("HG_OPTIONS"):
        options.update(json.loads(raw))

    request = AdapterRequest(
        repo=Path(os.environ["HG_REPO"]).resolve() if os.environ.get("HG_REPO") else None,
        run_dir=run_dir,
        options=options,
    )
    if rr := os.environ.get("HG_RUN_ROOT"):
        request.options.setdefault("run_root", rr)

    # Environment variables and JSON arrive as strings; the adapters are typed and
    # expect Paths. Coercing here rather than in nine adapters keeps the typed
    # contract meaningful -- and the first version of this entrypoint did not, which
    # is why four of nine failed with `'str' object has no attribute 'glob'` while
    # working fine when called directly from Python.
    _as_paths(request.options, ("run_root", "out_dir", "solution_path"))

    traj = call(module, request)
    Path(req["trajectory_out"]).write_text(json.dumps(traj, indent=1))

    # The method's model, declared in ONE place for every adapter.
    #
    # Without this the platform has no way to learn it: a method that does not
    # mention its model leaves `identity.method_model` as `n/a`, and the curve then
    # cannot say whether a weak improver or a weak harness produced the result.
    # Reading it from the method's own environment is the weakest form that is
    # still real -- the platform takes the method's word for it, but the word comes
    # from its configuration rather than from its prose.
    sys.stdout.write(json.dumps({
        "modified": True,
        "steps": len(traj.get("steps") or []),
        "generation_tokens": 0,
        "method_model": os.environ.get("HG_METHOD_MODEL") or None,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
