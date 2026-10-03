#!/usr/bin/env python3
"""Adapter: HyperAgents' own runs -> a HarnessGrad trajectory.

Read-only with respect to HyperAgents. This is the fourth shape and the one that
tests the platform's boundary vocabulary hardest, because HyperAgents does not
merely allow self-modification -- it **names the boundary in its patch tooling**:

  * `utils/gl_utils.py:416-436`  `process_meta_patch_files(..., reset_task_agent,
    reset_meta_agent)` filters a submitted patch by filename:
    `["task_agent.py"]` and `["meta_agent.py"]`. One patch may touch both, and
    either can be reverted on its own.
  * `gl_utils.py:363-364`  `meta_agent.py` and `run_meta_agent.py` can be
    removed outright when resetting the meta level.
  * `generate_loop.py:454-465`  per-generation metadata carries
    `current_genid`, `parent_genid`, `agent_archive_path`, `prev_patch_files`,
    `curr_patch_files` and `can_select_next_parent` -- so the trajectory is a
    **tree with a per-generation parent link**, and the patches are retained.
  * `gl_utils.py:65-83`  `get_score(domain, output_dir, genid, split="train")`
    reads `gen_<id>/<domain>_eval[_<split>]/report.json` and normalizes. A
    held-out split discipline is already present in the method.

And yet, per its own Limitations, components of the open-ended exploration loop
-- "parent selection, evaluation protocols" -- remain fixed, and it "cannot alter
the outer process that determines which agents are selected or how they are
evaluated".

So this method is the cleanest available demonstration of why the platform
records the boundary instead of asking a method to declare it: four methods, four
different boundaries, and all four are visible in what the diff touched.

Usage:
    python adapters/hyperagents.py --run-root <output_dir> --out trajectory.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

# The two files HyperAgents itself treats as the boundary.
TASK_AGENT = "task_agent.py"
META_AGENT = "meta_agent.py"


def find_generations(run_root: Path) -> list[Path]:
    out = []
    for d in run_root.glob("gen_*"):
        if d.is_dir() and (d / "metadata.json").exists():
            out.append(d)
    return sorted(out, key=lambda p: p.name)


def read_gen(d: Path, domain: str | None) -> dict:
    md = json.loads((d / "metadata.json").read_text())
    score, split_used = None, None
    candidates = []
    if domain:
        candidates = [f"{domain}_eval", f"{domain}_eval_test", f"{domain}_eval_val"]
    for sub in candidates or sorted(p.name for p in d.iterdir() if p.is_dir()):
        rep = d / sub / "report.json"
        if rep.exists():
            r = json.loads(rep.read_text())
            # The key is domain-specific (gl_utils.get_domain_score_key); take the
            # first numeric that is not an environment listing rather than
            # guessing a schema the method never fixed.
            for k, v in r.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if isinstance(v, float) and math.isnan(v):
                        continue
                    score, split_used = v, sub
                    break
            break
    return {
        "genid": md.get("current_genid", d.name),
        "parent_genid": md.get("parent_genid"),
        "score": score,
        "split_used": split_used,
        "patch_files": md.get("curr_patch_files") or [],
        "archive_path": md.get("agent_archive_path"),
        "can_select_next_parent": md.get("can_select_next_parent"),
        "metadata": md,
    }


def extract_generation(gen_dir: Path) -> dict[str, str]:
    """The two files HyperAgents itself treats as the boundary.

    `utils/gl_utils.py:70-75` copies `task_agent.py`, `run_task_agent.py` and
    `meta_agent.py` from the generation's root into the container, so those are
    the files a generation's harness consists of. Extracting exactly them keeps
    the outer loop (generation management, selection) out of what the platform
    would measure.
    """
    files: dict[str, str] = {}
    for name in ("task_agent.py", "run_task_agent.py", "meta_agent.py"):
        f = gen_dir / name
        if f.is_file():
            try:
                files[name] = f.read_text()
            except (UnicodeDecodeError, OSError):
                continue
    return files


def classify_patch(patch_files: list[str]) -> dict:
    """Which side of the boundary a generation's patch moved.

    This is the reading the whole platform exists to produce, and here it can be
    read off the patch itself: a patch touching `meta_agent.py` changed the
    improvement mechanism, one touching only `task_agent.py` did not.
    """
    touched_meta = touched_task = False
    for pf in patch_files:
        try:
            text = Path(pf).read_text()
        except OSError:
            continue
        if META_AGENT in text:
            touched_meta = True
        if TASK_AGENT in text:
            touched_task = True
    return {
        "touched_task_agent": touched_task,
        "touched_meta_agent": touched_meta,
        "changed_own_search_mechanism": touched_meta,
    }


def find_gen_dir(run_root: Path, genid) -> Path | None:
    for d in run_root.glob(f"gen_*{genid}*"):
        if d.is_dir():
            return d
    return None


def to_trajectory(run_root: Path, domain: str | None) -> dict:
    gens = [read_gen(d, domain) for d in find_generations(run_root)]
    if not gens:
        raise SystemExit(f"no gen_*/metadata.json under {run_root}; has it run?")

    steps = []
    for g in gens:
        boundary = classify_patch(g["patch_files"])
        files = extract_generation(d) if (d := find_gen_dir(run_root, g["genid"])) else {}
        steps.append({
            "files": files,
            "label": f"gen {g['genid']} (parent {g['parent_genid']})",
            # The method's own tooling names the boundary, so the kind of an
            # edit is readable off the patch: which of the two files it moved.
            "edit_kind": ("meta_agent"
                          if boundary["touched_meta_agent"]
                          else "task_agent" if boundary["touched_task_agent"]
                          else "none"),
            "claimed_cost": {"note": "per-generation cost not reported"},
            "method_reported": {
                "score": g["score"],
                "split_used": g["split_used"],
                "parent_genid": g["parent_genid"],
                "boundary": boundary,
                "materializable_files": sorted(files),
                "materializable": bool(files),
                "measurable_by_platform": False,
                "score_provenance": "method-reported (not platform-measured)",
                "not_measurable_because": (
                    "generations are copied into a per-run Docker container by "
                    "the method's own loop; the files are extractable but the "
                    "harness is not a freestanding entrypoint"
                ),
            },
        })

    return {
        "steps": steps,
        "nominated": len(steps) - 1,
        "trajectory_shape": "tree",
        "curve_drawn": "per_generation",
        "rhythm": {
            "note": "HyperAgents owns its loop; generations form a tree via "
                    "parent_genid, with selection fixed outside the patch surface",
        },
        "selected_on_reported_set": None,
        "selection_pool_size": len(steps),
        "provenance": {
            "method": "HyperAgents",
            "source": "facebookresearch/Hyperagents",
            "acceptance_rule": (
                "parent selection is fixed outside the patchable surface; the "
                "method's own Limitations report that selection and evaluation "
                "'remain fixed'"
            ),
            "editable_surface": {
                "declared_editable": f"{TASK_AGENT} and {META_AGENT}",
                "search_parameters_editable_by_search": "partly",
                "evidence": (
                    "process_meta_patch_files filters patches by filename "
                    "(utils/gl_utils.py:416-436), so the improvement mechanism "
                    "is patchable and independently revertible; but the parent "
                    "selection and evaluation protocols are not, per the paper's "
                    "Limitations"
                ),
            },
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--domain", default=None,
                    help="e.g. polyglot; used to locate <domain>_eval/report.json")
    ap.add_argument("--out", default="trajectory.json")
    args = ap.parse_args()

    traj = to_trajectory(Path(args.run_root), args.domain)
    Path(args.out).write_text(json.dumps(traj, indent=1))
    print(f"{len(traj['steps'])} generation(s) -> {args.out}")
    print(f"shape={traj['trajectory_shape']}")
    for s in traj["steps"][:5]:
        b = s["method_reported"]["boundary"]
        print(f"  {s['label']}  score={s['method_reported']['score']}  "
              f"touched_meta={b['touched_meta_agent']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
