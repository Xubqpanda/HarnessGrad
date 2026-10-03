"""Calling an external method, and keeping it out of the platform.

A method is code the platform did not write. It runs in the same sandbox a harness does,
with one difference: the platform is hidden **except** `methods/`, because a method cannot
be given less than its own program and must not be given more.

Two checks live here and they are different in kind. `_check_method_entrypoint` refuses an
unusable entrypoint at the door, before anything has been paid for. `_call_method` hashes
the platform around every call, because detection is what is actually available when the
method runs as the same user -- so the guarantee is "a run whose method changed the referee
is not a measurement", made loudly instead of silently.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys

from eval.integrity import diff
from eval.integrity import snapshot
from harnessgrad import PLATFORM_ROOT
import eval.sandbox as sandbox

def _check_method_entrypoint(argv: list[str]) -> str | None:
    """一个相对的脚本路径会让方法静默地跑不起来,所以在这里挡住。

    方法是在 harness 的工作树里执行的(`cwd=work`),所以 `python methods/x/run.py`
    会被解析成 `<work>/methods/x/run.py` —— 那个文件不存在,方法以 exit 2 结束,
    而平台把它记成"方法写不出 trajectory"。现象是**曲线停在 round 0,没有任何
    东西说入口找错了**。

    实测:我自己就这么写了一次,并且在"53 个测试全绿"的情况下没发现。所以这里
    不猜、也不自动改写(猜错会更难查),而是拒绝并说清正确写法。

    返回错误信息,或者 None 表示没问题。
    """
    for part in argv:
        # 只看像脚本的参数。路径可以出现在任何位置:解释器、-m、或者包装脚本。
        if not part.endswith(".py"):
            continue
        if Path(part).is_absolute():
            continue
        resolved = Path(part)
        hint = ""
        if resolved.exists():
            # 它相对于当前目录是存在的,那相对工作树多半就不存在了
            hint = f"(它相对于当前目录存在,但方法不在当前目录里执行:{resolved.resolve()})"
        return (f"method entrypoint names a relative script path: {part!r} {hint}\n"
                f"  The method runs with cwd set to the harness working tree, so a\n"
                f"  relative path resolves there and does not exist. Use an absolute\n"
                f"  path: python {PLATFORM_ROOT}/methods/<name>/run.py")
    return None

def _method_sandbox_plan(work: Path, argv: list[str], scratch: Path,
                         readonly: tuple[Path, ...] = (),
                         runtime: tuple[str, ...] = ()) -> dict:
    """What a method is allowed to see, expressed for `eval/sandbox.py`.

    A method used to run as an ordinary subprocess with the whole platform on disk.
    Measured, with a probe placed where a real method lives (`methods/<name>/run.py`):
    it could read `data/probe_set.py` -- **the expected answer for every task,
    including the eval side this platform goes to some trouble to withhold** -- along
    with `.env` and `eval/runner.py`, and it held both API keys. The only check was a
    hash either side of the call, which sees writes and not reads.

    So a method is sandboxed like the harness, with one difference: the platform is
    hidden **except** the directory its own program lives in. It cannot be given less
    than that and still run, and it must not be given more.

    `runtime` is the second thing it cannot be given less of, and it was missing for
    exactly as long as the improver layer has existed. A method whose contribution is an
    external tool -- `methods/cli_improver` runs whatever the platform resolved -- needs that
    tool's trees on its disk. Measured with `--improver codex`: round 1 died three times
    with `exit 1`, once because the method's own resolver lives under the hidden
    platform, and again with `FileNotFoundError: …/codex.js` because the namespace binds
    nothing under `$HOME`. The improver layer was usable only with `--no-sandbox`, so
    nobody had used it. The trees come from `tools/improver.py` (`runtime_trees`), which
    is where the improver was resolved and the only place that knows what it needs.
    """
    # Platform paths that survive the hide: its own program, and nothing else. Plus
    # any path the caller names that must be visible but not writable.
    visible = [PLATFORM_ROOT / "methods", *readonly]
    # Its own script, wherever it is -- a test fixture's method lives outside the
    # platform, and a method that cannot be read cannot start.
    for arg in argv:
        candidate = Path(arg)
        if candidate.is_absolute() and candidate.is_file():
            visible.append(candidate.parent)
    # **The improver's trees**, bound read-only like the method's own program. Two
    # guards: never a tree the platform lives in (that would undo the hide), and never a
    # tree the namespace already binds (`/usr`, the Python prefix), because a duplicate
    # mount is noise in a plan that is read by people.
    tools = []
    for entry in runtime:
        tree = Path(entry)
        if not tree.is_dir() or tree == Path("/"):
            continue
        if tree == PLATFORM_ROOT or PLATFORM_ROOT in tree.parents:
            continue
        if any(tree == Path(r) or Path(r) in tree.parents for r in sandbox.RUNTIME):
            continue
        tools.append(tree)
    visible.extend(tools)
    return {
        "platform": PLATFORM_ROOT,
        "runtime_paths": tuple(tools),
        "work_root": Path(os.environ.get("HARNESSGRAD_WORK_ROOT")
                          or work.parent.parent),
        # The whole method directory is writable, because the trajectory lands beside
        # the workspace rather than inside it. `readonly` is re-bound *after* this, and
        # a later mount wins -- which is how `base_harness` stays untouchable while its
        # sibling is the method's scratch.
        "task_dir": scratch,
        # **Not optional.** `eval/sandbox.py` reads `repo=None` as "the repo is the
        # platform" and binds it read-only -- so leaving this out handed the platform
        # straight back. Measured: with `repo` omitted, a probe method still read
        # `data/probe_set.py` and `.env`, and the sandbox looked like it was working.
        #
        # **A known footgun, measured, not fixed: a method script that lives in `/tmp`
        # cannot write its candidate.** The scratch is `/tmp/hg-method-<run>-<rand>/`, and
        # a script named by an absolute path has its own directory bound read-only -- so
        # when that directory is `/tmp`, the read-only bind lands on top of the writable
        # scratch (later mounts win) and the method dies with `OSError: [Errno 30]
        # Read-only file system: .../round1_ws/next`. Real methods live in the repository
        # or in the author's project, so this only bit a demo script; the fix, when someone
        # has one, is to bind the scratch *last* rather than to stop binding the ancestor.
        # The first element of `readonly` is the method's own read-only input tree.
        "repo": (readonly[0] if readonly else Path(argv[0]).resolve().parent),
        "visible_paths": tuple(dict.fromkeys(visible)),
    }

def _method_env(work: Path, improver: dict | None = None) -> dict:
    """The environment a method runs in: its own model, and not the agent's.

    `harness_env()` does the mirror of this for the harness. The symmetry matters
    because the two budgets are separate by design (`.env.example`), and separate
    configuration is not separation until something enforces it: a method that can
    read `HG_AGENT_API_KEY` can spend the agent's quota, and -- worse for this
    platform -- can answer the eval tasks itself.

    When the run resolved an improver that *is* an endpoint (`--improver deepseek`),
    the endpoint and the model are written in here. A method reaches a chat model through
    `HG_METHOD_BASE_URL` / `HG_METHOD_MODEL` (`methods/editor.py`), so a named improver
    that only lived in the registry would leave the record saying `deepseek` while the
    method called whatever `.env` happened to hold. That is the same failure as a harness
    misreporting its model, and it is fixed the same way: the platform states the fact
    where the consumer reads it.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("HG_AGENT_")}
    endpoint = (improver or {}).get("base_url")
    if endpoint:
        env["HG_METHOD_BASE_URL"] = str(endpoint)
        if (improver or {}).get("model"):
            env["HG_METHOD_MODEL"] = str(improver["model"])
        # The secret is copied from the variable the registry named, never from the
        # record: the record is written to disk and read by people.
        key_env = (improver or {}).get("api_key_env") or ""
        if key_env and os.environ.get(key_env):
            env["HG_METHOD_API_KEY"] = os.environ[key_env]
    env["HARNESSGRAD_WORKSPACE"] = str(work.resolve())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (f"{work.resolve()}{os.pathsep}{existing}"
                         if existing else str(work.resolve()))
    return env

