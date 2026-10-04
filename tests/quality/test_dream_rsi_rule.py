"""Dream-RSI 的选择规则:在**包含 incumbent** 的版本集合上取 argmax V。

**为什么这个移植不一样。** 我们其他每个方法都要为 mode A 缺的那个"拒绝步"道歉;
Dream-RSI 不需要 —— 它的接受/拒绝作用在"**部署哪个版本**"上,不作用在"已经交出去的候选"上。
只要把 incumbent 放进候选集合,它的不退化保证就成立:

    "Because the candidate set includes the current policy, this selection satisfies
     V^{m*} >= V^0."   (arXiv 2609.14858 §3)

而平台本来就把每一轮留在曲线上、最后一个是 incumbent。所以这个保证在这里是**核对出来的
事实**(`guarantee_holds`),不是一句声明。带不过来的是 replay simulator(`max_{v∈T} s_v`
要对已记录的 discovery tree 取最大,我们每轮只有一个分数):移植版退化成"固定题集上的
best-of",那仍然是这条规则有用的一半。
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load():
    spec = importlib.util.spec_from_file_location(
        "dream_rsi", ROOT / "methods" / "dream_rsi" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DR = _load()


def _point(round_no: int, quality: float | None, tokens: int | None = None, *,
           trials: int = 1, kind: str = "training") -> dict:
    point = {"round": round_no, "score_kind": kind, "n_trials": trials,
             "cost": {"harness_tokens": tokens},
             "identity": {"harness_sha": f"sha{round_no}"}}
    if kind == "training":
        point["score"] = quality
        point["per_task"] = {"a": quality} if quality is not None else {}
    else:
        point["score"] = 0.9
        point["per_task"] = {"a": 0.9}
    return point


# --------------------------------------------------------------- V 公式 ---

def test_v_is_quality_minus_cost_plus_the_parallelism_term():
    cfg = {"b1": 0.1, "b2": 0.2, "kilo_tokens": 1000.0}
    row = DR.version_row(_point(0, 0.5, 20000, trials=2), cfg)
    # 0.5 - 0.1*20 + 0.2*20/2 = 0.5 - 2 + 2 = 0.5
    assert row["cost_kilo_tokens"] == 20.0
    assert row["v"] == pytest.approx(0.5)
    assert row["k"] == 2


def test_the_default_makes_cost_a_tie_breaker_not_the_objective():
    """**默认系数是量出来的,不是抄的。** 若 b1=0.05,一轮 100k token 要扣 5.0 —— 是整个
    质量区间的五倍,`argmax V` 会悄悄变成"谁花得少"。默认取 0.001:质量 +0.1 能压过 10 倍
    成本,而同分时成本仍然说话。"""
    cfg = DR.config()
    assert cfg["b1"] * 100 <= 0.2, "100 kilo-token 的惩罚不该超过质量区间的五分之一"
    cheap = DR.version_row(_point(0, 0.4, 5000), cfg)["v"]
    pricey = DR.version_row(_point(1, 0.5, 50000), cfg)["v"]
    assert pricey > cheap, "质量 +0.1 应当压过 10 倍成本"
    same_a = DR.version_row(_point(0, 0.5, 10000), cfg)["v"]
    same_b = DR.version_row(_point(1, 0.5, 20000), cfg)["v"]
    assert same_a > same_b, "同分时成本必须能打破平票"


def test_an_unreported_cost_is_not_a_zero_cost():
    """没报告不是 0:喂 0.0 会让"从未生效的成本规则"看起来像"通过了成本规则"。"""
    row = DR.version_row(_point(0, 0.5, None), DR.config())
    assert row["v"] == 0.5 and row["cost_kilo_tokens"] is None
    assert row["cost_source"] == "not reported"


def test_the_exam_side_never_enters_the_comparison():
    row = DR.version_row(_point(0, 0.9, 1000, kind="exam"), DR.config())
    assert row["quality"] is None and row["v"] is None


# ------------------------------------------------------------- 选择规则 ---

def test_argmax_over_every_version_including_the_incumbent():
    history = [_point(0, 0.0, 100000), _point(1, 0.5, 50000), _point(2, 0.4, 5000)]
    got = DR.select(history)
    assert got["selected"] == 1
    assert got["incumbent"] == 2
    assert got["v_selected"] > got["v_incumbent"]
    assert got["guarantee_holds"] is True
    assert [v["round"] for v in got["versions"]] == [0, 1, 2]


def test_the_guarantee_is_checked_and_holds_even_when_the_incumbent_is_worst():
    """保证之所以便宜是因为它**构造上**成立 —— 而这正是值得核对的原因:哪天有人把
    incumbent 从候选集合里拿掉,这条断言就会失败。"""
    history = [_point(0, 0.9, 1000), _point(1, 0.0, 900000)]
    got = DR.select(history)
    assert got["selected"] == 0 and got["incumbent"] == 1
    assert got["v_selected"] > got["v_incumbent"]
    assert got["guarantee_holds"] is True


def test_a_tie_is_kept_by_the_earliest_version():
    """和 Meta-Harness 的 frontier 同一条约定:平局不换持有者,否则选择会在噪声上抖。"""
    got = DR.select([_point(0, 0.5, 10000), _point(1, 0.5, 10000)])
    assert got["selected"] == 0


def test_versions_with_no_score_are_ignored_but_recorded():
    got = DR.select([_point(0, None, 1000), _point(1, 0.5, 1000)])
    assert got["selected"] == 1
    assert got["versions"][0]["v"] is None


def test_no_history_selects_nothing():
    got = DR.select([])
    assert got["selected"] is None and got["guarantee_holds"] is False


def test_the_record_says_how_many_rounds_reported_a_cost():
    got = DR.select([_point(0, 0.0, None), _point(1, 0.5, 1000)])
    assert got["cost_reported_by_rounds"] == 1


# ------------------------------------------------------------ 编辑基准 ---

def _channel(tmp_path: Path, staged: dict[str, str] | None = None) -> Path:
    root = tmp_path / "ws"
    hg = root / "_harnessgrad" / "states"
    hg.mkdir(parents=True)
    index = {"kept": [], "window": 12, "available": 0}
    for round_no, harness_dir in (staged or {}).items():
        index["kept"].append(f"round-{round_no}")
        index[f"round-{round_no}"] = {"harness_dir": harness_dir}
    (hg / "index.json").write_text(json.dumps(index))
    return root


def test_the_selected_version_is_what_the_next_round_edits(tmp_path):
    staged = tmp_path / "state0"
    staged.mkdir()
    ws = _channel(tmp_path, staged={"0": str(staged)})
    history = [_point(0, 0.9, 1000), _point(1, 0.0, 1000)]
    selection = DR.select(history)
    base, why = DR.base_round(selection, history, ws)
    assert base == staged
    assert "r0" in why and "highest V" in why


def test_an_unstaged_selection_falls_back_and_says_so(tmp_path):
    ws = _channel(tmp_path, staged={})
    history = [_point(0, 0.9, 1000), _point(1, 0.0, 1000)]
    base, why = DR.base_round(DR.select(history), history, ws)
    assert base == ws
    assert "not staged" in why and "r0" in why


def test_an_incumbent_that_is_already_selected_is_not_copied(tmp_path):
    ws = _channel(tmp_path, staged={"1": str(tmp_path)})
    history = [_point(0, 0.0, 1000), _point(1, 0.9, 1000)]
    base, why = DR.base_round(DR.select(history), history, ws)
    assert base == ws and "already the incumbent" in why


# ----------------------------------------------- main() 带一个假模型 ---

def _harness(tmp_path: Path, history: list[dict]) -> Path:
    root = tmp_path / "base"
    (root / "_harnessgrad" / "history").mkdir(parents=True)
    (root / "_harnessgrad" / "traces").mkdir(parents=True)
    (root / "harness.json").write_text(json.dumps(
        {"name": "probe", "version": "1.0", "entrypoint": "agent.py"}))
    (root / "agent.py").write_text("print('hello')\n")
    for point in history:
        (root / "_harnessgrad" / "history" / f"round-{point['round']}.json").write_text(
            json.dumps(point))
    (root / "_harnessgrad" / "round.json").write_text(json.dumps(history[-1]))
    return root


def _invoke(mod, request: dict, monkeypatch, reply: dict) -> dict:
    import editor

    captured: dict = {}

    def fake_ask(prompt, system=editor.DEFAULT_SYSTEM, base=None, skill=True):
        captured["prompt"] = prompt
        return reply

    monkeypatch.setattr(editor, "ask", fake_ask)
    monkeypatch.setattr(mod.editor, "ask", fake_ask, raising=False)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "stdout", out)
    try:
        mod.main()
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise
    return {"stdout": json.loads(out.getvalue() or "{}"), **captured}


def _request(base: Path, work: Path, traj: Path, history: list[dict]) -> dict:
    return {"platform_api_version": "0.1.0", "mode": "A", "base_harness": str(base),
            "workspace": str(work), "round_index": 2, "incumbent_score": 0.0,
            "task_ids": ["t01"], "train_task_ids": ["t01"],
            "trajectory_out": str(traj), "history": history}


def test_main_puts_the_version_table_and_the_selection_in_the_prompt(tmp_path, monkeypatch):
    history = [_point(0, 0.0, 100000), _point(1, 0.7, 20000)]
    base = _harness(tmp_path, history)
    got = _invoke(DR, _request(base, tmp_path / "ws", tmp_path / "traj.json", history),
                  monkeypatch, {"no_change": "nothing to add"})
    prompt = got["prompt"]
    assert "| version | quality | cost (kilo-tokens) | k | V |" in prompt
    assert "| r1 |" in prompt
    assert "guarantee V* >= V^0 holds: True" in prompt
    assert got["stdout"]["changed"] is False


def test_main_hands_the_versions_and_the_guarantee_back_on_the_wire(tmp_path, monkeypatch):
    import editor

    history = [_point(0, 0.0, 100000), _point(1, 0.7, 20000)]
    base = _harness(tmp_path, history)
    captured: dict = {}
    real_report = editor.report

    def spy(req, **kw):
        captured.update(kw)
        return real_report(req, **kw)

    monkeypatch.setattr(DR.editor, "report", spy, raising=False)
    _invoke(DR, _request(base, tmp_path / "ws", tmp_path / "traj.json", history),
            monkeypatch, {"no_change": "stop"})
    reported = captured["method_reported"]["dream_rsi"]
    assert reported["selected"] == 1 and reported["guarantee_holds"] is True
    assert captured["extra"]["guarantee_holds"] is True


def test_main_reports_a_real_edit_as_changed(tmp_path, monkeypatch):
    history = [_point(0, 0.0, 100000)]
    base = _harness(tmp_path, history)
    got = _invoke(DR, _request(base, tmp_path / "ws", tmp_path / "traj.json", history),
                  monkeypatch, {"files": [{"path": "agent.py", "find": "print('hello')",
                                           "replace": "print('hello world')"}],
                                "hypothesis": "print more"})
    assert got["stdout"]["changed"] is True
    assert got["stdout"]["files"] == ["agent.py"]


def test_declining_keeps_the_budget(tmp_path, monkeypatch):
    history = [_point(0, 0.0, 100000)]
    base = _harness(tmp_path, history)
    got = _invoke(DR, _request(base, tmp_path / "ws", tmp_path / "traj.json", history),
                  monkeypatch, {"no_change": "all green"})
    assert got["stdout"].get("stop") is False
