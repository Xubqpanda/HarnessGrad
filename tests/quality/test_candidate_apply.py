"""两步协议:方法先产生一个 edit 序列,第二步**保证它 apply 成功**。

为什么这件事一直被忽略
----------------------
平台以前把方法产出的东西拷进工作区就测了。「apply 成功」等于 `write_text`,于是三种完全
不同的结局在曲线上长得一模一样:

  * 方法决定什么都不改
  * 方法的 edit 序列指向了 harness 外面的路径、受保护的文件、或者根本没写进去
  * 方法产出的东西**根本不是一只 harness** —— manifest 解析不了、entrypoint 不在、
    Python 编译不过、或者把 `env_kinds` 放宽了

三种都变成 `score 0.000`,读的人只能猜是哪一种。实测:`build-pmars` 三轮 "no edit";
另有一轮方法失败、那一步指向了原始 base 目录,而旧代码分不出这两者。

两个定义,两个主人
------------------
  * **apply 与 validate 在 `eval/candidate.py`** —— 那是平台对「我即将测的东西」的定义。
    如果每个方法各写一份,定义就会漂移;而且方法自检只是**声明**,平台的复检才是判据。
  * **重试在方法里**(`editor.propose_and_apply`) —— 重新提案是改进器自己的修复。平台若
    自己去调模型,那就是平台在写方法了。平台欠的是**检查器**,并且要能从这里调用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "methods"))

# apply 在方法侧(沙箱里可见),判据在平台侧(方法看不到也改不到)——两侧都测
import apply as method_apply                                   # noqa: E402
import eval.candidate as candidate                             # noqa: E402
apply = method_apply.apply


def _harness(root: Path, *, env_kinds=("files",), role="harness",
             entry="agent.py", body="print('hi')\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "harness.json").write_text(json.dumps({
        "name": "h", "version": "1.0", "path": ".", "entrypoint": entry,
        "env_kinds": list(env_kinds), "role": role}), encoding="utf-8")
    if entry:
        (root / entry).write_text(body, encoding="utf-8")
    return root


# ------------------------------------------------------- 第一步 / 第二步 ---

def test_an_edit_sequence_applies_and_is_reported(tmp_path):
    base = _harness(tmp_path / "base")
    applied, problems = apply(base, tmp_path / "dest", [
        {"path": "agent.py", "content": "print('new')\n"},
        {"path": "helper.py", "content": "X = 1\n"},
    ])
    assert problems == [] and applied == ["agent.py", "helper.py"]
    assert (tmp_path / "dest" / "helper.py").read_text() == "X = 1\n"
    assert (base / "agent.py").read_text() == "print('hi')\n", "base 被改动了"


def test_the_channel_is_never_copied_into_a_candidate(tmp_path):
    base = _harness(tmp_path / "base")
    (base / "_harnessgrad" / "tasks").mkdir(parents=True)
    (base / "_harnessgrad" / "tasks" / "t.json").write_text('{"goal":"gold"}')
    apply(base, tmp_path / "dest", [{"path": "agent.py", "content": "x\n"}])
    assert not (tmp_path / "dest" / "_harnessgrad").exists(), \
        "通道被拷进了候选 —— 而 harness 就跑在这棵树里"


def test_an_empty_sequence_is_not_a_change(tmp_path):
    base = _harness(tmp_path / "base")
    applied, problems = apply(base, tmp_path / "dest", [])
    assert applied == [] and problems, "空序列必须说出来,不能当成'没有改动'"


# ------------------------------------------------------------ 拒绝的理由 ---

def test_a_path_outside_the_harness_is_refused():
    base = _harness(Path("/tmp") / "hg-cand-base")
    _, problems = apply(base, Path("/tmp") / "hg-cand-dest",
                                  [{"path": "../../agent.py", "content": "x"}])
    assert any("outside the harness" in p for p in problems), problems


def test_the_manifest_cannot_be_rewritten():
    """`harness.json` 命名了这只 harness;能改它就能改自己被测量的身份。"""
    base = _harness(Path("/tmp") / "hg-cand-base2")
    _, problems = apply(base, Path("/tmp") / "hg-cand-dest2",
                                  [{"path": "harness.json", "content": "{}"}])
    assert any("protected" in p for p in problems), problems


def test_every_rejection_is_reported_with_its_index():
    """哪一条被拒、为什么 —— 重试要听到的就是这个。"""
    base = _harness(Path("/tmp") / "hg-cand-base3")
    _, problems = apply(base, Path("/tmp") / "hg-cand-dest3", [
        {"path": "ok.py", "content": "x=1\n"},
        {"path": "bad.py"},                                   # 没有 content
        "not-an-object",
    ])
    assert len(problems) == 2 and any("edit 1" in p for p in problems) \
        and any("edit 2" in p for p in problems), problems


# ------------------------------------------------------- 结果必须能加载 ---

def test_a_candidate_without_a_manifest_is_not_a_harness(tmp_path):
    base = _harness(tmp_path / "base")
    applied, _ = apply(base, tmp_path / "dest",
                                 [{"path": "agent.py", "content": "x\n"}])
    assert applied
    (tmp_path / "dest" / "harness.json").unlink()
    assert candidate.validate(tmp_path / "dest"), "没有 manifest 却不是问题"


def test_the_two_copies_are_two_roles_not_two_drafts():
    """平台不能靠方法侧那份判据 —— 方法既看不到平台,也**改得到** `methods/`。

    这是分两份的唯一理由,所以它可测。
    """
    import eval.sandbox as sandbox
    code = [ln.split("#", 1)[0].strip()
            for ln in (ROOT / "methods" / "editor.py").read_text(encoding="utf-8").splitlines()]
    offenders = [ln for ln in code
                 if ln.startswith(("import eval", "from eval"))]
    assert not offenders, (
        f"方法侧的代码 import 了平台:{offenders} —— 沙箱里 import 不到"
        f"(实测 ModuleNotFoundError: No module named 'eval')")
    # 平台自己那份判据现在住在实现包里;读整棵树,这样以后再挪位置也不会让这条断言
    # 变成在测"文件叫什么",而不是在测"平台有没有自己的一份"。
    impl_src = "\n".join(p.read_text(encoding="utf-8")
                          for p in (ROOT / "harnessgrad").rglob("*.py"))
    assert "import eval.candidate" in impl_src, "平台没有自己那份判据"
    assert "eval" not in [p.strip("/") for p in sandbox.RUNTIME], \
        "eval/ 出现在方法沙箱可见的路径里了"


def test_a_candidate_whose_entrypoint_disappeared_is_not_a_harness(tmp_path):
    base = _harness(tmp_path / "base")
    apply(base, tmp_path / "dest", [{"path": "agent.py", "content": "x\n"}])
    (tmp_path / "dest" / "agent.py").unlink()
    problems = candidate.validate(tmp_path / "dest")
    assert any("entrypoint" in p for p in problems), problems


def test_a_manifest_that_no_longer_parses_is_not_a_harness(tmp_path):
    base = _harness(tmp_path / "base")
    apply(base, tmp_path / "dest", [{"path": "agent.py", "content": "x\n"}])
    (tmp_path / "dest" / "harness.json").write_text("{ not json")
    problems = candidate.validate(tmp_path / "dest")
    assert any("valid JSON" in p for p in problems), problems


# -------------------------------------- 候选不能自己扩展自己的运行环境 ---

def test_a_candidate_may_not_widen_env_kinds(tmp_path):
    """能力门禁是在**门口**按 base 跑的(§2.5.9)。候选放宽它,就等于被测对象自己决定
    它可以在什么环境里跑 —— 而 mode A 测的正是方法产出的候选。"""
    base = _harness(tmp_path / "base", env_kinds=("files",))
    dest = _harness(tmp_path / "dest", env_kinds=("files", "exec"))
    problems = candidate.validate(dest, base_manifest={
        "env_kinds": ["files"], "role": "harness"})
    assert any("env_kinds" in p for p in problems), problems


def test_a_candidate_may_narrow_env_kinds(tmp_path):
    """收窄是安全的:它只是声明自己能力更少。"""
    dest = _harness(tmp_path / "dest", env_kinds=("files",))
    assert candidate.validate(dest, base_manifest={
        "env_kinds": ["files", "exec"], "role": "harness"}) == []


def test_a_candidate_may_not_change_its_role(tmp_path):
    """把 role 改成 control,就能在数字不动的情况下离开被测量的范围。"""
    dest = _harness(tmp_path / "dest", role="control")
    problems = candidate.validate(dest, base_manifest={
        "env_kinds": ["files"], "role": "harness"})
    assert any("role" in p for p in problems), problems


# ------------------------------------------------ 记录:提了什么、成没成 ---

def test_the_outcome_is_described_for_the_record(tmp_path):
    base = _harness(tmp_path / "base")
    good = method_apply.describe(base, tmp_path / "d1",
                                 [{"path": "agent.py", "content": "x\n"}])
    assert good["ok"] and good["applied"] == ["agent.py"]
    assert good["proposed"] == ["agent.py"]

    bad = method_apply.describe(base, tmp_path / "d2", [{"path": "../x", "content": "y"}])
    assert not bad["ok"] and bad["problems"]


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------------- 平台自己的复检:方法不可信,所以它必须独立看一眼 ---

_BAD_METHOD = '''
"""A method that bypasses `editor.report` and hands over a tree that is not a harness.

