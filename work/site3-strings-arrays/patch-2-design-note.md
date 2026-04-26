# Patch 2 — site 3 strings/arrays return-source migration: design note

Status: **proposal — for K's review BEFORE any compiler change.**

Branch: `feature/site3-strings-arrays-tier1`
Carriers in place (Patch 1): `lang/tests/memcheck/test_site3_return_source_alias_walk.py` (2 String shapes); existing `test_pkg_map_literal_string_leak.py` and `test_scope_drop_conditional_move.py` remain immovable gates.

---

## 1. What exactly replaces the alias-walk cleanup skip

**Proposal: extend the existing ledger consultation loop, NO new lattice axis.**

The lattice ALREADY tracks String/Array source locals. `_identify_return_consumed_loads` (`ownership_ledger.py:543-632`) examines every block ending in `Return` and walks the alias chain from `Return.value` through `AssignSSA` and composite constructors (`ConstructStruct`, `ConstructVariant`, `ConstructResultOk`, `ConstructIfaceValue`) to the source `LoadLocal(_, X)`. For each such `(loadlocal_idx, X)`, the walker transitions `X → MOVED_OUT` at the LoadLocal index.

Crucially — **`tracked = func.params | func.locals`** at `ownership_ledger.py:300` covers ALL locals including Strings and Arrays. The Return-as-move detection is type-agnostic. The data we need is already in the lattice; site 3 just isn't consulting it for strings/arrays.

The site-3 consultation loop today (`string_arc.py:1518-1535`) is:

```python
if _ledger is not None:
    _ledger_point = (block.name, len(block.instructions))
    for _local in destructible_locals:                     # ← only destructibles
        if _local in skip_cleanup_locals: continue
        _local_ty = local_types.get(_local)
        if _local_ty is None: continue
        _needs_drop_axis = bool(_compute_drop_policy(type_table, _local_ty).needs_drop)
        _verdict = _ledger.verdict_at(_ledger_point, _local, needs_drop=_needs_drop_axis)
        if _verdict is _DropVerdict.MUST_NOT_DROP:
            skip_cleanup_locals.add(_local)
```

**The minimal swap:** broaden the iteration to `destructible_locals | string_locals | array_locals`. ~1 line change; the rest of the consultation logic is type-uniform. Then delete the alias-walk's strings/arrays skip at `string_arc.py:1486-1491`.

**Why NOT a separate lattice axis.** A dedicated "is_return_source" axis would be redundant: it would compute exactly what `verdict_at` already returns for the chain-endpoint local at the Return cursor (`MUST_NOT_DROP` ⇔ source is MOVED_OUT). It also adds a new query path the rest of the lattice has to maintain. Keeping the answer in one place (`verdict_at` + the existing Return-as-move walker) is simpler and minimises authority surfaces.

The alias-walk in `string_arc.py:1462-1491` doesn't disappear entirely — `can_move_from_skipped_local` (used downstream for the actual return-value string-ownership transfer at line 1536-1541) still needs the LoadLocal-of-source-local detection. We keep that traversal but stop using its output for `skip_cleanup_locals`. (Per the existing comment at 1457-1460.)

---

## 2. Why this will not reopen prior memcheck failures

The 0.27.145 memcheck regression that triggered the narrowing-to-destructibles fix happened when the previous broader-consultation attempt let `MUST_NOT_DROP` verdicts for strings/arrays flow into `skip_cleanup_locals`. The carriers that broke:

### 2a. `lang/tests/memcheck/test_pkg_map_literal_string_leak.py`

Pattern:
```drift
val _ = logger.info("event", {"port": fmt.format_int(42)});
```
The `format_int(42)` String is consumed by the HashMap insert inside the map literal, then the map is consumed by `logger.info`.

