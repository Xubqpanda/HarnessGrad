"""平台记录、但不执行的两条度量政策(来自 Anthropic 的 AAR 工作)。

原话:分数是各 benchmark 的**几何平均**("the lowest one binds and all must be lifted"),
以及**资格门**——"disqualifies a method, whatever its score, if the trained model's 95%
confidence interval on any capability benchmark falls entirely below the base model's"。

**为什么是记录而不是执行**:这个平台没有接受规则(`docs/framework_design.md` §1:平台记录
规则,不拥有规则),而且一个已经交出去的候选没法被"拒绝"。所以两条都算出来写在曲线点上,
方法想用就从点上读 —— 与噪声带同一种分工:数字是平台的,决定是方法的。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harnessgrad import policies  # noqa: E402


# ------------------------------------------------------------- 几何平均 ---

def test_the_geometric_mean_refuses_to_trade_one_task_for_another():
    """**这就是这条政策存在的理由。** 算术平均允许"用一道题的大涨换另一道题的小跌";
    几何平均不允许 —— 最低的那一项说了算。"""
    assert policies.geomean([1.0, 1.0]) == pytest.approx(1.0)
    assert policies.geomean([1.0, 0.25]) == pytest.approx(0.5)
    # 算术平均会是 0.625;几何平均把它压到 0.5 以下
    assert policies.geomean([1.0, 0.25]) < sum([1.0, 0.25]) / 2
    assert policies.geomean([1.0, 0.5, 0.5]) == pytest.approx(0.63, abs=0.005)


def test_a_single_failed_task_binds_but_leaves_a_usable_number():
    """二值 per-task 分数下,几何平均在有题失败时就是 0 —— 那不是公式的缺陷,而是
    "最低项说了算"的极端形态。所以我们把 0 夹到一个极小的正数:仍然是"被最低项绑住",
    但结果还是个能比较的数(零和零没法比大小)。"""
    got = policies.geomean([1.0, 1.0, 0.0, 1.0])
    assert 0 < got < policies.ZERO_FLOOR ** 0.2, got
    assert got > 0


def test_an_empty_task_set_is_None_not_zero():
    """一道题都没测到不是"得了 0 分" —— 和 `invalid` 与 0 的区分是同一条规则。"""
    assert policies.geomean([]) is None
    assert policies.geomean(["x", None]) is None


def test_a_negative_score_is_refused_loudly():
    """负分的对数在 `math` 里会变成复数并在深处炸掉;在这里说清楚。"""
    with pytest.raises(ValueError, match="negative"):
        policies.geomean([1.0, -0.1])


# --------------------------------------------------------------- 资格门 ---

def test_a_task_below_the_baseline_is_a_violation():
    got = policies.floor_violations({"a": 0.0, "b": 1.0}, {"a": 1.0, "b": 1.0})
    assert [v["task_id"] for v in got["violations"]] == ["a"]
    assert got["violations"][0]["gap"] == -1.0
    assert got["checked"] == 2 and got["count"] == 1
    assert got["violations"][0]["ci_entirely_below"] is None, "一次采样答不了这个问题"


def test_the_interval_criterion_needs_a_spread_and_is_otherwise_unknown():
    """Anthropic 的判据是"区间整段低于基座"。一次采样时诚实的答案是
    `null`(问不出这个问题),不是一个读起来像"及格"的 `false`。"""
    cand = {"a": 0.0}
    base = {"a": 1.0}
    tight = policies.floor_violations(cand, base, candidate_std={"a": 0.01},
                                      baseline_std={"a": 0.01})
    assert tight["violations"][0]["ci_entirely_below"] is True
    assert tight["significant"] == 1 and tight["ci_available"] is True
    wide = policies.floor_violations(cand, base, candidate_std={"a": 0.6},
                                     baseline_std={"a": 0.01})
    assert wide["violations"][0]["ci_entirely_below"] is False
    assert wide["significant"] == 0


def test_the_baseline_wins_ties_and_tolerance_suppresses_small_gaps():
    assert policies.floor_violations({"a": 1.0}, {"a": 1.0})["count"] == 0
    assert policies.floor_violations({"a": 1.0}, {"a": 1.0})["checked"] == 1
    assert policies.floor_violations({"a": 0.95}, {"a": 1.0},
                                     tolerance=0.1)["count"] == 0
    assert policies.floor_violations({"a": 0.95}, {"a": 1.0},
                                     tolerance=0.0)["count"] == 1


def test_a_task_the_baseline_never_had_is_not_checked():
    got = policies.floor_violations({"a": 0.0, "new": 0.0}, {"a": 1.0})
    assert got["checked"] == 1 and [v["task_id"] for v in got["violations"]] == ["a"]


# ---------------------------------------- 诊断侧的选择:平台与方法的副本 ---

TRAINING = {"score_kind": "training", "per_task": {"t": 0.5}, "score": 0.5}
EXAM = {"score_kind": "exam", "per_task": {"t": 0.9}, "train_per_task": {"t": 0.25},
        "split": {"train": ["t"], "eval": ["t"]}}
OLD = {"score": 0.25, "train_score": 0.25}          # 没有 score_kind 的老记录


def test_the_platform_reads_the_studied_side_and_never_the_exam():
    assert policies.studied_per_task(TRAINING) == {"t": 0.5}
    assert policies.studied_per_task(EXAM) == {"t": 0.25}, "读了考试侧"
    assert policies.studied_per_task(OLD) == {}


def test_a_split_with_no_diagnostic_side_returns_nothing_rather_than_the_exam():
    point = {"score_kind": "exam", "per_task": {"t": 0.9},
             "split": {"train": ["t"], "eval": ["t"]}}
    assert policies.studied_per_task(point) == {}


def test_the_platform_copy_agrees_with_the_method_copy():
    """一条规则两份实现是代价(`methods/protocol.py` 是方法侧那份,沙箱里 import 不到平台)。
    代价的缓解就是这条断言:两份必须对同一批形状给同一个答案,否则"方法看到的"和
    "平台记录的"会分叉。"""
    spec = importlib.util.spec_from_file_location("mprotocol", ROOT / "methods" / "protocol.py")
    mprotocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mprotocol)
    for point in (TRAINING, EXAM, OLD,
                  {"score_kind": "training", "per_task": {}},
                  {"score": 0.9, "train_score": 0.1, "split": {"train": ["t"]}}):
        assert policies.studied_per_task(point) == mprotocol.studied_per_task(point), point


# --------------------------------------------------------- 端到端接线 ---

def test_a_candidate_round_carries_both_policies_and_h0_carries_neither(tmp_path):
    """**接线测试。** H0 是基线本身,不可能"低于基线",所以它不该带这两个字段;
    候选轮两个字段都要在。"""
    runs = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "1", "--run-id", "policies", "--dataset", "demo",
         "--sampling", "all", "--runs-root", str(runs),
         "--work-root", str(tmp_path / "work"),
         "--method-entrypoint", f"{sys.executable} {ROOT / 'methods/echo_base/run.py'}"],
        cwd=ROOT, capture_output=True, text=True, timeout=900,
        env={**os.environ, "HG_AGENT_BACKEND": "mock"})
    assert proc.returncode == 0, f"{proc.stdout[-600:]}\n{proc.stderr[-600:]}"
    points = [json.loads(l) for l in
              (runs / "policies" / "curve.jsonl").read_text().splitlines() if l.strip()]
    assert len(points) == 2, points
    assert "capability_floor" not in points[0] and "score_geomean" not in points[0]
    candidate = points[1]
    assert candidate["capability_floor"]["baseline"] == "round-0"
    assert candidate["capability_floor"]["checked"] == len(candidate["per_task"])
    assert isinstance(candidate["score_geomean"], float)
