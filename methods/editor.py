"""The edit half of a method, shared by every method in this directory.

Why this is one module and not eight copies
-------------------------------------------
Every method here ends the same way: either something changes in the harness, or
nothing should. What differs is *what the method knows* and *what it decides
before asking*. So the asking lives here once, and each method supplies its own
framing -- which is exactly the split that makes "is this method's advantage its
rule or its editor?" a question the platform can answer by running the same
editor with and without the rule.

Two defects are fixed structurally here rather than in each method
-----------------------------------------------------------------
1. **Every exit must write a trajectory.** This was violated twice in
   `llm_improver` alone: a provider error and an unusable proposal both exited
   without writing one, so the platform could only report "the method wrote no
   trajectory" and the actual cause -- a 503, a malformed reply -- was discarded.
   A method that exits through `report()` or `fail()` cannot have this bug.

2. **The method reads the channel at the path the contract names.**
   `INTERFACE.md` §4.7 puts `_harnessgrad/` inside the *harness* directory. The
   first version of `llm_improver` read it from `request["workspace"]`, which is
   the method's own empty scratch directory -- so every prompt it ever built said
   `(no traces available)` and it proposed edits from the harness source alone,
   while its docstring claimed it read how the harness behaved. Measured, not
   guessed: with a trace staged at the contract path, the old code produced
   `(no traces available)` and the new code produces the trace. `channel()` is the
   single place that decides where to look.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol                                             # noqa: E402
from protocol import emit, read_request, require_api        # noqa: E402,F401
# `methods/apply.py`, not `eval/candidate.py`: a method's sandbox hides the platform, so
# an `import eval.*` here fails inside a run (`ModuleNotFoundError: No module named
# 'eval'`) while passing every test that calls the editor directly. Measured, in a real
# run. The platform keeps its own copy in `eval/candidate.py`, and that one is the judge.
import apply as candidate                                    # noqa: E402

#: The framework<->method channel. Gitignored, so committing it cannot mint a sha.
CHANNEL = "_harnessgrad"

#: The one harness file a method may not rewrite: it *names* the harness, and a
#: method that could edit it could rename the thing it is measured as. Everything
#: else in the directory is a program, and changing the program is the job.
PROTECTED = {"harness.json"}

#: Text files larger than this are truncated in the prompt. A harness that ships a
#: 5 MB data file should still be improvable; the model does not need all of it.
#:
#: **The number was 12000 and it was silently corrupting every edit.** Methods are asked
#: to hand back whole files, so a file the model has only partly seen comes back with its
#: end missing -- and the platform measures that. Measured on the reference harness:
#: `agent.py` is 14916 characters, the model was shown the first 12000, and its candidate
#: was a 11880-character file whose tail -- including `def main()` and the
#: `__main__` guard -- no longer existed. The method's own words for it were "completing
#: the previously truncated/missing `run` body": it was doing exactly what it was asked,
#: from a view that could not contain the answer. Every method composed on this editor
#: (`ahe`, `dgm`, `rrsi`, `sica_ci`, `tthe`, `hyperagents`, `harnessx`, `llm_improver`)
#: inherited it, which is a plausible explanation for a platform whose curves have never
#: risen.
#:
#: 24000 shows the reference harness whole while still fitting a 32k-context improver
#: (the local vLLM's window); a model with a bigger window raises it, per run, with
#: `HG_METHOD_SOURCE_LIMIT`. Whatever the number, truncation is now **said out loud**
#: (`load_sources`), because the failure mode of a silent cap is a plausible-looking
#: candidate that cannot run.
SOURCE_LIMIT = int(os.environ.get("HG_METHOD_SOURCE_LIMIT", "24000"))
#: How much of the platform's skill reaches a prompt. A skill is prose read by a model with
#: a large context, so the limit bounds a runaway file rather than curating one. Raised from
#: 6000 the moment the platform's own skill outgrew it: it was 8407 characters, and the
#: section a round needed most -- the one written from the *previous* run's failure -- sat
#: at character 5595. Truncation is never silent; a prompt that quietly drops its own
#: guidance is worse than no guidance, because the round is spent and nothing says why.
SKILL_LIMIT = int(os.environ.get("HG_METHOD_SKILL_LIMIT", "24000"))


# --------------------------------------------------------------- the channel ---

def channel(base: Path) -> Path:
    """Where the platform staged what this method may read."""
    return Path(base) / CHANNEL


def load_history(base: Path) -> list[dict]:
    """Every previous curve point, oldest first.

    This is what makes a whole family of methods implementable: several published
    ones choose *which* previous state to build on, and that information is
    inherently across rounds. A method that only ever sees the current round can
    only ever hill-climb.
    """
    out = []
    hist = channel(base) / "history"
    if hist.is_dir():
        for f in sorted(hist.glob("round-*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
    out.sort(key=lambda p: p.get("round", 0))
    return out


def render_trace(text: str, max_events: int = 14, width: int = 220) -> str:
    """One task's trace, as lines a model can read.

    Deliberately tolerant about the event schema. The platform requires a harness
    to write a `usage` block and nothing else, so a renderer that assumed the
    reference harness's `command`/`reply` keys would silently show nothing for
    any other harness -- a method would then be diagnosing a blank page and would
    look like a bad method.
    """
    lines = []
    for raw in text.splitlines()[:max_events]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            lines.append(f"  {raw[:width]}")
            continue
        if not isinstance(event, dict):
            lines.append(f"  {str(event)[:width]}")
        elif "usage" in event and len(event) <= 2:
            continue                                    # accounting, not behaviour
        elif "command" in event:
            lines.append(f"  ran: {str(event['command'])[:width]}"
                         f"  (exit {event.get('exit')})")
            # `output` first, because that is what the reference harness writes
            # (`loop/agent.py`: `{"command": ..., "exit": ..., "output": ...}`). This
            # looked only for `stdout`/`stderr`, so **every** command's output was
            # dropped -- measured 0 of 441 bytes reaching the prompt. An improver was
            # shown `exit 100` with no idea why, which is precisely the feedback it
            # needs; it is also why "the install failed" and "the search found the
            # package" were both invisible.
            for key in ("output", "stdout", "stderr"):
                if event.get(key):
                    label = "" if key == "output" else f"{key}: "
                    # Indent continuation lines too. A command's output is usually
                    # multi-line, and leaving the tail flush against the margin makes
                    # the next event look like part of it.
                    body = "\n    ".join(
                        ln[:width] for ln in str(event[key]).strip().splitlines()[:40])
                    lines.append(f"    {label}{body}")
        elif "reply" in event:
            lines.append(f"  said: {str(event['reply'])[:width]}")
        elif "parse_error" in event:
            # A bare JSON blob reads as noise; it is not noise. It is the harness
            # discarding its own tool call, which is a harness behaviour and the single
            # most actionable line a trace can carry. Measured: `loop` threw away a step
            # on `Extra data: line 1 column 56` for three different tasks, because the
            # model emitted trailing braces -- and the improver was shown only the raw
            # object, with nothing saying what it meant.
            lines.append(f"  the harness could not read its own reply and dropped the "
                         f"step ({str(event['parse_error'])[:width]})")
        else:
            lines.append(f"  {json.dumps(event, ensure_ascii=False)[:width]}")
    return "\n".join(lines)


def load_traces(base: Path, limit: int = 6, order: list[str] | None = None) -> str:
    """What the method is allowed to see: behaviour, not answers.

    Failing tasks go first, because a method that reads only successes learns
    nothing -- but the trace carries no expected answer, and nothing here can add
    one. The platform withholds them (`runner.py` never writes `scorable` into a
    trace), so a diagnosis can legitimately fail.

    `limit` caps how many tasks are shown. How many a method reads is part of the
    method (one published one reads its worst 30 plus its best 6), so this is a
    default the caller may override, not a platform rule.
    """
    trace_dir = channel(base) / "traces"
    paths = sorted(trace_dir.glob("*.jsonl"))
    if order:
        rank = {tid: i for i, tid in enumerate(order)}
        paths.sort(key=lambda p: rank.get(p.stem, len(rank)))
    if not paths:
        return ("(the platform staged no traces for this round -- either the dataset "
                "declares no split and this round had none, or the method was called "
                "without one)")
    blocks = []
    for path in paths[:limit]:
        try:
            text = path.read_text()
        except OSError:
            continue
        blocks.append(f"### task {path.stem}\n{render_trace(text)}")
    return "\n\n".join(blocks)


def load_sources(base: Path, limit: int = SOURCE_LIMIT) -> dict[str, str]:
    """The harness's text files, so the model can see what it is changing.

    Walks the tree rather than naming files: a harness is a directory, and a
    method that could only see `agent.py` could not add a module, which is a
    legitimate and often necessary edit.
    """
    out: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or CHANNEL in path.parts:
            continue
        if path.name in PROTECTED:
            continue
        if path.suffix not in {".py", ".json", ".md", ".txt", ".yaml", ".yml", ".toml"}:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(base))
        if len(text) > limit:
            # In the text the model reads, not in a log: a model that is told nothing
            # will re-emit the part it saw and call the result complete.
            text = (text[:limit]
                    + f"\n# [platform] TRUNCATED: {limit} of {len(text)} characters are "
                      f"shown. Do NOT rewrite this file wholesale -- you have not seen "
                      f"how it ends.\n")
        out[rel] = text
    return out


def failing_first(base: Path) -> list[str]:
    """Task ids ordered worst-first, from this round's curve point if it says.

    Which side the method is *shown* is decided by the record's `score_kind` (§2.6),
    through `protocol.studied_per_task`. Ordering by the side it studies is the honest
    choice: a method reading traces of tasks it is not scored on should still look at
    the ones that are going badly.
    """
    point = {}
    rp = channel(base) / "round.json"
    if rp.exists():
        try:
            point = json.loads(rp.read_text())
        except (OSError, json.JSONDecodeError):
            point = {}
    # 哪一侧是「诊断侧」由记录自己说(§2.6),这条规则只在 protocol 里写一次。
    scores = protocol.studied_per_task(point)
    return [tid for tid, _ in sorted(scores.items(), key=lambda kv: kv[1])]


# ------------------------------------------------------------------ the model ---

DEFAULT_SYSTEM = """You are improving an agent harness.

