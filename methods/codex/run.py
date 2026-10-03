#!/usr/bin/env python3
"""A method whose improver is an external coding agent (codex), not a bundled model.

Why this exists
---------------
Every method before this one carried its own improver inside its own `run.py`: a prompt,
a JSON envelope, a retry policy, and a 9B model. That conflated two things the platform
needs to keep apart -- **the improver** (can it read a harness and write a correct diff?)
and **the rule** (`methods/rrsi`'s acceptance, `methods/dgm`'s archive) -- and it meant
that a comparison between two methods was partly a comparison between two prompts.

Here the improver is `codex`, resolved by `tools/improver.py` and recorded on the curve
point, and this file is only the **adaptation layer**: give it the evidence, tell it the
rules, run it in a copy of the harness, and report what changed. A method that wants a
different rule wraps a different rule around the same improver.

What it does NOT do: it does not decide whether the result is acceptable, and it does not
score anything. Mode A accepts one candidate per round, and the platform measures it. The
rule-shaped decisions a real method needs (an anneal, an archive, a critic) live in the
method, not here.

Invocation
----------
    HG_IMPROVER=/path/to/codex                  the command (default: tools/improver.py --which)
    HG_IMPROVER_MODEL=<model>                   optional `-c model=...` override
    HG_IMPROVER_EFFORT=<low|medium|high|xhigh>  optional reasoning-effort override
    HG_IMPROVER_ARGS=<shell string>             extra argv appended verbatim
    HG_IMPROVER_TIMEOUT_S=1800                  wall clock for one improvement session
    HG_IMPROVER_SKILL=/path/to/skill.md         default: the platform's `improvers/skill.md`
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol import emit, read_request, require_api  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

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


def _improver_command(req: dict) -> list[str]:
    """Which codex to run: the operator's, or the one the platform resolved.

    The platform's answer is the one that matters, and it arrives in the request. This
    method runs in a sandbox that hides the platform except `methods/`, so it cannot
    resolve codex for itself: `tools/improver.py` is not on its disk. Measured before
    this took the request's answer -- three attempts at round 1, three `exit 1`, stderr
    `codex method: no usable improver`, which reads as a broken method rather than a
    boundary that was never drawn. The `--no-sandbox` fallback below is kept for the
    case where the platform tree really is readable.
    """
    explicit = os.environ.get("HG_IMPROVER")
    if explicit:
        return shlex.split(explicit)
    given = (req.get("improver") or {}).get("path")
    if given:
        if not Path(given).exists():
            raise SystemExit(
                f"codex method: the platform named an improver at {given!r}, which does "
                f"not exist inside this sandbox. That is a platform bug (the improver's "
                f"runtime trees were not bound), not a broken method.")
        return [given]
    # Ask the platform's resolver rather than guessing at PATH: measured on the
    # development machine, PATH points at a codex whose native binary is missing, and
    # `codex --version` on it crashes. Documented in `tools/improver.py`.
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "improver.py"), "--which"],
                          capture_output=True, text=True, timeout=300)
    resolved = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if proc.returncode != 0 or not resolved:
        raise SystemExit(
            "codex method: no usable improver. Run `python3 tools/improver.py --list` "
            "to see what is on this machine, or set HG_IMPROVER.")
    return [resolved]


def _stage_evidence(workspace: Path, req: dict) -> Path:
    """Put the platform's channel where the improver will look for it.

    `workspace/_harnessgrad/` already holds the round summary, the traces, the task pages
    (with the verifier's verdict) and the earlier harness states -- staged by the driver,
    which is the only thing that knows what this run measured and what it is allowed to
    show. This copies it under the name the prompt uses rather than moving it, so a
    method that wants the original paths still has them.
    """
    src = Path(req.get("channel") or (Path(req["workspace"]) / "_harnessgrad"))
    dest = workspace / "evidence"
    if dest.exists():
        shutil.rmtree(dest)
    if src.is_dir():
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("states"))
        states = src / "states"
        if states.is_dir():
            # `states/` is complete harness trees and can be large; a symlink keeps the
            # copy cheap while leaving the improver able to read and copy from it.
            (dest / "states").symlink_to(states, target_is_directory=True)
    else:
        dest.mkdir(parents=True, exist_ok=True)

    # The default skill arrives **with the channel**: the platform stages its
    # `improvers/skill.md` as `_harnessgrad/SKILL.md` precisely because a method cannot
    # read the platform's copy from inside its sandbox, and the method that needs it is
    # the one that ships none of its own. `HG_IMPROVER_SKILL` still wins -- an operator
    # pointing at a skill is a deliberate act -- and the platform path is the
    # `--no-sandbox` fallback.
    if not (dest / "SKILL.md").is_file():
        override = os.environ.get("HG_IMPROVER_SKILL")
        skill = Path(override) if override else (ROOT / "improvers" / "skill.md")
        if skill.is_file():
            (dest / "SKILL.md").write_text(skill.read_text(encoding="utf-8"),
                                           encoding="utf-8")
    return dest


#: Things in the candidate that this method put there and that are **not** the harness.
#: Excluded from the change report, because a report that includes them is a report about
#: the platform's own scaffolding: measured on the first plumbing check, before this
#: existed, a one-line edit to `agent.py` came back as `22 file(s) changed`, nineteen of
#: them `.git/hooks/*.sample` and the rest the evidence copy. The driver's curve point
#: records `editable_surface_touched` from git, which does not see any of it -- so the two
#: files disagreed, and the wrong one was quoted to the reader.
_NOT_HARNESS = (".git", "__pycache__", "evidence", ".gitignore")


def _is_harness_file(rel: Path) -> bool:
    return not any(part in _NOT_HARNESS for part in rel.parts) \
        and not rel.name.endswith(".pyc")


def _changed_files(candidate: Path, pristine: Path) -> list[str]:
    """Which files differ from the pristine harness, by content."""
    changed = []
    for path in sorted(candidate.rglob("*")):
        if not path.is_file():
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


def main() -> int:
    req = read_request()
    require_api(req)

    workspace = Path(req["workspace"])
    pristine = Path(req["base_harness"])
    candidate = workspace / "candidate"
    if candidate.exists():
        shutil.rmtree(candidate)
    # A git repo, because codex's own `apply_patch` works through one, and because a
    # harness *is* a git repo on this platform (`ckpt/git_state.py`).
    shutil.copytree(pristine, candidate, ignore=shutil.ignore_patterns(".git"))
    subprocess.run(["git", "init", "-q", str(candidate)], check=True)
    (candidate / ".gitignore").write_text("_harnessgrad/\n__pycache__/\n*.pyc\n.state/\n")

    _stage_evidence(candidate, req)

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
    # Its own session state, so a run does not append to the operator's codex history.
    home = workspace / ".codex-home"
    home.mkdir(exist_ok=True)
    source_home = Path(os.environ.get("HG_CODEX_HOME_SOURCE", Path.home() / ".codex"))
    for name in ("config.toml", "auth.json"):
        if (source_home / name).is_file():
            shutil.copy2(source_home / name, home / name)
    cfg = home / "config.toml"
    text = cfg.read_text() if cfg.exists() else ""
    if "responses_experimental_transport" not in text:
        # Measured: a proxy that does not carry WebSockets makes codex retry, warn, and
        # time out (`Falling back from WebSockets to HTTPS transport`).
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
    changed = _changed_files(candidate, pristine)
    protected = [f for f in changed if Path(f).name == "harness.json"]

    # Its own sentence, for the record: the last non-empty line of the paragraph it was
    # asked to print. A method that reports nothing here costs a reader the only
    # explanation of the change.
    # The improver's own sentence. Skipped: its logging (codex prints `tokens used`,
    # timestamps and a session id), our own diagnostics, and anything short enough to be
    # a label rather than a reason -- measured, the first version reported the fake
    # improver's stderr line as the method's summary.
    summary = ""
    for line in reversed(transcript.strip().splitlines()):
        text = line.strip()
        if len(text) < 40 or text.startswith(("ERROR", "warning", "fake", "codex",
                                              "tokens used", "session id")):
            continue
        summary = text[:400]
        break

    step = {
        "harness_dir": str(candidate),
        "label": (f"codex: {summary[:80]}" if summary else "codex: no change"),
        "edit_kind": "codex",
        "claimed_cost": {"generation_tokens": 0},
        "method_reported": {
            "score": None,
            "improver": (req.get("improver") or {}).get("name")
                        or os.environ.get("HG_IMPROVER") or "codex (resolved)",
            "improver_path": (req.get("improver") or {}).get("path") or "",
            "exit_code": code,
            "seconds": round(time.time() - started, 1),
            "files_changed": changed,
            "note": summary,
        },
    }
    if code != 0 and not changed:
        step["method_reported"]["error"] = (
            f"the improver exited {code} and changed nothing; see improver.log")
    elif protected:
        step["method_reported"]["error"] = (
            f"the improver edited a protected file ({', '.join(protected)})")

    Path(req["trajectory_out"]).write_text(json.dumps({
        "steps": [step],
        "trajectory_shape": "sequence",
        "nominated": 0,
        "selection_pool_size": 1,
        "provenance": {
            "method": "codex (improver: external coding agent)",
            "acceptance_rule": {
                "text": "mode A measures one candidate; this method accepts whatever the "
                        "improver produced, and the platform judges it",
                "source": "INTERFACE.md §4.49", "calibrated": False},
        },
    }, indent=1), encoding="utf-8")

    emit({"steps": 1, "changed": bool(changed),
          "note": (f"{len(changed)} file(s) changed" if changed
                   else "the improver changed nothing"),
          "generation_tokens": 0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
