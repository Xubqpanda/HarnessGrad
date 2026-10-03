# What was read, and how to get it again

The methods HarnessGrad adapts are other people's repositories. They are **not
vendored** — a clone of this repo contains code and records, not datasets,
papers or four different licences. This file records what was read so the
adapters' claims can be checked.

Retrieve with `adapters/fetch.sh` (downloads to a sibling `methods_src/`).

| method | repo | branch | code files | digest |
| --- | --- | --- | ---: | --- |
| AHE | `china-qijizhifeng/agentic-harness-engineering` | `main` | 197 | `d9d5d16800a24a0d` |
| AHE (runtime) | — | — | — | needs the `nexau` engine and `LLM_*` env vars; see `adapters/README.md` |
| DGM | `jennyzzt/dgm` | `main` | 40 | `c0e9bd2f3d16d6b5` |
| HarnessX | `darwin-agent/HarnessX` | `main` | 609 | `e64f6e9dd4c62c5f` |
| HarnessX (runtime) | — | — | — | needs the `harnessx` package (`omegaconf` et al.) and a model config |
| HyperAgents | `facebookresearch/Hyperagents` | `main` | 131 | `090f0426b8331551` |
| MAC | `ant-research/meta-agent-challenge` | `main` | 107 | `57d00e2458d5750a` |
| MAC (runtime) | — | — | — | needs `TASK_MODEL_API_KEY` and the artifact's own endpoint |
| Meta-Harness (artifact only) | `stanford-iris-lab/meta-harness-tbench2-artifact` | `main` | 4 | `483f9304774e6615` |
| Meta-Harness (runtime) | — | — | — | needs harbor's sandbox protocol; see `adapters/README.md` |
| SICA | `MaximeRobeyns/self_improving_coding_agent` | **`master`** | 113 | `1e617c7abdba1c4d` |
| TTHE | `junnie00/TTHE` | `main` | 57 | `08a983bf109f4c33` |
| TTHE (runtime) | — | — | — | needs its own `config.yaml`; see `adapters/README.md` |
| RRSI | `google-research/rrsi` | — | — | see below |

**The digest** is `sha256` over the concatenated contents of every `.py`, `.md`,
`.yaml`, `.yml` and `.toml` file in the tree, sorted by path, truncated to 16
hex characters. It is a *content* fingerprint, not a commit id: `codeload` does
not report the resolved sha, and `jsDelivr` returns only the branch name. The
digest answers "is this the same code the adapters were written against?" without
needing network access. Excluded from it are data and asset files, which is why
the file counts here are lower than the raw tarball counts.

## Two sources that are not in the table

**RRSI** is the one method that had to be modified to run on this host at all.
Its patches, dataset pin and run script live in the RISE repository under
`tools/rrsi/` — that is where the disclosure belongs, because it is the only
method whose results here depend on code we changed.

**Meta-Harness** appears only as an artifact: four files
(`agent.py`, `prompt-templates/terminus-kira.txt`, `anthropic_caching.py`,
`pyproject.toml`). The search framework is not released. It is kept in the set
deliberately, as the extreme case of a method that publishes an *output* and not
a *process*.

## What is deliberately absent

`docs/assets/` (a 57 MB paper and image bundle in HarnessX),
`dgm/initial*/predictions` and `logs/` (52 MB of evaluation output), and a 49 MB
CSV corpus in HyperAgents' `paper_review` domain. None is code the adapters read.
Removing them took the set from 189 MB to 25 MB. `fetch.sh` prints the same
pruning commands for anyone who wants the same footprint.