You will be shown the harness's source, the per-task checks' own reports, and the \
record of running it on a set of tasks. Propose ONE concrete change that is likely to \
make it solve more of them.

Reply with JSON only:

  {"files": [{"path": "<relative path>",
              "find": "<exact text to replace, copied from the file>",
              "replace": "<the text that goes in its place>"}],
   "hypothesis": "<one sentence: what you changed and why>"}

Rules:
- **Prefer `find`/`replace`.** `find` must appear EXACTLY ONCE in the file -- copy it
  character for character, including indentation and blank lines, and include enough
  surrounding text to make it unique. If it appears zero times or more than once the
  edit is refused, and you will be told which.
- To create a file that does not exist yet, use `{"path": ..., "content": ...}` with the
  whole file. Do not send `content` for an existing file: regenerating a file you were
  shown loses whatever you did not reproduce (comments, docstrings, functions you did
  not mean to touch), and that loss has been measured on this platform.
- `path` is relative to the harness root. `harness.json` may not be changed.
- If the evidence does not support any change, reply {"no_change": "<reason>"}.
  That is a valid and useful answer, and it is better than a speculative edit.
"""


def model_settings() -> tuple[str, str, str]:
    base_url = os.environ.get("HG_METHOD_BASE_URL")
    api_key = os.environ.get("HG_METHOD_API_KEY")
    model = os.environ.get("HG_METHOD_MODEL")
    if not (base_url and api_key and model):
        raise SystemExit(
            "a method needs HG_METHOD_BASE_URL / HG_METHOD_API_KEY / HG_METHOD_MODEL. "
            "A method brings its own credentials; the platform does not supply them.")
    return base_url, api_key, model


class UnparseableReply(RuntimeError):
    """The model answered, but not in a form this platform could read.

    Distinct from `no_change` on purpose. A method that reports "I chose not to
    change anything" and a method whose reply was garbage are different events,
    and collapsing the second into the first puts a decision in the curve that
    nobody made.
    """


def _unfence(text: str) -> str:
    """Strip a ``` or ```json fence if the model wrapped its reply in one."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    parts = text.split("```")
    if len(parts) < 3:
        return text
    body = parts[1]
    return body[4:] if body.lstrip().startswith("json") else body


def repair_json_text(candidate: str) -> tuple[str, list[str], bool, int | None]:
    """Every textual repair, plus what the scanner learned while doing them.

    Returns `(text, unclosed_stack, still_in_string, first_balanced_at)`.

    Split out because it is what has to be inspected when a reply still will not
    parse: "the model's answer could not be read" is not actionable, but "here is
    what we turned it into, and here is where the brackets ended up" is. It is also
    the part worth testing on its own -- the candidate stage below only chooses
    among its outputs.
    """
    valid_escapes = set('"\\/bfnrtu')
    stack: list[str] = []
    repaired: list[str] = []
    in_string = False
    escaped = False
    complete_at: int | None = None
    i = 0
    while i < len(candidate):
        ch = candidate[i]
        if escaped:
            escaped = False
            repaired.append(ch)
        elif ch == "\\" and in_string:
            nxt = candidate[i + 1] if i + 1 < len(candidate) else ""
            if nxt in valid_escapes:
                escaped = True
                repaired.append(ch)
            else:
                # `\\d`, `\\s`, `C:\\data` -- the backslash was meant literally.
                repaired.append("\\\\")
        elif ch == '"':
            if in_string:
                # A quote *inside* a string that the model forgot to escape. The
                # discriminator is structural and reliable: in JSON a string is
                # always followed by `,`, `:`, `}` or `]` (or the document ends).
                # Anything else means this quote is content.
                look = i + 1
                while look < len(candidate) and candidate[look] in " \t\r\n":
                    look += 1
                nxt = candidate[look] if look < len(candidate) else ""
                if nxt and nxt not in ",:}]":
                    repaired.append('\\"')
                    i += 1
                    continue
            in_string = not in_string
            repaired.append(ch)
        elif in_string and ord(ch) < 0x20:
            repaired.append({"\n": "\\n", "\r": "\\r", "\t": "\\t",
                             "\b": "\\b", "\f": "\\f"}.get(ch, "\\u%04x" % ord(ch)))
        else:
            repaired.append(ch)
            if not in_string:
                if ch in "{[":
                    stack.append(ch)
                elif ch in "}]":
                    want = "{" if ch == "}" else "["
                    if stack and stack[-1] == want:
                        stack.pop()
                    elif stack:
                        # **The model closed the wrong kind of bracket.** Measured:
                        # an 11 KB reply ended `...task inputs."}]}`, where that first
                        # `}` was closing a *list* opened 340 characters earlier. It
                        # is neither missing nor extra -- it is a type mismatch, which
                        # `json.loads` rejects while a type-aware scanner sees a
                        # balanced document and therefore repairs nothing. This was
                        # the last hole, and it hid behind exactly that disagreement.
                        repaired[-1] = "}" if stack[-1] == "{" else "]"
                        stack.pop()
                    else:
                        repaired.pop()      # a stray closer with nothing open
                # Only the *first* return to balance. Updating it on every subsequent
                # character would swallow the very extra closers and trailing prose
                # this exists to remove.
                if not stack and complete_at is None:
                    complete_at = len(repaired)
        i += 1
    return "".join(repaired), stack, in_string, complete_at


