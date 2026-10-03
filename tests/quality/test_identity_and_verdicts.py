"""同一个 harness 必须只有一个名字;每一分必须带着它为什么是这一分。

两条不变量,都是被一次真实测量逼出来的(都发生在 `loop-terminal_bench-26452c`):

**身份。** 六轮的曲线报告了六个不同的 `identity.harness_sha`,而其中四轮的
`git rev-parse <sha>^{tree}` 完全相同、`diffs/round-N.patch` 全是 0 字节。原因是
身份用的是 **commit sha** —— 提交还带着作者时间戳和父提交,所以同样的内容每次
提交都换一个名字。代价不是"曲线不好看":缓存键是 `(harness_sha, task_id)`,
于是**缓存永远不命中**,同一份代码被实跑了四遍,agent 阶段在 98s 和 2841s 之间乱跳。

**归因。** 五道题全是 0.000,而 run 目录里没有任何一处能解释为什么。判分脚本的输出
(`verdict["detail"]`)和 harness 的 stderr 都已经算出来了,然后被丢掉。回答"为什么
0 分"只能靠人手工把题目自己的 test.sh 在平台外面重放一次。

这两条是同一类缺陷:**曲线报告了结论,却没有报告任何能解释结论的东西。**
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ckpt.git_state import commit_state, content_sha, init_repo, materialize  # noqa: E402


# ------------------------------------------------------------------ 身份 ---

def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (work / "harness.json").write_text(json.dumps(
        {"name": "probe", "version": "1", "entrypoint": "agent.py"}))
    (work / "agent.py").write_text("print('v0')\n")
    init_repo(work)
    return work


def test_identical_content_gets_one_identity(tmp_path):
    """这条如果失败,缓存就不存在,曲线上的"移动"可能是时间戳。"""
    work = _repo(tmp_path)
    first = commit_state(work, "H0")
    # 内容没动,再提交一次(真实场景:一轮"no change"照样走 commit_state)。
    second = commit_state(work, "round 1: no change")
    third = commit_state(work, "round 2: no change")
    assert first == second == third, (
        f"同一棵树得到了不同的身份:{first[:8]} {second[:8]} {third[:8]}")
    assert len(first) == 40


def test_different_content_gets_a_different_identity(tmp_path):
    """反向的一半:改了内容而身份不变,那才是真的糟。"""
    work = _repo(tmp_path)
    before = commit_state(work, "H0")
    (work / "agent.py").write_text("print('v1')\n")
    after = commit_state(work, "round 1")
    assert before != after


def test_the_harness_s_own_scratch_cannot_change_its_identity(tmp_path):
    """`.state/` 是 harness 做题时的草稿,不是 harness。

    它要是进了身份,一个写了缓存文件的 harness 每轮都换 sha,而没有任何方法改过它。
    """
    work = _repo(tmp_path)
    before = commit_state(work, "H0")
    (work / ".state").mkdir(exist_ok=True)
    (work / ".state" / "cache.json").write_text("{}")
    (work / "__pycache__").mkdir(exist_ok=True)
    (work / "__pycache__" / "agent.cpython-312.pyc").write_bytes(b"\x00")
    after = commit_state(work, "round 1")
    assert before == after, "harness 自己写出来的草稿改变了它的身份"


def test_the_identity_can_still_be_materialized(tmp_path):
    """身份换了含义,但"用身份把那一轮的完整 harness 取回来"必须还能用。

    `_stage_for_method` 就是拿 `identity.harness_sha` 去 `git archive` 的,所以一个
    我们自己算的哈希(而不是对象库里的对象)会把方法的状态窗口直接弄坏。
    """
    work = _repo(tmp_path)
    sha = commit_state(work, "H0")
    dest = tmp_path / "out"
    count = materialize(work, sha, dest)
    assert count >= 2, count
    assert (dest / "harness.json").is_file()
    assert (dest / "agent.py").read_text() == "print('v0')\n"


def test_content_sha_is_stable_without_a_new_commit(tmp_path):
    work = _repo(tmp_path)
    commit_state(work, "H0")
    assert content_sha(work) == content_sha(work)


# --------------------------------------------------- 真实 run 里的归因 ---

def _run(tmp_path: Path, run_id: str = "verdict-check"):
    runs = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "1", "--run-id", run_id, "--dataset", "demo",
         "--sampling", "all", "--runs-root", str(runs),
         "--work-root", str(tmp_path / "work"),
         "--method-entrypoint", f"{sys.executable} {ROOT / 'methods/noop/run.py'}"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={**os.environ, "HG_AGENT_BACKEND": "mock"})
    assert proc.returncode == 0, f"{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    return runs / run_id, proc


def test_every_task_gets_a_verdict_file_saying_why(tmp_path):
    """`per_task: {id: 0.0}` 不能是记录里仅有的东西。"""
    run_dir, _ = _run(tmp_path)
    dest = run_dir / "verdicts" / "round-0"
    assert dest.is_dir(), f"没有归因目录:{list(run_dir.iterdir())}"
    files = sorted(p.name for p in dest.glob("*.json"))
    assert files, "归因目录是空的"
    for name in files:
        entry = json.loads((dest / name).read_text())
        assert entry["task_id"] == name[:-5]
        # 这四个字段就是"为什么"。分数本身没有解释力。
        for field in ("score", "kind", "passed", "detail"):
            assert field in entry, (name, sorted(entry))
        assert entry["score"] is not None
        assert entry.get("harness"), (
            f"{name}: harness 的输出没有被记录 —— 这正是 0 分无法解释的那个缺口")


def test_round_0_and_later_rounds_both_record_verdicts(tmp_path):
    """round 0 和 round N 走的是两条代码路径(driver.main 与 _run_mode_a)。"""
    run_dir, _ = _run(tmp_path, "verdict-rounds")
    assert (run_dir / "verdicts" / "round-0").is_dir()
    assert (run_dir / "verdicts" / "round-1").is_dir(), \
        "轮内测量没有写归因 —— 只有 round 0 修了"


def test_the_check_s_own_output_reaches_the_event_stream(tmp_path):
    """面板要能看见它,不然"为什么 0"还是要靠终端考古。"""
    run_dir, _ = _run(tmp_path, "verdict-events")
    events = [json.loads(l) for l in
              (run_dir / "events.jsonl").read_text().splitlines() if l.strip()]
    logs = [e for e in events if e.get("kind") == "log"]
    assert logs, "事件流里没有任何 log 事件"
    assert {e["source"] for e in logs} == {"check"}, {e["source"] for e in logs}
    for e in logs:
        assert e["task"], e
        assert "round" in e, e
        assert e.get("detail") is not None or e.get("body") is not None, e


def test_a_run_whose_harness_did_not_change_reports_one_identity(tmp_path):
    """端到端:方法什么都不改,六轮的 sha 不该有六个。

    这一条是这次 bug 的复现。`methods/noop` 每一轮都返回"没有改动",所以 round 1
    和 round 2 的 harness 是同一棵树 —— 曲线必须说同一个名字。
    """
    runs = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "3", "--run-id", "identity-check", "--dataset", "demo",
         "--sampling", "all", "--runs-root", str(runs),
         "--work-root", str(tmp_path / "work"),
         "--method-entrypoint", f"{sys.executable} {ROOT / 'methods/noop/run.py'}"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={**os.environ, "HG_AGENT_BACKEND": "mock"})
    assert proc.returncode == 0, f"{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    points = [json.loads(l) for l in
              (runs / "identity-check" / "curve.jsonl").read_text().splitlines()
              if l.strip()]
    shas = [p["identity"]["harness_sha"] for p in points]
    assert len(shas) >= 2, shas
    assert len(set(shas)) == 1, (
        "noop 方法没有改动任何东西,曲线却报告了多个身份 —— "
        f"身份不是内容地址:{shas}")


def test_a_cached_round_still_explains_its_scores(tmp_path):
    """缓存命中的轮次也必须带着"为什么"。

    实测这个缺口的方式很直接:`loop-terminal_bench-41133` 的 round 5 全是缓存命中,
    五个 verdict 文件里 `kind=null`、`detail=""` —— 分数还在,解释没了。缓存里存的
    只有 score 和 trace,没有 verdict。
    """
    runs = tmp_path / "runs"
    argv = [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
            "--rounds", "2", "--run-id", "cache-verdict", "--dataset", "demo",
            "--sampling", "all", "--runs-root", str(runs),
            "--work-root", str(tmp_path / "work"),
            "--method-entrypoint", f"{sys.executable} {ROOT / 'methods/noop/run.py'}"]
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=300,
                          env={**os.environ, "HG_AGENT_BACKEND": "mock"})
    assert proc.returncode == 0, f"{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    # round 1 的 harness 与 round 0 相同(noop 不改),于是 round 1 是纯缓存命中。
    for rnd in (0, 1):
        d = runs / "cache-verdict" / "verdicts" / f"round-{rnd}"
        entries = [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]
        assert entries, f"round-{rnd} 没有 verdict 文件"
        for e in entries:
            assert e.get("kind"), f"round-{rnd} {e['task_id']}: 缓存命中后 kind 丢了"
            assert e.get("detail") is not None, (
                f"round-{rnd} {e['task_id']}: 缓存命中后 detail 丢了")


def test_the_point_names_the_skill_the_method_was_given(tmp_path, monkeypatch):
    """**同一个人,不同的说明书。** `improver` 说"用了哪个工具",说不出"工具被告知了什么"。

    实测:两次运行之间 `improvers/skill.md` 多了一条失败模式,方法名一样、improver 版本
    一样,编辑不一样,而两份记录里没有一个字解释这件事 —— 这正是身份块存在的意义。
    按调用读而不是 import 时缓存:平台可以在同一轮之内改自己的 skill。
    """
    import hashlib

    from harnessgrad import identity

    monkeypatch.setattr(identity, "PLATFORM_ROOT", tmp_path)
    (tmp_path / "improvers").mkdir()
    skill = tmp_path / "improvers" / "skill.md"
    skill.write_text("# v1\n")
    man = {"name": "probe", "version": "1"}
    first = identity._identity(man, "sha", "A")
    assert first["skill_sha"] == hashlib.sha256(b"# v1\n").hexdigest()

    skill.write_text("# v2\n")
    assert identity._identity(man, "sha", "A")["skill_sha"] != first["skill_sha"]

    skill.unlink()
    assert "skill_sha" not in identity._identity(man, "sha", "A"), \
        "没有 skill 就不该报一个空文件的哈希"
