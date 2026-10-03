"""What a measurement says it is.

The identity block is the part of a curve point a reader uses to decide whether two points
are comparable at all: which harness (by content hash), which models answered, which
improver composed the edits, on which side of the split, measured where. It is assembled
from a manifest, the models the run actually used, and whatever the harness declared about
itself in its own trace.

Pure on purpose -- no docker, no HTTP, and no filesystem beyond what it is handed, with one
deliberate exception that `_skill_sha` documents: the skill text is the platform's own input
to the method, it is a single fixed path, and a point that does not name it cannot be
compared with a point taken after it was edited. The improver used to be a module-level
global here that `main()` assigned into; it is an argument now, so "what does this point
claim" is answerable without running a program, and a caller that forgets it gets an
identity with no improver rather than one run's improver leaking into another's record.
"""

from __future__ import annotations

import hashlib
import os

from harnessgrad import FRAMEWORK_VERSION, PLATFORM_ROOT

def _model_of(declared: dict | None) -> str | None:
    """Which model actually answered, and where that answer came from.

    The harness's declaration wins over the platform's environment, because the
    harness is the only party that knows. When both exist and disagree, both are kept
    (`agent_model_conflict` below) -- resolving it silently in either direction is how
    a record ends up disagreeing with the run it describes.
    """
    declared = declared or {}
    return declared.get("agent_model") or os.environ.get("HG_AGENT_MODEL") or None

def _backend_of(declared: dict | None) -> str:
    declared = declared or {}
    return (declared.get("agent_backend")
            or os.environ.get("HG_AGENT_BACKEND") or "mock")

def _conflict(declared: dict | None) -> dict:
    """Both values, when the harness and the platform disagree about the model."""
    declared = declared or {}
    from_harness = declared.get("agent_model")
    from_env = os.environ.get("HG_AGENT_MODEL")
    if from_harness and from_env and from_harness != from_env:
        return {"harness": from_harness, "platform_env": from_env,
                "note": "the harness did not use HG_AGENT_MODEL; the declaration is "
                        "what ran"}
    return {}

def _skill_sha() -> str | None:
    """The sha256 of the skill the platform offered the method this round, or nothing.

    `improver` says *which tool* composed the candidate. It says nothing about what that
    tool was told, and the skill is the platform's own input to the method -- the same
    improver given a different skill produces different edits. Measured reason this field
    exists: `improvers/skill.md` gained a fourth failure mode between two runs, both points
    named the same method and the same improver version, the edits changed, and nothing in
    either record said why. That is a comparison a reader cannot make, which is the whole
    job of this block.

    Read per call, not cached at import, because the platform may edit its own skill between
    rounds of one run -- self-improvement on the method side is a case this platform exists
    to measure, and a hash frozen at import would report the first round's skill forever.

    `None` when the file is absent, which is a real state: a run with no default skill stages
    no `SKILL.md`, and the field is then omitted rather than hashed over an empty file.
    """
    try:
        return hashlib.sha256(
            (PLATFORM_ROOT / "improvers" / "skill.md").read_bytes()).hexdigest()
    except OSError:
        return None


#: The fields of a resolved improver that belong on a curve point. `ok` and `reason` are
#: bookkeeping for the process that resolved it and are dropped: a record should carry what
#: was used, not the outcome of looking for it.
IMPROVER_IDENTITY_FIELDS = ("name", "version", "sha256", "model", "requested", "modified",
                            "path")


def _improver_identity(improver: dict | None) -> dict:
    """The improver's identity, or nothing at all.

    Passed in rather than read from a module global: "which improver composed these edits"
    is a property of the point being written, and a global made it a property of whichever
    run happened to be in the same process. An empty dict means the run carried no improver
    -- the reference methods do their own editing -- and the field is then absent rather
    than empty, because a point must never claim an improver it did not use.
    """
    improver = improver or {}
    return {k: improver[k] for k in IMPROVER_IDENTITY_FIELDS if k in improver}


def _improver_runtime(improver: dict | None) -> tuple[str, ...]:
    """The trees on disk the improver needs in order to *run*.

    The other half of the same resolution, and deliberately not part of what a point
    says: this is what the method's sandbox has to bind so that the method can execute
    the tool the platform picked (`tools/improver.py:runtime_trees`;
    `harnessgrad/methods.py:_method_sandbox_plan`). A tuple rather than a list because it
    is handed straight to the sandbox plan, and a plan that changes between the hash
    check and the call is the kind of thing this platform exists to notice.
    """
    return tuple(str(path) for path in ((improver or {}).get("runtime") or []))


def _identity(man: dict, sha: str, mode: str, method_model: str = "n/a",
              declared: dict | None = None, improver: dict | None = None) -> dict:
    """Who produced this number.

    There is no platform-side method identity any more: the method IS the
    implementation of the entrypoints inside the harness repo. So the harness
    version is the method version, and `editable_surface_touched` (below) is how
    the platform reports what that method did to itself.
    """
    return {
        "harness_name": man["name"], "harness_version": man["version"],
        "harness_sha": sha,
        "improve_entrypoint": man.get("improve_entrypoint"),
        "accept_entrypoint": man.get("accept_entrypoint"),
        "run_mode": mode,
        "framework_version": FRAMEWORK_VERSION,
        # Read from the same names the harness reads, or the record disagrees with
        # the run: this line said `mock` while a live deepseek-flash answered every
        # task, because the variable had been renamed in the harness and not here.
        # A curve point that misreports its model is worse than one that omits it.
        #
        # `HG_AGENT_*` is *our* convention, and it is the whole story only for a
        # harness we wrote. An agent CLI reads its model from its own config file or a
        # flag, so the platform's environment can be entirely ignored -- which is how
        # the record starts lying. A harness may therefore declare what ran it, in its
        # trace (`eval/runner.py:_last_identity`), and the declaration wins.
        "agent_model": _model_of(declared),
        "agent_backend": _backend_of(declared),
        "agent_model_source": ("harness" if (declared or {}).get("agent_model")
                               else "platform-env"),
        "method_model": method_model,
        # **Which improver produced the candidate.** A method is a decision rule *and*
        # an improver, and until this field existed the platform recorded only the rule's
        # model -- so an improver upgrade was indistinguishable from a better method. The
        # version and the hash are the parts that cannot be reconstructed later: several
        # codex builds coexist on one machine (measured: 0.125.0 broken, 0.139.0, 0.156.1)
        # and `latest` is a request, not an identity.
        **({"improver": _improver_identity(improver)} if improver else {}),
        # Which instructions the improver was given, kept apart from which improver it was.
        **({"skill_sha": s} if (s := _skill_sha()) else {}),
        **({"harness_identity": declared} if declared else {}),
        **({"agent_model_conflict": c} if (c := _conflict(declared)) else {}),
    }