def loads_tolerant(text: str):
    """Parse a reply, repairing the ways a small model reliably breaks JSON.

    This is not speculative tidiness; it is the difference between a working method
    and a dead one on a small local model. Every repair comes from a measured
    failure on this host:

    * missing trailing closers -- a 9B model returned a complete 8.8 KB edit and
      omitted the final `]}`;
    * an extra trailing closer -- `{...}}`, the reply that broke the `loop` base
      harness (`base_harness/loop/agent.py:143` has the same flaw);
    * **a bracket of the wrong type** -- `]` where `}` belonged, which a type-aware
      scanner silently ignores and `json.loads` rejects;
    * trailing prose after the JSON;
    * raw newlines, tabs and other control characters inside a string;
    * **invalid escape sequences** -- code containing `\\d`, `\\s` or `C:\\data`;
    * an **unescaped `"` inside a string**, the hardest of these, distinguishable
      from a real terminator only by what follows it;
    * a trailing comma before `}` or `]`.

    Before this existed, every one of these was silently reported as `no_change` --
    a method decision that nobody made. A reply that is wrong in a way not listed
    here still fails, and still gets reported as a failure rather than as a choice.
    """
    body = _unfence(text)
    start = body.find("{")
    if start < 0:
        return None

    repaired, stack, in_string, complete_at = repair_json_text(body[start:])

    # The scanner's bracket stack is a *guess* at how the document should close, and
    # a hand-rolled scanner can still disagree with the real parser. So the guess is
    # not load-bearing: build every plausible ending and return the first that
    # actually parses.
    candidates: list[str] = []
    if complete_at is not None:
        # **Not** conditioned on the final stack being empty. Measured: a reply that
        # opened with prose containing one balanced `{...}` and then the real payload
        # has its first balanced point inside the prose, while the document as a whole
        # never balances. Requiring `not stack` threw that candidate away and the
        # whole reply was reported unreadable -- even though the platform had already
        # located a prefix that parses.
        candidates.append(repaired[:complete_at])
    candidates.append(repaired)
    closers = "".join("}" if c == "{" else "]" for c in reversed(stack))
    if in_string:
        candidates.append(repaired + '"')
        candidates.append(repaired + '"' + closers)
    if closers:
        candidates.append(repaired + closers)

    # ...plus truncations at the last few closing brackets, which is what handles a
    # reply with one (or more) closers too many. Bounded, so a pathological reply
    # cannot make this quadratic.
    for idx in range(len(repaired) - 1, max(0, len(repaired) - 2000), -1):
        if repaired[idx] in "}]":
            candidates.append(repaired[:idx + 1])

    for cand in candidates:
        try:
            return json.loads(_drop_trailing_commas(cand))
        except json.JSONDecodeError:
            continue
    return None