Deliberately uncooperative: `editor.report` refuses an invalid candidate, so the only way
to reach the platform's own check is to skip it. That is exactly the case the check exists
for -- a method is code the platform did not write.
"""
import json, sys
from pathlib import Path

req = json.load(sys.stdin)
dest = Path(req["workspace"]) / "candidate"
dest.mkdir(parents=True, exist_ok=True)
# A manifest that does not parse, and no entrypoint.
(dest / "harness.json").write_text("{ this is not json")
(dest / "agent.py").write_text("print('x')\\n")

Path(req["trajectory_out"]).write_text(json.dumps({
    "steps": [{"harness_dir": str(dest), "label": "bad candidate",
               "edit_kind": "harness_source",
               "claimed_cost": {"generation_tokens": 0},
               "method_reported": {"score": None}}],
    "trajectory_shape": "sequence", "nominated": 0,
    "provenance": {"method": "bad", "acceptance_rule": {"text": "none",
                   "source": "test", "calibrated": False}},
}))
sys.stdout.write(json.dumps({"changed": True, "files": ["harness.json"],
                             "hypothesis": "I broke the manifest on purpose"}))
'''


def test_a_bad_candidate_is_unmeasured_not_scored_zero(tmp_path):
    """**平台不能把「我产出的东西不是 harness」记成「我得了 0 分」。**

    这是本轮的核心。三件事在旧代码里长得一样:方法决定不改、edit 没 apply 上、产出的
    东西根本不是 harness。现在第二、三种会带着**具名的问题**记成 unmeasured —— 没有测量
    可报,而不是报一个会被读成「这只 harness 很弱」的 0.000。
    """
    import os
    import subprocess

    method = tmp_path / "bad_method.py"
    method.write_text(_BAD_METHOD, encoding="utf-8")
    runs = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "1", "--run-id", "bad-candidate", "--dataset", "demo",
         "--sampling", "all", "--side", "train",
         "--runs-root", str(runs), "--work-root", str(tmp_path / "work"),
         "--method-entrypoint", f"{sys.executable} {method}"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
        env={**os.environ, "HG_AGENT_BACKEND": "mock"})
    combined = proc.stdout + proc.stderr

    points = [json.loads(l) for l in
              (runs / "bad-candidate" / "curve.jsonl").read_text().splitlines() if l.strip()]
    broken = [p for p in points if p.get("unmeasured")]
    assert broken, f"坏候选没有被记成 unmeasured:\n{combined[-1500:]}"
    point = broken[-1]
    assert point["measured_by_platform"] is False
    problems = point["unmeasured"]["problems"]
    assert problems, "unmeasured 却没说是哪里不对"
    assert any("JSON" in p or "entrypoint" in p or "manifest" in p for p in problems), \
        problems
    # 提了什么也要记下来 —— 它和「改了什么」是不同的问题
    assert point["edits_applied"] == ["harness.json"], point["edits_applied"]
    assert point["method_hypothesis"] == "I broke the manifest on purpose"


# --------------------- 一个 step = 两个阶段,缺一个就不是一次测量 ---

def test_a_half_finished_step_is_not_a_measurement(tmp_path):
    """**你的规则,做成可执行的:** 观察轨迹产生 edit 序列 + apply 这个序列,两步都做完
    才算一个 step。

    方法自己报了错(两步没走完)时,旧代码把它当成一次正常的测量:那一步指向原始 base,
    于是曲线点上的分数是**那只没被动过的 harness 的** —— 一个关于「方法从未产出过的候选」
    的数字。实测:`UnparseableReply`(模型回了散文而不是 JSON 信封)被记成
    `round 1  method error  train 0.000`,读起来像「这只 harness 很弱」,而不像「这一轮丢了」。
    """
    import os
    import subprocess

    method = tmp_path / "fails.py"
    method.write_text('''
import json, sys
from pathlib import Path
req = json.load(sys.stdin)
Path(req["trajectory_out"]).write_text(json.dumps({
    "steps": [{"harness_dir": req["base_harness"], "label": "method error",
               "edit_kind": "none", "claimed_cost": {"generation_tokens": 0},
               "method_reported": {"error": "UnparseableReply: the model replied prose"}}],
    "trajectory_shape": "sequence", "nominated": 0,
    "provenance": {"method": "fails", "acceptance_rule": {"text": "not reached",
                   "source": "test", "calibrated": False}}}))
sys.stdout.write(json.dumps({"changed": False, "files": [],
                             "hypothesis": "UnparseableReply: the model replied prose"}))
''', encoding="utf-8")
    runs = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "2", "--run-id", "half-step", "--dataset", "demo",
         "--sampling", "all", "--side", "train",
         "--runs-root", str(runs), "--work-root", str(tmp_path / "work"),
         "--method-entrypoint", f"{sys.executable} {method}"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
        env={**os.environ, "HG_AGENT_BACKEND": "mock"})
    combined = proc.stdout + proc.stderr

    points = [json.loads(l) for l in
              (runs / "half-step" / "curve.jsonl").read_text().splitlines() if l.strip()]
    # 只有 H0 —— 没有为那个「从未产出的候选」记下任何一步
    assert [p["round"] for p in points] == [0], [p["round"] for p in points]
    assert "step failed" in combined, combined[-1200:]
    assert "UnparseableReply" in combined, "说了失败,却没说为什么"

    meta = json.loads((runs / "half-step" / "run_meta.json").read_text())
    assert meta.get("failed_steps"), "哪一轮没走完必须可查,不能只在控制台的滚动里"
    assert "UnparseableReply" in meta["failed_steps"][0]["why"]


def test_the_platform_never_runs_a_task_for_the_method(tmp_path):
    """apply 阶段允许方法自己分析、自己写 test,但**不准再跑一个 train task 取信息**。

    这条不是靠请求,是靠结构 —— 而且可测。任务环境要么在 docker 里(方法沙箱里
    `/var/run/docker.sock` 不存在),要么在平台侧的一份临时目录里(平台对方法不可见)。
    方法拿到的是**通道**(轨迹和每道题的那一页),那是它被允许研究的证据,不是环境。
    """
    import eval.sandbox as sandbox
    assert not any("docker" in p for p in sandbox.RUNTIME), \
        "docker 相关路径不该在方法的沙箱里可见"
    assert not any("docker" in p for p in sandbox.DNS)
    # 平台对方法不可见,而数据集在平台里
    assert sandbox.RUNTIME  # 非空,免得上面两条断言因为常量被清空而空过


def test_a_failed_step_is_retried_not_the_end_of_the_run(tmp_path):
    """**一次回复格式失败不能吃掉整个预算。**

    实测:一条 `--rounds 5` 的运行,因为一次回复是散文而不是 JSON,跑完 round 1 就结束了。
    而 `editor.fail` 的 `stop=False` 是**故意**的 —— 它自己的文档写着「一次瞬时故障就结束
    一条运行,会让方法失去剩下的预算」。driver 的 `break` 正好和这个意图相反。

    现在:只有**两个阶段都完成**的 step 才推进计数(`r = steps_done + 1`,所以曲线的轮次
    始终连续、方法的 round_index 始终和记录对得上),失败就地重试,次数有上限。
    """
    import os
    import subprocess

    method = tmp_path / "flaky.py"
    method.write_text('''
import json, sys
from pathlib import Path
req = json.load(sys.stdin)
# The method's only writable tree is its own scratch (`method_root`), which also
# survives a retry of the same step. `/tmp` inside the sandbox is a private, read-only
# tmpfs -- a fixture that counted calls there failed every time and made the retry look
# like it never happened.
n = Path(req["workspace"]).parent / "calls.txt"
seen = int(n.read_text()) if n.exists() else 0
n.write_text(str(seen + 1))'''
    + f'''

# 头两次答非所问(方法阶段失败),之后正常回一个 no_change
if seen < 2:
    Path(req["trajectory_out"]).write_text(json.dumps({{
        "steps": [{{"harness_dir": req["base_harness"], "label": "method error",
                   "edit_kind": "none", "claimed_cost": {{"generation_tokens": 0}},
                   "method_reported": {{"error": "UnparseableReply: prose"}}}}],
        "trajectory_shape": "sequence", "nominated": 0,
        "provenance": {{"method": "flaky", "acceptance_rule": {{"text": "x",
                       "source": "test", "calibrated": False}}}}}}))
    sys.stdout.write(json.dumps({{"changed": False, "files": [],
                                 "hypothesis": "UnparseableReply: prose", "stop": False}}))
else:
    Path(req["trajectory_out"]).write_text(json.dumps({{
        "steps": [{{"harness_dir": req["base_harness"], "label": "model: no change",
                   "edit_kind": "none", "claimed_cost": {{"generation_tokens": 0}},
                   "method_reported": {{"score": None}}}}],
        "trajectory_shape": "sequence", "nominated": 0,
        "provenance": {{"method": "flaky", "acceptance_rule": {{"text": "x",
                       "source": "test", "calibrated": False}}}}}}))
    sys.stdout.write(json.dumps({{"changed": False, "files": [],
                                 "hypothesis": "nothing to change", "stop": False}}))
''', encoding="utf-8")
    runs = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "2", "--run-id", "flaky", "--dataset", "demo",
         "--sampling", "all", "--side", "train",
         "--runs-root", str(runs), "--work-root", str(tmp_path / "work"),
         "--method-entrypoint", f"{sys.executable} {method}"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
        env={**os.environ, "HG_AGENT_BACKEND": "mock"})
    points = [json.loads(l) for l in
              (runs / "flaky" / "curve.jsonl").read_text().splitlines() if l.strip()]
    # 要 2 轮,拿到 2 轮 —— 两次失败只是被重试,没有吃掉后面那一步
    assert [p["round"] for p in points] == [0, 1, 2], [p["round"] for p in points]
    meta = json.loads((runs / "flaky" / "run_meta.json").read_text())
    assert len(meta["failed_steps"]) == 2, meta["failed_steps"]
    assert all(f["round"] == 1 for f in meta["failed_steps"]), \
        "重试必须是同一个 step,不是跳过去"


def test_an_unreadable_reply_is_repaired_in_phase_two_not_by_the_parser(tmp_path):
    """**你要的那条。** 回复读不出来,该由方法在第二阶段重试修,而不是让解析器猜。

    `ask()` 解析失败是**抛异常**的,而 `propose_and_apply` 的循环以前没有 try —— 于是
    异常一路穿到 `fail()`,第二阶段根本没机会修。实测:一条 `--rounds 5` 的运行,因为一次
    回复是 1029 字符的散文,跑完 round 1 就结束了。

    为什么不能靠解析器:`loads_tolerant` 已经修了**八种**实测的小模型破坏 JSON 的方式,而
    这份列表每遇到一种新的就要长一节 —— 扫描器只覆盖已经见过的情况。更糟的是,一个在散文里
    找「某个 JSON 对象」的扫描器,可能**静默挑中模型解释自己时举的例子**,而按那个例子去测
    比失败更糟。模型在被告诉哪里错了之后会自己修格式;那才是能推广的。

    这条测试同时钉住反馈**真的传回去了**:第二次提问里必须带着第一次的失败原因。
    """
    import editor

    base = _harness(tmp_path / "base")
    req = {"workspace": str(tmp_path / "ws"), "trajectory_out": str(tmp_path / "t.json"),
           "base_harness": str(base)}
    (tmp_path / "ws").mkdir()
    prompts: list[str] = []
    calls = {"n": 0}

    def fake_ask(prompt, system=None, base=None, skill=True):
        prompts.append(prompt)
        calls["n"] += 1
        if calls["n"] == 1:
            raise editor.UnparseableReply(
                "the model replied 1029 chars but no attempt parsed as JSON")
        return {"files": [{"path": "agent.py", "content": "print('fixed')\n"}],
                "hypothesis": "retried and produced the envelope"}

    orig = editor.ask
    editor.ask = fake_ask
    try:
        reply, dest, problems = editor.propose_and_apply(
            base, req, "system",
            lambda problems: "PROMPT" + editor.repair_block(
                problems, expect=editor.EDIT_EXPECT))
    finally:
        editor.ask = orig

    assert problems == "", f"解析失败没有被重试修好:{problems}"
    assert reply.get("files"), reply
    assert (dest / "agent.py").read_text() == "print('fixed')\n"
    assert len(prompts) == 2, prompts
    assert "could not be read as an edit sequence" in prompts[1], \
        "第二次提问没有带上失败原因 —— 那样模型只会再错一次"
    assert "JSON envelope" in prompts[1]


def test_one_stuck_step_cannot_consume_the_whole_budget(tmp_path):
    """一个卡住的 step 不能吃掉整条运行的额度。

    实测:一条 `--rounds 5` 的运行在第 4 步反复撞 `APITimeoutError`,跑到 **1 小时 31 分**
    还在继续。原因是我的上限写成了

        max_attempts = rounds * STEP_ATTEMPTS      # 15,是**总量**的界

    而注释说的是「一个 step 最多尝试几次」。步骤 1–3 各一次就成功,于是第 4 步继承了剩下的
    全部 12 次额度,每次都要等一个模型超时。

    现在界是**每步**的:一个走下坡的 step 试满 3 次就放弃整条运行(不能跳过它 —— 那会在曲线
    的轮次上留下一个说不清的洞),并且方法总共只被调用 3 次。
    """
    import os
    import subprocess

    method = tmp_path / "always_fails.py"
    method.write_text('''
import json, sys
from pathlib import Path
req = json.load(sys.stdin)
n = Path(req["workspace"]).parent / "calls.txt"
seen = int(n.read_text()) if n.exists() else 0
n.write_text(str(seen + 1))
Path(req["trajectory_out"]).write_text(json.dumps({
    "steps": [{"harness_dir": req["base_harness"], "label": "method error",
               "edit_kind": "none", "claimed_cost": {"generation_tokens": 0},
               "method_reported": {"error": "APITimeoutError: Request timed out."}}],
    "trajectory_shape": "sequence", "nominated": 0,
    "provenance": {"method": "always_fails", "acceptance_rule": {"text": "x",
                   "source": "test", "calibrated": False}}}))
sys.stdout.write(json.dumps({"changed": False, "files": [],
                             "hypothesis": "APITimeoutError", "stop": False}))
''', encoding="utf-8")
    runs = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "5", "--run-id", "stuck", "--dataset", "demo",
         "--sampling", "all", "--side", "train",
         "--runs-root", str(runs), "--work-root", str(tmp_path / "work"),
         "--method-entrypoint", f"{sys.executable} {method}"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
        env={**os.environ, "HG_AGENT_BACKEND": "mock"})
    combined = proc.stdout + proc.stderr

    # The method's scratch lives in a `mkdtemp` outside the work root, so its own
    # counter is not reachable from here. The log is the observable that matters: one
    # line per failed attempt.
    attempts = combined.count("the step did not finish")
    assert attempts == 3, (
        f"这一步被尝试了 {attempts} 次;每步的界是 3,而且第 1 步就卡住了,所以只该有 3 次"
        f"\n{combined[-800:]}")

    points = [json.loads(l) for l in
              (runs / "stuck" / "curve.jsonl").read_text().splitlines() if l.strip()]
    assert [p["round"] for p in points] == [0], [p["round"] for p in points]
    assert "gave up after 3 attempts" in combined, combined[-800:]
    meta = json.loads((runs / "stuck" / "run_meta.json").read_text())
    assert meta["failed_steps"] and meta["failed_steps"][-1]["round"] == 1


# ------------------------------- find/replace:小改动,不再整文件重生成 ---

def _tree(tmp_path: Path, body: str = "x = 1\n\ndef f():\n    return None\n"
                                        "\ndef g():\n    return None\n") -> Path:
    base = tmp_path / "base"
    base.mkdir()
    (base / "agent.py").write_text(body)
    (base / "harness.json").write_text('{"name": "probe", "version": "1", '
                                       '"entrypoint": "agent.py"}\n')
    return base


def test_a_replacement_applies_exactly_once(tmp_path):
    """**为什么需要这个。** 协议原本要求 `"content": "<complete new file>"`。

    实测(`deepseek-eigen-1`、`deepseek-focus-2`,两轮):模型回写的 agent.py 各删掉
    147 行和 133 行 —— 每一条 docstring、每一段"实测过什么"的注释 —— 而行为几乎没动。
    它不是偷懒:14 kB 的文件要塞进输出预算,被丢掉的正是散文。替换式编辑没法删掉它
    没有点名要删的东西。
    """
    base = _tree(tmp_path)
    applied, problems = apply(base, tmp_path / "d", [
        {"path": "agent.py", "find": "x = 1", "replace": "x = 42"}])
    assert applied == ["agent.py"] and not problems, problems
    text = (tmp_path / "d" / "agent.py").read_text()
    assert "x = 42" in text
    assert text.count("def ") == 2, "改动之外的函数不该被动到"


def test_an_ambiguous_replacement_is_refused_not_guessed(tmp_path):
    """`return None` 在 harness 里到处都是 —— 猜第一个会造出一个没人要求的候选。"""
    base = _tree(tmp_path)
    applied, problems = apply(base, tmp_path / "d", [
        {"path": "agent.py", "find": "return None", "replace": "return 1"}])
    assert applied == [] and "appears 2 times" in problems[0], problems


def test_a_replacement_that_does_not_match_is_refused(tmp_path):
    """找不到就要说找不到,并告诉模型"照抄要替换的那段原文"。"""
    base = _tree(tmp_path)
    applied, problems = apply(base, tmp_path / "d", [
        {"path": "agent.py", "find": "x = 2  # not there", "replace": "x = 3"}])
    assert applied == [] and "does not appear" in problems[0], problems


def test_a_replacement_may_not_touch_the_protected_file(tmp_path):
    """**守卫要挡的是路径,不是编辑的形状。** 第一版把路径检查放在整文件分支里,
    于是一个 `find/replace` 编辑可以改写 `harness.json` 而不遇到这条规则。"""
    base = _tree(tmp_path)
    applied, problems = apply(base, tmp_path / "d", [
        {"path": "harness.json", "find": '"version": "1"', "replace": '"version": "9"'}])
    assert applied == [] and "protected" in problems[0], problems


def test_a_replacement_may_not_climb_out_of_the_harness(tmp_path):
    base = _tree(tmp_path)
    applied, problems = apply(base, tmp_path / "d", [
        {"path": "../escape.py", "find": "a", "replace": "b"}])
    assert applied == [] and "outside the harness" in problems[0], problems


def test_a_new_file_is_still_created_with_content(tmp_path):
    """替换只能在已有文件上做;新建文件仍然用 `content`,这是两种形状的分工。"""
    base = _tree(tmp_path)
    applied, problems = apply(base, tmp_path / "d", [
        {"path": "helper.py", "content": "print('hi')\n"}])
    assert applied == ["helper.py"] and not problems, problems
    assert (tmp_path / "d" / "helper.py").read_text() == "print('hi')\n"


def test_a_replacement_on_a_missing_file_says_which_shape_to_use(tmp_path):
    base = _tree(tmp_path)
    applied, problems = apply(base, tmp_path / "d", [
        {"path": "gone.py", "find": "a", "replace": "b"}])
    assert applied == [] and "use `content`" in problems[0], problems


def test_an_edit_with_neither_shape_is_reported(tmp_path):
    base = _tree(tmp_path)
    applied, problems = apply(base, tmp_path / "d", [{"path": "agent.py"}])
    assert applied == [] and "neither" in problems[0], problems


def test_the_prompt_asks_for_a_replacement_first():
    """系统提示改了,行为才会改:整文件重生成是被提示词要求的。"""
    import editor
    assert "find" in editor.DEFAULT_SYSTEM and "replace" in editor.DEFAULT_SYSTEM
    assert "EXACTLY ONCE" in editor.DEFAULT_SYSTEM
