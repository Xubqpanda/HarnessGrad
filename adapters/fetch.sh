#!/usr/bin/env bash
# Fetch the method sources HarnessGrad adapts. Read-only: this downloads other
# people's repositories to a sibling directory and never edits them.
#
# Why tarballs and not git: from the machine this was written on, `git clone`
# to github.com times out (port 443), while codeload serves fine. The branch
# name must be checked -- SICA's default is `master`, and guessing `main`
# returns a 404 that looks like a missing repository.
#
# Why not vendored: these are four different licences and, with their data
# directories included, hundreds of megabytes. What this repo keeps instead is
# which revision was read (SOURCES.md) and how to get it (this script).
set -euo pipefail

DEST="${DEST:-$(cd "$(dirname "$0")/../.." && pwd)/methods_src}"
mkdir -p "$DEST"

fetch() {  # name repo branch
  local name="$1" repo="$2" branch="${3:-main}"
  if [ -d "$DEST/$name/.fetched" ]; then
    echo "== $name already present"; return 0
  fi
  echo "== $name  $repo@$branch"
  curl -sSL -o "$DEST/$name.tar.gz" \
    "https://codeload.github.com/$repo/tar.gz/refs/heads/$branch"
  mkdir -p "$DEST/$name"
  tar xzf "$DEST/$name.tar.gz" -C "$DEST/$name" --strip-components=1
  rm -f "$DEST/$name.tar.gz"
  touch "$DEST/$name/.fetched"
}

fetch dgm         jennyzzt/dgm                                   main
fetch sica        MaximeRobeyns/self_improving_coding_agent      master
fetch hyperagents facebookresearch/Hyperagents                   main
fetch tthe        junnie00/TTHE                                  main
fetch ahe         china-qijizhifeng/agentic-harness-engineering  main
fetch harnessx    darwin-agent/HarnessX                          main
fetch mac         ant-research/meta-agent-challenge              main
fetch metaharness stanford-iris-lab/meta-harness-tbench2-artifact main

# RRSI is not fetched here: it is the one method that had to be patched to run
# on this host at all, and RISE/tools/rrsi/ carries those patches plus the
# dataset pin. See adapters/SOURCES.md.

cat <<'NOTE'

Sources are now at: $DEST
These directories contain data and paper assets that this project does not use.
To strip them (189 MB -> 25 MB):

  rm -rf "$DEST"/*/docs/assets "$DEST"/dgm/initial*/predictions \
         "$DEST"/dgm/initial*/logs "$DEST"/dgm/misc
  find "$DEST" -type f -size +1M -delete
  find "$DEST" -name '*.pdf' -delete
NOTE
