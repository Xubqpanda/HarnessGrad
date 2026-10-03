#!/usr/bin/env python3
"""Adapter: SICA's own runs -> a HarnessGrad trajectory.

Read-only with respect to SICA. SICA is the third distinct shape, and it is the
one that makes the platform's boundary question answer itself.

  * `base_agent/__main__.py:150`  the meta-improvement prompt: *"make the coding
    agent system placed in /home/sandbox/workdir better at writing code"* --
    the directory handed to the improver IS the agent's own source.
  * `runner.py:470`  `agent_module = str(agent_dir / "agent_code")` -- so the
    artifact under improvement is `<experiment>/agent_<i>/agent_code/`.
  * `runner.py:88-148`  `select_base_agent()` picks which previous iteration to
    build on using **the lower confidence bound** of the best iteration:
    `best_lower_bound = best_stats["ci_lower"]`, then takes the newest iteration
    whose mean clears it. Unlike RRSI and DGM this method does not compare to a
    hand-set noise constant; it calibrates against its own measured dispersion.

State representation differs from the other two in a way worth recording: SICA
copies **the whole codebase per iteration** (`<experiment>/agent_<i>/`) rather
than checking out a commit. That is fine for the platform -- each iteration
directory is a tree that can be committed -- but it means the unit of state is
"a copy", not "a revision", and the adapter has to say so.

Usage:
    python adapters/sica.py --run-root <sica>/results/run_<id> --out trajectory.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def find_iterations(run_root: Path) -> list[Path]:
    """`agent_<i>` directories, ordered by i."""
    out = []
    for d in run_root.glob("agent_*"):
        if d.is_dir() and (d / "agent_code").is_dir():
            out.append(d)
    return sorted(out, key=lambda p: int(p.name.split("_")[1]))


def read_change_log(agent_code: Path) -> list[dict]:
    """SICA keeps a markdown change log inside its own source.

    `base_agent/agent_change_log.md` is a table of
    `| Iteration | Change Name | Was Successful? (pending/yes/no) |`, written by
    the agent itself. Its "Feature Outcome" section is filled in at the NEXT
    iteration, so the log is the closest thing SICA has to per-edit attribution
    -- and it is prose, authored by the subject.
    """
    log = agent_code / "agent_change_log.md"
    if not log.exists():
        return []
    rows = []
    for line in log.read_text().splitlines():
        if line.startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 3 and cells[0].lower() != "iteration":
                rows.append({"iteration": cells[0], "change": cells[1],
                             "successful": cells[2]})
    return rows


def extract_agent_code(code_dir: Path) -> dict[str, str]:
    """Every file of the agent's own source, as {path: contents}.

    This is the shape the platform has to accommodate for SICA: the improver's
    working directory IS the artifact (`base_agent/__main__.py:150`), so the
    state is a directory copy rather than a revision, and materializing it means
    copying the tree that iteration produced.
    """
    files: dict[str, str] = {}
    for f in sorted(code_dir.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        try:
            files[str(f.relative_to(code_dir))] = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
    return files


def read_score(agent_dir: Path, benchmark: str = "swebench") -> dict:
    """Per-iteration score, if the run left one.

    SICA writes per-problem results under
    `<agent_dir>/benchmarks/<benchmark>/`, and the aggregate is computed by
    `generate_benchmark_statistics` (runner.py:605). The adapter reads whatever
    is present rather than assuming a schema the method never promised.
    """
    base = agent_dir / "benchmarks" / benchmark
    out: dict = {}
    for name in ("results.jsonl", "statistics.json", "summary.json"):
        p = base / name
        if p.exists():
            out[name] = "present"
    return out


def to_trajectory(run_root: Path) -> dict:
    iters = find_iterations(run_root)
    if not iters:
        raise SystemExit(
            f"no agent_*/agent_code under {run_root}; has SICA run yet?")

    meta_path = run_root / "metadata.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    steps = []
    for d in iters:
        code = d / "agent_code"
        i = int(d.name.split("_")[1])
        files = extract_agent_code(code)
        steps.append({
            "files": files,
            "label": f"agent_{i}",
            "edit_kind": "self_edit",
            "claimed_cost": {
                "note": "SICA reports run-level USD (~7,000 / 15 iterations); "
                        "per-iteration cost is not reported",
            },
            "method_reported": {
                "iteration": i,
                "has_change_log": (code / "agent_change_log.md").exists(),
                "artifacts": read_score(d),
                "materializable_files": sorted(files),
                "materializable": bool(files),
                "measurable_by_platform": False,
                "score_provenance": "method-reported (not platform-measured)",
                "not_measurable_because": (
                    "the agent runs under the method's own benchmark runner with "
                    "Docker and per-benchmark config; it is not a freestanding "
                    "entrypoint the platform can invoke"
                ),
            },
        })

    return {
        "steps": steps,
        "nominated": len(steps) - 1,
        "trajectory_shape": "sequence_of_copies",
        "curve_drawn": "per_iteration",
        "rhythm": {
            "note": "SICA owns its loop; one iteration = one full copy of the "
                    "agent codebase",
        },
        "selected_on_reported_set": None,
        "selection_pool_size": len(steps),
        "provenance": {
            "method": "SICA",
            "source": "MaximeRobeyns/self_improving_coding_agent",
            "experiment": meta,
            "acceptance_rule": (
                "select_base_agent() picks the newest iteration whose mean "
                "clears the best iteration's lower confidence bound "
                "(runner.py:88-148) -- calibrated against measured dispersion, "
                "not a hand-set constant"
            ),
            "editable_surface": {
                "declared_editable": "the whole agent codebase "
                                     "(<exp>/agent_<i>/agent_code/)",
                "search_parameters_editable_by_search": True,
                "evidence": (
                    "the meta-improvement prompt points the improver at its own "
                    "source (base_agent/__main__.py:150) and the selection rule "
                    "lives in the same tree (runner.py), so this method can "
                    "rewrite its own acceptance rule by construction -- it never "
                    "drew a boundary rather than crossing one"
                ),
            },
            "change_log": read_change_log(iters[-1] / "agent_code"),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--out", default="trajectory.json")
    args = ap.parse_args()

    traj = to_trajectory(Path(args.run_root))
    Path(args.out).write_text(json.dumps(traj, indent=1))
    print(f"{len(traj['steps'])} iteration(s) -> {args.out}")
    print(f"shape={traj['trajectory_shape']}")
    surf = traj["provenance"]["editable_surface"]
    print(f"search rule editable by the method: "
          f"{surf['search_parameters_editable_by_search']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
