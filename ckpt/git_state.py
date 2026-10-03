"""Harness state, backed by git. See INTERFACE.md §1 and §4.

Why git and not tarballs: a harness is code, so the state of the art in
versioning code already exists. Commits give us checkpoints, prefixes give us
rollback, and identical prefixes are deduplicated for free.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

#: The harness's own writable scratch space inside its tree during a task. Named
#: here because three modules need to agree on it: `init_repo` creates it,
#: `eval/sandbox.py` makes it writable, and `run_one` exports its path.
STATE_DIRNAME = ".state"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def init_repo(root: Path) -> None:
    """Turn `root` into a git repo with one commit holding its current contents."""
    if (root / ".git").exists():
        return
    _git(root, "init", "-q")
    # `_harnessgrad/` is the framework<->Trainer channel, not part of the
    # harness. If it were committed, a no-op Trainer would still mint a new
    # sha every round and the control curve would look like movement.
    gi = root / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    # `_harnessgrad/` is the framework<->method channel; `__pycache__/` is noise
    # from the process-call entrypoints. Neither is harness state, and committing
    # either would make a no-op round mint a new sha and look like movement.
    # `.state/` is the harness's own scratch space during a task: it is the one
    # writable place inside an otherwise read-only tree (eval/sandbox.py), so its
    # contents are by definition not part of the harness and must never be
    # committed -- a harness that wrote a cache file would otherwise mint a new
    # sha and the curve would show movement that no method made.
    for rule in ("_harnessgrad/", "__pycache__/", "*.pyc", ".state/"):
        if rule not in existing:
            existing = existing.rstrip("\n") + f"\n{rule}\n"
    gi.write_text(existing)
    # Created here, once per run, rather than on demand: the sandbox mounts a
    # writable tmpfs over this path, and a mount target has to exist. Creating it
    # at staging time also means every harness in every run has it, so its presence
    # never becomes a difference between two candidates' sha.
    (root / STATE_DIRNAME).mkdir(exist_ok=True)
    _git(root, "config", "user.email", "harnessgrad@local")
    _git(root, "config", "user.name", "HarnessGrad")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "H0: base harness")
    # If the repo already had the ignore rule before this commit, git would have
    # tracked the staged files; drop them from the index so the rule takes hold.
    _git(root, "rm", "-r", "--cached", "-q", "--ignore-unmatch", "_harnessgrad")


def commit_state(repo: Path, message: str) -> str:
    """Commit whatever moved, and return the harness's **content address**.

    A harness is code, so its identity has to be the identity of code: equal content is
    the same harness. This used to return `HEAD` -- the commit sha -- and that is not a
    content address, because a commit also carries an author timestamp and a parent. It
    was wrong in a way that was invisible from the curve, and it cost real measurement.

    Measured on `loop-terminal_bench-26452c` (6 rounds x 5 tasks): rounds 2-5 reported
    four different `identity.harness_sha` values (`fd5a0e59`, `ff7beb56`, `6284989c`,
    `807eeac7`) for **one** tree. `git rev-parse <sha>^{tree}` is byte-identical
    (`88046acf`) for all of them, and every `diffs/round-N.patch` after round 1 is empty.
    So the curve reported four movements and the platform re-measured four times a
    harness that had not changed -- the cache key is `(harness_sha, task_id)`, so it
    could never hit. Nothing about the *behaviour* was wrong (the swap really does
    rebuild the tree from the candidate); the *name* of the state was.

    The tree hash is the right answer rather than a hash of the files we choose to count:
    git already computes it over the working tree's content, with `.gitignore` applied by
    construction, so the harness's own scratch (`.state/`) cannot enter it, and the
    object it names is in the object store -- `materialize` can `git archive` it, which
    it cannot do with a hash we computed ourselves. Two rounds whose trees match now
    genuinely report the same identity, and the cache starts working.
    """
    _git(repo, "add", "-A")
    dirty = _git(repo, "status", "--porcelain").strip()
    if dirty:
        _git(repo, "commit", "-q", "-m", message)
    return content_sha(repo)


def content_sha(repo: Path) -> str:
    """The tree hash of the current HEAD: equal content, equal name.

    A clean tree reuses the parent's tree, so this is stable for an unchanged harness
    without any extra bookkeeping -- the property `commit_state`'s docstring used to
    claim for the commit sha and did not have.
    """
    return _git(repo, "rev-parse", "HEAD^{tree}").strip()


def manifest(repo: Path) -> dict:
    return json.loads((repo / "harness.json").read_text())


def stage_workspace(harness_src: Path, work: Path) -> None:
    """Copy a harness repo into an isolated workspace and initialise its git."""
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(harness_src, work)
    init_repo(work)


def reset_hard(repo: Path, sha: str) -> None:
    """Put the working tree **and the index** back to the content named by `sha`.

    Needed because a method-driven run advances HEAD while it works. Without this,
    naming an earlier state is not enough to measure it: the evaluator reads the working
    tree, not the commit.

    `sha` is what `commit_state` returns -- a **tree** hash -- and a tree is not a commit,
    so both of the obvious spellings fail on it, each in its own way (measured):

        git checkout --force <tree>     error: pathspec ... did not match any file(s)
        git reset --hard <tree>         fatal: ... is a tree, not a commit

    `read-tree` writes the tree into the index, `checkout-index -af` writes the index to
    the worktree, and `clean -fd` removes what the tree does not have. The three together
    are what `reset --hard` would have done, and they are idempotent.

    HEAD is deliberately left where it was when `sha` is a tree: a tree cannot be a HEAD,
    and nothing downstream needs one. Everything that reads a state names it explicitly
    (`materialize`, `diff`, `_touched_paths`), and `commit_state` re-reads the worktree
    through `git add -A`. The one visible symptom is that `git status` reports the
    difference between the stale HEAD and the restored index; that is a property of this
    function's contract, not a leak, and it is written here because the next reader will
    see it.

    A **commit** is accepted too, and reset the ordinary way. `commit_state` stopped
    returning commits, but a caller holding one from an older record (or from `HEAD`
    directly, as `test_materialize_reads_dangling_commits` does) must not break: the
    distinction is one `git cat-file -t` away, and guessing wrong is a hard failure in
    the middle of a paid run.
    """
    kind = _git(repo, "cat-file", "-t", sha).strip()
    if kind == "commit":
        _git(repo, "reset", "-q", "--hard", sha)
        _git(repo, "clean", "-qfd")
        return
    _git(repo, "read-tree", sha)
    _git(repo, "checkout-index", "-af")
    _git(repo, "clean", "-qfd")


def base_commit(repo: Path) -> str:
    """The first commit, i.e. the state every trajectory starts from."""
    return _git(repo, "rev-list", "--max-parents=0", "HEAD").strip().splitlines()[-1]


def materialize(repo: Path, sha: str, dest: Path) -> int:
    """Write the complete tree at `sha` into `dest`. Returns the file count.

    Why this exists: a method that can only be handed the *current* harness cannot
    implement any rule whose contribution is *which previous state to build on*.
    That is not a small family -- SICA picks the newest iteration clearing a
    confidence bound, DGM samples a parent from an archive of every variant,
    HyperAgents penalises parents by child count, RRSI carries a frontier. With
    only the incumbent on disk, every one of those rules reduces to "hill-climb",
    and the method looks identical to the plain loop while claiming to be itself.

    `git archive` rather than a checkout: it reads the object store directly, so it
    works on the dangling commits mode A leaves behind after `reset_hard`, and it
    cannot disturb the working tree it is reading from. `.gitignore`d paths
    (`_harnessgrad/`, `.state/`) are excluded by construction, so a staged state is
    the harness alone.
    """
    import io
    import tarfile

    blob = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        check=True, capture_output=True,
    ).stdout
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        # `filter="data"` refuses absolute paths, `..` and special files. These
        # archives are our own commits, but a harness is arbitrary code and the
        # extraction happens with the platform on disk.
        try:
            tar.extractall(dest, filter="data")
        except TypeError:                                       # Python < 3.12
            tar.extractall(dest)
    return sum(1 for p in dest.rglob("*") if p.is_file())
