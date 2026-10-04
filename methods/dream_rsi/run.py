#!/usr/bin/env python3
"""Dream-RSI's selection rule, as a HarnessGrad method.

The rule, quoted from the paper
-------------------------------
*Dream-RSI: Recursive Self-Improvement through Evolving Worlds* (arXiv 2609.14858, Google /
UMD / Google DeepMind / UVA, 2026-09) makes the exploration policy **executable code** and
keeps an exact **replay simulator** of completed discovery trees, so candidate policies can
be scored off-policy at zero execution cost. Its selection step is stated verbatim in §3:

    V_i^m = max_{v in T_i^{m,*}} s_v - b1*N_i^m + b2*N_i^m / max(1, k_i^{m,*})
    V^m   = (1/t) * sum_{i=1..t} V_i^m
    pi_{t+1} = pi_t^{m*},  m* in argmax_{m in {0..M-1}} V^m
    "Because the candidate set includes the current policy, this selection satisfies
     V^{m*} >= V^0."

**Why this port fits a platform that cannot reject a candidate.** Every other method here has
to apologise for mode A's missing reject step: Dream-RSI does not need one, because its
accept/reject applies to the **policy** -- which version to deploy next -- not to a proposal.
Keeping the incumbent inside the candidate set is the whole of its no-regression guarantee,
and this platform already keeps every measured round on the curve with the last one being the
incumbent. So the port is exact where it matters:

* `versions()` computes `V` for **every** measured round, including the incumbent.
* `select()` takes `argmax V` over that set, so `V* >= V_incumbent` **by construction** --
  and the record carries `guarantee_holds` as a checked fact rather than a claim.
* `base_round()` makes the selection executable at mode A's granularity: the next candidate
  is built on the selected round's staged state (from `states/index.json`, never a guessed
  path), exactly as HarnessX's revert and Meta-Harness's frontier do.

What is transplanted
--------------------
* The `V` formula, with the cost term in **kilo-tokens** so `b1` is a readable number
  (`HG_DREAM_RSI_BETA1`, default 0.05 per 1000 harness tokens) and the parallelism term
  (`HG_DREAM_RSI_BETA2`, default 0.0 -- see the honest limit below).
* The selection over all versions with the incumbent in the set, and the guarantee.
* The base decision that follows from it.

What cannot be transplanted, stated plainly
-------------------------------------------
1. **The replay simulator.** Their `max_{v in T^{m,*}} s_v` is a max over the nodes of a
   recorded discovery tree, and their "zero execution cost" comes from replaying it. This
   platform records one score per round, not a tree, so `V^m` is the round's own measured
   quality: the port is a **fixed-test-set best-of comparison**, which is what their rule
   degrades to without the tree. Still the useful half -- the selection and its guarantee --
   but not the free lunch.
2. **The parallelism bonus cannot vary.** `k_i^m` is how many branches ran in parallel at a
   node. Mode A measures one candidate per round, so `k` is the run's `--trials` count, which
   is the platform's setting and not the method's; with `k` constant the `b2` term is a
   constant multiple of the cost term. It is therefore **0.0 by default** and its value is
   recorded, rather than shipped as a knob that pretends to shape the search.
3. **The offline phase.** They score `M` revisions per phase with no execution; here each
   round is one revision, and every one of them is paid for.
4. **The world stream.** Their `t` grows as new worlds arrive and the average is over
   worlds; this platform's task set is fixed by H0, so `t` is the number of tasks and it is
   already folded into the round's score.

Which score the rule reads
--------------------------
The studied side only, through `protocol.studied_score` / `protocol.studied_per_task`. Their
`V` is a policy score on a replay history; reading the *exam* side here would be the leak
`INTERFACE.md` §2.3 exists to prevent.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import editor                                              # noqa: E402
import protocol                                            # noqa: E402

ACCEPTANCE_RULE = (
    "Dream-RSI selects the version with the highest V from a candidate set that **includes "
    "the incumbent**, which is exactly why its guarantee V* >= V^0 holds. mode A has no "
    "reject step, so this file computes V for every measured round, records the argmax and "
    "the guarantee it checks, and builds the next candidate on the selected state; it never "
    "vetoes a measurement."
)

DREAM_RSI_SYSTEM = """You are improving an agent harness.

Dream-RSI's contribution is which *version* to build the next one on, not the edit itself:
the table below gives every measured version's cost-adjusted score V (quality minus the cost
of the tokens it spent, plus a parallelism term), and the version with the highest V is the
one this round edits. The incumbent is always in that comparison, which is what makes the
selection non-regressing.

