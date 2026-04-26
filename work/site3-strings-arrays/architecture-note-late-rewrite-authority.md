# Architecture note — late-rewrite passes and ledger authority

Triggered by: site 3 strings/arrays migration (Patch 3, frozen failing state). The diagnosis showed `verdict_at` is too broad as a String skip predicate because `string_arc` rewrites the return path AFTER the ledger is built. Before reverting Patch 3 to D1, K asked whether this is String/Array-specific or a general refcounted-handle problem.

**Answer: String/Array-specific TODAY. Arc<T> is safe today. The rule that explains both is correct and worth pinning.**

## 1. Where Arc<T> retain/release effects live

| Operation | MIR shape | Visible to ledger? |
|---|---|---|
| `arc(value)` (factory) | normal `Call(_arc_clone_impl_alloc_path, ...)` + `ConstructStruct + MoveOut(buf) + Return` | Yes |
| `arc.clone()` (refcount inc) | `Call(_arc_clone_impl, [&arc])` returning a new `Arc<T>` | Yes |
| `move arc` (transfer) | `MoveOut(t, arc, ty=Arc<T>)` | Yes (whole-local `_apply` transitions to MOVED_OUT) |
| `DropValue(arc, ty=Arc<T>)` (scope-exit destroy) | `MoveOut + DropValue` chain authored by site 1 / site 2 / etc.; codegen dispatches to `_arc_destroy_impl` via `destructor_fns` | Yes |

There is **no late-rewrite pass that synthesises Arc retain/release** the way `string_arc` synthesises `StringRetain`/`StringRelease`. Every refcount-affecting operation on Arc is either a user-visible Call (clone, destroy intrinsic) or a standard MIR ownership primitive (MoveOut, DropValue) emitted at HIR→MIR time and visible to the ledger.

Confirmed by code search: no `arc_arc.py` analogue, no late Arc-rewrite hook, no late insertion sites for Arc clone/destroy under `lang/driftc/stage2/`.

## 2. Does Arc<T> return-source cleanup depend on generic ledger authority?

Yes — and it works correctly.

A function `fn produce() -> Arc<T>` returning `move arc` lowers to `MoveOut(t, arc) + Return(t)`. The lattice's whole-local `_apply` transitions `arc → MOVED_OUT` at the MoveOut. At the Return cursor, `verdict_at(arc)` returns `MUST_NOT_DROP`. Site 1 / site 3's destructible consultation correctly skips the function-exit drop. Caller takes ownership.

