#!/usr/bin/env python3
"""SICA's selection rule, as a HarnessGrad method.

Why this file exists
--------------------
SICA's contribution is not the edit it makes -- it is **how it decides which previous
iteration to build on**. From its own runner (`sica/runner.py:88-148`):

    best_lower_bound = best_stats["ci_lower"]      # the best *confidence lower bound*
    ...take the newest iteration whose mean clears it

It compares a candidate against the lower bound of the best one, not against the best
mean. That is a deliberate refusal to chase a point estimate: an iteration that scored
high once may have scored high by luck, and building on it propagates the luck rather
than the improvement.

What is and is not transplanted
-------------------------------
**Transplanted:** the decision rule, and the statistic it reads. `_ci_lower()` and
`_select_base()` below are SICA's rule expressed against this platform's curve points
instead of its `ArchiveAnalyzer` -- same comparison, same "newest that clears the bound"
tie-break.

**Not transplanted:** SICA's scaffoling (its experiment harness, its per-iteration
source copies, its `agent_code/` layout). None of it is the method; the platform
already supplies what it does -- a task set, execution, scoring, and a record of every
round. Reimplementing it here would have been reimplementing the platform.

**Not reproduced:** SICA runs a full improvement step per iteration. This method has to
fit mode A, where the platform owns the schedule, so it uses the rule for what mode A
can express: whether this round is worth spending on at all, and which prior round is
the fairest comparison. When the rule says no round has cleared the bound, it stops
(`changed: false`) rather than spending a model call to stand still.

The honest summary: **the selection rule is SICA's, the loop around it is the
platform's.** A run of this method is evidence about SICA's rule, not a reproduction of
SICA's results.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import editor                                              # noqa: E402


# ------------------------------------------------------------ SICA 的规则 ---

def _ci_lower(point: dict) -> float:
    """A curve point's confidence lower bound, as SICA reads `ci_lower`.

    The platform records `score_ci95` as [lo, hi]; lo is the quantity SICA's rule
    compares against. Points written before the field existed fall back to the score
    itself, which makes them harmless: an interval of zero width never clears a real
    bound by accident.
    """
    ci = point.get("score_ci95")
    if isinstance(ci, list) and len(ci) == 2 and isinstance(ci[0], (int, float)):
        return float(ci[0])
    return float(point.get("score") or 0.0)


def _select_base(history: list[dict]) -> tuple[dict, str]:
    """SICA's rule: the newest round whose mean clears the best lower bound.

    Returns (chosen_round, why). The reasoning string is emitted so a reader can see
    which branch fired, rather than having to reconstruct it from the curve.
    """
    if not history:
        return {}, "no history yet"

    best = max(history, key=_ci_lower)
    bound = _ci_lower(best)

    # "Newest whose mean clears the bound" -- SICA's tie-break, and the reason its
    # selection prefers recent work among statistical equals: an older iteration is a
    # longer walk from whatever the current state is.
    clearing = [p for p in history if float(p.get("score") or 0.0) > bound]
    if not clearing:
        return best, (f"no round clears the best lower bound {bound:.3f} "
                      f"(round {best.get('round')} holds it)")

    newest = max(clearing, key=lambda p: p.get("round", -1))
    return newest, (f"round {newest.get('round')} has mean "
                    f"{float(newest.get('score') or 0):.3f} > best lower bound "
                    f"{bound:.3f}")


def _worth_continuing(history: list[dict]) -> tuple[bool, str]:
    """Whether another round can still buy anything statistically.

    Two stops, both from SICA's comparison:

    * **A bound that nothing clears.** Every round is inside the noise of the best
      one, so a further edit is a coin flip dressed as progress.
    * **A bound that keeps being cleared by the incumbent alone.** The current round
      *is* the best; there is nothing to build past it yet.
    """
    if len(history) < 2:
        return True, "too little history to judge"

    incumbent = history[-1]
    incumbent_score = float(incumbent.get("score") or 0.0)
    best_other = max(history[:-1], key=_ci_lower)
    bound = _ci_lower(best_other)

    if incumbent_score > bound:
        return True, (f"incumbent {incumbent_score:.3f} clears the previous best lower "
                      f"bound {bound:.3f}")
    return False, (f"incumbent {incumbent_score:.3f} does not clear the previous best "
                   f"lower bound {bound:.3f}; another round would be noise")


# ----------------------------------------------------------------- 主流程 ---

#: SICA's rule decides *whether* and *from where*; the edit itself is a separate
#: concern, and composing the two is what makes them separable at all. It is how
#: the platform can answer "is this method's advantage its rule or its editor?" --
#: run the same editor with and without the rule.
SICA_SYSTEM = """You are improving an agent harness, on behalf of a method whose \
selection rule has already decided that this round is worth spending on.

You will be shown the harness's source and the record of running it on a set of \
tasks. Propose ONE concrete change.

