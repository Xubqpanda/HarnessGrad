"""RRSI domain adapter: RRSI's search, HarnessGrad's measurement.

Why this file exists
--------------------
RRSI is explicit that it "never runs an agent, grades a deliverable or reads a
trajectory format" -- a domain adapter supplies those. The three adapters that ship
with it (`coding/`, `eng/`, `workspace/`) all supply harbor. This one supplies
HarnessGrad instead, which means **RRSI's own code does not change at all**: its
annealed budget (`rrsi/schedule.py`), its acceptance rule (`rrsi/selection.py`), its
novelty term, its critic and its loop all run as published. Only the two questions a
domain must answer are answered here:

    run()      put this harness in front of these tasks and record what happened
    score()    turn those records into the numbers the acceptance rule reads

Where the boundary falls
------------------------
HarnessGrad measures; RRSI decides. `run()` calls the platform's `eval.runner.run_one`
per task, in the platform's own sandbox, and writes one `result.json` per trial in the
shape the domain contract expects. `score()` returns `TaskResult` objects carrying
**per-trial rewards and per-trial token counts**, because RRSI's cost rule
(`Delta C = (C' - C_t) / C_t`) is the thing that lets a candidate buy score with
tokens only in proportion to what it gains. Get that wrong and the rule silently
permits everything.

Two failures found by building this, both worth keeping visible
--------------------------------------------------------------
1. **The environment.** A process that imports the platform does not get the
   platform's model configuration; `driver.py` loads `.env` and nothing else does.
   Without it the harness falls back to its `mock` backend and answers `ls -a`
   forever -- scored 0.0, reported zero tokens, raised no error, wrote an empty
   stderr. A benchmark that silently measures a fake model is worse than one that
   crashes, so `__init__` does what the driver does.

2. **The contract shapes.** `score()` returns `dict[str, TaskResult]`, not scores, and
   `run()` cannot be folded into the platform's `evaluate()` helper because that
   aggregates tokens across the whole task set while RRSI needs them per trial. Both
   were discovered by reading `domains/coding/adapter.py` rather than by guessing.

What is deliberately not here
-----------------------------
RRSI's `k` trials per task exist to estimate a noise band. The reference base harness
calls its model at `temperature=0.0`, so k trials of the *same* harness are identical
and its measured variance is zero -- the band must come from `delta_z` calibration
over repetitions, not from assuming the harness is stochastic. This adapter reports
what happened and does not pretend otherwise: `smoke()` checks liveness, and the
`extra` block records how many trials actually ran so a reader can see whether k did
anything.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

#: Where the platform lives. Importable, not installed -- the platform is a checkout,
#: and this adapter is the only thing that needs to know where.
HG_ROOT = Path(os.environ.get(
    "HARNESSGRAD_ROOT",
    Path(__file__).resolve().parent.parent.parent.parent / "HarnessGrad",
)).resolve()
if str(HG_ROOT) not in sys.path:
    sys.path.insert(0, str(HG_ROOT))

from rrsi.domain import Domain          # noqa: E402
from rrsi.evaluate import TaskResult    # noqa: E402


class HarnessGradDomain(Domain):
    name = "harnessgrad"
    harness_path = "harness"

    def __init__(self, cfg: dict | None = None):
        # The platform's `.env`, loaded exactly as `driver.py` loads it. See the
        # module docstring: without this the harness runs on a fake model and every
        # number downstream is fiction that looks like a measurement.
        from driver import load_env
        load_env(HG_ROOT / ".env")

        # The domain reads its own config, because RRSI's `load_domain()` constructs
        # the object with no arguments -- `rrsi.json` is the contract with RRSI, and
        # this directory is the only place that knows where it is. RRSI reads the same
        # file through `RRSIConfig.load()`, which ignores the keys it does not know
        # (`dataset`, `smoke_tasks`) and keeps them in `notes`.
        here = Path(__file__).resolve().parent
        cfg_file = here / "rrsi.json"
        self.cfg = cfg if cfg is not None else (
            json.loads(cfg_file.read_text()) if cfg_file.exists() else {})
        self.dataset = self.cfg.get("dataset", "probe_set")
        self.task_ids: list[str] | None = None
        self.scorable: dict[str, str] = {}

    # ---- the task set ------------------------------------------------------

    def _load_tasks(self) -> tuple[list[dict], dict[str, str]]:
        """The tasks, from the platform's dataset registry.

        Read through the platform rather than copied here, so a domain and the
        platform's own runs are measuring the same thing by construction. A second
        copy of the task list is a second thing to keep in sync, and the failure mode
        is two experiments that claim to be comparable.
        """
        import data.registry as registry
        return registry.load(self.dataset)

    def _by_id(self) -> dict[str, dict]:
        tasks, scorable = self._load_tasks()
        self.scorable = scorable
        return {t["task_id"]: t for t in tasks}

    def _split(self) -> dict | None:
        """The dataset's declared train/eval split, or None if it declares none."""
        import data.registry as registry
        _, _, split = registry.load_split(self.dataset)
        return split

    def evolve_ids(self) -> list[str]:
        """The tasks RRSI may select onto: the **train** side, when there is a split.

        RRSI's evolve set is what the search iterates on, so it must not reach into
        the eval side -- a search that can read the traces of the tasks it is later
        scored on is the exact failure the split exists to prevent.
        """
        _, scorable = self._load_tasks()
        split = self._split()
        return sorted(split["train"] if split else scorable)

    def heldout_ids(self) -> list[str]:
        """The **eval** side. Empty only when the dataset declares no split.

        This used to return `[]` unconditionally, with a comment explaining that six
        local tasks left nothing to hold out. That was a true statement about the
        dataset and a false one about the interface: the platform carries the split,
        and inventing a held-out number from the same tasks would have been worse
        than admitting there was none -- but *reading* it, once it exists, is the
        whole point.
        """
        split = self._split()
        return sorted(split["eval"]) if split else []

    def smoke_ids(self, incumbent_per_task=None) -> list[str]:
        """One task, chosen as the cheapest liveness check available.

        `smoke()` is a gate, not a selection rule (RRSI says so explicitly). Its job is
        to catch a harness that cannot start at all before k trials are spent on it,
        so the cheapest task that still exercises the entrypoint is the right choice.
        """
        ids = self.evolve_ids()
        return ids[:1]

    # ---- run ---------------------------------------------------------------

    def _trial_dir(self, runs_dir: Path, job: str, task_id: str, trial: int) -> Path:
        return Path(runs_dir) / "jobs" / job / f"{job}__{task_id}__t{trial}"

    def run(self, root: Path, runs_dir: Path, job: str, ids: list[str], k: int,
            log_prefix: str = "") -> None:
        """Measure the harness checked out under `root` on `ids`, k trials each.

        One `result.json` per trial, in the shape `domains/*/adapter.py` reads. The
        platform does the executing -- including the mount namespace that keeps the
        harness away from the scorer -- and this method only decides what to run and
        where to record it.

        Resume-safe by construction: an existing `result.json` is left alone, which is
        what RRSI's contract asks for and what makes a long run restartable.
        """
        from eval.runner import run_one, _last_usage

        root, runs_dir = Path(root).resolve(), Path(runs_dir).resolve()
        by_id = self._by_id()
        repo = self.harness_dir(root)

        if not (repo / "harness.json").exists():
            raise SystemExit(
                f"the harness RRSI is evolving has no harness.json: {repo}. "
                f"HarnessGrad's unit of exchange is a directory that can solve a task "
                f"on its own (INTERFACE.md §1.1), and RRSI is set up to evolve exactly "
                f"that directory.")

        for task_id in ids:
            task = by_id.get(task_id)
            if task is None:
                print(f"{log_prefix}unknown task {task_id!r}, skipped", file=sys.stderr)
                continue
            for trial in range(k):
                td = self._trial_dir(runs_dir, job, task_id, trial)
                if (td / "result.json").exists():
                    continue
                td.mkdir(parents=True, exist_ok=True)

                started = time.time()
                result = run_one(repo, task, sandbox=True)
                usage = _last_usage(result["trace"]) or {}
                expected = self.scorable.get(task_id)
                reward = float(
                    expected is not None
                    and result["answer"].strip().lower() == expected.strip().lower()
                )

                # Everything the platform saw, kept beside the verdict. When a number
                # looks wrong later, the trace and the stderr are the only way to tell
                # "the harness failed" from "the harness was never given a chance".
                (td / "harness_stderr.txt").write_text(result["stderr"] or "")
                (td / "harness_trace.jsonl").write_text(result["trace"] or "")
                (td / "answer.txt").write_text(result["answer"] or "")
                (td / "result.json").write_text(json.dumps({
                    "task_name": f"harnessgrad/{task_id}",
                    "verifier_result": {"rewards": {"reward": reward}},
                    "agent_result": {
                        "n_input_tokens": usage.get("input_tokens") or 0,
                        "n_output_tokens": usage.get("output_tokens") or 0,
                        "metadata": {
                            "n_episodes": usage.get("calls"),
                            "wall_clock_s": round(time.time() - started, 1),
                            "agent_model": os.environ.get("HG_AGENT_MODEL"),
                        },
                    },
                    # A non-zero exit or a missing trace is an infrastructure failure,
                    # and `infra_failure()` in the domain contract treats it as a
                    # missing trial rather than a wrong answer. Conflating "the harness
                    # crashed" with "the harness answered wrongly" moves the score for
                    # a reason that has nothing to do with the candidate.
                    "exception_info": (
                        {"exception_type": "HarnessError",
                         "exception_message": (result["stderr"] or "(no stderr)")[:400]}
                        if (result["exit_code"] != 0 or not result["trace"]) else None),
                }, indent=1))

    # ---- score -------------------------------------------------------------

    def score(self, runs_dir: Path, job: str, ids: list[str], k: int
              ) -> tuple[dict, dict]:
        """Per-task trial records -> what RRSI's acceptance rule reads.

        Returns `({task_id: TaskResult}, extra)`. Each `TaskResult` carries one reward
        and one token count per trial: RRSI averages rewards for `S` and averages
        tokens for `C`, and its cost rule compares candidates on both.

        A trial that never ran -- absent, or an infrastructure failure -- is counted as
        missing with reward 0.0, which is what the contract asks for. It is *not* the
        same as a wrong answer, and `extra` keeps the two apart so a run can be
        audited afterwards.
        """
        per: dict[str, TaskResult] = {}
        total_pass = infra = 0

        for task_id in ids:
            rewards: list[float] = []
            tokens: list[int | None] = []
            for trial in range(k):
                f = self._trial_dir(runs_dir, job, task_id, trial) / "result.json"
                if not f.exists():
                    continue
                record = json.loads(f.read_text())
                if _is_infra_failure(record):
                    infra += 1
                    continue
                reward = ((record.get("verifier_result") or {}).get("rewards")
                          or {}).get("reward")
                rewards.append(float(reward) if reward is not None else 0.0)
                ar = record.get("agent_result") or {}
                n = (ar.get("n_input_tokens") or 0) + (ar.get("n_output_tokens") or 0)
                # `None`, not 0: a harness that reports no usage and one that reports
                # zero usage are different facts, and RRSI's `C` is None when nothing
                # reported rather than zero.
                tokens.append(n or None)

            missing = k - len(rewards)
            rewards += [0.0] * missing
            tokens += [None] * missing
            passes = sum(1 for x in rewards if x == 1.0)
            total_pass += passes
            per[task_id] = TaskResult(
                rewards=rewards, tokens=tokens, missing=missing,
                extra={"passes": passes,
                       "trial_dirs": [str(self._trial_dir(runs_dir, job, task_id, t))
                                      for t in range(k)]})

        n_trials = len(ids) * k
        extra = {
            "total_passes": total_pass,
            "n_trials": n_trials,
            "pass_rate": total_pass / max(1, n_trials),
            "infra_failures": infra,
            "dataset": self.dataset,
        }
        extra.update(self._method_usage(runs_dir))
        return per, extra

    def _method_usage(self, runs_dir) -> dict:
        """What RRSI's own four roles have spent, so far in this process.

        This closes an accounting gap that ran the other way round from the usual
        one. The *harness's* cost was already observed -- the harness writes its own
        `usage` into its trace and the platform reads it -- while the *method's* cost
        was invisible: RRSI discarded `resp.usage` on every call it made. A curve could
        therefore say exactly what a harness spent and nothing about what the search
        spent to produce it, which is half of a fair comparison: a method that buys a
        score with a hundred times the tokens is not obviously the better method.

        Recorded as a snapshot rather than a per-round delta because the counter is
        cumulative for the process and the adapter does not own the round boundary.
        Timestamped, and rewritten on every score, so the sequence of snapshots
        reconstructs the spend per round afterwards -- and a reader can tell a
        cumulative counter from a per-round one by the timestamps.
        """
        try:
            import rrsi.llm as llm
            usage = llm.usage_snapshot()
        except Exception as exc:                      # noqa: BLE001
            return {"method_usage_error": f"{type(exc).__name__}: {exc}"}

        record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "cumulative": True,
                  "max_tokens_per_call": getattr(llm, "MAX_TOKENS", None),
                  "request_timeout_s": getattr(llm, "REQUEST_TIMEOUT", None),
                  **usage}
        path = Path(runs_dir) / "method_usage.json"
        try:
            history = json.loads(path.read_text()) if path.exists() else []
            history.append(record)
            path.write_text(json.dumps(history, indent=1))
        except OSError:
            pass  # a missing side-record must not fail a measurement
        return {"method_tokens_total": usage.get("total_tokens"),
                "method_calls": usage.get("calls"),
                "method_reasoning_tokens": usage.get("reasoning_tokens"),
                "method_empty_replies": usage.get("empty_replies")}

    # ---- evidence ----------------------------------------------------------

    def load_trial(self, runs_dir, job, task_id, trial):
        f = self._trial_dir(runs_dir, job, task_id, trial) / "result.json"
        if not f.exists():
            return None
        record = json.loads(f.read_text())
        ar = record.get("agent_result") or {}
        return {
            "task_id": task_id,
            "trial_dir": str(f.parent),
            "answer": (f.parent / "answer.txt").read_text() if (f.parent / "answer.txt").exists() else "",
            "reward": ((record.get("verifier_result") or {}).get("rewards") or {}).get("reward"),
            "episodes": (ar.get("metadata") or {}).get("n_episodes"),
            "wall_clock_s": (ar.get("metadata") or {}).get("wall_clock_s"),
            "exception": (str(record.get("exception_info"))[:200]
                          if record.get("exception_info") else None),
            "trace_path": str(f.parent / "harness_trace.jsonl"),
        }

    def render_trace(self, rec, detail: bool = False) -> str:
        """What RRSI's analyst and digester read.

        The harness's own trace is the evidence, and it is already a readable JSONL of
        replies and command results. Rendered with the task and the verdict around it,
        because a trace with no outcome attached is a transcript, not a finding.
        """
        if not isinstance(rec, dict):
            return str(rec)[:2000]
        parts = [f"task {rec.get('task_id')}: reward={rec.get('reward')} "
                 f"episodes={rec.get('episodes')}"]
        if rec.get("exception"):
            parts.append(f"exception: {rec['exception']}")
        path = rec.get("trace_path")
        if path and Path(path).exists():
            text = Path(path).read_text()
            if not detail:
                text = "\n".join(text.splitlines()[:24])
            parts.append(text[:4000])
        return "\n".join(parts)

    def task_row(self, task_id, rec, tr) -> str:
        if rec is None:
            return f"{task_id:<6} no trial"
        return (f"{task_id:<6} reward={rec.get('reward')} "
                f"episodes={rec.get('episodes')} "
                f"{(rec.get('answer') or '')[:40]!r}")

    def smoke(self, root, runs_dir, job, ids) -> tuple[bool, dict]:
        """Liveness only: did the harness start, reach a model, and write an answer?

        Deliberately not a quality gate. RRSI uses `smoke` to avoid spending k trials
        on a harness that cannot run at all; turning it into "did it pass" would make
        it a second, undocumented selection rule.
        """
        runs_dir = Path(runs_dir)
        self.run(root, runs_dir, job, ids, 1, log_prefix="smoke: ")
        details, alive = {}, True
        for task_id in ids:
            info = self.load_trial(runs_dir, job, task_id, 0)
            if info is None:
                details[task_id] = "no trial recorded"
                alive = False
                continue
            trace = Path(info["trace_path"])
            has_trace = trace.exists() and trace.stat().st_size > 0
            details[task_id] = {
                "ran": True, "wrote_trace": has_trace,
                "episodes": info["episodes"], "exception": info["exception"]}
            if not has_trace or info["exception"]:
                alive = False
        return alive, details

    # ---- what RRSI's roles are told about this domain ----------------------

    #: Deterministic denylist for RRSI's critic. Checked against the candidate's diff
    #: before any model is asked, and a hit is a hard rejection.
    #:
    #: **`(regex, reason)` pairs**, which is the format `rrsi/critic.py:precheck` unpacks.
    #: Measured: a list of bare strings raised
    #: `ValueError: too many values to unpack (expected 2)` on the first round, after the
    #: analyst had already run -- so the format costs a round to discover and is worth
    #: getting right the first time.
    #:
    #: Every entry here is a way to make the score mean something other than what it
    #: says. Note what is *absent*: `shell=True`. Running a shell command is how this
    #: harness does its job, and a denylist that rejects the harness's own tool would
    #: reject every real candidate.
    critic_patterns = [
        (r"\beval/runner\.py\b", "the platform's scorer in the diff"),
        (r"\beval/metrics\.py\b", "the platform's metrics in the diff"),
        (r"\bdriver\.py\b", "the platform's driver in the diff"),
        (r"\beval/integrity\.py\b", "the platform's self-check in the diff"),
        (r"\bscorable\b|\bSCORABLE\b", "the answer key referenced in the diff"),
        (r"\.env\b", "credential file referenced in the diff"),
        (r"\bHG_METHOD_", "the method's own budget referenced in the diff"),
        (r"\bharnessgrad_work\b", "the platform's work root referenced in the diff"),
        (r"answer\s*==\s*['\"]|['\"]\s*==\s*answer", "answer comparison hardcoded"),
    ]

    #: `(component, [regexes])` pairs mapping a diff to one of RRSI's nine component
    #: tags, which it records as `edit_kind` and uses for its novelty term.
    #:
    #: Both halves had to be looked up rather than guessed. `rrsi/components.py:classify_diff`
    #: iterates `for component, pats in domain_signals`, so the component name comes
    #: **first** and the patterns are a **list** -- the reverse of a `(regex, label)` pair,
    #: and a list of `(regex_string, name)` tuples (which is what this file first shipped)
    #: would have silently tagged every edit by substring rather than by component. The
    #: names must also come from RRSI's fixed vocabulary `K` (components.py:44): a tag
    #: outside it corrupts the count of what has been tried.
    #:
    #: RRSI's own signals describe its agent -- `TBMH_STATE_DIR` for memory, `episode` for
    #: control flow. None of those names exist in a 218-line ReAct loop, so the patterns
    #: here point at this harness's actual parts. `memory`, `skill` and `subagent` are
    #: deliberately absent rather than mapped onto something they are not: the generic
    #: signals already cover a candidate that genuinely adds one.
    component_signals = [
        ("client_tool", [r"subprocess\.run", r"shell=True", r"def _run_command",
                         r"def run_command", r"\btool\b.*dispatch"]),
        ("output_plumbing", [r"def _parse", r"json\.loads", r"strip\(\)", r"\bre\.search",
                             r"regex", r"extract_json", r"fence"]),
        ("context_mgmt", [r"messages\.append", r"messages\s*=", r"\btruncat",
                          r"max_tokens", r"MAX_TOKENS", r"summar", r"history"]),
        ("control_flow", [r"MAX_STEPS", r"max_steps", r"for step in range",
                          r"while True", r"\bbreak\b", r"attempt", r"retry", r"retries"]),
        ("config", [r"os\.environ", r"getenv", r"=\s*\d+\s*(#|$)", r"timeout",
                    r"DEFAULT_"]),
        ("prompt", [r"SYSTEM\s*=", r"system_prompt", r"\bprompt\b", r'"""']),
    ]

    @property
    def briefs(self) -> dict:
        """The four role briefs, loaded from beside this file.

        Not `import briefs`: RRSI execs this module by path
        (`rrsi/domain.py:load_domain`) and never puts the domain's directory on
        `sys.path`, so a plain import resolves only when the working directory
        happens to be the domain -- which it is not when RRSI is invoked as
        `python3 rrsi.py --domain harnessgrad` from the checkout root. Measured:
        `ModuleNotFoundError: No module named 'briefs'` from a correct install.
        """
        module = _load_sibling("briefs")
        return {"analyst": module.ANALYST, "digester": module.DIGESTER,
                "proposer": module.PROPOSER, "critic": module.CRITIC}


def _load_sibling(name: str):
    """Import a module that ships beside this adapter, by absolute path."""
    import importlib.util
    path = Path(__file__).resolve().parent / f"{name}.py"
    if not path.exists():
        raise SystemExit(f"the harnessgrad domain is incomplete: {path} is missing. "
                         f"Re-run adapters/install_rrsi_domain.sh.")
    spec = importlib.util.spec_from_file_location(f"harnessgrad_domain_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_infra_failure(record: dict) -> bool:
    """Did the trial fail to get a working environment, rather than answer wrongly?

    Same distinction the shipping adapters make with their docker/harbor patterns.
    Here the only infrastructure is the platform's own launcher, so anything the
    platform could not start is infrastructure and anything it started is a result.
    """
    exc = record.get("exception_info")
    if not exc:
        return False
    message = str(exc.get("exception_message", "")) if isinstance(exc, dict) else str(exc)
    return any(s in message for s in (
        "sandbox", "bwrap", "SandboxUnavailable", "harness.json",
        "could not start", "No such file"))


DOMAIN = HarnessGradDomain()