Arc is in `destructible_locals` (it's a struct with a user `core.Destructible` impl); the destructibles consultation already covers it (since 0.31.9 sub-step 1). No issue.

`return arc.clone()` is `Call(t, _arc_clone_impl, [&arc]) + Return(t)`. The Call returns a new Arc; `arc` itself is unchanged. Function-exit drops `arc` (correct: it still owns its +1; the clone gave the caller a fresh +1).

## 3. Why Arc differs from String

String's late-rewrite pass (`string_arc.py`) synthesises retain/release calls **after** the ledger is built. Specifically:

- A user-written `return s;` where `s: String` lowers to `LoadLocal(t, s) + Return(t)` (because String IS Copy — no `move` required). The pre-ledger MIR has no retain.
- `string_arc` then rewrites the return-value handler: it inserts `StringRetain` so the caller gets a new +1 stake, and the function still owns `s`'s original +1 (which must be released at function exit).
- The ledger, built BEFORE `string_arc`, sees the pre-rewrite `LoadLocal+Return` chain and correctly classifies `s` as MOVED_OUT (Return-as-move).
- That MOVED_OUT verdict is operationally **wrong as a skip predicate** for String, because `string_arc`'s subsequent retain insertion means the function STILL owns its +1.

Arc has no such late rewrite. Its refcount-affecting operations are all MIR-visible at ledger-build time. The lattice's view matches the operational reality.

## 4. The architectural rule, tested

K's proposed rule:

> **Ledger authority is valid only for ownership effects visible in the MIR snapshot used to build the ledger. Any late pass that creates/releases refcount stakes remains its own authority unless we rebuild/extend the ledger after that pass or move those effects earlier.**

**Test:** the rule predicts that any future late-rewrite pass introducing refcount-stake mutations (retain/release/equivalent) for a type T will reproduce the String-like trap. Conversely, any type whose refcount operations are MIR-first remains safe under generic ledger consultation.

**Evidence supporting the rule:**
- String: late-rewrite (string_arc) → trap, requires alias-walk on post-rewrite MIR (D1).
- Array<…>: same `string_arc` pass handles the parallel `_drop_all_arrays` pattern; same trap risk for arrays through the `array_locals` branch (deferred from Patch 3 by your direction; the rule predicts the same reasoning will apply when arrays are revisited).
- Arc<T>: MIR-first (no late rewrite) → safe under destructibles consultation today.
- Destructibles: MIR-first → safe (already migrated, sub-step 1, 0.31.9).

The rule holds across all four cases.

**Corollary 1.** The site-3 strings/arrays migration as originally framed ("strings/arrays should move to ledger authority via verdict_at") is structurally wrong. The lattice models pre-rewrite ownership; `string_arc`'s post-rewrite shape is what site 3's cleanup decisions need to consult.

**Corollary 2.** Future Share / refcounted-handle work that introduces String-like late retain/release rewrites must EITHER (a) make the rewrite MIR-first so the lattice sees it, OR (b) own its own cleanup decisions at the late-rewrite layer (not delegate to ledger consultation).

## 5. Near-term model alternatives — assessed

### (a) Rebuild the ledger after `string_arc`

**Verdict: not worth it for this branch.** A post-`string_arc` rebuild would let a later consultation pass make cleanup decisions on the rewritten MIR. But cleanup decisions for sites 1/2/3 are MADE INSIDE `string_arc`'s own pass (or before it). Rebuilding after wouldn't help unless we also restructured WHO makes those decisions.

A future restructure could move site-3 cleanup-decision logic into a post-`string_arc` pass that consults the rebuilt ledger. That's a substantial refactor; it doesn't unblock anything we need today.

### (b) Move String/Array retain/release effects earlier into MIR

**Verdict: cleanest long-term, out of scope for this branch.** Express `StringRetain` / `StringRelease` (and the array equivalents) as first-class MIR ops emitted at HIR→MIR time. The lattice would model them. The "string_arc" pass would just lower them to LLVM intrinsics; it would not synthesise new refcount stakes.

This is the most architecturally honest fix — it eliminates the late-rewrite category entirely for refcounted scalars/arrays. Cost: substantial HIR→MIR refactor + needs ledger info at HIR→MIR time (currently the ledger is built post-HIR→MIR). Or always-emit retain/release and let a late peephole optimise; that's wasteful before optimization.

Worth considering as a future track. Not this branch.

### (c) Post-rewrite refcount-stake model

**Verdict: equivalent to (a) plus a new lattice axis.** Build a separate stake-tracking lattice over the post-`string_arc` MIR, used only by site 3. Adds a parallel authority. Same restructuring cost as (a) without (a)'s benefit of a single-source ledger.

### (d) Declare `string_arc` the authority for refcounted-builtin return-source cleanup

**Verdict: this is D1 made explicit.** Document that the alias-walk in `string_arc.py:1486-1491` is the canonical authority for String/Array return-source cleanup decisions, NOT a legacy artefact awaiting ledger migration. Site 3's destructibles consultation remains ledger-authoritative; the alias-walk's String/Array branch is ledger-PARALLEL-authoritative by design. The new Patch 1 carrier becomes the regression gate.

This is **factually accurate** about the current architecture, **honest** about why the alias-walk exists, and **safe** as the closure for this branch.

## 6. Recommendation

**Adopt D1 (close as documented split authority), with the architectural rule pinned.**

Concrete steps (ordered):

1. Revert Patch 3's String addition: restore `for _local in destructible_locals:` and the `if prev.local in string_locals or prev.local in array_locals:` alias-walk skip.
2. Restore version to 0.31.13 (or roll Patch 1's carriers under a 0.31.14 if you want a marker for "site 3 strings/arrays gated by carrier"; my recommendation is to roll back to 0.31.13 — no compiler behavior change actually landed for this track).
3. Remove the DIAG instrumentation from string_arc.py.
4. Keep the Patch 1 carriers (`test_site3_return_source_alias_walk.py`) — they're now permanent gates on the alias-walk's String branch.
5. Update `work/site3-strings-arrays/README.md` to reflect the corrected understanding: the alias-walk is the canonical authority for refcounted-builtin return-source cleanup. The future migration question is no longer "swap to ledger" but "should this be moved earlier into MIR (option b) or left as documented split authority?"
6. Add a memory entry for the architectural rule (`feedback_late_rewrite_authority.md`) so future Share / refcounted-handle work doesn't reproduce the trap.

Branch closes as "site 3 destructibles + variant zero-tag widening + destructor self migrated; strings/arrays explicitly retained as `string_arc`-authority by architectural rule."

ABI no bump. Compiler version: my recommendation is roll back to 0.31.13 since no behavior change ships. Alternative: keep at 0.31.14 to mark the new carriers + documentation tightening; trivial choice.

## Open questions for K

1. **Roll back to 0.31.13 or keep 0.31.14?** No behavior change ships either way; question is whether the carrier addition + architectural rule documentation merits a version marker.
2. **Memory entry for the architectural rule?** I recommend yes (`feedback_late_rewrite_authority.md`) so the rule is visible to future work.
3. **Should the kickoff README be updated to reflect "this is closure, not next-branch kickoff"?** Or kept as a future-work pointer for option (b) — moving String/Array effects earlier into MIR? My recommendation is the latter: keep it as a real follow-up brief but with the corrected model.
