"""改进器必须被找到、被验证、被记名,而且不能用 `PATH` 猜。

一个 method 是**一条决策规则 + 一个改进器**。在这次的解耦之前,平台只记录了规则用的
模型(`method_model`),于是一次"改进器升级"和一次"方法变强"在记录里长得一模一样。
这个文件测的是把改进器变成一等公民之后必须成立的四件事:

  * 机器上有好几个 codex 时,坏的那个**不能**被选中(`/usr/local/bin/codex` 实测
    0.125.0 缺平台二进制,`--version` 直接抛异常);
  * `latest` 是"别钉版本",不是"别记录"——解析出来的版本和 sha256 必须随曲线点走;
  * 改过的 codex 拿不到、也不该假装拿得到,但**必须能被声明**,而且声明了就要记下来;
  * 解析不了不能静默降级:一个自带改进器的方法照样能跑,而这个 run 记录里不能出现
    它并没有用过的改进器。
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

from tools import improver  # noqa: E402


# --------------------------------------------------------------- 解析 ---

def test_a_candidate_is_verified_by_running_it(tmp_path):
    """`--version` 跑不动的东西不能算候选 —— 那正是坏安装的失败形态。"""
    bad = tmp_path / "codex"
    bad.write_text("#!/bin/sh\necho 'Missing optional dependency' >&2\nexit 1\n")
    bad.chmod(0o755)
    ok, version, reason = improver._run_version(bad)
    assert not ok and not version
    assert "Missing optional dependency" in reason


def test_a_working_candidate_reports_its_version(tmp_path):
    good = tmp_path / "codex"
    good.write_text("#!/bin/sh\necho 'codex-cli 9.9.9'\n")
    good.chmod(0o755)
    ok, version, reason = improver._run_version(good)
    assert (ok, version, reason) == (True, "9.9.9", "")


def test_latest_picks_the_highest_verified_version_not_the_first_on_path(monkeypatch):
    """PATH 上恰好是坏的那份,所以"第一个找到的"是错的语义。"""
    made = [
        {"path": "/a/codex", "ok": True, "version": "0.125.0", "reason": "", "sha256": "x"},
        {"path": "/b/codex", "ok": False, "version": "", "reason": "broken", "sha256": None},
        {"path": "/c/codex", "ok": True, "version": "0.156.1", "reason": "", "sha256": "y"},
    ]

    class _C:
        """和 `Candidate` 同形状。**不要**把 sha256 放进 `__dict__`:那会盖住方法。"""

        def __init__(self, d):
            self.path = Path(d["path"])
            self.ok = d["ok"]
            self.version = d["version"]
            self.reason = d["reason"]

        def as_dict(self):
            return {"path": str(self.path), "ok": self.ok, "version": self.version,
                    "reason": self.reason}

        def sha256(self):
            return "deadbeef"

    monkeypatch.setattr(improver, "candidates", lambda extra=None: [_C(d) for d in made])
    got = improver.resolve({"default": "codex", "improvers": {"codex": {}}}, version="latest")
    assert got["ok"] and got["version"] == "0.156.1", got


def test_asking_for_a_version_that_is_not_here_is_a_refusal(monkeypatch):
    """静默换成另一个版本,就是让记录撒谎。"""
    class _C:
        def __init__(self, version):
            self.path = Path(f"/x/{version}/codex")
            self.ok, self.version, self.reason = True, version, ""

        def as_dict(self):
            return {"path": str(self.path), "ok": True, "version": self.version,
                    "reason": "", "sha256": "z"}

        def sha256(self):
            return "z"

    monkeypatch.setattr(improver, "candidates",
                        lambda extra=None: [_C("0.139.0")])
    got = improver.resolve({"default": "codex", "improvers": {"codex": {}}},
                           version="0.156.1")
    assert not got["ok"]
    assert "0.156.1" in got["reason"]
    assert got["candidates"], "拒绝时必须列出它到底找到了什么"


def test_no_working_candidate_at_all_is_a_refusal(monkeypatch):
    monkeypatch.setattr(improver, "candidates", lambda extra=None: [])
    got = improver.resolve({"default": "codex", "improvers": {"codex": {}}})
    assert not got["ok"] and "no working codex" in got["reason"]


# ----------------------------------------------------------- 记名 ---

def test_the_resolved_model_comes_from_the_improver_s_own_config(tmp_path, monkeypatch):
    """codex 从自己的 config.toml 读模型,所以 `HG_METHOD_MODEL` 说什么都不算数。"""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-6-sol"\nmodel_provider = "OPENAI"\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert improver.declared_model({"default": "codex", "improvers": {"codex": {}}}) == "gpt-6-sol"


def test_a_modified_improver_can_be_declared_and_is_visible(monkeypatch):
    """拿不到别人的二进制,但可以不让他匿名。

    平台对 harness 就是这个立场:它不能判断一个 harness 写得好不好,但它能说清楚
    跑的是哪一份字节。改过的 codex 同理 —— 声明 `modified: true`,解析结果里就得有
    这个事实,曲线点才会带着它。
    """
    class _C:
        def __init__(self):
            self.path = Path("/opt/my-codex/codex")
            self.ok, self.version, self.reason = True, "0.156.1", ""

        def as_dict(self):
            return {"path": str(self.path), "ok": True, "version": self.version,
                    "reason": "", "sha256": "abc"}

        def sha256(self):
            return "abc"

    monkeypatch.setattr(improver, "candidates", lambda extra=None: [_C()])
    monkeypatch.setattr(improver, "declared_model", lambda config=None, source=None: "my-model")
    cfg = {"default": "mine",
           "improvers": {"mine": {"name": "mine", "path": "/opt/my-codex/codex",
                                  "modified": True}}}
    got = improver.resolve(cfg)
    assert got["ok"] and got["modified"] is True, got
    assert got["name"] == "mine" and got["model"] == "my-model"


# --------------------------------------------- 真实 run 里的身份 ---

def test_a_real_run_records_the_improver_on_every_point(tmp_path):
    runs = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "driver.py", "--harness", "loop_plain", "--mode", "A",
         "--rounds", "1", "--run-id", "improver-id", "--dataset", "demo",
         "--sampling", "all", "--runs-root", str(runs),
         "--work-root", str(tmp_path / "work"),
         "--method-entrypoint", f"{sys.executable} {ROOT / 'methods/noop/run.py'}"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={**__import__("os").environ, "HG_AGENT_BACKEND": "mock"})
    assert proc.returncode == 0, f"{proc.stdout[-600:]}\n{proc.stderr[-600:]}"
    points = [json.loads(l) for l in
              (runs / "improver-id" / "curve.jsonl").read_text().splitlines() if l.strip()]
    assert points
    for p in points:
        imp = (p.get("identity") or {}).get("improver")
        if imp is None:
            # 这台机器上没有可用的改进器:那么一个 run 也不能声称自己用过谁。
            assert "no improver" in proc.stdout + proc.stderr, \
                "曲线上没有 improver,但日志里也没有说为什么"
            continue
        # `ok`/`reason` 是本次进程的簿记,不是记录该带的东西。
        assert "ok" not in imp and "reason" not in imp, imp
        for field in ("name", "version", "sha256"):
            assert imp.get(field), (field, imp)


# ------------------------------------------- 改进器的运行环境(不是身份) ---

def test_runtime_trees_names_the_package_and_not_the_launcher(tmp_path):
    """一个 npm CLI 是"包装脚本 + 旁边的原生二进制",所以绑文件不够。

    实测:`codex.js` 只负责转交给同一个包里 vendor/ 下面的原生二进制。只绑那个 .js
    文件,方法在沙箱里照样跑不起来 —— 报错还是 `FileNotFoundError`,看起来像方法坏了。
    """
    package = tmp_path / "lib" / "node_modules" / "@openai" / "codex" / "bin"
    package.mkdir(parents=True)
    wrapper = package / "codex.js"
    wrapper.write_text("#!/usr/bin/env node\n")
    trees = improver.runtime_trees(wrapper)
    assert str(tmp_path / "lib" / "node_modules") in trees, trees
    assert str(package) not in trees, "绑了 launcher 所在目录,旁边那个包还是读不到"


def test_runtime_trees_follows_the_shebang_to_the_interpreter(tmp_path, monkeypatch):
    """`#!/usr/bin/env node` 里的 node 在这台机器上不在 /usr 下面,而在 $HOME 里。

    沙箱绑了 `/usr`、`/bin`、`/lib` 和 Python 前缀,别的运行时必须被点名,而"需要什么
    运行时"只有脚本第一行知道。
    """
    node = tmp_path / "node-v22" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\n")
    node.chmod(0o755)
    monkeypatch.setattr(improver.shutil, "which", lambda name: str(node) if name == "node" else None)
    script = tmp_path / "pkg" / "cli.js"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env node\n")
    trees = improver.runtime_trees(script)
    assert str(tmp_path / "node-v22") in trees, trees


def test_runtime_trees_includes_the_config_home_that_holds_the_credentials(
        tmp_path, monkeypatch):
    """没有 config.toml 和 auth.json,会话就没有 provider 也没有钥匙。

    方法会把这两份文件拷进自己的 CODEX_HOME,所以它们必须在方法的命名空间里可读;
    否则失败长得像"方法调不动模型"。
    """
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    script = tmp_path / "pkg" / "cli.js"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    assert str(home) in improver.runtime_trees(script)


def test_the_method_sandbox_binds_the_improver_but_never_the_platform(tmp_path):
    """`runtime` 是"方法必须能执行的工具",不是"把平台还回去"。

    两条护栏:平台自己的路径永远不进 `visible_paths`(那会让整个隐藏失效),`/usr`
    这类已经绑过的树不重复绑(计划的读者是人)。而工具那棵树必须真的被绑,否则
    `--improver codex` 只能在 `--no-sandbox` 下用 —— 那正是它之前的状态。
    """
    import harnessgrad.methods as methods
    tool = tmp_path / "node_modules" / "@openai" / "codex"
    tool.mkdir(parents=True)
    plan = methods._method_sandbox_plan(
        tmp_path / "work", [sys.executable, str(ROOT / "methods" / "llm_improver" / "run.py")],
        tmp_path / "scratch",
        runtime=(str(tool), str(ROOT / "tools"), "/usr", str(ROOT / "harnessgrad")))
    assert plan["runtime_paths"] == (tool,), plan["runtime_paths"]
    assert tool in plan["visible_paths"]
    assert ROOT / "tools" not in plan["visible_paths"]
    assert ROOT / "harnessgrad" not in plan["visible_paths"]


def test_a_named_endpoint_improver_declares_its_own_url_model_and_key(monkeypatch):
    """`--improver deepseek` 不能在记录里写 deepseek,而方法实际打的是 `.env` 里的本地端点。

    这条和 harness 误报自己模型是同一类 bug,修法也一样:平台把事实写在消费者读的地方。
    注册项声明端点/模型/钥匙变量,`.env` 只是通用兜底 —— 一个**具名**的改进器被 `.env`
    悄悄改道,正是"记录说 A、实际跑 B"的形态。
    """
    # `.env` 会被 driver 提前塞进 os.environ,所以在真实运行里这两个变量**是**设着的 ——
    # 第一版测试把它们删掉才通过,于是漏掉了实测到的那次错误:具名改进器的端点被 .env
    # 覆盖,记录写 deepseek、方法实际打本地 vLLM。
    monkeypatch.setenv("HG_METHOD_BASE_URL", "http://127.0.0.1:8001/v1")
    monkeypatch.setenv("HG_METHOD_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k" * 12)
    cfg = {"default": "deepseek", "improvers": {"deepseek": {
        "name": "deepseek", "kind": "openai-compatible", "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"}}}
    got = improver.resolve(cfg, requested="deepseek")
    assert got["ok"], got
    assert got["path"] == "https://api.deepseek.com/v1"
    assert got["model"] == "deepseek-v4-pro"
    assert got["api_key_env"] == "DEEPSEEK_API_KEY"


def test_an_endpoint_improver_without_its_credential_is_refused_by_name(monkeypatch):
    """没有钥匙要在**门口**拒绝,而不是花掉一轮之后在方法里报一个像方法坏了的错。"""
    monkeypatch.setenv("HG_METHOD_BASE_URL", "http://127.0.0.1:8001/v1")
    monkeypatch.setenv("HG_METHOD_MODEL", "qwen3.5-9b-local")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = {"default": "deepseek", "improvers": {"deepseek": {
        "name": "deepseek", "kind": "openai-compatible", "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"}}}
    got = improver.resolve(cfg, requested="deepseek")
    assert not got["ok"] and "DEEPSEEK_API_KEY" in got["reason"], got


def test_the_method_environment_is_pointed_at_the_resolved_endpoint(monkeypatch):
    """方法侧的客户端读 `HG_METHOD_*`,所以平台必须把解析出来的端点写进去。

    少了这一步,`HG_METHOD_*` 会来自 `.env`,而曲线点上是注册表里的名字 —— 记录和实际
    调用的端点就不是同一个东西了。钥匙从注册表点名的变量复制,不进记录。
    """
    import harnessgrad.methods as methods
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-value")
    env = methods._method_env(Path("/tmp"), {
        "name": "deepseek", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro", "api_key_env": "DEEPSEEK_API_KEY"})
    assert env["HG_METHOD_BASE_URL"] == "https://api.deepseek.com/v1"
    assert env["HG_METHOD_MODEL"] == "deepseek-v4-pro"
    assert env["HG_METHOD_API_KEY"] == "secret-value"
    # 没有端点(例如 codex 这种 CLI 改进器)时不能改动方法自己的配置
    plain = methods._method_env(Path("/tmp"), {"name": "codex", "path": "/usr/bin/codex"})
    assert "HG_METHOD_BASE_URL" not in plain or \
        plain["HG_METHOD_BASE_URL"] == os.environ.get("HG_METHOD_BASE_URL")


def test_a_model_the_project_does_not_use_is_refused_by_name(monkeypatch):
    """项目规定只用 `deepseek-flash`。这条不能是"大家记得",必须是一次拒绝。

    理由是可比的:一个悄悄用了另一个模型的 run,会在曲线上留一个谁也没法与之比较的数字,
    而记录里没有一个字说明这件事。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k" * 12)
    cfg = {"default": "deepseek", "_forbidden_models": {"deepseek-v4-pro": "we use flash"},
           "improvers": {"deepseek": {"name": "deepseek", "kind": "openai-compatible",
                                      "model": "deepseek-flash",
                                      "base_url": "https://api.deepseek.com/v1",
                                      "api_key_env": "DEEPSEEK_API_KEY"}}}
    got = improver.resolve(cfg, requested="deepseek", model_override="deepseek-v4-pro")
    assert not got["ok"] and "not allowed" in got["reason"], got
    ok = improver.resolve(cfg, requested="deepseek")
    assert ok["ok"] and ok["model"] == "deepseek-flash", ok


def test_the_registry_itself_ships_flash_and_names_the_banned_model():
    """注册表是这条规则的家:`--improver deepseek` 不指定模型时必须就是 flash。"""
    cfg = improver.load_config()
    assert cfg["improvers"]["deepseek"]["model"] == "deepseek-flash"
    assert "deepseek-v4-pro" in cfg.get("_forbidden_models", {})
    assert improver.forbidden_reason(cfg, "deepseek-flash") == ""
    assert improver.forbidden_reason(cfg, "deepseek-v4-pro") != ""
