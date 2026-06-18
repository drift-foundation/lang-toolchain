# Central MIR CFG successor contract

**Status:** planning — not started.
**Baseline:** the current scalar-match *linear equality-chain* lowering is correct
and is the **certified baseline**. Nothing in this work area changes observable
behavior of shipped code until each step is individually reviewed and tested.
**Boundary:** compiler-internal only. **ABI stays 17** unless the investigation
turns up a boundary-shape change (none expected). `DRIFTC_VERSION` bumps only when
an observable perf-lowering change ships.
**Baseline commit:** `0b060f27`.

---

## Why this exists (the core feature)

Today **many MIR passes independently know how to walk CFG successors/predecessors**
from a block terminator. Each one hand-rolls the same `isinstance(term, Goto) →
[target]; isinstance(term, IfTerminator) → [then, else]` dispatch. The audit below
counts **10+ such sites across 6 files.**

That duplication is a recurring **silent-failure mode**: when a terminator shape
changes — or a new terminator is added — *one missed walker* makes a pass believe
reachable code is dead, or makes cleanup/liveness/phi-placement incomplete. The
failure is silent (a missed case returns "no successors", not an error), so it
surfaces later as a leak, a use-after-free, or a miscompile.

This codebase has a **documented history of exactly this class of bug** — missed
walker / reconstructor cases that produced serious memory defects (e.g. the
match-arm alpha-renamer missing `HTry`/`HBlock`; the hidden-lambda capture-id
colliding with `HMatchArm.binder_ids`; the VT capture implicit-move zero-back;
and, in this very session, the scalar-literal arm being misread as `default`
because six separate `arm.ctor is None` checks each had to be fixed). Reducing the
*structural* version of that class — CFG successor enumeration — is valuable on its
own, independent of any optimization that follows.

### Primary goal

> Introduce **one authoritative MIR terminator successor API** and migrate every
> CFG user to it.

After that, adding a new terminator means implementing successors in **one place**;
every dataflow/cleanup/SSA/dominance pass inherits it correctly and is covered by
one central test.

---

## Design sketch (Part A — the contract)

A single source of truth for "given a terminator, what blocks does it branch to":

```python
# stage2/mir_nodes.py  (or a small stage2/cfg.py utility)
class MTerminator:
    def successors(self) -> list[str]:
        """Block names this terminator may branch to, in a stable order.
        Goto → [target]; IfTerminator → [then_target, else_target];
        Return/Unreachable → []. Each terminator subtype overrides/extends."""
```

Plus thin shared CFG utilities built on it, so passes stop re-deriving them:

```python
def block_successors(block) -> list[str]: ...        # block.terminator.successors()
def compute_predecessors(func) -> dict[str, list[str]]: ...
def successor_edges(block) -> list[tuple[str, str]]:  # for edge-labelled passes
```

Notes / requirements the contract must satisfy (these are the things the current
hand-rolled walkers encode and a central API must preserve):

- **Stable, deterministic order** — several passes iterate successors and must not
  reorder across runs.
- **Edge identity** — `cleanup_authoring` distinguishes the `if_then` vs `if_else`
  edge (for edge-splitting). The API (or a sibling `successor_edges`) must expose
  per-edge labels, not just a flat target set, or those passes can't migrate.
- **Mutation is separate** — the read API does not replace the one terminator-target
  *write* path: the edge-split rewrite in `cleanup_authoring.py:760–762`
  (`term.then_target = …`). (There is no separate block-name remap path.) When
  `SwitchTerminator` lands, a companion `remap_targets(term, mapping)` is worth
  adding so any future target-rewriting has one owner that knows every terminator's
  target fields.
- **No behavior change** — migration is refactor-only; every pass must produce
  byte-identical MIR/IR before and after.

---

## Audit — every CFG-successor / terminator-dispatch site to migrate

All `file:line` are at baseline `0b060f27`. **Correctness** = a missed terminator
case here causes a silent miscompile/leak/UAF, not just a cosmetic issue.