Propose ONE bounded edit sequence for the version named above. Prefer targeted replacements
where you can name the text being replaced. Return the JSON envelope described below and
nothing else.
"""


# --------------------------------------------------------------- the rule ---

def config() -> dict:
    """`b1`, `b2` and the cost unit, from the environment, with what they mean.

    `b1` is per **1000 harness tokens**, because the formula mixes a score in [0, 1] with a
    token count in the hundreds of thousands; the paper states no units, and a scaled
    coefficient that is recorded beats a raw one nobody can read.

    **The default was measured down from 0.05.** At 0.05 a 100-kilo-token round costs 5.0 --
    five times the whole quality range -- so `argmax V` silently became "use the fewest
    tokens", which is not the rule: the paper's cost term is a penalty on spending *more for
    no gain*, not the objective. `0.001` per kilo-token makes 100k tokens cost 0.1 of score:
    real enough to break a tie between two versions of equal quality, small enough that a
    genuine quality gain survives it. A run that wants cost to dominate says so with
    `HG_DREAM_RSI_BETA1`, and the value used is on every point (`beta`).
    """
    def number(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    return {"b1": number("HG_DREAM_RSI_BETA1", 0.001),
            "b2": number("HG_DREAM_RSI_BETA2", 0.0),
            "kilo_tokens": 1000.0}


def version_row(point: dict, cfg: dict) -> dict:
    """One version's `V`, and the three numbers it came from.

    `quality` is the studied side's score. `cost` is the harness's own token spend in
    kilo-tokens -- `None` when no task reported any, because **not reported is not zero**:
    feeding 0.0 into a cost rule lets a rule that never fired look like one that passed (the
    same defect `methods/rrsi/run.py` documents). With no cost the formula degrades to
    `V = quality` and `cost_source` says so.
    """
    quality = protocol.studied_score(point)
    cost = (point.get("cost") or {})
    tokens = cost.get("harness_tokens")
    k = int(point.get("n_trials") or 1)
    row = {"round": point.get("round"), "quality": quality,
           "cost_kilo_tokens": None, "k": k, "v": quality, "cost_source": "not reported"}
    if quality is None:
        return row
    if isinstance(tokens, (int, float)) and tokens > 0:
        kilo = float(tokens) / cfg["kilo_tokens"]
        row["cost_kilo_tokens"] = round(kilo, 4)
        row["cost_source"] = "harness_tokens"
        row["v"] = round(quality - cfg["b1"] * kilo
                         + cfg["b2"] * kilo / max(1, k), 6)
    return row


def select(history: list[dict], cfg: dict | None = None) -> dict:
    """`argmax V` over every measured round, with the incumbent inside the set.

    The guarantee is not asserted from the formula: it is **checked** here and recorded, so a
    reader can see that the selected version is not worse than the incumbent *on this run's
    own numbers* rather than trusting the argument. (It holds by construction -- the incumbent
    is a candidate -- which is precisely why a check is cheap and worth having: it fails the
    day someone changes `versions` to exclude the incumbent.)
    """
    cfg = cfg or config()
    rows = [version_row(p, cfg) for p in history]
    scored = [r for r in rows if r["v"] is not None]
    incumbent = next((r for r in reversed(scored)
                      if r["round"] == (history[-1].get("round") if history else None)),
                     scored[-1] if scored else None)
    best = max(scored, key=lambda r: (r["v"], -(r["round"] or 0))) if scored else None
    return {
        "kind": "dream_rsi_versions_v1",
        "beta": {k: cfg[k] for k in ("b1", "b2")},
        "versions": rows,
        "selected": (best or {}).get("round"),
        "v_selected": (best or {}).get("v"),
        "incumbent": (incumbent or {}).get("round"),
        "v_incumbent": (incumbent or {}).get("v"),
        "guarantee_holds": bool(best and incumbent and best["v"] >= incumbent["v"] - 1e-9),
        "cost_reported_by_rounds": sum(1 for r in rows if r["cost_kilo_tokens"] is not None),
    }


def _state_dir(channel_base: Path, round_no) -> str | None:
    """Where the platform staged a round's harness, from the index (never a guessed path)."""
    index = editor.channel(channel_base) / "states" / "index.json"
    if not index.exists():
        return None
    try:
        data = json.loads(index.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return (data.get(f"round-{round_no}") or {}).get("harness_dir")


def base_round(selection: dict, history: list[dict], channel_base: Path,
               ) -> tuple[Path, str]:
    """The version this round edits: the argmax-`V` one, when its state is staged.

    Dream-RSI *deploys* the selected version. Mode A's expression of "deploy" is "build the
    next candidate on that version's tree", so this is the executable half of the rule. An
    unstaged selection falls back to the incumbent and says so -- the frontier that silently
    falls back is the one that lies about what it edited.
    """
    selected = selection.get("selected")
    if selected is None:
        return channel_base, "no measured version yet: this round edits the incumbent"
    incumbent_round = history[-1].get("round") if history else None
    if selected == incumbent_round:
        return channel_base, (f"r{selected} has the highest V and is already the incumbent")
    staged = _state_dir(channel_base, selected)
    if staged and Path(staged).is_dir():
        return Path(staged), (
            f"r{selected} has the highest V ({selection.get('v_selected')} vs the "
            f"incumbent's {selection.get('v_incumbent')}), so this round revises that "
            f"version's staged state")
    return channel_base, (
        f"r{selected} has the highest V but its state is not staged (outside the window, or "
        f"it did not materialize); this round revises the incumbent r{incumbent_round}")


def build_prompt(edit_base: Path, channel_base: Path, req: dict, selection: dict,
                 base_why: str) -> str:
    """The version table, the selected one, and the harness sources of the tree being edited.

    Sources come from the **edit base** (which may be a staged state) and the channel from the
    incumbent: a staged state is a harness commit with no `_harnessgrad/` in it, so reading the
    traces from the edit base would show the proposer nothing on exactly the rounds where the
    selection moved.
    """
    sources = editor.load_sources(edit_base)
    traces = editor.load_traces(channel_base, limit=6,
                                order=editor.failing_first(channel_base))
    table = "\n".join(
        f"| r{r['round']} | {r['quality']} | {r['cost_kilo_tokens']} | {r['k']} | {r['v']} |"
        for r in selection["versions"])
    return (
        f"Round {req.get('round_index', 1)}. Dream-RSI's version comparison "
        f"(b1={selection['beta']['b1']}/kilo-token, b2={selection['beta']['b2']}):\n\n"
        f"| version | quality | cost (kilo-tokens) | k | V |\n"
        f"| --- | --- | --- | --- | --- |\n{table}\n\n"
        f"selected: r{selection['selected']} (V={selection['v_selected']}), "
        f"incumbent: r{selection['incumbent']} (V={selection['v_incumbent']}), "
        f"guarantee V* >= V^0 holds: {selection['guarantee_holds']}\n\n"
        f"## Why this round edits what it edits\n{base_why}\n\n"
        f"## What the harness did last round\n{traces}\n\n"
        "## Current harness sources\n"
        + "\n\n".join(f"### {k}\n```\n{v}\n```" for k, v in sources.items())
        + "\n\nPropose one edit for the selected version, or say no_change."
    )


# ------------------------------------------------------------------ main ---

def main() -> int:
    req = editor.read_request()
    editor.require_api(req)

    incumbent = Path(req["base_harness"])
    history = editor.load_history(incumbent)
    cfg = config()
    selection = select(history, cfg)
    edit_base, base_why = base_round(selection, history, incumbent)

    def report(**kw):
        return editor.report(
            req, method="dream_rsi", harness_dir=kw.pop("harness_dir", incumbent),
            method_reported={"dream_rsi": selection, "dream_rsi_base": base_why,
                             **kw.pop("reported", {})},
            acceptance_rule=ACCEPTANCE_RULE,
            extra={"selection_rule": "argmax V over all measured versions, incumbent "
                                     "included",
                   "v_selected": selection["v_selected"],
                   "v_incumbent": selection["v_incumbent"],
                   "guarantee_holds": selection["guarantee_holds"]},
            **kw)

    dest = editor.candidate_path(req)
    try:
        reply, dest, apply_problems = editor.propose_and_apply(
            edit_base, req, DREAM_RSI_SYSTEM,
            lambda problems: build_prompt(edit_base, incumbent, req, selection, base_why)
                             + editor.repair_block(problems, expect=editor.EDIT_EXPECT))
        if apply_problems:
            return report(harness_dir=dest, changed=False, stop=False, edit_kind="none",
                          hypothesis=f"the edit sequence did not apply: {apply_problems}",
                          reported={"rejected": apply_problems})
    except SystemExit:
        raise
    except Exception as exc:                                   # noqa: BLE001
        return editor.fail(req, method="dream_rsi", exc=exc, base=incumbent,
                           state={"dream_rsi": selection, "dream_rsi_base": base_why})

    if isinstance(reply, dict) and "no_change" in reply:
        editor.apply_edits(edit_base, dest, [])
        return report(harness_dir=dest, changed=False, stop=False, edit_kind="none",
                      hypothesis=str(reply["no_change"])[:200],
                      label="model: no change")

    changed, files, note = editor.apply_edits(edit_base, dest, reply.get("files"))
    if not changed:
        return report(harness_dir=dest, changed=False, stop=False, edit_kind="none",
                      hypothesis=f"unusable proposal: {note}",
                      reported={"rejected": note}, label="unusable proposal")

    hypothesis = editor.hypothesis_of(reply)[:200]
    return report(harness_dir=dest, changed=True, files=files, hypothesis=hypothesis,
                  edit_kind="dream_rsi_rule",
                  label=(f"[dream-rsi r{selection['selected']} "
                         f"V={selection['v_selected']}] {hypothesis[:50]}"))


if __name__ == "__main__":
    raise SystemExit(main())
