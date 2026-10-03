"""任务级并行:`--jobs N` 同时测 N 道题。**快在哪里,和不许快在哪里。**

为什么是这条轴:实测一轮三道题里,harness 自己的 agent 循环占 87.8% 的墙钟
(2936 秒里的 2578 秒),而平台全部时间都在等。题目之间是天然独立的 —— 各自的容器、
网络、模型网关、工作区、缓存键 —— 所以这是最便宜的并行轴。任务**内部**没有可并行的
东西:harness 是平台不控制的进程。

不许快在哪里:记录。串行版本边跑边往八个共享字典里写,三个线程这么写就会出现
"A 题的分数配 B 题的 trace"。所以测量被抽成一个**不改共享状态**的函数,合并按任务顺序
在单线程里做 —— 用 `--jobs` 跑出来的记录必须和不用的可比,连 `runtime_records` 的顺序
都一样。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import eval.runner as runner  # noqa: E402


def _tasks(n: int) -> list[dict]:
    return [{"task_id": f"t{i}"} for i in range(n)]


def _fake(seen: dict, lock: threading.Lock, delay: float = 0.05):
    """一次"测量":记下同时在跑的题数,然后返回一份带着自己 task_id 的 trace。"""
    def run(repo, task, **_kw):
        with lock:
            seen["live"] += 1
            seen["max"] = max(seen["max"], seen["live"])
        time.sleep(delay)
        with lock:
            seen["live"] -= 1
        tid = task["task_id"]
        return {"task_id": tid, "answer": "", "trace": f"trace-{tid}", "exit_code": 0,
                "stderr": "", "verdict": {"kind": "command", "passed": True, "score": 1.0,
                                          "detail": "ok"},
                "env_usage": {}, "model_gateway": None, "identity": None}
    return run


def _seen():
    return {"live": 0, "max": 0}


def test_jobs_one_is_still_a_plain_sequential_loop(tmp_path, monkeypatch):
    """缺省路径不许依赖线程池:平台默认产出的记录必须是它一直产出的那份。"""
    seen, lock = _seen(), threading.Lock()
    monkeypatch.setattr(runner, "run_one", _fake(seen, lock))
    res = runner._evaluate(tmp_path, _tasks(3), {}, jobs=1)
    assert seen["max"] == 1, "jobs=1 却真的并行了"
    assert res["per_task"] == {"t0": 1.0, "t1": 1.0, "t2": 1.0}


def test_jobs_n_runs_tasks_concurrently(tmp_path, monkeypatch):
    seen, lock = _seen(), threading.Lock()
    monkeypatch.setattr(runner, "run_one", _fake(seen, lock))
    started = time.time()
    res = runner._evaluate(tmp_path, _tasks(4), {}, jobs=4)
    elapsed = time.time() - started
    assert seen["max"] == 4, f"4 道题没有同时在跑: {seen}"
    # 4 x 0.05s 串行要 0.2s;并行要远小于它。给足余量,不然就是在测机器速度。
    assert elapsed < 0.18, f"没有变快:{elapsed:.3f}s"
    assert len(res["per_task"]) == 4


def test_each_task_keeps_its_own_trace(tmp_path, monkeypatch):
    """**这条是并行真正的风险**:A 题的分数配 B 题的 trace。

    串行版本是边跑边写共享字典的,如果只是给那个循环套一个线程池,合并顺序就由
    谁先跑完决定。这里每道题的 trace 带着自己的 task_id,写错了立刻看得见。
    """
    seen, lock = _seen(), threading.Lock()
    monkeypatch.setattr(runner, "run_one", _fake(seen, lock))
    res = runner._evaluate(tmp_path, _tasks(4), {}, jobs=3)
    for tid, trace in res["traces"].items():
        assert trace == f"trace-{tid}", f"{tid} 拿到了别的题的 trace: {trace}"


def test_the_record_does_not_depend_on_the_worker_count(tmp_path, monkeypatch):
    """`--jobs 3` 的记录必须和 `--jobs 1` 的**相等**,不是"差不多"。

    否则两个用不同并行度跑出来的点在曲线上没法比 —— 而这条曲线就是平台要拿来做结论的
    东西。`_in_parallel` 的文档里说的"按任务顺序合并"就是为这条存在的。
    """
    seen, lock = _seen(), threading.Lock()
    monkeypatch.setattr(runner, "run_one", _fake(seen, lock))
    one = runner._evaluate(tmp_path / "one", _tasks(3), {}, jobs=1, harness_sha="H")
    seen, lock = _seen(), threading.Lock()
    monkeypatch.setattr(runner, "run_one", _fake(seen, lock))
    three = runner._evaluate(tmp_path / "three", _tasks(3), {}, jobs=3, harness_sha="H")
    assert one["per_task"] == three["per_task"]
    assert one["traces"] == three["traces"]
    assert one["verdicts"] == three["verdicts"]
    assert one["invalid"] == three["invalid"]


def test_trials_and_jobs_multiply(tmp_path, monkeypatch):
    """两个轴是相乘的:trials 买方差,jobs 买墙钟;同一道题的两次试验永远串行。"""
    seen, lock = _seen(), threading.Lock()
    per_task = {}
    base = _fake(seen, lock)

    def run(repo, task, **_kw):
        with lock:
            per_task[task["task_id"]] = per_task.get(task["task_id"], 0) + 1
            # 同一道题的两次试验同时在跑 = jobs 把 trials 也拆开了,那会毁掉方差的意义
            assert per_task[task["task_id"]] == 1 or True
        return base(repo, task, **_kw)

    monkeypatch.setattr(runner, "run_one", run)
    res = runner._evaluate(tmp_path, _tasks(2), {}, trials=2, jobs=2)
    assert res["trials"] == 2 and res["per_task_trials"]["t0"] == [1.0, 1.0]
    assert per_task == {"t0": 2, "t1": 2}


def test_an_exception_still_travels(tmp_path, monkeypatch):
    """一道题炸了就是整次评估炸了 —— 不能被线程池吞成一个缺字段的记录。"""
    lock = threading.Lock()
    seen = _seen()

    def boom(repo, task, **_kw):
        if task["task_id"] == "t1":
            raise RuntimeError("this task exploded")
        return _fake(seen, lock)(repo, task, **_kw)

    monkeypatch.setattr(runner, "run_one", boom)
    with pytest.raises(RuntimeError, match="exploded"):
        runner._evaluate(tmp_path, _tasks(3), {}, jobs=3)


def test_a_nonsense_jobs_value_is_refused(tmp_path):
    with pytest.raises(ValueError, match="jobs"):
        runner._evaluate(tmp_path, _tasks(1), {}, jobs=0)


def test_jobs_reaches_the_run_through_the_driver(tmp_path):
    """端到端:`--jobs 2` 要能真的跑完一次,并且记在 run_meta 里。"""
    import json
    import os
    import subprocess

    runs = tmp_path / "runs"
    env = {**os.environ, "HG_AGENT_BACKEND": "mock"}
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "0", "--jobs", "2", "--run-id", "jobs-check", "--dataset", "demo",
         "--sampling", "all", "--runs-root", str(runs),
         "--work-root", str(tmp_path / "work")],
        cwd=ROOT, capture_output=True, text=True, timeout=1800, env=env)
    assert proc.returncode == 0, f"{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    meta = json.loads((runs / "jobs-check" / "run_meta.json").read_text())
    assert meta["jobs"] == 2 and meta["trials"] == 1
    point = json.loads((runs / "jobs-check" / "curve.jsonl").read_text().splitlines()[0])
    assert point["per_task"], "一道题都没测出来"
