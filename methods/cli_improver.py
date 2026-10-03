#!/usr/bin/env python3
"""How a method drives an external coding-agent improver.

**Why this is a module and not a method.** Until 2026-10-04 this code was
`methods/codex/run.py`, and `methods/` is supposed to hold the decision rules a paper
proposes. "Run the improver the platform resolved" is not a rule: it is the *improver layer*,
the same job `editor.py` does for a chat endpoint, and a method that is nothing but that
layer is a tool wearing a method's name -- it would also make `方法 = codex` a true sentence
about a run, when codex is a tool and the method is whichever rule wrapped it. So it moved
here. **No method in `methods/` calls it today**, and that is the honest state: the reference
method (`llm_improver`) delegated to it for one afternoon, and the delegation was withdrawn
because the registry's *default* improver is codex -- a CLI -- so delegating silently changed
what an ordinary run does. The trade-off is recorded as an open decision in
`INTERFACE.md` §4.49; the code is kept because it is the only implementation of the
improver layer and every fact in the list below was paid for once already.

**The split of responsibility.** The platform resolves the improver
(`tools/improver.py`), binds its runtime trees read-only into the method sandbox, and hands
the method its identity in the request (`req["improver"] = {name, path, version, model,
sha256}`). What a method must still supply is everything method-shaped: the evidence staged
where the tool can read it, the prompt, the timeout, and the report. That is this file.

**Measured facts this code carries**, each from a real failure:

* The command comes from the **request**, not from PATH. Measured: three attempts at round 1,
  three `exit 1`, and `codex method: no usable improver`, because the sandbox hides the
  platform and `tools/improver.py` is not on the method's disk. On this machine PATH also
  points at a codex whose native binary is missing, which is why the resolver exists at all.
* `CODEX_HOME` is redirected into the workspace, so a run does not append to the operator's
  own codex history.
* `responses_experimental_transport = "https"` is appended to the copied config: measured, a
  proxy that does not carry WebSockets makes codex retry, warn and time out
  (`Falling back from WebSockets to HTTPS transport`).
* Changed files are decided by **content**, not by git status. Measured: a one-line edit came
  back as `22 file(s) changed`, nineteen of them ours, and the wrong one was quoted to the
  reader.
* The transcript is written to `<workspace>/improver.log`, which the platform copies into the
  record (`harnessgrad/records.py:_keep_method_logs`): the question "why did the improver
  change nothing?" is normally asked after the run has deleted its scratch directory.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editor                                              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: Directories that are never part of the harness: the platform's channel, caches, and the
#: tool's own session state.
_NOT_HARNESS = frozenset({"_harnessgrad", "__pycache__", ".git", ".codex-home", ".state",
                          "evidence", "node_modules", ".venv"})

PROMPT = """\
You are improving a **harness** -- the structured execution layer around a model that
turns a task into model calls and decides when to stop. Your job is to produce a better
harness for the tasks described in `evidence/tasks/`.

Read `evidence/SKILL.md` first: it defines the harness contract, where the evidence is,
what a valid change is, and which failure modes are known to matter on this platform.

