#!/usr/bin/env python3
"""Adapter: the Meta-Harness artifact -> a HarnessGrad trajectory.

This is the ninth shape and the limiting case of the whole exercise: **a method
that published its output and not its process**, and whose output cannot be
measured outside the environment it was built for.

What it is
----------
One file, `agent.py`, 1321 lines, defining `class AgentHarness(Terminus2)`
(`:218`). Its own README is unusually direct about both halves of that:

  * *"Meta-Harness extends the Terminus-KIRA agent with **environment
    bootstrapping**: before the agent loop starts, it gathers a snapshot of the
    sandbox environment … and injects it into the initial prompt. This saves 2-5
    early exploration turns."*
  * *"The agent was discovered through automated harness evolution. More details
    coming soon."*

So the contribution is a small delta over an inherited harness, and the search that
found it is not in the release. That matches what the literature review turned up
independently: the winning Terminal-Bench harness *"inherits Terminus-KIRA's native
tool calling, 30KB output cap, and multi-perspective completion checklist"*, and
what Meta-Harness adds is bootstrapping.

Why it cannot be compiled into a measurable harness
---------------------------------------------------
Every other method in this set hands over something a platform can *invoke*: a
config, a class with a `solve(task)` method, a set of declarations for a named
engine. This one does not, and the reason is structural rather than a missing
dependency:

    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None      # agent.py:298

The harness's own contribution **requires** the environment to be a first-class
argument, because `_gather_env_snapshot` (`:873`) runs a command inside the sandbox
to build the snapshot it injects. A `BaseEnvironment` is not something an adapter
can synthesize from a working directory; it is harbor's sandbox abstraction. So
writing an entrypoint here would not be "connecting" -- it would mean
re-implementing the substrate the harness is a harness *for*.

That is the finding, and it is worth more than a working adapter would have been:
**the platform's unit of exchange is a directory that can solve a task, and a
harness whose interface includes its execution environment is not expressible in
it.** The project's own rule covers the case -- a state the platform cannot
materialize is reported, never guessed at -- so this adapter reports.

Usage:
    python adapters/metaharness.py --repo <artifact> --out trajectory.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

#: What the artifact would need before a platform could measure it. Recorded as
#: data so a consumer can act on it rather than re-deriving it from prose.
UNMEASURABLE_BECAUSE = {
    "structural": (
        "the harness's interface includes the execution environment: "
        "run(instruction, environment: BaseEnvironment, context: AgentContext), "
        "and its contribution (_gather_env_snapshot, agent.py:873) executes a "
        "command inside that sandbox"
    ),
    "not_just_dependencies": (
        "other methods in this set needed an engine or a credential, which a host "
        "can install or be given. This one needs to BE the agent inside harbor's "
        "sandbox protocol, so satisfying it means implementing the substrate"
    ),
    "what_would_be_required": [
        "harbor's sandbox/runtime and the terminal-bench@2.0 task environment",
        "the agent as a harbor AgentHarness, launched through harbor's runner",
        "a model endpoint (the published numbers use anthropic/claude-opus-4-6)",
    ],
}


def inspect(repo: Path) -> dict:
    agent = repo / "agent.py"
    if not agent.is_file():
        raise SystemExit(f"no agent.py under {repo}")

    src = agent.read_text()
    return {
        "files": sorted(str(p.relative_to(repo)) for p in repo.rglob("*")
                        if p.is_file() and "__pycache__" not in p.parts),
        "agent_lines": len(src.splitlines()),
        "class_bases": [
            line.split("class ", 1)[1].split("(", 1)[1].split(")")[0]
            for line in src.splitlines()
            if line.startswith("class AgentHarness(")
        ],
        "external_imports": sorted({
            line.split()[1].split(".")[0]
            for line in src.splitlines()
            if line.startswith(("import ", "from "))
        }),
        # The harness's only named contribution, recorded because a reader of the
        # paper's 76.4% should know that this is what was added to get it.
        "adds": "environment bootstrapping (_gather_env_snapshot)",
        "inherits": "Terminus-KIRA / harbor's Terminus-2",
    }


def to_trajectory(repo: Path) -> dict:
    info = inspect(repo)

    return {
        "steps": [{
            # No directory: there is nothing the platform can invoke. `files: {}`
            # is the honest value and the driver stops the trajectory with a
            # recorded reason rather than scoring a state it does not have.
            "files": {},
            "label": "the published artifact (the only state that exists)",
            "edit_kind": "environment_bootstrap",
            "claimed_cost": {
                "note": "the method reports no search cost; the released README "
                        "reports only the final score",
            },
            "method_reported": {
                "score": 0.764,
                "score_provenance": (
                    "the method's own README: 76.4% on Terminal-Bench 2.0, "
                    "89 tasks x 5 trials, Claude Opus 4.6"
                ),
                "materializable": False,
                "measurable_by_platform": False,
                "not_measurable_because": UNMEASURABLE_BECAUSE["structural"],
            },
        }],
        "nominated": 0,
        # One point is not a curve. Saying so is the point: the released artifact
        # carries no trajectory, and drawing a line through a single point would
        # invent the process the method chose not to publish.
        "trajectory_shape": "single_point",
        "curve_drawn": "none (one state, no process)",
        "rhythm": {
            "note": "not reported: the search that produced this artifact is not "
                    "in the release. Its README says 'more details coming soon'.",
        },
        "selected_on_reported_set": None,
        "selection_pool_size": 1,
        "provenance": {
            "method": "Meta-Harness",
            "source": "stanford-iris-lab/meta-harness-tbench2-artifact",
            "artifact": info,
            "acceptance_rule": {
                "text": "not reported in the release; the accompanying paper "
                        "reports search and final evaluation on the same 89 tasks, "
                        "with overfitting controlled by manual inspection and "
                        "regex audits for task-string leakage",
                "source": "the paper, not this release",
                "calibrated": False,
            },
            "editable_surface": {
                "declared_editable": "the whole harness (it is a single file)",
                "declared_in": "the release contains only the finished artifact",
                "search_parameters_editable_by_search": None,
                "evidence": (
                    "unanswerable from the release: neither the search loop nor its "
                    "configuration is published, so whether the search could modify "
                    "its own rules is not recoverable here"
                ),
            },
            "unmeasurable_because": UNMEASURABLE_BECAUSE,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", default="trajectory.json")
    args = ap.parse_args()

    traj = to_trajectory(Path(args.repo))
    Path(args.out).write_text(json.dumps(traj, indent=1))
    s = traj["steps"][0]
    art = traj["provenance"]["artifact"]
    print(f"1 step (single point) -> {args.out}")
    print(f"  class      : AgentHarness({', '.join(art['class_bases'])})"
          f"  [{art['agent_lines']} lines]")
    print(f"  adds       : {art['adds']}")
    print(f"  inherits   : {art['inherits']}")
    print(f"  imports    : {', '.join(art['external_imports'][:6])}")
    print(f"  measurable : {s['method_reported']['measurable_by_platform']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
