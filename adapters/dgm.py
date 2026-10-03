#!/usr/bin/env python3
"""Adapter: DGM's own runs -> a HarnessGrad trajectory, with materialized states.

Read-only with respect to DGM. DGM's shape differs from RRSI's in two ways the
platform has to accommodate.

1. A run is a **tree of variants**, not a sequence of rounds
   (`DGM_outer.py:19-35`: the archive is a list of run ids; each variant carries
   its own lineage).

2. The editable surface is **declared in prose, by the method**, and the adapter
   can read it off: `prompts/self_improvement_prompt.py:8-13` names the main file
   and the two directories the coding agent may change --

       - Main File: coding_agent.py
       - Prompts:   prompts/
       - Tools:     tools/

   So DGM's harness is a *subset* of its repository, and materializing a state
   means extracting exactly those paths -- not the tree, which also contains the
   outer loop that manages the archive. This is the first method whose boundary
   had to be read out of a prompt rather than out of code or config, which is
   worth recording: the platform can only materialize a state whose extent the
   method has stated somewhere.

Usage:
    python adapters/dgm.py --repo <dgm> --run-root <dgm-output-dir> --out trajectory.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

# From prompts/self_improvement_prompt.py:8-13 of the DGM checkout: everything
# the coding agent is told it may change, and nothing else.
EDITABLE = ["coding_agent.py", "prompts/", "tools/"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).stdout


def extract_editable(repo: Path, ref: str = "HEAD") -> dict[str, str]:
    """The declared editable surface at `ref`, as {path: contents}.

    Bounded by EDITABLE rather than by the whole repository: a DGM variant's
    harness is the coding agent, not the loop that manages the archive. Shipping
    the loop as part of the harness would let the platform measure a state that
    also contains the method.
    """
    files: dict[str, str] = {}
    for target in EDITABLE:
        listing = git(repo, "ls-tree", "-r", "--name-only", f"{ref}:{target}").strip()
        if not listing:
            # A bare file rather than a directory.
            blob = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{target}"],
                                  capture_output=True, text=True)
            if blob.returncode == 0:
                files[target] = blob.stdout
            continue
        for name in listing.splitlines():
            if not name.strip():
                continue
            blob = subprocess.run(
                ["git", "-C", str(repo), "show", f"{ref}:{target}{name}"],
                capture_output=True, text=True)
            if blob.returncode == 0:
                files[f"{target}{name}"] = blob.stdout
    return files


def find_variants(run_root: Path) -> list[Path]:
    return sorted(p.parent for p in run_root.glob("*/metadata.json"))


def read_variant(d: Path) -> dict:
    md = json.loads((d / "metadata.json").read_text())
    perf = md.get("overall_performance") or {}
    return {
        "run_id": md.get("run_id", d.name),
        "score": perf.get("accuracy_score"),
        "resolved": perf.get("total_resolved_instances"),
        "submitted": perf.get("total_submitted_instances"),
    }


def to_trajectory(repo: Path, run_root: Path) -> dict:
    variants = [read_variant(d) for d in find_variants(run_root)]
    if not variants:
        raise SystemExit(f"no */metadata.json under {run_root}; has DGM run yet?")

    files = extract_editable(repo)

    # Best-so-far is the only honest single curve for a tree: the platform must
    # not draw a lineage DGM never had.
    steps, best = [], None
    for v in variants:
        s = v["score"]
        if s is not None and (best is None or s > best):
            best = s
        steps.append({
            "files": files,
            "label": f"variant {v['run_id']}",
            "edit_kind": "harness_edit",
            "claimed_cost": {
                "note": "this method reports a run-level cost (~USD 22,000/run); "
                        "per-variant cost is not reported",
            },
            "method_reported": {
                "score": s,
                "score_provenance": "method-reported (not platform-measured)",
                "run_id": v["run_id"],
                "resolved": v["resolved"],
                "submitted": v["submitted"],
                "best_so_far": best,
                "materializable_files": sorted(files),
                "materializable": bool(files),
                "measurable_by_platform": False,
                "score_provenance": "method-reported (not platform-measured)",
                "not_measurable_because": (
                    "the coding agent is invoked by DGM's own container scaffold "
                    "against SWE-bench/Polyglot; it is not a freestanding "
                    "entrypoint the platform can call"
                ),
            },
        })

    return {
        "steps": steps,
        "nominated": len(steps) - 1,
        "trajectory_shape": "tree",
        "curve_drawn": "best_so_far_over_variants",
        "rhythm": {
            "note": "DGM owns its loop; a round yields several children from "
                    "several parents, so 'the harness at round t' is undefined",
        },
        "selected_on_reported_set": None,
        "selection_pool_size": len(steps),
        "provenance": {
            "method": "DGM",
            "source": "jennyzzt/dgm",
            "acceptance_rule": {
                # 这里原本只写了 `keep_better`。代码里的 CLI 默认是 `keep_all`
                # (`DGM_outer.py:232`),`keep_better` 要显式打开 —— 而这两件事很不一样:
                # keep_better 是一道过滤门,keep_all 根本不是门,每个编译出来的变体都进
                # 归档。一个默认跑 keep_all 却声称自己是 keep_better 的记录,是对别人
                # 方法的一句假话,所以两个都写出来。
                "text": "keep_all by default: every compiled variant enters the "
                        "archive. keep_better is opt-in (--update_archive) and retains "
                        "a variant within noise_leeway=0.1 of the *original* score, "
                        "never of the best archive member",
                "source": "DGM_outer.py:174-190; CLI default at :232; "
                          "noise_leeway at :238",
                "calibrated": False,
            },
            "editable_surface": {
                "declared_editable": EDITABLE,
                "declared_in": "prompts/self_improvement_prompt.py:8-13",
                "search_parameters_editable_by_search": False,
                "evidence": (
                    "archive management (update_archive, get_full_eval_threshold) "
                    "lives in DGM_outer.py, which is not among the paths the prompt "
                    "names as editable, so a variant cannot change how variants "
                    "are selected"
                ),
            },
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--out", default="trajectory.json")
    args = ap.parse_args()

    traj = to_trajectory(Path(args.repo), Path(args.run_root))
    Path(args.out).write_text(json.dumps(traj, indent=1))
    print(f"{len(traj['steps'])} variant(s) -> {args.out}")
    print(f"shape={traj['trajectory_shape']}  editable={EDITABLE}")
    for s in traj["steps"][:5]:
        m = s["method_reported"]
        print(f"  {s['label']:22} score={m['score']}  best={m['best_so_far']}  "
              f"files={len(s['files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