def _drop_trailing_commas(text: str) -> str:
    """Remove `,` immediately before `}` or `]`, outside strings.

    String-aware rather than a regex, because `,}` inside a string literal is
    content. A model writing a file appends a trailing comma often enough that it
    is worth one pass.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for idx, ch in enumerate(text):
        if escaped:
            escaped = False
        elif ch == "\\" and in_string:
            escaped = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string and ch == ",":
            if text[idx + 1:].lstrip()[:1] in ("}", "]"):
                continue
        out.append(ch)
    return "".join(out)


def run_facts(base: Path) -> str:
    """How this run is configured, in the form a model can act on.

    Why this exists, measured rather than assumed: a method was asked to improve a
    harness whose source contains a `_call_mock` branch, and the model concluded
    from that branch that "the mock backend is a feature, not a bug" and declined to
    edit anything -- on a run that had in fact gone to a real model. The trace it was
    shown could not settle it either: a trace records replies and usage, not which
    backend produced them.

    The platform knows. It records `agent_backend` and `agent_model` on every curve
    point, and the point is in this channel. Leaving the method to infer a fact the
    platform already holds is how a model talks itself out of a real improvement.

    Returns "" when the point says nothing, so a caller can concatenate blindly.
    """
    point = {}
    rp = channel(base) / "round.json"
    if rp.exists():
        try:
            point = json.loads(rp.read_text())
        except (OSError, json.JSONDecodeError):
            point = {}
    if not point:
        return ""
    identity = point.get("identity") or {}
    backend = identity.get("agent_backend") or "unknown"
    model = identity.get("agent_model") or "(not recorded)"
    split = point.get("split") or {}
    lines = [
        "## How this run is configured (from the platform's own record)",
        f"- the harness runs against backend `{backend}`, model `{model}`"
        + ("" if backend != "mock" else "  <- THIS IS A MOCK RUN; scores are not real"),
        f"- the harness is shown {len(protocol.studied_per_task(point))} task(s) and "
        f"scored on {point.get('n_scored', '?')}",
        f"- its score on the tasks it is shown: "
        f"{protocol.studied_score(point) if protocol.studied_score(point) is not None else 'n/a'}",
    ]
    if split:
        lines.append(f"- split: train {len(split.get('train') or [])} / "
                     f"eval {len(split.get('eval') or [])} (the eval side's per-task "
                     f"results are withheld from you on purpose)")
    return "\n".join(lines)


def hypothesis_of(reply: dict) -> str:
    """The model's one-line rationale, wherever it decided to put it.

    Measured: asked for a top-level `hypothesis`, a 9B model nested it inside the
    file object instead. The label then came out empty, so a real 8 KB edit was
    recorded on the curve with no stated reason at all. Reading both places costs
    nothing and a missing rationale is a worse record than a slightly loose reading.
    """
    text = reply.get("hypothesis")
    if not text:
        for entry in (reply.get("files") or []):
            if isinstance(entry, dict) and entry.get("hypothesis"):
                text = entry["hypothesis"]
                break
    return str(text or "")[:300]


#: Preferred reply format, appended to whatever system prompt a method supplies.
#:
#: A whole file inside a JSON string is where nearly every parse failure came from:
#: the model must escape every quote, newline and backslash in an 8 KB program, and
#: a 9B model does not. Measured across this host: missing closers, extra closers,
#: mismatched bracket types, raw control characters, invalid `\\d` escapes and
#: unescaped quotes -- all of them, in the end, are one problem, which is that the
#: file's own quoting has to survive a second encoding.
#:
#: A fenced block has no second encoding. The file is written verbatim, and JSON is
#: still accepted as a fallback, so nothing that worked before breaks.
FORMAT_HINT = """

