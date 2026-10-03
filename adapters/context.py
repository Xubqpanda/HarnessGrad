"""The uniform contract every adapter implements, so the platform can call any
of them the same way.

Why this file exists: the nine adapters were written one at a time and ended up
with three different signatures --

    (repo, run_root)      rrsi, dgm
    (run_root)            sica
    (run_root, domain)    hyperagents
    (repo, domain, out_dir)  tthe
    (repo, out_dir)       ahe, mac, harnessx
    (repo)                metaharness

-- and `entrypoint.py` supplied a fixed set of keywords. Two thirds of them could
not be called through the platform at all, which meant "nine adapters" described
nine scripts rather than nine integrations.

The uniform shape is three arguments, because three is what the adapters actually
vary over:

    repo      where the method's own checkout is (read-only)
    run_dir   where this adapter may write (scratch, compiled states, output)
    options   method-specific keys, e.g. {"domain": "text_to_sql"}

An adapter reads what it needs from `options` and ignores the rest. Adapters that
compile states write them under `run_dir`; adapters that only read artifacts leave
`run_dir` alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AdapterRequest:
    repo: Path | None = None
    run_dir: Path = field(default_factory=lambda: Path("/tmp/hg-adapter"))
    options: dict = field(default_factory=dict)

    def option(self, *names: str, default=None):
        """First present option among `names`.

        Adapters accumulated synonyms while being written one at a time
        (`domain` / `example`, `run_root` / `repo`). Accepting a list means the
        call site does not have to know which spelling an adapter chose.
        """
        for n in names:
            if n in self.options and self.options[n] is not None:
                return self.options[n]
        return default

    @property
    def out_dir(self) -> Path:
        return self.run_dir


def call(module, request: AdapterRequest) -> dict:
    """Invoke an adapter's `to_trajectory` under the uniform contract.

    Adapters written against the old three-signature spread are called with the
    arguments they declare, discovered by inspection rather than by a hand-kept
    table -- a table would drift the moment someone writes the tenth adapter, and
    the drift would look like "that adapter is broken".
    """
    import inspect

    fn = module.to_trajectory
    params = inspect.signature(fn).parameters

    kwargs: dict = {}
    if "repo" in params:
        if request.repo is None:
            raise ValueError(
                f"{module.__name__}.to_trajectory needs `repo`; set HG_REPO")
        kwargs["repo"] = request.repo
    if "run_root" in params:
        kwargs["run_root"] = request.option("run_root") or request.run_dir
    if "run_dir" in params:
        kwargs["run_dir"] = request.run_dir
    if "out_dir" in params:
        kwargs["out_dir"] = request.out_dir
    if "domain" in params:
        kwargs["domain"] = request.option("domain")
    if "example" in params:
        kwargs["example"] = request.option("example", "example_name",
                                           default="minimal")
    if "solution" in params:
        kwargs["solution"] = request.option("solution")
    return fn(**kwargs)
