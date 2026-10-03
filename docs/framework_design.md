# Framework design, derived from four adapters

> **Status note (added later).** The *analysis* below still holds — the four
> method shapes really do disagree about where the improving/improved boundary
> sits — but its conclusion about the route does not. Replay was dropped: a
> replayed state is another method's harness driven by that method's runner, so
> its score can never be the platform's own and can never share an axis with
> anything else. The platform now ports **decision rules** instead
> (`methods/<name>/run.py`), and `adapters/` is kept as a historical record.
> Section 4's "replay vs live" distinction is still the reason `curve_point` must
> record `measured_by_platform`, which is the one part that outlived the route.

Everything below is a conclusion from `docs/method_shapes.md` plus the four
working adapters, not from first principles. Where a design choice follows from
something a method actually does, the evidence is named.

---

## 1. What the four methods agreed on

Four independent implementations — RRSI (loop-owning), DGM (archive), SICA
(self-modifying), HyperAgents (named boundary) — agreed on exactly four things.
They disagreed about almost everything else.

**Agreed:**

| # | Agreement | Evidence across all four |
| --- | --- | --- |
| A1 | a **state** exists that can be scored | RRSI commit, DGM run id, SICA `agent_<i>/agent_code/`, HyperAgents `gen_<id>/` |
| A2 | states carry a **parent relation** | RRSI `t`; DGM archive; SICA `select_base_agent`; HyperAgents `parent_genid` |
| A3 | there is an **acceptance rule**, and it is hand-set | RRSI calibrated `delta`; DGM `noise_leeway=0.1`; SICA lower confidence bound; HyperAgents fixed parent selection |
| A4 | the method **draws a boundary** between what it may edit and what it may not | RRSI pinned dir; DGM archive management outside patches; SICA no boundary at all; HyperAgents two named files |

**Disagreed:** whether the trajectory is a sequence or a tree; whether the method
can change its own selection rule; whether cost is reported; what a "round" is.

**Design consequence.** The platform is built on the four agreements and is
agnostic about every disagreement. Concretely: it *requires* A1–A4 to be
expressible, and it requires nothing else.

**The corollary that matters most**: A3 and A4 are agreements about what the
platform must **record**, never about what it must **do**. The platform has no
acceptance rule and no boundary. It has fields for both.

---

## 2. Three objects

The whole framework is three nouns and the relations between them.

```
Harness                 a repository, plus the path inside it that is in scope
   │                    (repo, path_in_repo)          <- confirmed by all four
   │
   │  has states
   ▼
State                   a point in the harness's history: (harness, commit)
   │                    or an addressable snapshot the adapter can commit
   │
   │  measured, producing
   ▼
Run                     states + scores + costs + the method's own claims
                         + the boundary reading
```

And one verb:

```
Method                  something that, given a Harness and a fixed task set,
                        produces States. Reached only through an entrypoint.
```

**Deliberately absent: a "Method" object with typed internals.** A method is a
repo plus an entrypoint. Modelling it more richly would mean the platform has
opinions about methods, which is the thing four disagreements say it cannot have.

---

## 3. What the platform provides

Six capabilities. Each exists because at least one of the four methods is missing
it, and the four are unanimous about none of them.

| # | Capability | Why it exists | Who lacks it today |
| --- | --- | --- | --- |
| C1 | **Fixed task set, paired scoring** | every reported delta is otherwise uninterpretable | all four: no CI, no paired comparison |
| C2 | **Curve points with an explicit x-axis** | "rounds" are not comparable between methods | RRSI T=20, SICA copies, DGM variants |
| C3 | **Selection-effect disclosure** | a max over N on the same set is not the same number | DGM/RRSI record a best-so-far; none label it |
| C4 | **Cost of improving, per method** | $15 to $100K with no common unit | RRSI and TTHE report nothing |
| C5 | **Boundary reading** | "can it change its own search rule?" is unanswerable today | all four; each answers differently |
| C6 | **Replay of runs that already exist** | nobody will re-run a $22K experiment to be measured | — |

**C6 is the one that decides adoption**, and it is the one a platform usually
gets wrong. See §4.

---

## 4. The distinction the framework turns on: replay vs live

The platform must do two things that look similar and are not:

| | **Replay** | **Live** |
| --- | --- | --- |
| input | a run that already happened | a harness and a method that have not run |
| the platform | *reads* artifacts, normalizes them, scores what it can | *drives* the loop, owns the task set, records everything |
| cost | ~zero | the full cost of the run |
| adoption | a method author can adopt it **today**, about yesterday's run | requires them to change how they run |
| risk | the method's own numbers may be missing or self-reported | none — the platform measured it |

**Replay is why this framework can exist before anyone adopts it.** If the only
way to be measured by HarnessGrad were to re-run inside it, no one would — DGM's
authors will not spend another $22,000 so that a third party can plot their curve.

So: **replay is the on-ramp, live is the destination, and a curve point must say
which it is.** A replayed point carries the method's claimed score and the
platform's own score side by side, and the difference between them is itself a
finding.

This is also how the framework gets its first real result without running
anything expensive: four methods, four adapters, one table — and the table is
about boundaries and reporting gaps, which do not require re-running evolution.

---

## 5. What the interface must change, and what it must not

Derived strictly from the adapters. A change is made only when two independent
adapters ask for the same thing.

| Need | Asked for by | Verdict |
| --- | --- | --- |
| `(repo, path_in_repo, commit)` instead of a standalone repo | RRSI (subdirectory) **and** DGM (repo root) | **change made** — already reflected in §1 of `INTERFACE.md` |
| a state materialized from outside the repo | DGM (predicted) | **not made** — disproof in `docs/method_shapes.md` |
| `trajectory_shape` (sequence vs tree) | DGM **and** HyperAgents, both `parent_genid`-style | **change to make**: a run must declare its shape and which curve is drawn |
| claims recorded beside measurements | all four report their own score; none report cost | **change to make**: run records `method_reported` next to measured fields |
| a declared boundary field | SICA (none) vs HyperAgents (named) | **not a declaration** — recorded, not asked. Keep `editable_surface_touched` |
| an acceptance-rule field | all four have one, all four differ | **change to make**: record the rule as a string with its source location |

So the interface gains three fields and loses none:

```
Run.trajectory_shape      "sequence" | "tree"
Run.curve_drawn           which curve, when the shape is a tree
Run.method_reported       the method's own claims, kept beside the measured ones
Run.acceptance_rule       {text, source, calibrated: bool}
```

And it deliberately does **not** gain a `mode` field for "real RSI", because
SICA and HyperAgents answer that question in opposite ways while both being
structurally honest, and only the diff can tell them apart.

---

## 6. What this means for the paper

The demo's claim is not "our method is better". It is:

> **Four published harness-evolution methods can be measured on one axis without
> being re-run, and doing so exposes three things none of them report: whether
> the method can change its own search rule, what the improvement cost, and
> whether the headline number was selected on the set it is reported on.**

That claim is supported by artifacts that already exist, which is why it is
reachable now and why the crowded-paper problem does not apply to it: the four
methods' own authors did not and would not produce this table, because each of
them is measuring only itself.

**The honest limit**, to state in the paper: replaying a method means trusting its
evaluation. The platform's own score is only available for states it can run.
So the boundary column (C5) is fully trustworthy, the cost column (C4) is only as
good as the method's reporting, and the score column is the method's until the
platform runs it live.