## How to reply
Prefer a fenced block per file -- it needs no escaping, so it cannot be mangled:

```file:<relative path>
<the complete new file contents, written verbatim>
```

HYPOTHESIS: <one sentence: what you changed and why>

If the evidence supports no change, reply exactly `NO_CHANGE: <reason>`.
JSON (`{"files": [{"path": ..., "content": ...}], "hypothesis": ...}`) is also
accepted if you prefer it, but every quote and newline in the file must then be
escaped. One of these two, and nothing else on stdout.
"""


def parse_reply(text: str) -> dict:
    """Read a model reply in either accepted format.

    Fenced `file:` blocks first, because they are the format that cannot be broken
    by the file's own quoting, then JSON with the repairs in `loads_tolerant`.
    Returns `{}` when neither yields anything, so the caller reports a failure
    rather than inventing a decision.
    """
    # Scanned on the **raw** text: `_unfence` strips a leading ``` fence, which is
    # exactly the fence a `file:` block begins with, so unfencing first destroyed
    # the format this function exists to read. Found by its own test.
    blocks: list[dict] = []
    lines = text.splitlines()
    i = 0
    hypothesis = ""
    no_change = None
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```file:"):
            rel = stripped[len("```file:"):].strip()
            i += 1
            chunk: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                chunk.append(lines[i])
                i += 1
            blocks.append({"path": rel, "content": "\n".join(chunk) + "\n"})
        elif stripped.upper().startswith("HYPOTHESIS:"):
            hypothesis = stripped[len("HYPOTHESIS:"):].strip()
        elif stripped.upper().startswith("NO_CHANGE:"):
            no_change = stripped[len("NO_CHANGE:"):].strip()
        i += 1
    if blocks:
        return {"files": blocks, "hypothesis": hypothesis}
    if no_change is not None:
        return {"no_change": no_change}

    parsed = loads_tolerant(text)
    return parsed if isinstance(parsed, dict) else {}


# ------------------------------------------------------------ what it cost ---

#: The model calls **this method invocation** has made, in tokens.
#:
#: Module-level because the process *is* the invocation: the driver spawns one method
#: process per round and reads its reply once. Before this existed, every method had to
#: remember to carry a number from wherever it called the model down to `report`, and the
#: measured result was that none of them did -- `cost.method_generation_tokens` was 0 on
#: every round of every run, including rounds where the improver answered six model calls
#: over a 24 KB prompt. A cost-aware acceptance rule was therefore reading a constant, and
#: a constant is a rule that permits everything.
_SPENT: dict = {"input": 0, "output": 0, "calls": 0, "model": None}


def spent() -> dict:
    """A copy of what this process has spent so far.

    `calls` is the field that keeps an unreported spend from looking like no spend: an
    OpenAI-compatible endpoint that omits `usage` leaves the token sums at 0, and a reader
    otherwise cannot tell "the improver did nothing" from "the improver's spend was not
    reported".
    """
    return dict(_SPENT)


def spent_tokens() -> int:
    """Input + output: the single number the method reply carries as `generation_tokens`."""
    return _SPENT["input"] + _SPENT["output"]


def reset_spent() -> None:
    """Start a fresh accounting window inside one process.

    A method process is one invocation and calls `report` once, so no method needs this.
    It exists for a caller that wants the spend of one *phase* rather than of the process
    -- the two-stage loops that ask a proposer and then a critic -- and for tests, which
    share an interpreter and would otherwise read each other's numbers.
    """
    _SPENT.update(input=0, output=0, calls=0, model=None)


def _record_usage(usage, model: str | None) -> None:
    """Add one reply's usage. One call is counted whether or not the provider reported it."""
    _SPENT["calls"] += 1
    _SPENT["model"] = getattr(usage, "model", None) or model or _SPENT["model"]
    if usage is None:
        return
    _SPENT["input"] += int(getattr(usage, "prompt_tokens", 0) or 0)
    _SPENT["output"] += int(getattr(usage, "completion_tokens", 0) or 0)


