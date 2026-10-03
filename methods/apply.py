"""Apply an edit sequence, and check the result is still a harness.

This is the **method's** copy, and the copy is deliberate rather than an oversight.

`eval/candidate.py` holds the platform's judgement, and a method cannot reach it: a
method's sandbox hides the platform (measured -- a probe method asking for
`data/probe_set.py`, `.env` and `eval/runner.py` gets none of them), so an
`import eval.candidate` inside `methods/editor.py` fails with `ModuleNotFoundError`. And
sharing one module the other way is worse than duplication: the sandbox makes the whole
method directory **writable**, so a checker living in `methods/` could be rewritten by
the thing it checks.

So the two files are two roles, not two drafts of one:

    methods/apply.py    a pre-check, so a method can find out that its sequence does not
                        apply *before* reporting it, and re-propose (editor.propose_and_apply)
    eval/candidate.py   the platform's own check on the candidate it is about to measure,
                        reusing tools/validate_harness.py so the door, the CLI and the run
                        path agree on what a harness is

The platform's is the one that decides. A method that skips this module and hands over a
tree anyway is caught there, and the round is recorded as a failed step rather than as a
weak harness.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

#: A file a method may not rewrite: it *names* the harness, and a method that could edit
#: it could rename the thing it is measured as.
PROTECTED = {"harness.json"}

#: The framework<->method channel, not part of the harness. Excluded from every copy
#: because it must never reach a measured tree: the harness runs inside that tree.
CHANNEL = "_harnessgrad"

#: Manifests and sources are text. A megabyte is a payload, not an edit.
MAX_EDIT_BYTES = 2 * 1024 * 1024


def _target(dest: Path, rel: str) -> tuple[Path | None, str | None]:
    """Where an edit lands, or why it may not land there.

    Checked once, before either edit shape is applied. It used to sit inside the
    whole-file branch, so a *replacement* could name `harness.json` or climb out of the
    harness and never meet the rule -- the guard was on the shape, not on the path, and
    the shape was the newer code.
    """
    root = dest.resolve()
    target = (dest / rel).resolve()
    try:
        inside = os.path.commonpath([str(target), str(root)]) == str(root)
    except ValueError:                       # different drives, on Windows
        inside = False
    if not inside or target == root:
        return None, f"names a path outside the harness: {rel!r}"
    if Path(rel).name in PROTECTED and Path(rel).parent == Path("."):
        return None, f"rewrites the protected file {rel!r}"
    return target, None


def apply(base: Path, dest: Path, edits) -> tuple[list[str], list[str]]:
    """Apply an edit sequence onto a copy of `base`. Returns `(applied, problems)`.

    `problems` non-empty means the sequence did not apply. `applied` still lists what
    landed before the rejection, because "three of your four edits landed" is what a
    repair round needs to hear.

    Every rejection is **reported**. A method whose edit was dropped for a traversal
    reason and which then reports "nothing to change" has told the platform a lie about
    its own behaviour, and the record cannot tell that from a method that genuinely found
    nothing.
    """
    base, dest = Path(base), Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base, dest, ignore=shutil.ignore_patterns(".git", CHANNEL))

    if not isinstance(edits, list):
        return [], [f"no edit sequence: `files` was {type(edits).__name__}, not a list"]
    if not edits:
        return [], ["the edit sequence is empty"]

    applied: list[str] = []
    problems: list[str] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            problems.append(f"edit {index} is {type(edit).__name__}, not an object")
            continue
        rel = str(edit.get("path") or "").lstrip("/")
        if not rel:
            problems.append(f"edit {index} names no path")
            continue
        target, why = _target(dest, rel)
        if target is None:
            problems.append(f"edit {index} ({rel!r}) {why}")
            continue

        find, replace = edit.get("find"), edit.get("replace")
        content = edit.get("content")
        if find is not None or replace is not None:
            # **A whole-file rewrite is a lossy regeneration.** Measured with a model
            # asked for `content`: `"content" must be the COMPLETE new file` (the old
            # `DEFAULT_SYSTEM`), 147 then 133 lines of the harness -- every docstring and
            # every measured-history comment -- disappeared across two rounds while the
            # behaviour barely moved. The model was not being lazy: a 14 kB file has to
            # fit an output budget, and prose is what gets dropped. A replacement cannot
            # delete what it did not name.
            if not isinstance(find, str) or not isinstance(replace, str):
                problems.append(f"edit {index} ({rel!r}) is a replacement, so it needs "
                                f"both `find` and `replace` strings")
                continue
            if not target.is_file():
                problems.append(f"edit {index} ({rel!r}) replaces text in a file that "
                                f"does not exist; use `content` to create one")
                continue
            if len(replace.encode("utf-8", "replace")) > MAX_EDIT_BYTES:
                problems.append(f"edit {index} ({rel!r}) is larger than "
                                f"{MAX_EDIT_BYTES} bytes")
                continue
            text = target.read_text(encoding="utf-8", errors="replace")
            hits = text.count(find)
            if hits == 0:
                problems.append(f"edit {index} ({rel!r}): `find` does not appear in the "
                                f"file. Copy the exact text you are replacing, "
                                f"including whitespace")
                continue
            if hits > 1:
                # Ambiguity is a refusal, never a first-match guess: `return None` and
                # its neighbours appear many times in a harness, and replacing the wrong
                # one produces a candidate nobody asked for that still validates.
                problems.append(f"edit {index} ({rel!r}): `find` appears {hits} times; "
                                f"include more surrounding text so it is unique")
                continue
            try:
                target.write_text(text.replace(find, replace, 1), encoding="utf-8")
            except OSError as exc:
                problems.append(f"edit {index} ({rel!r}) could not be written: {exc}")
                continue
            applied.append(rel)
            continue

        if not isinstance(content, str):
            problems.append(f"edit {index} ({rel!r}) carries neither a `content` string "
                            f"nor a `find`/`replace` pair")
            continue
        if len(content.encode("utf-8", "replace")) > MAX_EDIT_BYTES:
            problems.append(f"edit {index} ({rel!r}) is larger than {MAX_EDIT_BYTES} "
                            f"bytes")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            problems.append(f"edit {index} ({rel!r}) could not be written: {exc}")
            continue
        applied.append(rel)
    return applied, problems


def validate(repo: Path) -> list[str]:
    """Is this still a harness? The method's own pre-check.

    Split into two groups on purpose. The manifest checks are *schema*, which
    `tools/validate_harness.py` owns and which this cannot import (a method's sandbox
    hides `tools/`); they are restated here narrowly so a method finds out before it
    reports, and the platform re-checks the same things authoritatively.
    """
    repo = Path(repo)
    problems: list[str] = []
    man_path = repo / "harness.json"
    if not man_path.is_file():
        return [f"no harness.json at {repo}"]
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"harness.json is not valid JSON: {exc}"]
    if not isinstance(man, dict):
        return ["harness.json is not an object"]
    for field in ("name", "version", "entrypoint"):
        if not man.get(field):
            problems.append(f"harness.json is missing required field {field!r}")
    if man.get("backend", "cli") != "cli":
        problems.append(f"backend {man['backend']!r} is not implemented; only 'cli' is")
    sub = man.get("path", ".")
    if not isinstance(sub, str):
        problems.append(f"`path` must be a string, not {type(sub).__name__}")
        sub = "."
    declared = man.get("entrypoint")
    if declared is not None and not isinstance(declared, str):
        problems.append(f"`entrypoint` must be a string naming one file, not "
                        f"{type(declared).__name__}")
    elif declared:
        if not (repo / sub / declared).is_file():
            problems.append(f"entrypoint {declared!r} not found under {sub!r}")
    install = man.get("install")
    if install and not (repo / sub / install).is_file():
        problems.append(f"install file {install!r} not found under {sub!r}")
    return problems


def describe(base: Path, dest: Path, edits) -> dict:
    """The whole two-step outcome, for a method's own log or for a caller's record.

    Paths rather than contents: a curve point has to say *which* files the method
    touched without carrying harness source into every round.
    """
    applied, problems = apply(base, dest, edits)
    if not problems:
        problems = validate(dest)
    return {"applied": applied, "problems": problems, "ok": not problems,
            "proposed": [str((e or {}).get("path") or "?") for e in (edits or [])
                         if isinstance(e, dict)]}