| # | Site | File:line | Role | Criticality |
|---|------|-----------|------|-------------|
| 1 | `_successors(term)` | `stage2/ownership_ledger.py:399` (used 360, 394) | drop/liveness dataflow successors | **correctness** |
| 2 | `_block_succs(term)` | `stage2/string_arc.py:607` | Arc-ownership CFG traversal | **correctness** |
| 3 | pred-map build + edge labels | `stage2/cleanup_authoring.py:168–175` | drop authoring predecessor map (`if_then`/`if_else`) | **correctness** |
| 4 | `_is_multi_successor` | `stage2/cleanup_authoring.py:178–184` | edge-split decision | **correctness** |
| 5 | edge-split target rewrite | `stage2/cleanup_authoring.py:760–762` | mutates `then/else_target` (only real target write path) | **correctness (write path)** |
| 6 | SSA preds | `stage4/ssa.py:199–202` | phi placement predecessors | **correctness** |
| 7 | SSA succs | `stage4/ssa.py:248–251` | phi/def-use successors | **correctness** |
| 8 | SSA reachability targets | `stage4/ssa.py:306–308` | reachable-block pruning | **correctness** |
| 9 | SSA preds+succs (2nd) | `stage4/ssa.py:346–352` | additional CFG pass | **correctness** |
| 10 | SSA post-order targets | `stage4/ssa.py:566–568` | block ordering DFS | **correctness** |
| 11 | dom preds | `stage4/dom.py:65–68` | dominator predecessors | **correctness** |
| 12 | dom preds+succs | `stage4/dom.py:136–142` | dominator/CFG build | **correctness** |
| 13 | iface-init CFG validator | `lang/driftc/mir_validate.py:851–860` (`validate_mir_iface_init_invariants`) | builds succ/pred for MIR dataflow validation | **correctness** |
| 14 | terminator emission | `lang/codegen/llvm/llvm_codegen.py:7479–7525` | `Goto→br`, `If→br i1`, `Return`, `Unreachable` | trivial (last consumer) |
| 15 | MIR structural invariants | `stage2/hir_to_mir.py` "missing terminator" asserts | block-terminator presence | low |

> Row 13 (`mir_validate.py`) was **found in review after the first audit** — it
> lives outside `stage2/`/`stage4/` so the original `grep` scope missed it. A real
> CFG dataflow validator: if `SwitchTerminator` landed with this site unmigrated it
> would treat switch blocks as having no outgoing edges. Lesson: the audit grep must
> span all of `lang/driftc` + `lang/codegen`, not just the MIR stage dirs.

> Existing terminators (`stage2/mir_nodes.py`): `Goto` (1475), `IfTerminator`
> (1481), `Return` (1489), `Unreachable` (1495). No multi-target/switch terminator
> exists yet. The **only** terminator-target *write* path is the cleanup
> edge-split (row 5); there is no separate block-name remap path.

Whether SSA/dom (stage4) actually run in the default codegen pipeline or only under
specific configs is an **open question** (see PROGRESS.md) — but they must be
migrated regardless so the contract is complete.

### Adjacent — NOT a successor walker (do not migrate to the API)

- **`_iter_value_uses()` — `stage2/ownership_ledger.py:673`.** This is a
  *value-use* scanner for the Return-as-move external-use check; its field list
  (`then_target`, `else_target`, `target`, …) is an **exclusion** list — those
  fields are block names, not SSA value-ids, so they are skipped. It does **not**
  enumerate successors and is **not** a write/remap path, so it is out of scope for
  the successor contract. It *does* need a separate audit when `SwitchTerminator`
  lands: ensure the value-bearing `scrutinee` IS scanned as a use, and the
  block-name fields (`default_target`, the block names inside `cases`) are excluded.
  Tracked as an open item in PROGRESS.md, not as a migration row.

---

## Part B (motivating follow-up optimization) — scalar-match LLVM `switch`

This is **why** the contract matters in the near term, but it is *not* the reason
the contract exists. It ships only after Part A lands.

**Motivation.** Drift has no source-level `switch`; integer scalar `match` is the
canonical switch-like construct. v1 lowers it as a **linear equality chain**
(`if n == a else if n == b … else default`, one `IfTerminator` per arm —
`stage2/hir_to_mir.py` `_lower_scalar_match`). LLVM has a native `switch`
terminator and its backend chooses **jump table, bit-test, compare-tree, or linear
compares** based on case density and target. Emitting one `switch` lets LLVM pick.

