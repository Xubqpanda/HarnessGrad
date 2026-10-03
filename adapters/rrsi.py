#!/usr/bin/env python3
"""Adapter: RRSI's own runs -> a HarnessGrad trajectory, with materialized states.

Read-only with respect to RRSI. It reads the run RRSI already wrote, extracts the
harness state from RRSI's own git, and restates both in the platform's
vocabulary. It does not run RRSI, re-implement it, or edit it.

Where RRSI keeps its state
--------------------------
`rrsi/loop.py:89` builds `harness_rel = domains/<domain>/<domain.harness_path>`,
and `domains/coding/adapter.py:108` sets `harness_path = "../../third_party/harbor_terminus2"`,
which normalizes to **`third_party/harbor_terminus2` at the repository root**.
`rrsi/loop.py:130` then defines the harness state as
`git rev-parse <ref>:third_party/harbor_terminus2` -- a git tree hash. So the
state RRSI records in `frontier.json` as `harness_tree` is a real, extractable
tree, and this adapter extracts it.

The boundary RRSI draws
-----------------------
The search parameters are a CLI-only table (`rrsi.py:61-70`) and the editable
surface is pinned to one directory that does not contain the search code, so the
answer to "can this method change its own search rule?" is structurally no. That
reading comes from the configuration, not from a diff, and it needs no run.

Usage:
    python adapters/rrsi.py --repo <rrsi> --run-root <rrsi>/runs/coding \
        --out trajectory.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

HARNESS_REL = "third_party/harbor_terminus2"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).stdout


def extract_tree(repo: Path, ref: str, rel: str = HARNESS_REL) -> dict[str, str]:
    """Every file under `rel` at `ref`, as {relative path: contents}.

    This is what the platform needs in order to *measure* a state rather than
    merely name it: an external run names something in its own git, and the
    evaluator reads a working tree.
    """
    listing = git(repo, "ls-tree", "-r", "--name-only", f"{ref}:{rel}")
    if not listing.strip():
        return {}
    files: dict[str, str] = {}
    for name in listing.splitlines():
        if not name.strip():
            continue
        blob = subprocess.run(
            ["git", "-C", str(repo), "show", f"{ref}:{rel}/{name}"],
            capture_output=True, text=True)
        if blob.returncode == 0:
            files[name] = blob.stdout
    return files


def read_frontier(run_root: Path) -> dict:
    path = run_root / "frontier.json"
    if not path.exists():
        raise SystemExit(f"no frontier.json under {run_root}; has RRSI run yet?")
    return json.loads(path.read_text())


def read_history(run_root: Path) -> list[dict]:
    """`history.jsonl` carries the component tag per edit, which frontier.json
    does not. It is the only place RRSI says what kind of thing an edit was."""
    path = run_root / "history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def editable_surface(frontier: dict) -> dict:
    return {
        "declared_editable": HARNESS_REL,
        "search_parameters": sorted(frontier.get("config", {})),
        "search_parameters_location": "domains/<domain>/rrsi.json + rrsi/config.py",
        "search_parameters_editable_by_search": False,
        "evidence": (
            "harness_path pins the editable surface to one directory that does "
            "not contain the search code (domains/coding/adapter.py:108), and the "
            "parameter table is consumed as CLI arguments (rrsi.py:61-70), so the "
            "search loop has no entry point to it"
        ),
    }


def to_trajectory(repo: Path, run_root: Path) -> dict:
    frontier = read_frontier(run_root)
    history = read_history(run_root)
    by_t = {h.get("t"): h for h in history if h.get("t") is not None}

    incumbent = frontier.get("incumbent", {})
    steps = []
    for entry in frontier.get("trajectory", []):
        t = entry.get("t")
        commit = entry.get("commit")
        h = by_t.get(t, {})
        # `harness_tree` is recorded on the incumbent, not on each trajectory
        # entry; for the incumbent's own round it is the value that applies.
        recorded_tree = (entry.get("harness_tree")
                         or (incumbent.get("harness_tree")
                             if t == incumbent.get("t") else None))
        _files = extract_tree(repo, commit)
        # RRSI's harness has no executable entry point -- it is a module tree
        # driven by harbor's own runner (`terminus_2.py`, no `__main__`). The
        # platform cannot invoke it, so the state is reported as materializable
        # but not measurable rather than being handed over under a fabricated
        # manifest. Writing these files would also overwrite the platform's own
        # entrypoint with a module that does nothing standalone.
        files = {}
        actual = git(repo, "rev-parse", f"{commit}:{HARNESS_REL}").strip()[:12]
        steps.append({
            # The state itself, so the platform can measure rather than list it.
            "files": files,
            "label": f"t={t} {h.get('outcome') or ''}".strip(),
            # RRSI tags each accepted edit with a component from a fixed
            # vocabulary of nine (rrsi/components.py:44-46). It is the only
            # method in the set that emits such a tag at all, and its tag is
            # recovered from the diff by regex when the model's declaration is
            # invalid (rrsi/components.py:95-100) -- which the platform records
            # as-is rather than judging.
            "edit_kind": h.get("component"),
            "claimed_cost": {
                "inference_tokens_per_trial": entry.get("C"),
                "note": "this method does not report the cost of the search itself",
            },
            "method_reported": {
                "score": entry.get("S"),
                "score_provenance": "method-reported (not platform-measured)",
                "materializable_files": sorted(_files),
                "materializable": bool(_files),
                "measurable_by_platform": False,
                "not_measurable_because": (
                    "the harness is a module tree with no executable entry "
                    "point; it is driven by the method's own runner"
                ),
                "component": h.get("component"),
                "hypothesis": h.get("hypothesis"),
                "accepted": h.get("accepted"),
                "delta_S": h.get("delta_S"),
                "harness_tree_recorded": recorded_tree,
                "harness_tree_extracted": actual,
                "tree_matches": recorded_tree == actual if recorded_tree else None,
            },
        })

    return {
        "steps": steps,
        "nominated": len(steps) - 1 if steps else None,
        "trajectory_shape": "sequence",
        "curve_drawn": "incumbent_per_round",
        "rhythm": {
            "of_rounds_T": (frontier.get("config") or {}).get("T"),
            "candidates_per_round_k": (frontier.get("config") or {}).get("k"),
            "note": "this method owns its loop; the platform did not schedule it",
        },
        "selected_on_reported_set": None,
        "selection_pool_size": len(steps),
        "provenance": {
            "method": "RRSI",
            "source": "google-research/rrsi",
            "domain": frontier.get("domain"),
            "S_star": frontier.get("S_star"),
            "incumbent_score": incumbent.get("S"),
            "acceptance_rule": {
                "text": "noise-adjusted floor S >= S* - delta, plus a "
                        "gain-dependent cost rule and a within-band rule",
                "source": "rrsi/loop.py selection; delta calibrated by "
                          "rrsi/calibrate.py from the null distribution",
                "calibrated": True,
            },
            "editable_surface": editable_surface(frontier),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="the RRSI checkout")
    ap.add_argument("--run-root", required=True, help="<rrsi>/runs/<domain>")
    ap.add_argument("--out", default="trajectory.json")
    args = ap.parse_args()

    traj = to_trajectory(Path(args.repo), Path(args.run_root))
    Path(args.out).write_text(json.dumps(traj, indent=1))
    print(f"{len(traj['steps'])} step(s) -> {args.out}")
    for s in traj["steps"]:
        m = s["method_reported"]
        print(f"  {s['label']:22} score={m['score']}  "
              f"files={len(s['files'])}  tree_matches={m['tree_matches']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