def skill_block(base: Path | None) -> str:
    """The platform's skill, as the prompt block every method inherits by default.

    **Why this is here and not in each method.** The platform stages `improvers/skill.md`
    as `_harnessgrad/SKILL.md` for every method (`harnessgrad/channel.py`), and
    `identity.skill_sha` records it on every point -- but until this function existed only
    four of twelve methods read it, each with its own copy of the path, the header and the
    truncation. That is the same defect the record already had once: a point that names a
    skill it cannot prove was in the prompt. With the block injected here, `skill_sha`
    means "the skill this prompt carried", and a method that wants a different or no skill
    says so once, explicitly, at its call site -- `ask(..., skill=False)`.

    `None`/empty when nothing was staged: a run with no skill gets no block, and the point
    omits `skill_sha` for the same reason (`harnessgrad/identity.py:_skill_sha`).
    """
    if base is None:
        return ""
    path = channel(base) / "SKILL.md"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > SKILL_LIMIT:
        text = (text[:SKILL_LIMIT]
                + f"\n\n[truncated: the skill is {len(text)} chars long, "
                  f"limit {SKILL_LIMIT}] ...\n")
    return ("## How to improve a harness (the platform's skill)\n" + text)


def ask(prompt: str, system: str = DEFAULT_SYSTEM, base: Path | None = None,
        skill: bool = True) -> dict:
    """One model call, with a bounded retry when the reply cannot be read.

    `base` adds the run's configuration block ahead of the method's own prompt. It is
    passed explicitly rather than inferred, because the prompt a method builds is its
    own business and silently rewriting it would make every method's prompt
    unreproducible from its source. The same `base` also supplies the platform's **skill**
    (`skill_block`), injected by default because it is staged for every method and named on
    every point; `skill=False` is how a method says it wants none.

    Retries because a small model's malformed JSON is intermittent, so a second
    sample usually parses -- but only a bounded number of times, and then it is
    reported. Silently returning `no_change` on a parse failure is the one thing
    that must not happen: it turns a broken call into a method decision.
    """
    from openai import OpenAI

    base_url, api_key, model = model_settings()
    client = OpenAI(base_url=base_url, api_key=api_key,
                    timeout=float(os.environ.get("HG_METHOD_TIMEOUT_S", "180")),
                    max_retries=2)
    attempts = max(1, int(os.environ.get("HG_METHOD_PARSE_RETRIES", "2")))
    system = system + FORMAT_HINT
    if base is not None:
        facts = run_facts(base)
        if facts:
            prompt = facts + "\n\n" + prompt
        if skill:
            block = skill_block(base)
            if block:
                prompt = block + "\n\n" + prompt
    last = ""
    for _ in range(attempts):
        resp = client.chat.completions.create(
            model=model, temperature=0.0,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
        )
        last = resp.choices[0].message.content or ""
        # Counted before the reply is judged. A retry that produced unparseable JSON is
        # still a call the method paid for, and a parse failure is exactly when someone
        # asks what the round cost.
        _record_usage(getattr(resp, "usage", None), getattr(resp, "model", None) or model)
        # Debug affordance: `HG_METHOD_DUMP=<path>` writes the raw reply out. A model
        # reply that will not parse is the hardest failure to diagnose from the
        # outside -- the platform only ever sees "unparseable" and 200 characters of
        # tail -- so the raw text has to be obtainable without editing the method.
        dump = os.environ.get("HG_METHOD_DUMP")
        if dump:
            try:
                Path(dump).write_text(last)
            except OSError:
                pass
        parsed = parse_reply(last)
        if parsed:
            return parsed
    raise UnparseableReply(
        f"the model replied {len(last)} chars but no attempt parsed as JSON; "
        f"tail: {last[-200:]!r}")