**Expected benefit.** Material for **larger scalar matches (~8–16+ arms), and
especially 20+ dense/clustered cases**, where a jump table or bit-test beats a
linear compare cascade. For small matches (≤3–4 arms) LLVM lowers a switch back to
compares, so the benefit there is ~zero — this is an optimization for the big cases,
not a v1 correctness change.

**Shape.** A new `SwitchTerminator(scrutinee, cases: list[(int, str)],
default_target)`; `_lower_scalar_match` emits it instead of the `IfTerminator`
chain; codegen emits `switch iN %scrut, label %default [ iN C0, label %B0 … ]`.
Width comes from the scrutinee type (`i8` Byte, `i32` Int32/Uint32, `i64`
Int/Uint/Uint64); equality is bit-pattern so **signedness is irrelevant, width is
mandatory**. No dense/sparse heuristic in Drift — emit the `switch`, let LLVM
decide.

**Risk, post-contract.** Low. Once Part A exists, `SwitchTerminator` implements
`successors()` once and all 13 dataflow sites inherit it. Without Part A, the same
change touches every site by hand — the exact silent-failure surface we're removing.

---

## Implementation sequence

1. **Add the central successor helper** (`MTerminator.successors()` + CFG utilities)
   and a unit test covering every existing terminator (`Goto`, `IfTerminator`,
   `Return`, `Unreachable`) and the edge-label variant.
2. **Migrate all CFG users** (audit rows 1–13) to the helper, one pass per commit,
   each asserting byte-identical MIR/IR vs. baseline. Keep the write paths
   (edge-split, remap) centralized too.
3. **Add `SwitchTerminator(scrutinee, cases, default_target)`** to `mir_nodes.py`
   with its `successors()` implementation; extend the central test.
4. **Lower scalar integer matches** to `SwitchTerminator` in `_lower_scalar_match`
   (scalar-only; variant and Bool lowering untouched).
5. **Emit `switch iN`** in codegen terminator emission.
6. **Regression + memcheck coverage** — see below.

Steps 1–2 are the deliverable we want regardless. Steps 3–6 are the optimization.

---

## Regression / coverage plan

**Part A (contract):**
- Central unit test: `successors()` for every terminator returns the exact target
  set + order; edge labels correct.
- Per-pass migration: each refactor commit re-runs `lang/tests/stage2`,
  `lang/tests/checker`, and the full `-k match` suite; MIR/IR must be byte-identical
  to baseline (a golden-IR diff harness is worth standing up).

**Part B (switch):**
- One **switch-lowered runtime positive per scalar type** (Int/Uint/Int32/Uint32/
  Uint64/Byte) — reuse the existing 13 `scalar_match_*` e2e, now exercising the
  switch path.
- **High-bit** Uint32 (`0xFFFFFFFF`) and Uint64 (`2^64-1`) dispatch correctly.
- **default fallthrough** taken when no case matches.
- **The decisive test:** a scalar match whose **arm results are destructible**
  (e.g. own a `String`), memcheck-clean — a missed cleanup/successor edge on the
  switch shows up here as a leak/UAF. This is the test that catches an incomplete
  migration; it must exist before step 4 lands.
- Existing **negative checker tests are unchanged** (parser/checker untouched).

---

## Constraints (unchanged contract)

- Parser stores **raw** scalar literal data (`scalar_literal_kind`/`magnitude`).
- Checker validates signedness/range and records the canonical signed
  `scalar_value`.
- Lowering (chain *or* switch) consumes **only** `scalar_value`.
- **ABI 17 unchanged** unless the investigation finds a boundary-shape change.

## Non-goals

- No dense/sparse heuristics in Drift (LLVM owns that decision).
- No change to variant or Bool match lowering.
- No source-level `switch` keyword.
- Not part of the scalar-match v1 release; the equality chain stays the certified
  baseline until Part B is reviewed and shipped on its own.

## References

- Scalar match v1: `lang/driftc/stage2/hir_to_mir.py` `_lower_scalar_match`;
  memory `project_scalar_match_support.md`.
- Missed-walker bug history (motivation): memory `MEMORY.md` "Recently-fixed
  LANGUAGE_BUGs" (match-arm renamer, capture-id collision, VT zero-back).
- Audit table above (baseline `0b060f27`).
