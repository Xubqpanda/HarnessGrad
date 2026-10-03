"""方法和平台之间的共享编辑层。

这里钉的是两类缺陷,它们都不是"某个方法写错了",而是**每个方法都会犯**的:

  1. **方法必须能看到契约承诺给它的东西。** `INTERFACE.md` §4.7 把 `_harnessgrad/`
     放在 harness 目录里。第一版 `llm_improver` 从 `request["workspace"]`(方法自己的
     空 scratch 目录)读 traces,于是它build 出来的每一个 prompt 都写着
     `(no traces available)` —— 它只看着 harness 源码改代码,而它的 docstring 声称它
     读了 harness 的实际行为。这条测试就是那次测量的复现。

  2. **每一条出口都要写出 trajectory。** 这条规则已经被违反过两次:provider 报错和
     提案不可用都直接退出、没写文件,于是平台只能说"方法什么都没写",真正的原因
     (一个 503、一个格式坏掉的回复)被丢掉了。不可读的失败比被报告的失败更糟。
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
sys.path.insert(0, str(ROOT / "methods"))


def _write_harness(root: Path, *, traces: dict[str, str] | None = None,
                   history: list[dict] | None = None) -> Path:
    """A base harness with the channel staged exactly where the contract puts it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "harness.json").write_text(
        json.dumps({"name": "probe", "version": "1.0", "entrypoint": "agent.py"}))
    (root / "agent.py").write_text("print('hello')\n")
    hg = root / "_harnessgrad"
    (hg / "traces").mkdir(parents=True, exist_ok=True)
    (hg / "history").mkdir(parents=True, exist_ok=True)
    for tid, body in (traces or {}).items():
        (hg / "traces" / f"{tid}.jsonl").write_text(body)
    for point in (history or []):
        (hg / "history" / f"round-{point.get('round', 0)}.json").write_text(
            json.dumps(point))
    (hg / "round.json").write_text(json.dumps(
        (history or [{}])[-1] if history else {"round": 0, "score": 0.0}))
    return root