# ------------------------------------------------------------- the candidate ---

class CandidateInvalid(Exception):
    """The candidate is not a harness the platform can measure. Raised by `report`.

    Raised rather than returned because it is not a method *decision* -- the method
    wanted to hand over a harness and what it has is not one. A method that catches this
    can re-propose; a method that does not gets the `fail()` path, which records the
    reason instead of a score.
    """


def apply_edits(base: Path, dest: Path, edits: list) -> tuple[bool, list[str], str]:
    """Apply an edit sequence onto a copy of `base`, then check the result is a harness.

    The applying itself lives in `eval/candidate.py`, which is the platform's definition
    of what it is about to measure. It used to live here, which meant every port carried
    its own idea of "applied", and the one thing the platform could not do was check the
    claim independently.

    **Step 2 of the two-step protocol.** Step 1 is the model proposing the sequence; this
    is where it is guaranteed to apply. Returns `(changed, paths, problems)` -- a
    non-empty third value is a *reported* failure the caller is expected to act on, not a
    silent `False`.
    """
    applied, problems = candidate.apply(base, dest, edits)
    if not problems:
        problems = candidate.validate(dest)
    return (not problems and bool(applied)), applied, "; ".join(problems)


def _read_manifest(root: Path) -> dict:
    try:
        man = json.loads((Path(root) / "harness.json").read_text(encoding="utf-8"))
        return man if isinstance(man, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def repair_block(problems: str, *, expect: str) -> str:
    """Append a precise apply/format failure to the method's next prompt."""
    if not problems:
        return ""
    return ("\n\n## Your previous reply could not be used\n\n"
            f"{problems}\n\n{expect}")


EDIT_EXPECT = ("Return the complete corrected edit sequence as JSON, with the complete "
               "contents of every file it names, and nothing else.")


def ask_for_json(prompt_for, *, system, base=None, attempts: int = 3,
                 accept=None, what: str = "a JSON reply",
                 skill: bool = True) -> tuple[dict, str]:
    """Retry a JSON-producing model call with the failure fed back to the model.

    `accept(reply)` returns an empty string when the reply is usable, or the text that
    should be placed in the next prompt. This is deliberately shared by proposer and
    critic: repeating the exact same payload after a formatting failure is three copies
    of the same failure, not a repair.
    """
    problems = ""
    reply: dict = {}
    for _ in range(max(1, attempts)):
        try:
            reply = ask(prompt_for(problems), system=system, base=base, skill=skill)
        except UnparseableReply as exc:
            problems = (f"your reply could not be read as {what}: {exc}. "
                        "Answer with the JSON envelope only, and nothing else.")
            continue
        if accept is None:
            return reply, ""
        problem = accept(reply)
        if not problem:
            return reply, ""
        problems = problem
    return reply, problems


def propose_and_apply(base: Path, req: dict, system: str, prompt_for, *,
                      attempts: int = 3, skill: bool = True) -> tuple[dict, Path, str]:
    """Two phases: propose an edit sequence, then apply and validate it."""
    dest = candidate_path(req)

    def accept(reply: dict) -> str:
        if isinstance(reply, dict) and "no_change" in reply:
            return ""
        _, _, problems = apply_edits(base, dest, reply.get("files"))
        return problems

    reply, problems = ask_for_json(
        prompt_for, system=system, base=base, attempts=attempts,
        accept=accept, what="an edit sequence", skill=skill)
    return reply, dest, problems


def report(req: dict, *, method: str, harness_dir: Path, changed: bool,
           files: list[str] | None = None, hypothesis: str = "",
           edit_kind: str | None = None, method_reported: dict | None = None,
           acceptance_rule: str = "", tokens: int | None = None, label: str = "",
           extra: dict | None = None, stop: bool | None = None,
           note: str | None = None) -> int:
    """Write the trajectory and emit the reply. Every exit path goes through here.

    Centralised because "the method wrote no trajectory" is the least diagnosable
    failure this platform has: the driver can only report the absence, and the
    cause -- a provider error, an unparseable reply, a rejected path -- is lost at
    exactly the moment someone needs it.

    **A candidate that does not load is refused here, not measured.** `report` validates
    `harness_dir` and raises `CandidateInvalid` when it is not a harness -- so "reported a
    candidate" and "the edit sequence applied" are the same statement, and a method cannot
    hand over a tree that the platform will score as a weak harness. `changed=False` with
    the pristine base as `harness_dir` is still allowed and still means "I chose not to
    change anything"; that is the one case a reader is entitled to read as a decision.

    `stop` separates two things `changed: false` used to mean at once. A method
    that *rejects* a candidate -- RRSI's leakage critic, TTHE's rollback gate,
    AHE's HARMFUL verdict -- is mid-loop and wants its next round; a method that
    has nothing left to say is finished. Left unset, `stop` defaults to
    `not changed`, which is the behaviour every method had before the field
    existed. A filter sets `stop=False` and keeps its budget.

    `tokens=None` means **what this process spent**, not zero: the numbers come from
    `spent()` (see `_SPENT`). A method may still pass an explicit count, and the one that
    passes 0 is a method that made no model call at all (`echo_base`), which is a fact
    rather than a default.
    """
    spent_now = spent()
    if tokens is None:
        tokens = spent_now["input"] + spent_now["output"]
    if changed:
        problems = candidate.validate(Path(harness_dir))
        if problems:
            raise CandidateInvalid(
                "refusing to report a candidate that is not a harness: "
                + "; ".join(problems))

    trajectory = {
        "steps": [{
            "harness_dir": str(harness_dir),
            "label": label or hypothesis[:80] or ("edit" if changed else "no change"),
            "edit_kind": edit_kind or ("none" if not changed else "harness_source"),
            # `generation_tokens` is the contract field and stays the single number a
            # cost-aware rule reads. The split is kept beside it because it is the part
            # that says *why* the number is large: measured on our own improver, one
            # round is ~24 KB of harness sources in and ~200 tokens out, so a rising
            # improver cost is a source-reading cost, not a thinking cost.
            "claimed_cost": {"generation_tokens": tokens,
                             "method_input_tokens": spent_now["input"] or None,
                             "method_output_tokens": spent_now["output"] or None,
                             "method_model_calls": spent_now["calls"] or None,
                             "method_model": spent_now["model"]},
            "method_reported": {"score": None, **(method_reported or {})},
        }],
        "trajectory_shape": "sequence",
        # Mode A: the platform owns the schedule, so nobody nominates a step.
        "nominated": 0,
        "provenance": {
            "method": method,
            "acceptance_rule": {"text": acceptance_rule,
                                "source": f"methods/{method}/run.py",
                                "calibrated": False},
            **(extra or {}),
        },
    }
    # `Path("")` is `PosixPath('.')`, which is truthy, so the old `if out:` guard let a
    # request with no `trajectory_out` through and then wrote the trajectory *onto a
    # directory*. The driver always sets the key, so this only ever hit a method invoked
    # by hand -- which is exactly how the next person will invoke it.
    raw_out = req.get("trajectory_out")
    out = Path(raw_out) if raw_out else None
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(trajectory, indent=1))

    response = {"modified": bool(changed), "changed": bool(changed),
                "files": list(files or []), "hypothesis": hypothesis,
                "generation_tokens": tokens,
                # What the process itself saw the provider report. Kept apart from
                # `generation_tokens` on purpose: the claim and the observation are two
                # statements, and a method that counts its own calls by hand is exactly
                # the case where they can disagree.
                "method_usage": spent_now,
                "method_reported": method_reported or {}}
    if stop is not None:
        response["stop"] = bool(stop)
    # The method's own one-line account of this round. The driver falls back to a
    # neutral default, because it cannot tell a rejected candidate from a declined
    # proposal -- and guessing makes the log say something the method did not.
    if note is not None:
        response["note"] = str(note)[:160]
    emit(response)
    return 0


