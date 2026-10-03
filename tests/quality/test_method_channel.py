"""方法的反馈信号:轨迹里有什么、通道里有什么、以及「为什么」有没有被记下来。

三次改动,同一个根源
--------------------
§2.3 承诺方法能看到它研究那一侧的**轨迹和逐题分数**。实测下来,三样东西没有到达:

  * **命令的输出。** `loop` 把 `{"command":…, "exit":…, "output":…}` 写进轨迹,而
    `render_trace` 只找 `stdout`/`stderr` —— 实测 **441 字节的输出,0 字节进入提示词**。
    于是一个改进器看到 `exit 100`,却看不到 apt 说的「找不到这个包」;它看到自己那次
    `apt-cache search` 成功,却看不到它找到了什么。
  * **逐题分数。** `WITHHELD = ("per_task",)` 是 §2.6 之前的形状留下来的:那时一个点同时
    带两侧,`per_task` 是考试。§2.6 之后一次运行只关于一侧,注释劝人去用的
    `train_per_task` 变成按设计为空 —— 于是通道把契约说要给的数据扣掉了。实测两个真实
    训练运行:`studied_per_task()` 每一轮都返回 `{}`。
  * **任务本身。** 题面、检查脚本、失败的理由,一个都不在通道里。方法只知道某题得了 0。

以及一件被丢掉的话
------------------
`editor.report` 一直发送 `hypothesis` —— 方法自己写的理由 —— 而 `driver.py` 从来没读过它
(`grep hypothesis driver.py` 零命中)。于是一次「不改」的记录里只剩方法自己起的标签,
而那个标签会误导:RRSI 写的是 `RRSI rule: no edit`,把**模型**的决定记成了**规则**的决定。
实测:模型当时想的是「这是回复格式错误,不是 harness 的逻辑缺陷」—— 一句能直接回答
「为什么」的话,被扔了三个小时。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "methods"))

import editor                                                    # noqa: E402
from harnessgrad.channel import (_for_method,                    # noqa: E402
                                 _stage_task_context)
from harnessgrad.records import _curve_point                     # noqa: E402

TRACE = "\n".join([
    json.dumps({"step": 0, "reply": json.dumps(
        {"tool": "bash", "command": "apt-get install -y pmars-source"})}),
    json.dumps({"step": 0, "command": "apt-get install -y pmars-source", "exit": 100,
                "output": "E: Unable to locate package pmars-source\nsecond line"}),
    json.dumps({"step": 1, "reply": json.dumps(
        {"tool": "bash", "command": "apt-cache search pmars"})}),
    json.dumps({"step": 1, "command": "apt-cache search pmars", "exit": 0,
                "output": "pmars - Portable MARS, Core War simulator"}),
    json.dumps({"step": 2, "parse_error": "Extra data: line 1 column 56 (char 55)"}),
    json.dumps({"usage": {"calls": 3, "input_tokens": 1616, "output_tokens": 60}}),
])


# ------------------------------------------- 轨迹:命令的输出必须到达提示词 ---

def test_the_command_output_reaches_the_prompt():
    """`output` 是参考 harness 写的键。曾经 441 字节里 0 字节到达提示词。"""
    rendered = editor.render_trace(TRACE)
    assert "Unable to locate package pmars-source" in rendered, (
        "命令的输出没进提示词 —— 改进器只能看到 exit code,看不到原因")
    assert "Portable MARS" in rendered, (
        "它自己那次搜索的结果也没进去,于是「找到了正确的包」这件事是隐形的")


def test_usage_blocks_stay_out_of_the_prompt():
    assert "input_tokens" not in editor.render_trace(TRACE)


def test_a_dropped_tool_call_is_explained_not_dumped():
    """`parse_error` 曾经渲染成一行裸 JSON —— 读起来像噪音,其实是 harness 的行为。"""
    rendered = editor.render_trace(TRACE)
    assert "could not read its own reply" in rendered
    assert "Extra data" in rendered
    assert not rendered.rstrip().endswith("}"), "还是把原始对象直接倒出来了"


def test_multiline_output_is_indented():
    """命令输出的续行要缩进,否则下一条事件看起来像上一条的一部分。"""
    lines = editor.render_trace(TRACE).splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("  ran:")]
    assert starts, lines
    assert lines[starts[0] + 2].startswith("    "), lines[starts[0]:starts[0] + 3]


# --------------------------------------- 通道:promise 与 deliver 要对得上 ---

TRAIN_POINT = {
    "run_id": "r", "round": 1, "side": "train", "score_kind": "training",
    "score": 0.0, "per_task": {"t01": 0.0, "t02": 1.0},
    "train_score": None, "train_per_task": {},
    "split": {"train": ["t01", "t02"], "eval": ["e01"]},
}


def _platform_functions() -> dict[str, ast.FunctionDef]:
    """Every function the platform defines, by name, wherever it lives.

    These are structural checks about "does the loop do X", not about which file the loop
    is in: when the implementation moved out of `driver.py` into `harnessgrad/`, a check
    pinned to one path would have failed on a refactor that changed no behaviour.
    """
    found: dict[str, ast.FunctionDef] = {}
    paths = sorted((ROOT / "harnessgrad").rglob("*.py")) + [ROOT / "driver.py"]
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef):
                found.setdefault(node.name, node)
    return found


def test_the_diagnostic_per_task_scores_reach_the_method():
    """契约测试:§2.3 说给逐题分数,那就必须给到。

    这条断言是本轮最该存在的一条 —— 它把「承诺」和「送达」放在同一个表达式里。
    修复前 `studied_per_task` 在训练点上返回 `{}`。
    """
    import protocol
    staged = _for_method(TRAIN_POINT)
    assert staged["per_task"] == TRAIN_POINT["per_task"], \
        "通道扣掉了 per_task,而 §2.3 说方法应当拿到它"
    assert protocol.studied_per_task(staged) == TRAIN_POINT["per_task"], \
        "方法自己的读取函数与平台记录不一致"
    assert protocol.studied_score(staged) == 0.0


def test_an_exam_point_is_refused_rather_than_staged():
    """去掉 `WITHHELD` 之所以安全,是因为方法只看得到训练点。这句话要是可执行的。

    哪天有人给一个 exam 点暂存通道,这里必须响亮地失败,而不是把考试递出去。
    """
    with pytest.raises(AssertionError, match="exam"):
        _for_method({"score_kind": "exam", "per_task": {"e01": 1.0}})


def test_the_channel_is_staged_by_both_train_loops():
    """mode B 曾经完全不暂存通道 —— 方法拿到一个空的 base harness。

    结构检查而不是端到端:两个循环是两条独立的路径,一条跑得通不代表另一条。
    """
    functions = _platform_functions()
    for name in ("_run_mode_a", "_run_mode_b"):
        fn = functions.get(name)
        assert fn is not None, name
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_stage_for_method" in called, f"{name} 不暂存通道"


# ------------------------------- 通道:一道题自己的那一页(题面/金标/理由) ---

def _stage(tmp_path, dataset="terminal_bench", task_ids=("build-pmars",)):
    import data.registry as datasets
    tasks, setups, verifiers = datasets.load_tasks(dataset)
    verdicts = {t: {"kind": "command", "passed": False, "score": 0.0,
                    "detail": "reward 0 from /logs/verifier/reward.txt (exit 0)"}
                for t in task_ids}
    _stage_task_context(tmp_path, tasks, setups, verifiers,
                               {"per_task": {t: 0.0 for t in task_ids},
                                "verdicts": verdicts}, list(task_ids))
    return tmp_path / "_harnessgrad" / "tasks"


def test_the_instruction_and_the_gold_are_staged(tmp_path):
    """方法要能读到题目问的是什么、检查的是什么、以及为什么没过。

    没有这一页,一个改进器在 `exit 100` 面前只有三个 exit code 可以推理 —— 实测:它回答
    `no_change`,而 `no_change` 是对那份输入而言合理的答案。
    """
    tasks_dir = _stage(tmp_path)
    entry = json.loads((tasks_dir / "build-pmars.json").read_text(encoding="utf-8"))
    assert entry["goal"] and len(entry["goal"]) > 40
    assert entry["score"] == 0.0
    assert entry["verifier"]["kind"] == "command"
    assert entry["verifier"]["argv"]
    assert "reward 0" in entry["verdict"]["detail"]


def test_the_check_program_is_resolved_not_left_as_a_pointer(tmp_path):
    """TB 的 `inputs` 是 `{"dst": "/tests", "from": <checkout 路径>}` —— 一个指针。

    方法需要的是**程序**,不是指针,所以两种形态都要在这里解析成文本。
    """
    tasks_dir = _stage(tmp_path)
    entry = json.loads((tasks_dir / "build-pmars.json").read_text(encoding="utf-8"))
    checks = entry["verifier"]["checks"]
    assert set(checks) == {"test.sh", "test_outputs.py"}, sorted(checks)
    assert len(checks["test_outputs.py"]) > 500


def test_the_initial_state_says_so_when_it_is_the_image(tmp_path):
    """container 状态下初始状态**就是镜像**,不能显示成空列表让人以为任务从空开始。"""
    tasks_dir = _stage(tmp_path)
    entry = json.loads((tasks_dir / "build-pmars.json").read_text(encoding="utf-8"))
    assert entry["initial_state"]["kind"] == "environment"
    assert "environment" in entry["initial_state"]["note"]


def test_only_the_runs_own_tasks_are_staged(tmp_path):
    """暂存用的 task_ids 和轨迹用的是同一个收窄集合:不能把这次没评的题递出去。"""
    tasks_dir = _stage(tmp_path, task_ids=("build-pmars",))
    assert [p.name for p in tasks_dir.glob("*.json")] == ["build-pmars.json"]


def test_an_inline_input_is_staged_too(tmp_path):
    """`verify_demo` 那种内联 `{"path","content"}` 也要解析,不然只支持一半数据集。"""
    import data.registry as datasets
    tasks, setups, verifiers = datasets.load_tasks("verify_demo")
    _stage_task_context(tmp_path, tasks, setups, verifiers,
                               {"per_task": {"v01": 0.0}, "verdicts": {}}, ["v01"])
    entry = json.loads((tmp_path / "_harnessgrad" / "tasks" / "v01.json")
                       .read_text(encoding="utf-8"))
    assert entry["verifier"]["checks"], "内联的检查输入没被解析出来"
    assert any("test_broken" in name for name in entry["verifier"]["checks"]), entry


# ------------------------------- 记录:方法自己写的理由不能被丢掉 ---

def test_the_methods_own_reason_is_recorded():
    """`editor.report` 一直发送 `hypothesis`,而 driver 从来没读过它。

    实测三次:`RRSI rule: no edit (b_t=4)` 是唯一的记录,而模型当时说的是「这是回复格式
    错误,不是 harness 的逻辑缺陷」。那句话就是「为什么」的答案。
    """
    point = _curve_point(
        run_id="r", round_index=1,
        identity={}, sampling={}, scores=[0.0], task_ids=["t01"], cost={},
        cumulative={}, hypothesis="the evidence does not support a change")
    assert point["method_hypothesis"] == "the evidence does not support a change"


def test_a_round_with_no_method_records_no_hypothesis():
    """round 0 与 eval 点没有方法,所以是 `None` 而不是一个编出来的理由。"""
    point = _curve_point(
        run_id="r", round_index=0, identity={}, sampling={}, scores=[0.0],
        task_ids=["t01"], cost={}, cumulative={})
    assert point["method_hypothesis"] is None


def test_the_console_prints_the_reason_not_only_the_label():
    """标签是方法的**摘要**,而摘要可以误导 —— RRSI 就是。理由要单独印出来。"""
    import io as _io
    from eval.console import Console
    stream = _io.StringIO()
    console = Console(stream=stream)
    console.round_line(2, "model: no change (b_t=4)", point={
        "score": 0.0, "score_kind": "training", "side": "train",
        "identity": {}, "method_hypothesis": "the reply was malformed, not a logic flaw"})
    out = stream.getvalue()
    assert "the reply was malformed" in out, out
    assert "no change" in out


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


# ------------------------------- 通道绝不能落进被评测的树里 ---

def test_the_channel_is_removed_from_the_tree_that_gets_measured():
    """方法指着哪个目录都行,包括那个带着通道的 `base_dir`。

    实测:方法失败时 `editor.fail` 把「那一步」指向 `round1_base` —— 而它是**暂存之后**
    的 `work` 副本,所以带着 `_harnessgrad/`。driver 擦掉 `work` 再把它拷进来,于是通道
    回到了被评测的工作区,再进 `state_after_round<N>`(而 eval 运行正是从那里起步的)。
    harness 就跑在 `work` 里,所以那等于把题面和验证器金标放进被测对象手边 —— §4.7 划的
    是**给方法的**通道,不是一个 artifact 读得到的目录。

    断言的是**每个模式都至少有一处**,不是某个数字:这一轮又加了一处(暂存后立刻从 `work`
    移除,比等到交换更早),而写死数字的测试会因此失败 —— 那说明它测的是实现形状,不是
    不变量。
    """
    per_mode: dict[str, int] = {}
    for name in ("_run_mode_a", "_run_mode_b"):
        fn = _platform_functions().get(name)
        assert fn is not None, name
        hits = 0
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "rmtree" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.BinOp) and isinstance(arg.right, ast.Constant) \
                        and arg.right.value == "_harnessgrad":
                    hits += 1
        per_mode[fn.name] = hits
    assert set(per_mode) == {"_run_mode_a", "_run_mode_b"}, per_mode
    missing = [name for name, n in per_mode.items() if n < 1]
    assert not missing, (
        f"这些模式没有把通道从被评测的树里移除:{missing} —— 方法失败那一轮会把题面和金标"
        f"带回工作区")


def test_the_task_page_carries_the_harness_s_own_crash_reason(tmp_path):
    """方法必须能看到"harness 自己说了什么",不只是"它怎么结束的"。

    `loop.ended = crashed` 和 `RuntimeError: 3 attempts failed, last: Connection error`
    是两件事:前者说"它死了",后者说是网关断了。少了后者,改进器会把一次连接抖动
    读成"模型不会写代码",然后去改完全不相干的地方。
    """
    from harnessgrad.channel import _stage_task_context
    frames = "  File \"agent.py\", line 180, in _call_openai\n" * 200
    result = {
        "per_task": {"t01": 0.0},
        "verdicts": {"t01": {"kind": "command", "passed": False, "score": 0.0,
                             "detail": "reward 0 | check_report (...) | FAILED test_import: "
                                       "ModuleNotFoundError: No module named 'x'"}},
        "harness_output": {"t01": {"exit_code": 1,
                                   "stderr": "Traceback (most recent call last):\n"
                                             + frames
                                             + "RuntimeError: 3 attempts failed, last: "
                                               "Connection error.\n"}},
    }
    _stage_task_context(tmp_path, [{"task_id": "t01", "goal": "g"}], {},
                        {"t01": {"kind": "command"}}, result, ["t01"])
    page = json.loads((tmp_path / "_harnessgrad" / "tasks" / "t01.json").read_text())
    harness = page.get("harness") or {}
    assert harness.get("exit_code") == 1
    # 两头都要留:头部是异常名,最深的原因是最后一行。
    assert harness["stderr"].startswith("Traceback (most recent call last):")
    assert harness["stderr"].rstrip().endswith("Connection error.")
    assert len(harness["stderr"]) < 1400
    # 判分那一侧的证据也还在(两者是不同的问题)
    assert "check_report" in json.dumps(page)
