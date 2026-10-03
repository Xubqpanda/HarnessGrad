"""两次测量的差别,和"根本没测到"——一个样本说不出这两件事里的任何一件。

为什么要单独钉这两条:

  * **一次评估就是一个样本。** 实测:同一份**没改过**的 harness、同一个模型、同一批题,
    一次拿到 0.333,下一次拿到 0.000 —— 因为模型偶尔吐出 harness 解析不了的工具调用方言,
    harness 就把这道题结束了。不记方差的曲线,撑不起"这次编辑有用"这个平台存在的理由。
  * **检查没跑成不是 0。** 实测 83/89 道 TB 题的检查要靠网络装自己的 pytest;装不上时
    reward 文件是个 0,而平台以前把这个 0 记在 harness 头上。现在平台能证明"报告没写出来"
    时,这道题归 `invalid`(不写进 per_task),和"平台测不出这道题"是同一类事实。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import eval.runner as runner  # noqa: E402


# --------------------------------------------------------------- 方差 ---

def test_the_spread_is_a_population_standard_deviation():
    """全体标准差,不是样本标准差:n 次试验就是全部,不是从一个更大的总体里抽的。"""
    assert runner._spread([1.0]) == 0.0
    assert runner._spread([0.5, 0.5, 0.5]) == 0.0
    assert runner._spread([0.0, 1.0]) == pytest.approx(0.5)
    # mean 2/3, deviations -2/3, 1/3, 1/3 -> variance 2/9
    assert runner._spread([0.0, 1.0, 1.0]) == pytest.approx((2 / 9) ** 0.5)


def test_trials_average_and_record_their_spread(tmp_path, monkeypatch):
    """`--trials 2`:任务分数是两次的均值,并且两次都留在记录里。"""
    seq = iter([0.0, 1.0])

    def fake(*_a, **_k):
        return {"verdict": {"kind": "command", "passed": True, "score": next(seq),
                            "detail": "d"},
                "trace": "x", "exit_code": 0, "stderr": ""}

    monkeypatch.setattr(runner, "run_one", fake)
    res = runner._evaluate(tmp_path, [{"task_id": "t1"}], {}, trials=2)
    assert res["per_task"]["t1"] == 0.5
    assert res["per_task_trials"]["t1"] == [0.0, 1.0]
    assert res["per_task_std"]["t1"] == pytest.approx(0.5)
    assert res["score_std"] == pytest.approx(0.5)
    assert res["trials"] == 2


def test_one_trial_records_no_spread_at_all(tmp_path, monkeypatch):
    """"缺字段"必须读成"只测了一次",不能读成"方差为零"。"""
    monkeypatch.setattr(runner, "run_one", lambda *_a, **_k: {
        "verdict": {"kind": "command", "passed": True, "score": 1.0, "detail": "d"},
        "trace": "x", "exit_code": 0, "stderr": ""})
    res = runner._evaluate(tmp_path, [{"task_id": "t1"}], {}, trials=1)
    assert res["per_task_std"] == {} and res["per_task_trials"] == {}
    assert res["score_std"] == 0.0


def test_trials_reach_the_curve_point_end_to_end(tmp_path):
    """端到端:`--trials` 要真的走到曲线点上,而不是只在 runner 里存在。"""
    runs = tmp_path / "runs"
    env = {**os.environ, "HG_AGENT_BACKEND": "mock"}
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "0", "--trials", "2", "--run-id", "trials-check", "--dataset", "demo",
         "--sampling", "all", "--runs-root", str(runs),
         "--work-root", str(tmp_path / "work")],
        cwd=ROOT, capture_output=True, text=True, timeout=600, env=env)
    assert proc.returncode == 0, f"{proc.stdout[-500:]}\n{proc.stderr[-500:]}"
    point = json.loads((runs / "trials-check" / "curve.jsonl").read_text().splitlines()[0])
    assert point["n_trials"] == 2
    assert "score_std" in point and "per_task_std" in point
    # 每道题一个方差(float) —— 两次试验的原始分数在 runner 的结果里,不在曲线点上
    assert point["per_task_std"] and all(v >= 0 for v in point["per_task_std"].values())


# ------------------------------------------------- 没测到 ≠ 测出来是 0 ---

def test_a_check_that_never_ran_is_invalid_not_zero(tmp_path, monkeypatch):
    """平台能证明"检查没跑"时,这道题是 `invalid`:不进 per_task,也不缓存。"""
    monkeypatch.setattr(runner, "run_one", lambda *_a, **_k: {
        "verdict": {"kind": "command", "passed": False, "score": 0.0,
                    "unmeasured": "the check wrote no /logs/verifier/ctrf.json: "
                                  "its tests did not run, so this task was not measured",
                    "detail": "reward 0 from /logs/verifier/reward.txt (exit 0)"},
        "trace": "", "exit_code": 0, "stderr": ""})
    res = runner._evaluate(tmp_path, [{"task_id": "t1"}], {}, trials=1)
    assert res["per_task"] == {}, "没测到的题不能写进 per_task"
    assert res["invalid"]["t1"]["stage"] == "check"
    assert "ctrf.json" in res["invalid"]["t1"]["detail"]


def test_a_measured_zero_is_still_a_zero(tmp_path, monkeypatch):
    """没有 `unmeasured` 标记的 0 仍然是 0——这条不能因为上一条而变宽。"""
    monkeypatch.setattr(runner, "run_one", lambda *_a, **_k: {
        "verdict": {"kind": "command", "passed": False, "score": 0.0,
                    "detail": "reward 0 from /logs/verifier/reward.txt (exit 0)"},
        "trace": "x", "exit_code": 0, "stderr": ""})
    res = runner._evaluate(tmp_path, [{"task_id": "t1"}], {}, trials=1)
    assert res["per_task"] == {"t1": 0.0}
    assert res["invalid"] == {}