def fail(req: dict, *, method: str, exc: BaseException | str, base: Path,
         state: dict | None = None, stop: bool = False) -> int:
    """A trajectory for the error path, and the reason on stderr where it is read.

    An unreadable failure is worse than a reported one: it looks like the method
    did nothing.

    `state` carries the method's own record through the failure. Several methods
    keep their *cross-round memory* there -- DGM's `dgm_parent` is the lineage the
    child-count term reads, AHE's `ahe_manifest` is the pre-registration the next
    round grades, RRSI's `rrsi_rule` holds the pruned components -- so a failure
    that dropped it would silently erase that memory. Measured: five of the seven
    ports lost their record on the error path while `tthe` and `harnessx`, which
    report instead of failing, kept theirs. Same event, two different records.

    `stop` defaults to **False**, because a caught exception here is a runtime call
    failure -- a 500, a timeout, an unreadable reply -- and not a decision by the
    method. Structural failures (missing credentials, a wrong API version) raise
    `SystemExit` and are re-raised by the callers rather than caught, so they never
    reach this function. Ending a run on one transient blip would cost a method the
    rest of its budget, which is the same unfairness the `stop` field exists to
    remove.
    """
    why = exc if isinstance(exc, str) else f"{type(exc).__name__}: {str(exc)[:300]}"
    print(f"{method}: {why}", file=sys.stderr)
    return report(req, method=method, harness_dir=base, changed=False, stop=stop,
                  hypothesis=why, edit_kind="none",
                  method_reported={"error": why, **(state or {})},
                  acceptance_rule="not reached -- the method failed before deciding",
                  label="method error")


def candidate_path(req: dict, name: str = "candidate") -> Path:
    """Where a method writes its candidate harness."""
    return Path(req["workspace"]) / name
