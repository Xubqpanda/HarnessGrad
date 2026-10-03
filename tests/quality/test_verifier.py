"""任务 = 环境 + 验证器(INTERFACE.md §2.4)。这里钉的是它凭什么可信。

三条主张,每一条都必须由测试证明,而不是由文档承诺:

  1. **只有 `answer` 的数据集行为一字不变。** 加这一层不许动任何已有数据集的数。
  2. **验证器的输入在验证时重建。** harness 在工作目录里写什么都行,包括改测试 ——
     所以读它留下的那份测试来评分,等于让它自己给自己打分。这一条是整套设计成不成立
     的地方,也是这个文件里最重要的一条测试。
  3. **退出码与分数彼此独立。** harness 崩了但把活干完了,照样得分:平台评的是**产物**,
     不是**过程**。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import data.registry as registry                                  # noqa: E402
from eval.runner import evaluate, run_one                          # noqa: E402


def _harness(tmp_path: Path, body: str, *, exit_code: int = 0) -> Path:
    """A minimal harness whose `agent.py` does whatever `body` says.

    Written to a temp tree rather than taken from `base_harness/`, because these tests
    are about what the *platform* does with a harness's artifacts — using a real
    harness would make a failure ambiguous between the two.
    """
    repo = tmp_path / "harness"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "harness.json").write_text(json.dumps(
        {"name": "probe", "version": "1", "entrypoint": "agent.py"}))
    (repo / "agent.py").write_text(
        "import argparse, pathlib, sys\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--task'); ap.add_argument('--workdir')\n"
        "a = ap.parse_args()\n"
        "work = pathlib.Path(a.workdir)\n"
        f"{body}\n"
        f"sys.exit({exit_code})\n")
    return repo


def _task(tid: str) -> dict:
    return {"task_id": tid, "goal": "see the dataset"}


# ------------------------------------------------ 1) answer 路径一字不变 ---

def test_the_datasets_that_predate_verifiers_behave_exactly_as_before():
    """`SCORABLE` 不是另一套机制,它就是短写的 `answer` 验证器。

    这条是向后兼容的落点:两个老数据集声明的东西一个字没改,而它们现在走的是新代码。
    """
    tasks, setups, verifiers = registry.load_tasks("probe_set")
    assert setups == {}, "probe_set 不该有 setup"
    assert all(v == {"kind": "answer", "expected": v["expected"]}
               for v in verifiers.values())
    assert verifiers["d01"]["expected"] == "42"
    # demo 也一样,而且它的 split 不受影响
    _, _, v2 = registry.load_tasks("demo")
    assert all(v["kind"] == "answer" for v in v2.values())


def test_an_answer_verifier_grades_on_the_artifact_not_the_exit_code(tmp_path):
    """留下正确答案但崩掉的 harness 仍然得分 —— 平台评产物,不评过程。"""
    repo = _harness(tmp_path, "work.joinpath('answer.txt').write_text('alpha')\n",
                    exit_code=3)
    r = run_one(repo, _task("t"), sandbox=False,
                verifier={"kind": "answer", "expected": "alpha"})
    assert r["exit_code"] == 3, "退出码要如实记录"
    assert r["verdict"]["passed"] is True, "分数不该被退出码影响"


# ------------------------------------------------ 2) 新契约真的能用 ---

def test_a_setup_and_a_command_verifier_work_end_to_end(tmp_path):
    """铺环境 → harness 干活 → 平台在环境里跑检查。"""
    repo = _harness(tmp_path, "work.joinpath('report.txt').write_text('harness ok')\n")
    r = run_one(repo, _task("v02"), sandbox=False,
                setup={"files": []},
                verifier={"kind": "command", "argv": [
                    "python3", "-c",
                    "import pathlib,sys;"
                    "sys.exit(0 if pathlib.Path('report.txt').read_text().strip()"
                    "=='harness ok' else 1)"]})
    assert r["verdict"]["passed"] is True, r["verdict"]


def test_setup_materialises_the_task_before_the_harness_runs(tmp_path):
    """任务的环境是平台铺的,不是 harness 自己造的 —— 否则任务就没有"给定条件"。"""
    repo = _harness(tmp_path, "print(work.joinpath('seed.txt').read_text().strip())\n")
    r = run_one(repo, _task("v"), sandbox=False,
                setup={"files": [{"path": "seed.txt", "content": "given\n"}]},
                verifier={"kind": "answer", "expected": "x"})
    assert r["verdict"]["kind"] == "answer", "没铺成功的话 harness 会崩,这里就白测了"


# ------------------------------ 3) 最重要的那一条:改测试不算数 ---

def test_a_harness_that_rewrites_the_task_s_tests_still_fails(tmp_path):
    """**这套设计成不成立就看这一条。**

    harness 对自己的工作目录有完全写权限,任务自带的测试就在里面。如果验证器读的是
    harness 留下的那份测试,那么"把测试改成永远通过"就是最省事的得分方式 —— 而且从
    分数上完全看不出来。

    所以验证器在跑之前**重铺自己的输入**。这里用一个真实的 harness 证明它:它把
    `test_broken.py` 换成一个永远通过的版本,而 `broken.py` 一行没动。
    """
    body = (
        "work.joinpath('test_broken.py').write_text("
        "'print(\"all good\")\\n')\n"                     # the cheat
        "work.joinpath('answer.txt').write_text('done')\n"
    )
    repo = _harness(tmp_path, body)
    tasks, setups, verifiers = registry.load_tasks("verify_demo")
    task = next(t for t in tasks if t["task_id"] == "v01")

    r = run_one(repo, task, sandbox=False,
                setup=setups["v01"], verifier=verifiers["v01"])

    assert r["verdict"]["passed"] is False, (
        "harness 改掉了任务自带的测试,验证器却判过了 —— 那评分就是假的;"
        f"verdict={r['verdict']}")


def test_and_the_same_task_passes_when_the_harness_actually_fixes_it(tmp_path):
    """上一条的反向对照:同样的检查,真的把 bug 修好就通过。

    没有这一条,上一条可能只是因为环境铺错了、命令跑不起来之类的原因"恰好失败"。
    """
    body = (
        "work.joinpath('broken.py').write_text("
        "'def add(a, b):\\n    return a + b\\n')\n"
    )
    repo = _harness(tmp_path, body)
    tasks, setups, verifiers = registry.load_tasks("verify_demo")
    task = next(t for t in tasks if t["task_id"] == "v01")

    r = run_one(repo, task, sandbox=False,
                setup=setups["v01"], verifier=verifiers["v01"])
    assert r["verdict"]["passed"] is True, r["verdict"]


# ------------------------------------------------ 4) 通道里不许有验证器 ---

def test_the_task_dict_never_carries_the_grade():
    """`TASKS` 会被原样写成 `task.json` 交给 harness,所以它里面不能有秘密。

    注册表在加载时就拒绝,而不是等到某个数据集不小心把 `expected` 写进 task 里、
    跑了一轮之后才发现。
    """
    import importlib
    mod = importlib.import_module("data.verify_demo")
    for task in mod.TASKS:
        assert not ({"verify", "verifier", "setup", "expected", "scorable"}
                    & set(task)), f"task {task['task_id']} 里带了不该带的东西"


def test_a_dataset_that_leaks_into_the_task_dict_is_refused(tmp_path, monkeypatch):
    """反向验证上面那条:真的放进去,注册表必须当场拒。"""
    bad = tmp_path / "data"
    bad.mkdir()
    (bad / "__init__.py").write_text("")
    (bad / "leaky.py").write_text(
        "TASKS = [{'task_id': 'x', 'goal': 'g', 'expected': 'secret'}]\n"
        "SCORABLE = {'x': 'secret'}\n"
        "def load():\n    return TASKS, SCORABLE\n")
    # `data` is already imported as the real package, so the temp module has to be
    # reachable *through it* -- a sys.path entry would not be consulted for a
    # submodule of an already-imported package.
    import data as data_pkg
    monkeypatch.setattr(data_pkg, "__path__",
                        [str(tmp_path / "data"), *list(data_pkg.__path__)])
    monkeypatch.delitem(sys.modules, "data.leaky", raising=False)
    with pytest.raises(ValueError) as exc:
        registry.load_tasks("leaky")
    assert "expected" in str(exc.value), exc.value


# ------------------------------------------------ 5) 曲线点只带种类 ---

def test_curve_points_carry_the_verifier_kind_and_not_its_arguments(tmp_path):
    """曲线点会被写进方法能读的通道,所以只能有**种类**。

    argv 或 expected 出现在那里,等于把方法正在被怎么评分告诉它 —— 和扣住 eval trace
    是同一类泄漏。
    """
    runs = tmp_path / "runs"
    import os
    import subprocess
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "1", "--run-id", "vkind", "--dataset", "verify_demo",
         "--sampling", "all", "--runs-root", str(runs),
         "--work-root", str(tmp_path / "work"),
         "--method-entrypoint", f"{sys.executable} {ROOT / 'methods/noop/run.py'}"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={**os.environ, "HG_AGENT_BACKEND": "mock"})
    assert proc.returncode == 0, f"{proc.stdout[-500:]}\n{proc.stderr[-500:]}"

    point = json.loads((runs / "vkind" / "curve.jsonl").read_text().splitlines()[0])
    # `verify_demo` 的 train 侧是 v01/v02,两个都是 `command`;`answer` 属于 eval 侧的
    # v03。这条断言曾经写的是 {"answer", "command"} —— 那是把整个数据集的种类记在
    # 一次只跑了 train 的点上,于是曲线点声称自己被一个从未运行过的 `answer` 验证器
    # 评过分。种类是**这次运行**用什么评的,不是数据集里有什么。
    assert set(point["verifier_kinds"]) == {"command"}, point["verifier_kinds"]
    assert "answer" not in point["verifier_kinds"], \
        "曲线点带上了另一侧的验证器种类 —— 那不是这次运行的评分方式"
    blob = json.dumps(point)
    assert "test_broken" not in blob and "report.txt" not in blob, \
        "曲线的记录里出现了验证器的参数"


# ------------------------------ 6) 沙箱内也要成立 ---

@pytest.mark.parametrize("label,body,expected", [
    ("修好了", "work.joinpath('broken.py').write_text("
               "'def add(a, b):\\n    return a + b\\n')\n", True),
    ("把测试改成永远通过", "work.joinpath('test_broken.py').write_text("
                        "'print(\"ok\")\\n')\n", False),
    ("什么都不做", "pass\n", False),
])
def test_the_same_rules_hold_inside_the_sandbox(tmp_path, monkeypatch,
                                               label, body, expected):
    """验证是在**沙箱里**跑的:工作目录只读、平台仍然藏住。

    上面几条用的是 `sandbox=False`,只证明"重铺输入"这个逻辑对。这一条走真实路径,
    证明只读挂载和平台隐藏也成立 —— 否则作弊的 harness 还是可以绕过重铺。

    `HARNESSGRAD_WORK_ROOT` 必须设成这棵临时树的祖先,因为沙箱的隐藏逻辑是从它推出来的;
    不设的话 `run_one` 会退回 `repo.parent.parent`,那是 pytest 的临时目录,隐藏照样成立,
    但显式设出来能让这个测试说明自己在依赖什么。
    """
    monkeypatch.setenv("HARNESSGRAD_WORK_ROOT", str(tmp_path))
    repo = _harness(tmp_path, body)
    tasks, setups, verifiers = registry.load_tasks("verify_demo")
    task = next(t for t in tasks if t["task_id"] == "v01")

    r = run_one(repo, task, sandbox=True,
                setup=setups["v01"], verifier=verifiers["v01"])
    assert r["verdict"]["passed"] is expected, (label, r["verdict"])


# ------------------------------------- 判分要留下"哪一条失败、为什么" ---

def test_a_check_report_is_summarised_into_the_verdict():
    """`reward 0` 与"哪条测试失败、断言说了什么"是两件事,而方法只能对后者动手。

    实测:`cancel-async-tasks` 的 verdict 全文是 `Setting up libcurl4 ... Processing
    triggers for libc-bin` —— 检查在跑测试之前先 `apt-get install`,把测试报告顶出了
    最后 600 字符。改进器因此只能猜。
    """
    from eval.runner import _test_report_failures
    report = {"results": {
        "summary": {"tests": 6, "passed": 5, "failed": 1, "skipped": 0},
        "tests": [
            {"name": "test_run_py_file_exists", "status": "passed"},
            # `message` 是 pytest-json-ctrf 的套话,真正的断言在 `trace` 的 `E ` 行里。
            {"name": "test_tasks_cancel_above_max_concurrent", "status": "failed",
             "message": "The test failed in the call phase due to an assertion error",
             "trace": "def test_x():\n>       assert started == [1, 2]\n"
                      "E       assert [] == [1, 2]\n\nmore frames"},
            {"name": "test_other", "status": "skipped"},
        ]}}
    failures, counts = _test_report_failures(report)
    assert failures == ["test_tasks_cancel_above_max_concurrent: "
                        "assert [] == [1, 2]"], failures
    assert counts == "5 passed 1 failed", counts


def test_an_unreadable_check_report_cannot_take_the_verdict_down():
    """这是别人的格式,读不了只能少说一句,不能让整个 verdict 变成异常。"""
    from eval.runner import _test_report_failures
    for junk in (None, {}, {"results": None}, {"results": {"tests": "nope"}},
                 {"results": {"tests": [None, {"status": "failed"}]}}):
        failures, counts = _test_report_failures(junk)
        assert isinstance(failures, list) and isinstance(counts, str)
    assert _test_report_failures({"results": {"tests": [{"status": "failed"}]}}) == (["?"], "")


def test_both_of_a_check_s_streams_reach_the_verdict():
    """stderr 以前整个被丢掉,而"检查根本没能跑"这句话正是在 stderr 里。

    实测:`break-filter-js-from-html` 的 `uvx: command not found` 在 stderr,stdout 里
    是 apt 的进度,于是"检查没跑成"读起来像"harness 答错了"。
    """
    from eval.runner import _check_output_detail
    detail = _check_output_detail("apt progress\n", "uvx: command not found\n")
    assert "stdout apt progress" in detail and "stderr uvx: command not found" in detail
    assert _check_output_detail("", "only stderr") == "stderr only stderr"
    assert _check_output_detail("  \n", "") == ""


def test_the_terminal_bench_tasks_declare_where_their_report_lands():
    """判分报告是任务的 `test.sh` 写的,所以由数据集声明,平台不能猜。"""
    import data.terminal_bench as tb
    spec = tb.VERIFY["cancel-async-tasks"]
    assert spec["evidence_file"] == tb.CHECK_REPORT
    assert spec["reward_file"] == tb.REWARD_FILE


def test_a_configured_egress_proxy_reaches_the_check_and_only_the_check(monkeypatch):
    """83/89 道 TB 题的检查要靠 `curl https://astral.sh/... | sh` 自己装 pytest。

    这台机器上直连不到(容器里 403 / 宿主机 TLS 超时),于是**检查根本没跑**,而记录里那是
    "harness 答错了"。`HG_EGRESS_PROXY` 是操作员说"检查的抓取走这里"。

    只给检查,不给 harness:harness 的模型调用走平台自己的路(host-local 时是 model
    gateway),给那条路加代理会把它弄坏。
    """
    import importlib
    import eval.runner as runner
    importlib.reload(runner)
    monkeypatch.delenv("HG_EGRESS_PROXY", raising=False)
    assert runner._check_env() == {}, "没配置就不能改变任何行为"
    monkeypatch.setenv("HG_EGRESS_PROXY", "http://172.17.0.1:7890")
    env = runner._check_env()
    # apt 读小写,curl 偏好大写,uv 两个都认 —— 四种拼写都要给。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[key] == "http://172.17.0.1:7890", (key, env)
    assert "127.0.0.1" in env["no_proxy"] and "127.0.0.1" in env["NO_PROXY"]
