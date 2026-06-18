# PROGRESS — central MIR CFG successor contract (+ scalar-match switch)

Running log. Newest entries at the top. See `README.md` for the design and the
full audit. Baseline commit at creation: `0b060f27`.

---

## Status summary

| Phase | Deliverable | State |
|-------|-------------|-------|
| A1 | Central `successors()` API + unit test | ☐ not started |
| A2 | Migrate all CFG users (rows 1–13) | ☐ not started |
| B3 | `SwitchTerminator` MIR node + `successors()` | ☐ not started |
| B4 | `_lower_scalar_match` → `SwitchTerminator` | ☐ not started |
| B5 | Codegen `switch iN` emission | ☐ not started |
| B6 | Regression + destructible-arm memcheck | ☐ not started |

Primary deliverable = **A1 + A2** (valuable regardless of B). B is the motivating
optimization.

---

## Migration tracker (Part A)

One row per CFG-successor site from the README audit. "Identical IR" = refactor
verified byte-identical to baseline.

| # | Site (file:line @ 0b060f27) | Migrated | Tested | Identical IR | Notes |
|---|------------------------------|----------|--------|--------------|-------|
| 1 | `ownership_ledger.py:399` `_successors` | ☐ | ☐ | ☐ | closest to a central helper today; make it delegate |
| 2 | `string_arc.py:607` `_block_succs` | ☐ | ☐ | ☐ | exact duplicate of #1 |
| 3 | `cleanup_authoring.py:168–175` pred-map | ☐ | ☐ | ☐ | **needs edge labels** (if_then/if_else) |
| 4 | `cleanup_authoring.py:178–184` multi-succ | ☐ | ☐ | ☐ | switch is always multi-successor |
| 5 | `cleanup_authoring.py:760–762` edge-split | ☐ | ☐ | ☐ | only real target write path |
| 6 | `ssa.py:199–202` preds | ☐ | ☐ | ☐ | — |
| 7 | `ssa.py:248–251` succs | ☐ | ☐ | ☐ | — |
| 8 | `ssa.py:306–308` reachability | ☐ | ☐ | ☐ | — |
| 9 | `ssa.py:346–352` preds+succs | ☐ | ☐ | ☐ | — |
| 10 | `ssa.py:566–568` post-order | ☐ | ☐ | ☐ | — |
| 11 | `dom.py:65–68` preds | ☐ | ☐ | ☐ | — |
| 12 | `dom.py:136–142` preds+succs | ☐ | ☐ | ☐ | — |
| 13 | `llvm_codegen.py:7479–7525` emit | ☐ | ☐ | n/a | last consumer; only touched in B5 |

> **Not in this table:** `ownership_ledger.py:673` `_iter_value_uses` — a *value-use*
> scanner whose `then_target`/`else_target`/`target` entries are an **exclusion**
> list (block names, not values), not a successor walker or write path. Separate
> SwitchTerminator concern; tracked under Open questions, not migrated.

---

## Findings

- **Surface is larger than first estimated.** SSA (`stage4/ssa.py`) alone holds 5
  independent successor/predecessor walkers; `dom.py` 2; plus ownership_ledger,
  string_arc, cleanup_authoring. **~12 successor/predecessor read-or-decision sites
  + 1 target-write path (cleanup edge-split)** across 6 files. This is the strongest
  argument for the central contract. (`ownership_ledger.py:673` was initially
  miscounted as a remap write path — it is actually a value-use scanner that
  *excludes* terminator block-name fields; see the migration-table note and Open
  question 5.)
- **Codegen already emits LLVM `switch`** as an internal variant-construction
  helper (`llvm_codegen.py:9160`, `switch i8 %tag, …`). So `switch iN` emission is a
  proven, low-risk pattern — the B-side codegen step is trivial.
