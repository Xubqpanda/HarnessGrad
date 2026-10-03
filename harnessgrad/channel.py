"""What a method is shown, and where it is shown it.

A method runs as an external process with the platform hidden except `methods/`. Everything
it is allowed to know arrives through this channel: the round's summary, the traces of the
side it is allowed to study, one page per task (the instruction, the grading rule, the
verifier's own words, and the harness's loop facts), and the harness trees of earlier rounds
so a method whose contribution is *which state to build on* has something to build on.

The channel is a directory, rebuilt each round and removed again before the candidate is
measured, so nothing staged here can end up inside the artifact under measurement. A copy is
kept under `runs/<id>/` because "what did the method actually see" has to survive the run
that asked the question.
"""

from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess

from ckpt.git_state import materialize
from eval.trace import loop_facts as _loop_facts

from harnessgrad import PLATFORM_ROOT

def _loop_summary(traces: dict) -> dict:
    """整轮的 harness 侧事实:每题一次调用就交卷的有几道、预算用满的有几道。

    和每题页里的 `loop` 是同一份数据,只是聚合到轮级 —— 方法最先读的是 `round.json`,
    而"这一轮 5 道题里有 3 道只调用了一次模型"这句话,值一整页解释。
    """
    if not traces:
        return {}
    facts = {tid: _loop_facts(text) for tid, text in traces.items()}
    ended: dict[str, int] = {}
    for f in facts.values():
        ended[f["ended"]] = ended.get(f["ended"], 0) + 1
    calls = sorted(f["model_calls"] for f in facts.values())
    return {
        "per_task": facts,
        "ended_breakdown": ended,
        "model_calls_total": sum(f["model_calls"] for f in facts.values()),
        "model_calls_median": calls[len(calls) // 2] if calls else 0,
        # 只调用一次就结束的题:最可疑的一类 —— 要么它真聪明,要么它根本没动手。
        "single_call_tasks": sorted(t for t, f in facts.items() if f["model_calls"] <= 1),
    }

def _for_method(point: dict) -> dict:
    """A curve point as the method is allowed to see it. Never mutates the input.

    A copy, not a delete: the same dict objects are the run's curve, and stripping a
    field in place would quietly remove it from the record too -- the run would then
    lose the very detail the platform is supposed to keep.

    **Nothing is withheld**, and the split is the reason. There used to be a
    `WITHHELD = ("per_task",)` here, written for the shape §2.6 replaced: one point
    carrying both sides, where `per_task` was the exam and `train_per_task` was the
    diagnostic. Two things then went wrong at once. §2.6 made a run cover one side and
    emptied `train_*` on it, so this comment's advice -- "methods that need per-task
    attribution use `train_per_task`" -- pointed at a field that is `{}` by design; and
    `per_task` on a train point *is* the diagnostic breakdown §2.3 promises. So the
    channel withheld the data the contract says to give. Measured on two real train
    runs: `protocol.studied_per_task()` returned `{}` on every round.

    What makes removing it safe is that a method only ever sees a train point:
    `_stage_for_method` is called from the two train loops, and `_run_eval_side` has no
    method at all ("No method, no rounds, one point"). The assertion below is that
    argument, executable -- the day somebody stages a channel for an exam point, this
    fails loudly instead of handing over the exam.
    """
    if point.get("score_kind") == "exam":
        raise AssertionError(
            "refusing to stage an exam curve point into a method channel: methods exist "
            "only on the train side, and `per_task` on an exam point is the exam "
            "(INTERFACE.md §2.3, §2.6)")
    return dict(point)

def _resolve_checks(spec: dict, budget: list[int]) -> dict:
    """A verifier's inputs, resolved to text so a method reads the *check* not a pointer.

    `VERIFY`'s `inputs` come in two shapes and both are in this repository: inline
    (`{"path": ..., "content": ...}`, what `verify_demo` writes) and a reference
    (`{"dst": "/tests", "from": "<a directory in the dataset's checkout>"}`, what the
    Terminal-Bench adapter writes, because the check is a program the benchmark owns).
    A method needs the program, not the pointer.

    **Giving the method the gold is deliberate.** A method that fits the tasks it studies
    has fitted those tasks, and an eval run is what exposes that -- the eval side's tasks
    are not in this run at all (§2.6). What must never happen is the check being
    reachable by the *harness* at run time, because the same harness runs the exam;
    §2.5.9 copies the inputs in after it has stopped, into a fresh container, and that is
    where that boundary lives -- not here.
    """
    out: dict[str, str] = {}
    for entry in (spec or {}).get("inputs") or []:
        src = entry.get("from")
        if src:
            base = Path(src)
            files = sorted(p for p in base.rglob("*") if p.is_file()) \
                if base.is_dir() else ([base] if base.is_file() else [])
            for path in files:
                out[str(path.relative_to(base) if base.is_dir() else path.name)] = \
                    _read_capped(path, budget)
        elif entry.get("content") is not None:
            name = entry.get("path") or entry.get("dst") or "input"
            out[str(name)] = _truncate(str(entry["content"]), budget)
    return out

def _truncate(text: str, budget: list[int]) -> str:
    if len(text) > CONTEXT_FILE_LIMIT:
        return text[:CONTEXT_FILE_LIMIT] + f"\n... [truncated at {CONTEXT_FILE_LIMIT} bytes]"
    if budget[0] - len(text) < 0:
        return f"[omitted: the per-task context budget of {CONTEXT_TOTAL_LIMIT} bytes is spent]"
    budget[0] -= len(text)
    return text

def _read_capped(path: Path, budget: list[int]) -> str:
    try:
        return _truncate(path.read_text(encoding="utf-8", errors="replace"), budget)
    except OSError as exc:
        return f"[unreadable: {exc}]"

def _clip(text: str, limit: int) -> str:
    """Both ends of a traceback, which is why it is not `[:limit]`.

    The head names the exception; the deepest cause -- usually the sentence that says
    what actually happened (`Connection error`) -- is on the last line. A one-ended cut
    keeps the frames and drops the answer.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 2], text[-limit // 2 :]
    return f"{head}\n... [{len(text) - limit} bytes omitted] ...\n{tail}"


def _stderr_of(record: dict) -> str:
    """The stderr a failure record carries, whichever shape it was stored in."""
    return str((record or {}).get("stderr") or "")


def _stage_task_context(work: Path, tasks, setups, verifiers, result,
                        task_ids) -> None:
    """One file per task in this run: the instruction, the gold, and why it failed.

    §2.3 says a method is shown the traces **and the per-task scores** of the side it
    studies, and the page it needs to read them against is the task itself. Without
    this, a method diagnosing `exit 100` has no way to know what the task asked for, no
    way to read the check, and no way to learn *why* the verifier said 0 -- measured: the
    only line an improver got about a failure was `score 0.0`, and it answered
    `no_change`, which is a defensible answer to that input.

    The initial state is the one thing that cannot always be handed over: for a
    `container`-state task the state *is* the image (§2.5.9), so the file says so rather
    than showing an empty list and letting a reader conclude the task starts empty.
    """
    dest = work / "_harnessgrad" / "tasks"
    dest.mkdir(parents=True, exist_ok=True)
    verdicts = (result or {}).get("verdicts") or {}
    scores = (result or {}).get("per_task") or {}
    #: What each harness **printed**, kept by `eval/runner.py` whether it failed or not.
    #: Shown here for the same reason the verifier's report is: the platform captured it
    #: and, until this existed, nobody read it.
    harness_output = (result or {}).get("harness_output") or {}
    harness_failed = (result or {}).get("harness_failed") or {}
    for tid in sorted(set(task_ids or [])):
        task = next((t for t in (tasks or []) if t.get("task_id") == tid), None)
        if task is None:
            continue
        spec = (verifiers or {}).get(tid) or {}
        setup = (setups or {}).get(tid) or {}
        budget = [CONTEXT_TOTAL_LIMIT]
        entry = {
            "task_id": tid,
            "goal": task.get("goal"),
            "score": scores.get(tid),
            "verifier": {
                "kind": spec.get("kind"),
                # `expected` is the gold for an `answer` verifier; `checks` is the gold
                # for a `command` one. Both are given, for the reason in `_resolve_checks`.
                "expected": spec.get("expected"),
                "argv": spec.get("argv"),
                "reward_file": spec.get("reward_file"),
                "checks": _resolve_checks(spec, budget),
            },
            "verdict": dict(verdicts.get(tid) or {}),
            # **harness 内部的事实。** 判分说的是"产物对不对",这里说的是"harness 怎么
            # 结束的" —— 两者缺一不可:一道题 0 分可能是产物不合格,也可能是 harness
            # 在第 1 次调用后就交卷了。后者在只给判分的记录里完全看不出来。
            "loop": _loop_facts((result or {}).get("traces", {}).get(tid, "")),
            # **Why it ended, in the harness's own words.** `loop` above says *how* a run
            # ended (`crashed`, `budget_exhausted`, `answered`); it cannot say why, and a
            # method reading only that has to guess between "the model wrote bad code" and
            # "the harness died". Measured on `headless-terminal`: `loop.ended` was
            # `crashed` after 4 steps, and the reason --
            # `RuntimeError: 3 attempts failed, last: Connection error` -- had been sitting
            # in the platform's own record the whole time. The score says the artifact is
            # missing; this says the harness never got to write it.
            # Bounded, because a traceback is mostly frames: the head names the exception,
            # the tail carries the deepest cause.
            **({"harness": {
                "exit_code": (harness_output.get(tid) or {}).get("exit_code"),
                "stderr": _clip(str((harness_output.get(tid) or {}).get("stderr")
                                    or _stderr_of(harness_failed.get(tid) or {})),
                                1200),
            }} if (harness_output.get(tid) or harness_failed.get(tid)) else {}),
            # 平台侧判定"这道题根本没测到"的原因(§2.5.7)。方法必须看到它:
            # 一次网关抖动被它读成"harness 很弱",它会去改完全不相干的地方。
            **({"invalid": (result or {}).get("invalid", {})[tid]}
               if tid in ((result or {}).get("invalid") or {}) else {}),
        }
        files = (setup or {}).get("files")
        if files:
            entry["initial_state"] = {"kind": "inline", "files": files}
        else:
            entry["initial_state"] = {
                "kind": "environment",
                "note": ("the task ships no SETUP files; its starting files are the "
                         "environment's, so read them through a trace or set them up as "
                         "the environment does")}
        (dest / f"{tid}.json").write_text(
            json.dumps(entry, indent=1, ensure_ascii=False), encoding="utf-8")

def _stage_for_method(work: Path, point: dict, traces: dict,
                      history: list[dict] | None = None,
                      task_ids: list[str] | None = None,
                      run_dir: Path | None = None,
                      tasks=None, setups=None, verifiers=None,
                      result=None) -> None:
    """Hand the method this round's evidence, and the record of the rounds before it.

    `history` is what makes a whole family of methods implementable. Several published
    ones choose *which previous state to build on* rather than always building on the
    incumbent: SICA takes the newest iteration whose mean clears the best confidence
    lower bound, DGM samples from an archive, RRSI carries a frontier. None of them can
    do that from one round's diagnostics -- the information is inherently across rounds.

    Measured before this was written: `history/` was created and left empty, so a method
    could see only the current round. That is a limit the platform imposed silently, and
    it rules out exactly the methods whose contribution *is* the selection rule.

    Curve points as well as traces. `history/round-<n>.json` gets one file per point,
    round 0 included, so the series a selection rule reads is complete; and
    `history/traces/round-<n>/` gets that round's **train** traces, so a method can
    compare what the harness did then against what it does now.

    That second half was missing, and the comment here claimed it was not: it said a
    method "can name the round it came from", when past traces were staged nowhere at
    all -- `traces/` was overwritten every round with the incumbent's. Measured: a
    method asked about its own history could see every past *score* and every past
    *harness directory* (via `states/`) but not one past trace. Several published
    methods diagnose by comparing rounds, so the gap was a silent limit on which
    methods this platform could host.

    Train traces only, for the same reason the live `traces/` are: the eval side's
    behaviour is part of what a method must not fit itself to. The window matches
    `STATES_KEPT`, because a trace is useful mainly next to the state that produced
    it.

    `task_ids` is the train side of the split. This is the one place where the split
    becomes real: everything else about it is bookkeeping, but a method that can read
    an eval trace can edit the harness against the tasks it is about to be scored on,
    and the resulting curve is indistinguishable from improvement.

    `states/` is what makes a method's *parent-selection rule* expressible at all.
    Without it a method is handed only the incumbent's files, so every rule whose
    contribution is "which previous state to build on" -- SICA's confidence bound,
    DGM's archive, HyperAgents' child-count penalty, RRSI's frontier -- collapses to
    the same hill-climb, and eight different methods run identically while each
    claims to be itself. The window is capped and the cap is recorded: keeping every
    state would grow the channel without bound on a long run.
    """
    hg = work / "_harnessgrad"
    if hg.exists():
        shutil.rmtree(hg)
    (hg / "traces").mkdir(parents=True)
    (hg / "history").mkdir(parents=True)
    (hg / "history" / "traces").mkdir(parents=True)
    (hg / "states").mkdir(parents=True)
    # 轮次摘要 = 曲线点 + **harness 侧事实的聚合**。方法最先读这一份;没有后半段,
    # "5 道题里 3 道只调用了一次模型"这句话在记录里根本不存在。
    summary = _for_method(point)
    summary["loop_summary"] = _loop_summary(traces or {})
    (hg / "round.json").write_text(json.dumps(summary, indent=1))
    # **The default skill, in the channel.** A method runs with the platform hidden, so
    # it cannot read `improvers/skill.md` -- and the method that needs this copy is
    # exactly the one that ships no skill of its own. Staged as a file rather than
    # passed in the request because it is something the improver is *asked to read*, and
    # §4.7 already says where the things a method may read live. A method with its own
    # skill simply ignores it.
    skill = PLATFORM_ROOT / "improvers" / "skill.md"
    if skill.is_file():
        (hg / "SKILL.md").write_text(skill.read_text(encoding="utf-8"), encoding="utf-8")
    for past in (history or []):
        (hg / "history" / f"round-{past.get('round', 0)}.json").write_text(
            json.dumps(_for_method(past), indent=1))
    visible = traces if task_ids is None else {
        tid: t for tid, t in traces.items() if tid in set(task_ids)}
    for tid, trace in visible.items():
        (hg / "traces" / f"{tid}.jsonl").write_text(trace)

    # Each past round's train traces, newest window only, same cap as `states/`.
    for past_round in [p.get("round") for p in
                       [x for x in (history or [])
                        if (x.get("identity") or {}).get("harness_sha")][-STATES_KEPT:]]:
        src = (run_dir / f"round{past_round}_traces") if run_dir else None
        if src is None or not src.is_dir():
            continue
        dest = hg / "history" / "traces" / f"round-{past_round}"
        dest.mkdir(parents=True, exist_ok=True)
        for trace in sorted(src.glob("*.jsonl")):
            shutil.copy2(trace, dest / trace.name)

    # The states themselves, newest window only. `git archive` reads the object
    # store, so this works on the dangling commits mode A leaves after `reset_hard`.
    points = [p for p in (history or [])
              if (p.get("identity") or {}).get("harness_sha")]
    kept = points[-STATES_KEPT:]
    index: dict = {"kept": [], "window": STATES_KEPT, "available": len(points)}
    for past in kept:
        name = f"round-{past.get('round', 0)}"
        sha = past["identity"]["harness_sha"]
        try:
            count = materialize(work, sha, hg / "states" / name)
        except (subprocess.CalledProcessError, OSError, ValueError):
            # A state that cannot be materialized is left out of the index rather
            # than listed as a directory that is not there: a method that trusts the
            # index and finds nothing would report a failure the platform caused.
            continue
        index["kept"].append(name)
        index[name] = {"sha": sha, "files": count,
                       "score": past.get("score"),
                       "train_score": past.get("train_score"),
                       "label": past.get("label"),
                       "harness_dir": str(hg / "states" / name)}
    (hg / "states" / "index.json").write_text(json.dumps(index, indent=1))

    # The task's own page: instruction, gold, and the verifier's verdict. Last, and with
    # the same `task_ids` the traces were narrowed to, so a method can never be handed a
    # task this run did not evaluate.
    _stage_task_context(work, tasks, setups, verifiers, result, visible.keys())

    # Keep a copy **in the record**, because the live channel is ephemeral by design: the
    # candidate swap deletes it so it can never end up in a measured tree, and that means
    # "what did the method actually see?" was unanswerable after a run. It came up
    # immediately -- a reader asked why a method changed nothing, and the answer was in a
    # directory that no longer existed.
    #
    # `states/` is left out: it holds complete harness trees, and those are already in the
    # record as `state_after_round<N>/`. Everything else is text and small (round.json,
    # the traces, the history points, and each task's page).
    if run_dir is not None and point.get("round") is not None:
        kept = run_dir / "method_channel" / f"round{point['round']}"
        shutil.rmtree(kept, ignore_errors=True)
        shutil.copytree(hg, kept, ignore=shutil.ignore_patterns("states"))

#: How many past harness states are staged into the method channel each round.
#: A parent-selection rule needs its candidates to exist on disk; keeping every one
#: would grow the channel without bound on a long run, so the window is explicit and
#: `states/index.json` records both the window and how many states exist in total.
STATES_KEPT = 12

#: Caps for the per-task context staged into the method channel. The gold is usually a
#: program and programs are small; a dataset that ships a large asset as a check input
#: would otherwise put megabytes into every round's channel.
CONTEXT_FILE_LIMIT = 64 * 1024

CONTEXT_TOTAL_LIMIT = 256 * 1024