Everything you may read is under `evidence/`:

  evidence/SKILL.md            how to improve a harness (read this first)
  evidence/round.json          what the incumbent scored, per task
  evidence/traces/*.jsonl      the incumbent's real conversations, one per task
  evidence/tasks/*.json        the instruction, the grading, and the verifier's verdict
  evidence/history/*.json      earlier rounds' summaries
  evidence/states/             complete harness trees from earlier rounds

The harness you are changing is the current directory. Edit it in place.

Hard rules:
  * `harness.json` must not be modified.
  * Do not add or widen `env_kinds`.
  * The entrypoint named in `harness.json` must still exist and still run.
  * Do not run any task from the dataset. You may write and run your own tests of the
    harness, and you may run the harness's entrypoint on a task you construct yourself.

When you are done, print one short paragraph: what you changed and why it should raise
the score. If you conclude that nothing should change, make no edits and say so.
"""


def applies(req: dict) -> bool:
    """Is the improver this run resolved a **command**, rather than an endpoint?

    True when the operator named one (`HG_IMPROVER`) or when the platform's resolution
    carries a `path` and no `base_url`. The distinction matters because the two are driven
    completely differently: an endpoint is a chat call (`editor.ask`), a CLI is a
    subprocess in the candidate tree.
    """
    if os.environ.get("HG_IMPROVER"):
        return True
    improver = req.get("improver") or {}
    return bool(improver.get("path")) and not improver.get("base_url")


def _improver_command(req: dict) -> list[str]:
    """Which command to run: the operator's, or the one the platform resolved."""
    explicit = os.environ.get("HG_IMPROVER")
    if explicit:
        return shlex.split(explicit)
    given = (req.get("improver") or {}).get("path")
    if given:
        if not Path(given).exists():
            raise SystemExit(
                f"the platform named an improver at {given!r}, which does not exist inside "
                f"this sandbox. That is a platform bug (the improver's runtime trees were "
                f"not bound), not a broken method.")
        return [given]
    # Ask the resolver rather than guessing at PATH: measured on the development machine,
    # PATH points at a codex whose native binary is missing and `codex --version` crashes.
    # Reachable only without a sandbox -- a sandboxed method cannot see `tools/`.
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "improver.py"), "--which"],
                          capture_output=True, text=True, timeout=300)
    resolved = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if proc.returncode != 0 or not resolved:
        raise SystemExit(
            "no usable improver. Run `python3 tools/improver.py --list` to see what is on "
            "this machine, or set HG_IMPROVER.")
    return [resolved]


def _stage_evidence(workspace: Path, req: dict) -> Path:
    """Put the platform's channel where the improver will look for it.

    `workspace/_harnessgrad/` already holds the round summary, the traces, the task pages
    (with the verifier's verdict) and the earlier harness states -- staged by the driver,
    which is the only thing that knows what this run measured and what it is allowed to
    show. This copies it under the name the prompt uses rather than moving it, so a method
    that wants the original paths still has them. `states/` becomes a symlink: those are
    complete harness trees and copying them would double the cost of every round.
    """
    src = Path(req.get("channel") or (Path(req["workspace"]) / "_harnessgrad"))
    dest = workspace / "evidence"
    if dest.exists():
        shutil.rmtree(dest)
    if src.is_dir():
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("states"))
        states = src / "states"
        if states.is_dir():
            (dest / "states").symlink_to(states, target_is_directory=True)
    else:
        dest.mkdir(parents=True, exist_ok=True)

    # The skill arrives **with the channel**: the platform stages `improvers/skill.md` as
    # `_harnessgrad/SKILL.md` precisely because a method cannot read the platform's copy
    # from inside its sandbox. `HG_IMPROVER_SKILL` still wins -- an operator pointing at a
    # skill is a deliberate act -- and the platform path is the `--no-sandbox` fallback.
    # This is the file-based twin of `editor.skill_block`, which does the same job for a
    # chat call: one staged skill, two ways of handing it over.
    if not (dest / "SKILL.md").is_file():
        override = os.environ.get("HG_IMPROVER_SKILL")
        skill = Path(override) if override else (ROOT / "improvers" / "skill.md")
        if skill.is_file():
            (dest / "SKILL.md").write_text(skill.read_text(encoding="utf-8"),
                                           encoding="utf-8")
    return dest


def _is_harness_file(rel: Path) -> bool:
    return not any(part in _NOT_HARNESS for part in rel.parts) \
        and not rel.name.endswith(".pyc")


def _changed_files(candidate: Path, pristine: Path) -> list[str]:
    """Which files differ from the pristine harness, **by content**.

    Not `git status`: measured, a one-line edit to `agent.py` came back as
    `22 file(s) changed`, nineteen of them files this platform wrote, and the wrong one was
    quoted back to a reader as the method's change.
    """
    changed = []
    for path in sorted(candidate.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(candidate)
        if not _is_harness_file(rel):
            continue
        other = pristine / rel
        if not other.is_file() or other.read_bytes() != path.read_bytes():
            changed.append(str(rel))
    for path in sorted(pristine.rglob("*")):
        if path.is_file() and _is_harness_file(path.relative_to(pristine)):
            rel = path.relative_to(pristine)
            if not (candidate / rel).is_file():
                changed.append(str(rel))
    return sorted(set(changed))


def _summary_of(transcript: str) -> str:
    """The improver's own sentence: the last substantial non-logging line it printed.

    Skipped: its logging (`tokens used`, timestamps, a session id), our diagnostics, and
    anything short enough to be a label rather than a reason -- measured, the first version
    reported a fake improver's stderr line as the method's summary.
    """
    for line in reversed((transcript or "").strip().splitlines()):
        text = line.strip()
        if len(text) < 40 or text.startswith(("ERROR", "warning", "fake", "codex",
                                              "tokens used", "session id")):
            continue
        return text[:400]
    return ""


def run(req: dict, base: Path, *, method: str) -> int:
    """Copy the harness, stage the evidence, run the improver, report what moved.

    Everything here is improver-shaped and nothing is rule-shaped, which is the point: a
    method with a decision rule of its own calls this only when it has already decided to
    spend the round, and whatever it does with the result is its own business.
    """
    workspace = Path(req["workspace"])
    candidate = workspace / "candidate"
    if candidate.exists():
        shutil.rmtree(candidate)
    # A git repo, because codex's own `apply_patch` works through one, and because a harness
    # *is* a git repo on this platform (`ckpt/git_state.py`).
    shutil.copytree(base, candidate, ignore=shutil.ignore_patterns(".git"))
    subprocess.run(["git", "init", "-q", str(candidate)], check=True)
    (candidate / ".gitignore").write_text("_harnessgrad/\n__pycache__/\n*.pyc\n.state/\n")

    _stage_evidence(candidate, req)

    # The shipped CLI improver is codex, so the argv is codex's. A different CLI needs its
    # own argv, which is what `HG_IMPROVER_ARGS` is for; a second CLI would earn a proper
    # argument builder here rather than a guess.
    argv = _improver_command(req) + ["exec", "--skip-git-repo-check",
                                     "--sandbox", "danger-full-access",
                                     "-C", str(candidate)]
    if model := os.environ.get("HG_IMPROVER_MODEL"):
        argv += ["-c", f'model="{model}"']
    if effort := os.environ.get("HG_IMPROVER_EFFORT"):
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv += shlex.split(os.environ.get("HG_IMPROVER_ARGS", ""))
    argv += [PROMPT]

    timeout = int(os.environ.get("HG_IMPROVER_TIMEOUT_S", "1800"))
    started = time.time()
    env = dict(os.environ)
    home = workspace / ".codex-home"
    home.mkdir(exist_ok=True)
    source_home = Path(os.environ.get("HG_CODEX_HOME_SOURCE", Path.home() / ".codex"))
    for name in ("config.toml", "auth.json"):
        if (source_home / name).is_file():
            shutil.copy2(source_home / name, home / name)
    cfg = home / "config.toml"
    text = cfg.read_text() if cfg.exists() else ""
    if "responses_experimental_transport" not in text:
        cfg.write_text(text + '\nresponses_experimental_transport = "https"\n')
    env["CODEX_HOME"] = str(home)

    transcript = ""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              cwd=str(candidate), env=env)
        transcript = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        transcript = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") \
            + ((exc.stderr or "") if isinstance(exc.stderr, str) else "")
        code = -1
    (workspace / "improver.log").write_text(transcript[-200_000:], encoding="utf-8")

    changed = _changed_files(candidate, base)
    protected = [f for f in changed if Path(f).name == "harness.json"]
    summary = _summary_of(transcript)
    reported = {
        "improver": (req.get("improver") or {}).get("name")
                    or os.environ.get("HG_IMPROVER") or "cli (resolved)",
        "improver_path": (req.get("improver") or {}).get("path") or "",
        "exit_code": code,
        "seconds": round(time.time() - started, 1),
        "files_changed": changed,
        "note": summary,
    }
    if code != 0 and not changed:
        reported["error"] = (f"the improver exited {code} and changed nothing; see "
                             f"improver.log in the run's method logs")
    elif protected:
        # Recorded, not raised: the platform's own `candidate.validate` decides whether the
        # tree is still a harness, and a method that edited a file it was told not to is a
        # fact for the record rather than a reason to lose the round.
        reported["error"] = (f"the improver edited a protected file "
                             f"({', '.join(protected)})")

    return editor.report(
        req, method=method, harness_dir=candidate, changed=bool(changed), files=changed,
        hypothesis=summary, edit_kind="cli_improver", method_reported=reported,
        acceptance_rule=(
            "the improver the platform resolved produces the edits; the platform measures "
            "and judges the result. This layer makes no decision about whether to spend the "
            "round or whether to keep the candidate -- that belongs to the method"),
        label=(f"{reported['improver']}: {summary[:70]}" if summary
               else f"{reported['improver']}: no change"))