- **Locals are memory-based** (match result flows through a `result_local` alloca
  via `StoreLocal`/`LoadLocal`, loaded at the join). Whether `stage4/ssa.py` (phi
  promotion) runs in the default codegen path is an **open question** — if it does,
  the switch's N join-predecessors must be enumerated correctly for phi placement
  (which the contract guarantees). Either way the contract must cover SSA.
- **Edge identity matters.** `cleanup_authoring` distinguishes `if_then` vs
  `if_else` edges for edge-splitting, so the central API needs an edge-labelled
  variant (`successor_edges`), not just a flat successor list, or that pass can't
  migrate cleanly.

## Open questions

1. Does `stage4/ssa.py` / `stage4/dom.py` run in the default `-> LLVM IR` pipeline,
   or only under an optional analysis/opt config? (Determines whether SSA migration
   is correctness-critical for shipped builds or "future-proofing only" — migrate
   regardless, but it changes test emphasis.)
2. Best home for the API: a method on `MTerminator` (subtype-dispatched) vs a free
   function `terminator_successors(term)` in a new `stage2/cfg.py`. Method keeps
   semantics next to the node; free function avoids importing behavior into the
   data model. **Leaning:** method on `MTerminator` + thin `cfg.py` utilities
   (`block_successors`, `compute_predecessors`, `successor_edges`).
3. Should `remap_targets(term, mapping)` (write side) land in the same step as the
   read API, so the only target-write path (cleanup edge-split, `cleanup_authoring.py:760–762`)
   and any future ones share one owner that knows every terminator's target fields?
4. Golden-IR diff harness: do we have one, or stand one up for the
   byte-identical-MIR/IR assertion across the migration?
5. **`_iter_value_uses` SwitchTerminator audit (`ownership_ledger.py:673`).** Not a
   successor walker — a value-use scanner whose `then_target`/`else_target`/`target`
   entries are an exclusion list. When `SwitchTerminator(scrutinee, cases,
   default_target)` is added (step B3), confirm: (a) `scrutinee` IS scanned as a
   value use; (b) `default_target` and the block-name elements inside `cases` are
   excluded (the current loop only excludes named scalar fields and would treat a
   `list` of block-name strings as value uses — `cases` is a list of `(int, str)`
   tuples, so verify the tuple shape isn't mis-scanned). Out of scope for the
   successor contract itself.

## Test status

- Not started. No code changes yet.
- Baseline regression (pre-work, for reference): scalar-match v1 + bool + variant
  match all green — `stage2+checker` 332/419 passed across recent runs; 13
  `scalar_match_*` e2e + contract test pass; memcheck clean. (See
  `project_scalar_match_support.md`.)

## Review outcomes

- _(none yet)_

---

## Log

### 2026-06-18 — review correction
- Reviewer flagged `ownership_ledger.py:673` as miscategorized: it is
  `_iter_value_uses` (a value-use scanner that *excludes* terminator block-name
  fields), **not** a block-name remap / write path. Removed it from the migration
  table (rows renumbered 14→13), corrected the design-sketch "Mutation is separate"
  bullet (the only target-write path is the cleanup edge-split, `cleanup_authoring.py:760–762`),
  and re-filed it as Open question 5 (a SwitchTerminator value-use audit, not part
  of the successor contract).

### 2026-06-18 — work area created
- Created `work/scalar-match-jump-table/` with README + PROGRESS.
- Completed the CFG-successor audit (now: ~12 successor read/decision sites + 1
  target-write path across 6 files); recorded in the migration tracker above.
- Reframed per request: **central MIR CFG successor contract is the primary
  deliverable**; scalar-match LLVM `switch` is the motivating follow-up optimization
  that becomes low-risk once the contract lands.
- Confirmed no existing central successor helper; `ownership_ledger._successors`
  is the closest (module-private, used by 2 call sites).
- No source changes made — investigation/planning only. Baseline `0b060f27`,
  ABI 17, scalar-match equality chain remains the certified baseline.