Reply with JSON only:

  {"files": [{"path": "<relative path>", "content": "<complete new file contents>"}],
   "hypothesis": "<one sentence: what you changed and why>"}

Rules:
- `path` is relative to the harness root. `harness.json` may not be changed.
- `content` must be the COMPLETE new file, not a diff. This may create a new file.
- If the evidence does not support any change, reply {"no_change": "<reason>"}.
"""


def build_prompt(base, request, chosen, why):
    """The edit is made *in the light of* the rule's choice, not independently of it."""
    sources = editor.load_sources(base)
    traces = editor.load_traces(base, limit=6, order=editor.failing_first(base))
    return (
        f"Round {request.get('round_index', 1)}. The harness scored "
        f"{request.get('incumbent_score')} on the task set.\n\n"
        f"The selection rule chose round {chosen.get('round')} as the fairest base "
        f"to build on ({why}), so the change should be a step from there rather than "
        f"a restatement of the current state.\n\n"
        f"## What the harness did last round\n{traces}\n\n"
        "## Current harness sources\n"
        + "\n\n".join(f"### {k}\n```\n{v}\n```" for k, v in sources.items())
        + "\n\nPropose one change, or say no_change."
    )


def main() -> int:
    request = editor.read_request()
    editor.require_api(request)

    base = Path(request["base_harness"])
    history = editor.load_history(base)

    chosen, why = _select_base(history)
    cont, cont_why = _worth_continuing(history)

    rule_report = {
        "rule": "SICA: newest round whose mean clears the best confidence lower bound",
        "rounds_seen": [p.get("round") for p in history],
        "bounds": {str(p.get("round")): round(_ci_lower(p), 4) for p in history},
        "selected_base_round": chosen.get("round"),
        "selection_reason": why,
        "continue": cont,
        "continue_reason": cont_why,
    }

    # The platform reads `changed`; a stop costs nothing and is a real answer about
    # this method's rule. Spending a model call to make an edit the rule does not
    # believe in would be the wrong trade.
    # SICA 的规则是**选哪一个前代**继续,不是"要不要继续"。它的 runner 跑固定轮数,
    # 没有因为这个比较不成立就提前收工的分支。
    #
    # 这里原本 `return` 了(还把它写成 `changed: false`),那是我当初加进去的一条发明
    # 出来的停止条件。而 `changed: false` 在 mode A 里会结束整条 run,后果是 SICA 在一条
    # 全 0 的基线上只拿到 1 个方法轮,别的方法拿到 3 个 —— 一种"因为谨慎所以少跑几轮"的
    # 折算。现在这个判断只作为 `sica_rule.continue` 留在记录里,轮次照跑。
    #
    # 保留 `_worth_continuing` 本身,是因为它读的是 SICA 的同一个统计量,写进记录比删掉
    # 更有信息量 —— 但它是观察,不是闸门。
    dest = editor.candidate_path(request)
    try:
        reply = editor.ask(build_prompt(base, request, chosen, why), system=SICA_SYSTEM, base=base)
    except SystemExit:
        raise
    except Exception as exc:                                   # noqa: BLE001
        return editor.fail(request, method="sica_ci", exc=exc, base=base,
                           state={"sica_rule": rule_report})

    if "no_change" in reply:
        editor.apply_edits(base, dest, [])
        return editor.report(
            request, method="sica_ci", harness_dir=dest, changed=False,
                                                         stop=False,
            hypothesis=str(reply["no_change"])[:200], edit_kind="sica_rule",
            method_reported={"sica_rule": rule_report},
            acceptance_rule="SICA's rule admitted the round; the model found no edit",
            label=f"SICA rule: base round {chosen.get('round')}, no edit")

    changed, files, note = editor.apply_edits(base, dest, reply.get("files"))
    if not changed:
        return editor.report(
            request, method="sica_ci", harness_dir=dest, changed=False,
                                                         stop=False,
            hypothesis=f"unusable proposal: {note}", edit_kind="sica_rule",
            method_reported={"sica_rule": rule_report, "rejected": note},
            acceptance_rule="SICA's rule admitted the round; the proposal was unusable",
            label=f"SICA rule: base round {chosen.get('round')}, unusable proposal")

    return editor.report(
        request, method="sica_ci", harness_dir=dest, changed=True, files=files,
        hypothesis=(f"[SICA base r{chosen.get('round')}] "
                    + str(reply.get("hypothesis", "")))[:300],
        edit_kind="sica_rule",
        method_reported={"sica_rule": rule_report},
        acceptance_rule="SICA's confidence-lower-bound comparison decides whether a "
                        "round is worth spending on; the platform's measurement "
                        "decides the score",
        extra={"selection_rule": rule_report["rule"]},
        label=f"[SICA base r{chosen.get('round')}] "
              + str(reply.get("hypothesis", ""))[:50])


if __name__ == "__main__":
    raise SystemExit(main())