**Hypothesised previous failure mode** (best reconstruction from the source comments, since the prior failing IR isn't archived): the lattice marked the `format_int` result's source local MOVED_OUT (because the value flowed through `Return` of `_emit_throwing` or a composite Construct chain). Site 3 then skipped releasing it. But the legacy `moved_out_locals` set would NOT have flagged it (the value never went through an explicit `MoveOut` instruction at the function-exit block — it was consumed earlier by a Call). Net: the legacy machinery would correctly fire a release for the local's stake; the new machinery would skip.

**Why the new model avoids this:** the lattice's `verdict_at` query is at the Return cursor of the FUNCTION currently being analysed. For `_emit_throwing` (where the String is consumed by HashMap insert), the Return cursor's lattice state for the source local is influenced by whether the lattice models the call as consuming the local. Today, calls do not transition arg locals to MOVED_OUT in `_apply` (`ownership_ledger.py:677-695`); only an explicit `MoveOut(_, local)` does. So if the call doesn't lower into a MoveOut, the lattice keeps the local LIVE → `verdict_at` returns MUST_DROP → site 3 does NOT skip. Same answer as the legacy machinery.

The Return-as-move walker is the only other path to MOVED_OUT for whole-locals, and it ONLY fires when `Return.value` traces to a `LoadLocal(_, X)` chain. For `format_int(42)` consumed by map literal, the value flows into a Call, not the Return chain. The walker doesn't transition.

So: **for the patterns in `test_pkg_map_literal_string_leak`, the lattice and the legacy machinery should agree** (both say MUST_DROP / don't skip). The previous attempt may have failed for a DIFFERENT reason — e.g., it accidentally extended the consultation in a way that aliased the iteration set (passing strings/arrays through a path that double-counted, or interacted with `moved_out_locals` already-applied state).

### 2b. `lang/tests/memcheck/test_scope_drop_conditional_move.py`

Pattern: variant local conditionally moved across match arms; the Phase 4 sub-step 3 variant-zero-tag widening covers the live-on-some-paths case.

**Why the new model is unaffected:** this carrier's failure shape is distinct from the strings/arrays return-source path. The variant local in question is destructible (handled by destructible_locals consultation, already migrated). The fix landed in 0.27.145 was specifically for the variant zero-tag widening (sub-step 3), not for strings/arrays. Broadening strings/arrays consultation does not touch variant zero-tag widening.

### 2c. **Empirical safeguard — Rule 42**

The above analysis is the BEST reconstruction; the prior failing-state IR was not archived. Per K's Rule 42, the implementation patch (Patch 3) MUST gate on:

- `test_pkg_map_literal_string_leak.py` (existing; broke last time)
- `test_scope_drop_conditional_move.py` (existing; broke last time)
- `test_site3_return_source_alias_walk.py` (Patch 1; new gate)
- Full `lang/tests/memcheck/`
- Driver suite
- Observe sweep (bucket 6 = 0)

If any of the first three turn red after the swap, **freeze on the failing state and diagnose**. Do NOT roll back to the legacy alias-walk. The diagnosis output goes back to K for direction.

If diagnosis surfaces a real lattice over-report shape we hadn't anticipated, the fix is at the lattice (tighten Return-as-move's chain detection or add a string/array-specific guard) — NOT at site 3.

---

## 3. String vs Array scope

| Shape | Pinned today | Carrier file | Notes |
|---|---|---|---|
| String direct return (`return s;`) | ✅ Patch 1 | `test_site3_return_source_alias_walk.py::test_site3_direct_string_return_source_no_leak` | Most common factory pattern. |
| String aliased return (`val r = s; return r;`) | ✅ Patch 1 | `..._aliased_string_...` | Forces alias-walk through AssignSSA chain. |
| Array<…> natural return (`return move arr;`) | NOT a carrier | — | Lowers to `MoveOut + Return`; Phase 4 Return-as-move handles it via the whole-local `_apply` MoveOut path; the alias-walk's `array_locals` branch is not exercised by this shape. |
| Array<…> via LoadLocal+Return | NOT pinned | — | If such a path exists in real code (e.g., generic / borrow indirection), it should be discovered before touching the `array_locals` branch. |

**Consequence for Patch 3:** the migration may safely include the `array_locals` branch in the broadened consultation loop (uniform iteration over `destructible_locals | string_locals | array_locals`). The risk is bounded — if the `array_locals` branch is vestigial today, broadening to it has no behavioral impact. If it's load-bearing for some shape we haven't catalogued, the existing observe sweep + driver suite will surface it; freeze and diagnose per Rule 42.

If we want a tighter scope, we can broaden `string_locals` only and leave the alias-walk's `array_locals` branch untouched. K's call. My recommendation: broaden both (uniform consultation is structurally cleaner; the array branch IS load-bearing in the legacy code, deletion of the legacy code requires the ledger to cover it).

---

## 4. Pass-order / ownership interaction

**Pipeline order** (`driftc.py:6885-7010`):

1. `build_ledger` (initial).
2. `match_cleanup_authoring` runs; rebuilds ledger.
3. `cleanup_authoring` runs; rebuilds ledger.
4. `drop_flags` runs.
5. `string_arc.insert_string_arc` runs.

**No additional rebuild needed.** Step 3's rebuild already incorporates any per-field StoreLocal/MoveOut transitions the cleanup_authoring pass introduced. The Return-as-move walker re-runs in that rebuild and produces the up-to-date MOVED_OUT transitions for the function's Return blocks. By the time string_arc reads `_ledger.verdict_at(...)` at the Return cursor, the value is final.

**Interaction with site-3's parallel ownership-tracking machinery:**

- `_release_all_locals(out=, skip_locals=skip_cleanup_locals)` — gated on skip_cleanup_locals. The new model just adds more entries to skip_cleanup_locals (where the lattice says MUST_NOT_DROP). No conflict.
- `_drop_all_arrays(out=, skip_locals=skip_cleanup_locals)` — same.
- `moved_out_locals` — already folded into `skip_cleanup_locals` at line 1434 (`skip_cleanup_locals |= moved_out_locals`). The new ledger consultation is additive: a local can be in BOTH moved_out_locals AND have a MUST_NOT_DROP verdict (consistent), or in only one (consistent with the other source). No double-counting issue because skip_cleanup_locals is a set.
- `owned_values` — tracks SSA values, not locals. Unaffected by the consultation broadening (we only iterate locals).
- `explicitly_dropped_locals` — same as `moved_out_locals`; folded into skip_cleanup_locals; additive with the new consultation.

**Race condition to watch:** the consultation reads `_ledger.verdict_at(...)`, but the lattice's per-instruction state at the Return cursor is computed from the MIR as it was when the ledger was rebuilt (after `cleanup_authoring`). string_arc's own pass mutates the MIR (inserts retains/releases/etc.), but only AFTER the consultation. So the lattice state we read is consistent with the MIR shape we're consulting it about.

If a future change moves string_arc's consultation BEFORE `cleanup_authoring`'s authoring step, the lattice would be stale relative to the post-authoring MIR. This isn't the case today and shouldn't be without a separate ledger rebuild.

---

## 5. Expected observe movement

**Bucket 6 (real_disagreement)** — must remain **0**. The observe gate is the regression-first proof.

**Bucket counts to monitor:**

| Bucket | Expected change | Reason |
|---|---|---|
| `agree` | **+ small** | Each strings/arrays return-source verdict that the new consultation produces emits an `agree` record (site says skip via the new path; ledger says MUST_NOT_DROP). |
| `moved_unconditional` | unchanged or + small | Some strings already enter via this bucket (when explicitly MoveOut'd). |
| `path_dependent` | unchanged | Currently 2 records; not touching that surface. |
| `droppolicy_approximation` | **0** (must stay 0) | Strings/arrays consultation does NOT touch the droppolicy heuristic. |
| `real_disagreement` (bucket 6) | **0** (must stay 0) | The gate. Any non-zero is a freeze condition per Rule 42. |
| `implicit_return_move_gap` | unchanged | This is a different class (HIR `_moved_locals` over-report); not touched. |
| `per_field_still_disagrees` | unchanged | Per-field surface; not touched. |

**Decommissioned observe surface:** the legacy alias-walk emits no observe records itself today (it just mutates `skip_cleanup_locals`); removing it has no observe-shape impact beyond the bucket movements above.

If observe surfaces NEW records in `droppolicy_approximation` (bucket 2) or `real_disagreement` (bucket 6) after the swap, Rule 42 applies: freeze, diagnose, do not roll back.

---

## Summary — recommended decision

Proceed with Patch 3 as:

- **Broaden the existing destructibles consultation loop** at `string_arc.py:1518-1535` to iterate `destructible_locals | string_locals | array_locals`.
- **Delete the alias-walk's strings/arrays skip** at `string_arc.py:1486-1491` (the `if prev.local in string_locals or prev.local in array_locals: skip_cleanup_locals.add(...)` line).
- **Keep the alias-walk traversal itself** for `can_move_from_skipped_local` — different concern, different consumer at line 1536-1541.
- **No new lattice axis. No new MIR primitive. No additional pass rebuild.**
- **Version bump:** compiler minor (0.31.13 → 0.31.14). ABI no bump.
- **Gate:** the four sources listed in §2c. Rule 42 on first failure.

If you'd rather do a narrower swap (strings only, leave `array_locals` alone), the change set shrinks to a single-line iteration broadening + a single-line alias-walk edit; the array branch stays. My recommendation is broaden both for uniformity, but I'll defer to your call.

---

## Open questions for review

1. **Strings only or strings + arrays?** I lean toward both for uniform consultation; you may prefer narrower.
2. **Empirical regression-first check at Patch 3 boundary?** Should I temporarily disable the alias-walk to confirm the new carrier turns red BEFORE writing the migration, or trust by-inspection?
3. **Version bump 0.31.14 vs roll into a future bundle?** Same release-window question as the finale's 0.31.10 → 0.31.13 split.
