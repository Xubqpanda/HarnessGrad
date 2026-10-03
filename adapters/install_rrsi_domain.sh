#!/usr/bin/env bash
# Install the HarnessGrad domain into an RRSI checkout, and put a harness in it.
#
# The adapter's source lives here, with HarnessGrad, because it is HarnessGrad's half
# of the integration -- it is the thing that has to change when the platform's
# measurement API changes. RRSI's own `domains/` directory belongs to RRSI, so this
# copies rather than editing in place, and re-running it is how you update.
#
#     ./adapters/install_rrsi_domain.sh [--rrsi DIR] [--harness NAME] [--check]
#
# What it does, in order:
#   1. copies the domain (adapter, config, briefs, and the two prompt documents)
#   2. puts a copy of a base harness where the domain expects to evolve one
#   3. prints how to run it
#
# It does not touch RRSI's Python, its config, or any of its own domains.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/adapters/harnessgrad_domain"

RRSI="${RRSI_ROOT:-/mnt/20t/xubuqiang/Study/rrsi-run}"
HARNESS="loop"
CHECK_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --rrsi) RRSI="$2"; shift 2 ;;
        --harness) HARNESS="$2"; shift 2 ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

fail() { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$1"; }
info() { printf '  %s\n' "$1"; }

[ -d "$SRC" ] || fail "no domain source at $SRC"
[ -d "$RRSI" ] || fail "no RRSI checkout at $RRSI (pass --rrsi DIR)"
[ -d "$RRSI/rrsi" ] || fail "$RRSI does not look like an RRSI checkout (no rrsi/)"

# The domain must be importable by RRSI's own loader, which execs the file directly.
[ -f "$RRSI/rrsi/domain.py" ] || fail "$RRSI/rrsi/domain.py is missing"

DOMAIN_NAME="harnessgrad"
DEST="$RRSI/domains/$DOMAIN_NAME"
if [ "$CHECK_ONLY" = 1 ]; then
    info "would copy $SRC -> $DEST"
    info "would place base_harness/$HARNESS at $DEST/harness"
    exit 0
fi

mkdir -p "$DEST"
for f in adapter.py briefs.py rrsi.json SKILL.md PATTERNS.md; do
    [ -f "$SRC/$f" ] || fail "domain source is missing $f"
    cp "$SRC/$f" "$DEST/$f"
done
ok "domain installed at $DEST"

# The harness the search starts from.
#
# Vendored *inside RRSI's repository*, and this is forced rather than chosen: RRSI
# evaluates every candidate in a `git worktree` of its own repo
# (`runs/<domain>/wt/incumbent/`, see rrsi/gitops.py), and `Domain.harness_dir()`
# resolves `domains/<name>/<harness_path>` **relative to that worktree**. So a harness
# that is not tracked by RRSI's git is simply absent from every worktree, and the
# adapter sees a directory with no `harness.json`. RRSI's own coding domain does the
# same thing (`harness_path = "../../third_party/harbor_terminus2"`).
#
# Measured: an untracked harness produced
#   "the harness RRSI is evolving has no harness.json:
#    runs/harnessgrad/wt/incumbent/domains/harnessgrad/harness"
# which reads like an adapter bug and is a git-tracking requirement.
HSRC="$ROOT/base_harness/$HARNESS"
[ -d "$HSRC" ] || fail "no base harness named $HARNESS at $HSRC"
rm -rf "$DEST/harness"
mkdir -p "$DEST/harness"
# Copy the contents, not the directory: `harness_path = "harness"` means the domain
# directory must contain a directory called `harness`.
cp -r "$HSRC"/. "$DEST/harness/"
rm -rf "$DEST/harness/__pycache__" "$DEST/harness/.git"
# A nested .git would make RRSI's worktree treat it as a submodule with no entry.
ok "base harness '$HARNESS' placed at $DEST/harness"

# Tracked, or the worktrees cannot see it. `git add` only -- committing is the
# user's call, because it writes to a repository this script does not own.
if git -C "$RRSI" rev-parse --git-dir >/dev/null 2>&1; then
    if git -C "$RRSI" ls-files --error-unmatch "domains/$DOMAIN_NAME/harness/harness.json" \
            >/dev/null 2>&1; then
        ok "already tracked by RRSI's git"
        if ! git -C "$RRSI" diff --quiet -- "domains/$DOMAIN_NAME"; then
            printf '  ! the tracked harness differs from base_harness/%s\n' "$HARNESS"
            printf '    commit it if that is intended: (cd %s && git add domains/%s && git commit)\n' \
                   "$RRSI" "$DOMAIN_NAME"
        fi
    else
        git -C "$RRSI" add "domains/$DOMAIN_NAME" 2>/dev/null
        printf '  ! staged but NOT committed. RRSI evaluates candidates in git\n'
        printf '    worktrees of its own repo, so the harness must be tracked or every\n'
        printf '    worktree will be missing it. Run:\n\n'
        printf '        cd %s && git commit -m "harnessgrad domain: %s base harness"\n\n' \
               "$RRSI" "$HARNESS"
    fi
else
    printf '  ! %s is not a git repository; RRSI needs one to build worktrees.\n' "$RRSI"
fi

# RRSI's search roles call their own model. Report whether that is configured rather
# than letting the first paid round discover it.
step() { printf '\n%s\n' "$1"; }
step "RRSI's own credentials (the analyst / proposer / critic models)"
if [ -n "${ANTHROPIC_API_KEY:-}${GOOGLE_APPLICATION_CREDENTIALS:-}${VERTEX_PROJECT:-}" ]; then
    ok "something looks configured for RRSI's roles"
else
    printf '  ! no ANTHROPIC_API_KEY / VERTEX_PROJECT in this shell.\n'
    printf '    RRSI calls Claude for its four search roles; the domain does not\n'
    printf '    supply that. Set it in the shell you run RRSI from, or override\n'
    printf '    proposer_model/analyst_model/critic_model in %s/rrsi.json.\n' "$DEST"
fi

step "HarnessGrad's credentials (the frozen policy the harness drives)"
if [ -f "$ROOT/.env" ] && grep -q '^HG_AGENT_API_KEY=..' "$ROOT/.env"; then
    ok "$ROOT/.env has HG_AGENT_* -- the adapter loads it for every run"
else
    printf '  ! %s/.env has no HG_AGENT_API_KEY.\n' "$ROOT"
    printf '    The harness would fall back to its mock backend and every score\n'
    printf '    would be fiction. Run ./install.sh and fill in .env first.\n'
fi

# A stale `evolve/<domain>` branch pins every worktree to an old commit, and
# `rrsi/gitops.py:ensure_branch` only creates the branch when it is absent -- it
# never moves it. Measured failure: after committing the domain, RRSI still loaded a
# worktree at the previous commit, so `domains/harnessgrad/` did not exist inside it
# and the adapter reported a harness with no `harness.json`. The message looks like an
# adapter bug and is a stale git ref.
#
# This matters beyond the first run: the domain's own code (`adapter.py`, `harness/`)
# is checked out from that branch, so an updated adapter is invisible until the branch
# is recreated.
BRANCH="evolve/$DOMAIN_NAME"
if git -C "$RRSI" rev-parse --verify --quiet "$BRANCH" >/dev/null 2>&1; then
    branch_sha="$(git -C "$RRSI" rev-parse --short "$BRANCH")"
    head_sha="$(git -C "$RRSI" rev-parse --short HEAD)"
    if [ "$branch_sha" != "$head_sha" ]; then
        printf '\n  ! %s is at %s but HEAD is %s.\n' "$BRANCH" "$branch_sha" "$head_sha"
        printf '    Every worktree is checked out from that branch, and RRSI never\n'
        printf '    moves it -- so the domain and harness inside them are the old\n'
        printf '    ones. Clear it before the next baseline:\n\n'
        printf '        cd %s && git worktree prune && git branch -D %s && rm -rf runs/%s\n' \
               "$RRSI" "$BRANCH" "$DOMAIN_NAME"
    else
        ok "$BRANCH is at HEAD"
    fi
fi

step "Run it"
cat <<EOF

    cd $RRSI
    python3 rrsi.py --domain harnessgrad baseline     # H0, and the task set
    python3 rrsi.py --domain harnessgrad calibrate    # estimate delta (delta is null)
    python3 rrsi.py --domain harnessgrad step --t 0   # one round

EOF
info "rrsi.json holds T, k, delta and the acceptance weights; edit the copy in"
info "$DEST, not this repo's, if you change them for a run."