def _call_entrypoint(work: Path, argv: list[str], request: dict,
                     timeout_s: int = 3600, *, sandboxed: bool = True,
                     scratch: Path | None = None, cwd: Path | None = None,
                     readonly: tuple[Path, ...] = (),
                     runtime: tuple[str, ...] = (),
                     improver: dict | None = None) -> dict:
    """Invoke a method entrypoint as a SUBPROCESS and parse its one JSON object.

    Never an import. A method that rewrites its own source must still be
    reachable on the next round, and an imported module is frozen at load time --
    importing would make genuine self-modification unrepresentable.

    A failure here is recorded, not raised: a method that breaks its own
    interface is an observation about that method, and losing a paid run to hide
    it would be the wrong trade.
    """
    env = _method_env(work, improver)
    where = cwd or scratch or work
    payload = json.dumps(request)
    try:
        if sandboxed:
            plan = _method_sandbox_plan(work, argv, scratch or where, readonly,
                                        runtime)
            proc = sandbox.run(
                argv, input_text=payload, timeout_s=timeout_s, cwd=where, env=env,
                **plan)
        else:
            proc = subprocess.run(
                argv, cwd=where, input=payload, capture_output=True,
                text=True, timeout=timeout_s, env=env,
            )
    except subprocess.TimeoutExpired:
        return {"_error": f"entrypoint timed out after {timeout_s}s"}
    except OSError as exc:
        return {"_error": f"entrypoint could not start: {exc}"}
    except sandbox.SandboxUnavailable as exc:
        return {"_error": f"refusing to run the method: sandbox unavailable: {exc}"}

    if proc.returncode != 0:
        return {"_error": f"exit {proc.returncode}",
                "_stderr": proc.stderr[-1500:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"_error": f"non-JSON stdout: {exc}",
                "_stdout": proc.stdout[:500], "_stderr": proc.stderr[-500:]}

def _call_method(work: Path, argv: list[str], request: dict, timeout_s: int,
                 *, sandboxed: bool = True, scratch: Path | None = None,
                 cwd: Path | None = None, readonly: tuple[Path, ...] = (),
                 runtime: tuple[str, ...] = (),
                 improver: dict | None = None) -> dict:
    """Invoke a method with the platform's own files checked before and after.

    Detection, not prevention: a method runs as an ordinary subprocess with the
    platform on disk, and nothing here stops it from appending to `eval/runner.py`.
    What this does is make that loud. A run whose method changed the referee is
    not a measurement, and the failure mode without this check is a curve that
    looks fine -- the method scored itself and the platform reported the number.
    """
    before = snapshot(PLATFORM_ROOT)
    reply = _call_entrypoint(work, argv, request, timeout_s=timeout_s,
                             sandboxed=sandboxed, scratch=scratch, cwd=cwd,
                             readonly=readonly, runtime=runtime, improver=improver)
    changes = diff(before, snapshot(PLATFORM_ROOT))
    touched = changes["added"] + changes["removed"] + changes["modified"]
    if touched:
        shown = ", ".join(sorted(set(touched))[:8])
        print(f"\nFAILING RUN: the method modified the platform: {shown}\n"
              f"  A method that edits the scorer is not being measured. Restore\n"
              f"  the named files from your backup or a clean checkout, then\n"
              f"  re-run.", file=sys.stderr)
        raise PlatformTampered(changes)
    return reply

def _touched_paths(work: Path, before: str, after: str) -> list[str]:
    """Which files a step actually moved.

    This is how the platform observes the editable surface without asking the
    method to declare anything: `method/accept.py` in this list means the method
    rewrote its own acceptance rule, which is the line most systems say they
    cannot cross. The platform never writes these files itself.
    """
    if before == after:
        return []
    out = subprocess.run(
        ["git", "-C", str(work), "diff", "--name-only", before, after],
        capture_output=True, text=True,
    ).stdout
    return sorted(p for p in out.splitlines() if p.strip())

class PlatformTampered(RuntimeError):
    """A run tried to score itself. The run stops; the exception names the files."""