def _load(name: str):
    path = ROOT / "methods" / name / "run.py"
    spec = importlib.util.spec_from_file_location(f"m_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _invoke(mod, request: dict, monkeypatch, reply=None, raises=None) -> dict:
    """Run a method's main() with a stubbed model, returning its stdout object."""
    import editor

    captured: dict = {}

    def fake_ask(prompt, system=editor.DEFAULT_SYSTEM, base=None):
        captured["prompt"] = prompt
        captured["system"] = system
        if raises is not None:
            raise raises
        return reply if reply is not None else {"no_change": "stub"}

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


def _request(base: Path, work: Path, traj: Path) -> dict:
    return {"platform_api_version": "0.1.0", "mode": "A", "base_harness": str(base),
            "workspace": str(work), "round_index": 1, "incumbent_score": 0.0,
            "task_ids": ["t01"], "train_task_ids": ["t01"],
            "trajectory_out": str(traj)}


# ------------------------------------------- 方法必须看到承诺给它看的东西 ---

def test_the_editor_is_given_the_traces_the_contract_promises(tmp_path, monkeypatch):
    """回归测试:traces 在 harness 目录里,不在方法的 workspace 里。

    这条测试是那次测量的直接复现 —— 旧代码在这种目录布局下报告
    `(no traces available)`,因为它在 `workspace/_harnessgrad/traces` 找文件,而那里
    永远是空的。
    """
    base = _write_harness(tmp_path / "base", traces={
        "t01": json.dumps({"command": "ls -a", "exit": 1}) + "\n"
               + json.dumps({"reply": "I cannot do that"}) + "\n"})
    work = tmp_path / "ws"; work.mkdir()          # 方法的 scratch 目录:空的
    mod = _load("llm_improver")

    got = _invoke(mod, _request(base, work, tmp_path / "traj.json"), monkeypatch)

    prompt = got["prompt"]
    assert "ran: ls -a" in prompt, "方法没拿到 trace 里的命令"
    assert "said: I cannot do that" in prompt, "方法没拿到 trace 里的回复"
    assert "no traces available" not in prompt, \
        "方法又从错误的路径读 traces 了(workspace 而不是 harness)"


def test_history_is_read_from_the_contract_path(tmp_path, monkeypatch):
    """`history/` 同样在 harness 目录里,而且它是选择类规则的唯一输入。"""
    base = _write_harness(tmp_path / "base", traces={"t01": "{}\n"}, history=[
        {"round": 0, "score": 0.0, "score_ci95": [0.0, 0.1]},
        {"round": 1, "score": 0.5, "score_ci95": [0.4, 0.6]},
    ])
    work = tmp_path / "ws"; work.mkdir()
    mod = _load("sica_ci")

    got = _invoke(mod, _request(base, work, tmp_path / "traj.json"), monkeypatch,
                  reply={"files": [{"path": "agent.py", "content": "x=1\n"}],
                         "hypothesis": "h"})
    rule = got["stdout"]["method_reported"]["sica_rule"]
    assert rule["rounds_seen"] == [0, 1], f"SICA 的规则没看到历史:{rule}"
    assert rule["selected_base_round"] == 1


# ---------------------------------------------- 每条出口都要写 trajectory ---

def test_a_provider_error_still_produces_a_trajectory(tmp_path, monkeypatch):
    """不可读的失败比被报告的失败更糟:它看起来像方法什么都没做。"""
    base = _write_harness(tmp_path / "base", traces={"t01": "{}\n"})
    work = tmp_path / "ws"; work.mkdir()
    traj = tmp_path / "traj.json"
    mod = _load("llm_improver")

    got = _invoke(mod, _request(base, work, traj), monkeypatch,
                  raises=RuntimeError("503 no available channel"))

    assert traj.exists(), "provider 报错时没有写出 trajectory"
    step = json.loads(traj.read_text())["steps"][0]
    assert "503" in json.dumps(step["method_reported"]), \
        "真正的原因必须进记录,否则只剩「方法什么都没写」"
    assert got["stdout"]["changed"] is False


def test_a_no_change_reply_still_produces_a_trajectory(tmp_path, monkeypatch):
    base = _write_harness(tmp_path / "base", traces={"t01": "{}\n"})
    work = tmp_path / "ws"; work.mkdir()
    traj = tmp_path / "traj.json"
    mod = _load("llm_improver")

    _invoke(mod, _request(base, work, traj), monkeypatch,
            reply={"no_change": "the evidence supports nothing"})

    assert traj.exists()
    assert json.loads(traj.read_text())["steps"][0]["harness_dir"]


def test_an_unusable_proposal_is_reported_not_swallowed(tmp_path, monkeypatch):
    """被拒绝的编辑和「方法选择不动」是两件事,记录里必须分得开。"""
    base = _write_harness(tmp_path / "base", traces={"t01": "{}\n"})
    work = tmp_path / "ws"; work.mkdir()
    traj = tmp_path / "traj.json"
    mod = _load("llm_improver")

    got = _invoke(mod, _request(base, work, traj), monkeypatch,
                  reply={"files": [{"path": "nope.py"}]})     # 没有 content

    assert traj.exists()
    assert got["stdout"]["changed"] is False
    note = json.loads(traj.read_text())["steps"][0]["method_reported"]
    assert "rejected" in note, note


# ------------------------------------------------- 编辑不能跑到 harness 外 ---

def test_an_edit_outside_the_harness_is_rejected_not_clamped(tmp_path):
    """`../agent.py` 被悄悄夹成 `agent.py`,等于测了一个模型没要求的东西。"""
    import editor

    base = _write_harness(tmp_path / "base")
    dest = tmp_path / "candidate"

    changed, written, note = editor.apply_edits(
        base, dest, [{"path": "../escaped.py", "content": "x"}])
    assert changed is False and not written
    assert "outside the harness" in note
    assert not (tmp_path / "escaped.py").exists()


def test_the_manifest_cannot_be_rewritten(tmp_path):
    """`harness.json` 命名了这个 harness;能改它的方法能改自己被测量成什么。"""
    import editor

    base = _write_harness(tmp_path / "base")
    dest = tmp_path / "candidate"
    changed, written, note = editor.apply_edits(
        base, dest, [{"path": "harness.json", "content": "{}"}])
    assert changed is False
    assert "protected" in note


def test_a_legitimate_multi_file_edit_is_applied(tmp_path):
    """方法可能要新增一个模块 —— 只允许改 agent.py 会把这类改进挡在门外。"""
    import editor

    base = _write_harness(tmp_path / "base")
    dest = tmp_path / "candidate"
    changed, written, _ = editor.apply_edits(base, dest, [
        {"path": "agent.py", "content": "print('better')\n"},
        {"path": "tools/helper.py", "content": "VALUE = 1\n"},
    ])
    assert changed and sorted(written) == ["agent.py", "tools/helper.py"]
    assert (dest / "tools" / "helper.py").read_text() == "VALUE = 1\n"
    assert (dest / "harness.json").read_text() == (base / "harness.json").read_text()
    assert not (dest / "_harnessgrad").exists(), \
        "channel 不该被复制进候选,否则它会成为一个被提交的文件"


# ------------------------------------------------ 读不懂 ≠ 决定不改 ---

def test_a_reply_missing_its_trailing_brackets_is_repaired():
    """小模型把整个文件写进 JSON 字符串,然后忘了结尾的 `]}`。

    这是实测的形状,不是构造的:一次真实运行里,9B 模型返回了完整的 8.8 KB 编辑,
    `content` 在 8864 字符处正确闭合、`hypothesis` 也正常,就是没写最后的 `]}`。
    `json.loads` 报 "Expecting ',' delimiter at EOF";旧的容错回退取
    `text[第一个{ : 最后一个}]`,拿到的仍然没有闭合,于是放弃 —— 然后方法报
    `no_change`。六轮下来读作"方法拒绝了六次"。
    """
    import editor

    real = ('{"files": [{"path": "agent.py", "content": "print(1)\\n", '
            '"hypothesis": "h"}')
    got = editor.loads_tolerant(real)
    assert got and got["files"][0]["path"] == "agent.py", got


def test_a_reply_with_raw_newlines_inside_a_string_is_repaired():
    """同一种失败的另一半:整文件内容里的**裸换行**。

    裸换行让 JSON 非法,任何"补括号"都救不了它,所以修复必须在扫描时做。
    """
    import editor

    got = editor.loads_tolerant('{"files": [{"path": "a.py", "content": "l1\nl2"}]}')
    assert got and got["files"][0]["content"] == "l1\nl2", got
    got = editor.loads_tolerant('{"files": [{"path": "a.py", "content": "a\tb"}]}')
    assert got and got["files"][0]["content"] == "a\tb", got


def test_a_reply_with_a_bracket_too_many_is_also_repaired():
    """反方向的同一种错:多一个结尾括号。

    实测的形状:让 `base_harness/loop` 失败的那条回复就是 `{...}}` —— 尾部多了一个
    `}`。`json.loads` 报 "Extra data",而 harness 自己的回退取
    `reply[find("{") : rfind("}")]`,最后一个 `}` 恰恰就是多余的那个,所以它同样失败
    (`base_harness/loop/agent.py:143`)。补括号救不了这种情况,只有**在第一个平衡点
    截断**才行 —— 两个方向需要相反的修法,所以两件都做。
    """
    import editor

    assert editor.loads_tolerant('{"tool": "bash", "command": "x"}}') == \
        {"tool": "bash", "command": "x"}
    assert editor.loads_tolerant('{"a": 1}}}') == {"a": 1}
    # 尾部有散文也是同一类:JSON 在前,后面还有话
    assert editor.loads_tolerant('{"a": 1}\n\nHope that helps!') == {"a": 1}


#: 每一种都是**实测**遇到过的坏法,不是假设。左边是模型怎么坏的,右边是修好之后的值。
#: 这些以前全都被静默地报成 `no_change` —— 那才是真正的危害:一次读不懂变成了一个决定。
MALFORMED = {
    "正常": ('{"a": 1}', {"a": 1}),
    "少一个结尾括号": ('{"files": [{"path": "a.py", "content": "x"}',
                       {"files": [{"path": "a.py", "content": "x"}]}),
    "多一个结尾括号": ('{"a": 1}}', {"a": 1}),
    # 这条是最难的一个:类型配错了对,`}` 关的是 `[`。既不是缺括号也不是多括号,
    # 而"按类型匹配"的扫描器会**悄悄忽略**它,于是文档看起来是平衡的。
    # 实测来自一条 11 KB 的回复:结尾 `...inputs."}]}`,那个 `}` 关的是 340 字符前
    # 打开的列表。json.loads 直接拒绝,扫描器却什么都不报。
    "括号类型配错": ('{"a": [1, 2}', {"a": [1, 2]}),
    "类型错且多余": ('{"a": [1}]}', {"a": [1]}),
    "字符串里的裸换行": ('{"a": "x\ny"}', {"a": "x\ny"}),
    "字符串里的裸控制字符": ('{"a": "x\x0by"}', {"a": "x\x0by"}),
    # 模型写代码/正则时几乎必然出现:`\d`、`\s`、`C:\data` 都是非法 JSON 转义
    "非法转义 \\d": ('{"a": "\\d+"}', {"a": "\\d+"}),
    "Windows 路径": ('{"a": "C:\\data\\x"}', {"a": "C:\\data\\x"}),
    "未转义的引号": ('{"a": "he said "hi" loudly", "b": 1}',
                     {"a": 'he said "hi" loudly', "b": 1}),
    "末尾多余逗号": ('{"a": 1,}', {"a": 1}),
    "数组末尾逗号": ('{"a": [1, 2,]}', {"a": [1, 2]}),
    "JSON 后面还有散文": ('{"a": 1} hope this helps', {"a": 1}),
    "围栏包着": ('```json\n{"a": 1}\n```', {"a": 1}),
    "前面有散文": ('Sure! Here you go:\n{"a": 1}', {"a": 1}),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_every_malformed_shape_a_small_model_produces_is_repaired(name):
    """读不懂一条回复,代价是整个方法看起来"没有 diff"。

    这张表的每一行都来自这台机器上真实发生过的一次失败,而不是一份"LLM 可能怎么写坏
    JSON"的清单。修不好它们的时候,方法报的是 `no_change` —— 于是六轮下来读作
    "这个方法拒绝了六次",而真相是平台没能读懂它。
    """
    import editor

    raw, expect = MALFORMED[name]
    assert editor.loads_tolerant(raw) == expect


def test_the_parser_leaves_valid_json_alone():
    """修复不能吃掉合法的内容:字符串里的 `,}`、合法的 `\n` 和嵌套都必须原样保留。"""
    import editor

    assert editor.loads_tolerant('{"a": "x,}y"}') == {"a": "x,}y"}
    assert editor.loads_tolerant('{"a": "x\\ny"}') == {"a": "x\ny"}
    assert editor.loads_tolerant('{"a": {"b": [1, {"c": 2}]}}') == \
        {"a": {"b": [1, {"c": 2}]}}
    # 真的读不懂时必须**放弃**,而不是硬凑一个出来
    assert editor.loads_tolerant("not json at all") is None
    assert editor.loads_tolerant('{"a": ') is None


def test_the_parser_only_adds_what_the_structure_says_is_missing():
    """保守:它只补文本自身结构表明缺了的括号,别的错仍然失败。"""
    import editor

    assert editor.loads_tolerant('{"a": [1, 2') == {"a": [1, 2]}
    assert editor.loads_tolerant('{"a": 1}') == {"a": 1}
    assert editor.loads_tolerant("这不是 JSON") is None
    assert editor.loads_tolerant('{"a": ') is None


def test_an_unreadable_reply_raises_instead_of_reporting_no_change(monkeypatch):
    """**这条是重点。** 读不懂不能变成"方法决定不改"。

    两者是完全不同的事件,把第二个折进第一个,等于在曲线上放了一个没人做过的决定。
    现在它抛 `UnparseableReply`,调用方的 `except Exception` 会走 `editor.fail`,
    记录成一次方法错误,原因留在 trajectory 里。
    """
    import editor

    monkeypatch.setenv("HG_METHOD_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("HG_METHOD_API_KEY", "x")
    monkeypatch.setenv("HG_METHOD_MODEL", "m")
    monkeypatch.setenv("HG_METHOD_PARSE_RETRIES", "1")

    class _Msg:
        content = "I refuse to answer in JSON."

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kw):
            return _Resp()

    class _Client:
        def __init__(self, **kw):
            self.chat = type("C", (), {"completions": _Completions()})()

    import openai
    monkeypatch.setattr(openai, "OpenAI", _Client)
    with pytest.raises(editor.UnparseableReply) as exc:
        editor.ask("prompt")
    assert "refuse" in str(exc.value), exc.value


def test_the_hypothesis_is_found_wherever_the_model_put_it():
    """实测:要的是顶层 `hypothesis`,模型塞进了 file 对象里,于是标签是空的 ——
    一次真实的 8 KB 编辑被记成了"没有理由"。两处都读。"""
    import editor

    top = {"files": [{"path": "a.py", "content": "x"}], "hypothesis": "顶层"}
    nested = {"files": [{"path": "a.py", "content": "x", "hypothesis": "嵌套"}]}
    assert editor.hypothesis_of(top) == "顶层"
    assert editor.hypothesis_of(nested) == "嵌套"
    assert editor.hypothesis_of({"files": []}) == ""


# ------------------------------------------- 不再要求模型转义整个文件 ---

def test_a_fenced_file_block_is_the_preferred_reply_format():
    """把整个文件塞进 JSON 字符串,是所有解析失败的共同来源。

    模型要在 8 KB 的程序里转义每一个引号、换行和反斜杠,而 9B 模型做不到。本机实测过的
    每一种坏法 —— 缺括号、多括号、括号类型错、裸控制字符、非法 `\\d` 转义、未转义的引号
    —— 归根到底都是同一个问题:文件自己的引号必须活过第二层编码。

    围栏块没有第二层编码:文件原样写出来。JSON 仍然接受,所以没有东西被破坏。
    """
    import editor

    assert editor.parse_reply("```file:a.py\nprint(1)\n```\nHYPOTHESIS: h") == {
        "files": [{"path": "a.py", "content": "print(1)\n"}], "hypothesis": "h"}
    # 前面有散文也要认
    assert editor.parse_reply("Sure:\n```file:a.py\nx = 1\n```\nHYPOTHESIS: y") == {
        "files": [{"path": "a.py", "content": "x = 1\n"}], "hypothesis": "y"}
    # 多个文件
    two = editor.parse_reply("```file:a.py\nA\n```\n```file:b.py\nB\n```\nHYPOTHESIS: t")
    assert [f["path"] for f in two["files"]] == ["a.py", "b.py"]
    # 显式拒绝
    assert editor.parse_reply("NO_CHANGE: nothing to do") == {
        "no_change": "nothing to do"}


def test_the_fenced_format_survives_what_json_could_not():
    """这条是它存在的全部理由:一段真实的 Python,里面全是引号、反斜杠和正则。

    同一段内容如果要求模型写成 JSON 字符串,本机实测会坏;写在围栏里则逐字保留。
    """
    import editor

    source = ('import re\n'
              'PAT = re.compile(r"\\\\d+\\\\s*")\n'
              'PATH = "C:\\\\data\\\\x"\n'
              'MSG = \'he said "hi"\'\n'
              'if __name__ == "__main__":\n'
              '    print(f"{PAT.pattern} {PATH} {MSG}")\n')
    reply = f"```file:agent.py\n{source}```\nHYPOTHESIS: use a regex"
    got = editor.parse_reply(reply)
    assert got["files"][0]["content"] == source
    assert got["hypothesis"] == "use a regex"


def test_json_is_still_accepted_so_nothing_that_worked_breaks():
    import editor

    assert editor.parse_reply('{"files":[{"path":"a.py","content":"x"}]}') == {
        "files": [{"path": "a.py", "content": "x"}]}
    # 一个都不是的时候必须返回空 —— 让调用方报成失败,而不是编一个决定出来
    assert editor.parse_reply("I refuse.") == {}


# ------------------------------------------- 改进器要看到"为什么失败" ---

def test_the_prompt_carries_why_each_task_failed(tmp_path):
    """traces 说"它做了什么",任务页说"为什么没通过" —— 少了后者只能猜。

    实测:平台把判分报告和 harness 自己的 stderr 都写进了任务页,而 `llm_improver` 的
    prompt 只有 traces + 源码。结果是模型删掉了 147 行注释、没改行为(`headless-terminal`
    那一轮)。它不是偷懒:那份输入里没有任何一句"哪条测试失败、断言是什么"。
    """
    mod = _load("llm_improver")
    root = _write_harness(tmp_path / "base")
    tasks = root / "_harnessgrad" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "t01.json").write_text(json.dumps({
        "task_id": "t01", "score": 0.0,
        "verdict": {"kind": "command", "passed": False, "score": 0.0,
                    "detail": "reward 0 | check_report /logs/verifier/ctrf.json "
                              "(1 passed 6 failed) | FAILED test_outputs.py::test_import: "
                              "ModuleNotFoundError: No module named 'headless_terminal'"},
        "harness": {"exit_code": 1,
                    "stderr": "Traceback...\nRuntimeError: 3 attempts failed, last: "
                              "Connection error."},
        "loop": {"ended": "crashed"}}))
    prompt = mod.build_prompt(root, {"round_index": 1, "incumbent_score": 0.0})
    assert "FAILED test_outputs.py::test_import" in prompt
    assert "No module named 'headless_terminal'" in prompt
    assert "Connection error" in prompt


def test_the_prompt_carries_the_platform_skill_when_it_is_staged(tmp_path):
    """默认 skill 由平台放进通道 —— 方法要读它,否则"怎么改 harness"要靠自己发明。"""
    mod = _load("llm_improver")
    root = _write_harness(tmp_path / "base")
    (root / "_harnessgrad" / "SKILL.md").write_text("# How to improve a harness\nXYZ\n")
    prompt = mod.build_prompt(root, {"round_index": 2, "incumbent_score": 0.0})
    assert "How to improve a harness" in prompt and "XYZ" in prompt


def test_a_prompt_without_pages_still_builds(tmp_path):
    """老通道(没有 tasks/ 也没有 SKILL.md)不能把方法搞崩。"""
    mod = _load("llm_improver")
    root = _write_harness(tmp_path / "base")
    prompt = mod.build_prompt(root, {"round_index": 1, "incumbent_score": 0.0})
    assert "## Current harness sources" in prompt and "no_change" in prompt
