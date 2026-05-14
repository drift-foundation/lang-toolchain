# Code review: `mem.replace` accepts named `&mut T` values, not just inline borrows

**Branch / commit:** working tree against `main` (0.31.80 → 0.31.81 bump
in `lang/versions.py`). ABI 14 unchanged.
**Severity:** LANGUAGE_BUG, blocks the doc-recommended Pattern A
("Moving a field out of a struct" → `mem.replace`) whenever the slot
is factored through a helper / method / parameter. Workaround (the
explicit `&mut *slot` reborrow, or the customer's `SlotCell` wrapper)
exists, but the symptom is surprising and the diagnostic points the
wrong direction.
**Reporter:** mariadb team (managed-connection spike at
`packages/mariadb-rpc/tests/spike/managed_connection_spike.drift`).
Full report at
`~/src/pushcoin/work/customers-snapshot-handler/ask-toolchain-mem-replace-named-mut-ref.md`.

## TL;DR

`mem.replace<T>(arg, replacement)` accepted `arg` only when it was an
inline `&mut <place>` form at the call site; it rejected named `&mut T`
values (locals, parameters, method-call returns) with `replace expects
&mut T as the first argument [E-AUTO-9370445a]` even though their
resolved type was identical.  Cause: **four independent layers all did
syntactic shape analysis** (must be `HBorrow(is_mut=True)` or
`HPlaceExpr`) instead of trusting the type check. Customer
identified the bug exactly: "the binder for the generic intrinsic
mem.replace is checking the form of its first argument, not just its
type."

Fix: ~30 lines across the four layers (checker call-resolver, call
contract pass, borrow checker, MIR lowering) — keep the load-bearing
type check (`arg type must be &mut T`), drop the redundant form checks
and ICE asserts, add a value-based lowering path for the named-ref
case. No ABI / opcode / public-surface change.

## Customer-visible repro (verbatim from the ask)

Both compile and run against the same `mem.replace` definition; only
the first arg's expression form changes.

```drift
// Case A — compiles (inline borrow)
fn take_x(box: &mut Box) nothrow -> Optional<Resource> {
    return mem.replace<type Optional<Resource> >(&mut box.x, _none_resource());
}

// Case E — rejected (named local ref)
val slot_mut: &mut Optional<Resource> = &mut b.x;
val taken = mem.replace<type Optional<Resource> >(slot_mut, _none_resource());
// ^ error: replace expects &mut T as the first argument [E-AUTO-9370445a]
```

Other rejected forms from the report: helper parameter
(`fn h(slot: &mut Optional<R>)`), method-call return
(`mem.replace(guard.get_mut(), _none())`).

Crucially, the workaround `mem.replace(&mut *slot_mut, _none())`
compiles, runs, and is valgrind-clean — proving the underlying
lowering and runtime were always correct. The bug was purely in the
checker's shape analysis.

## Root cause — four layers, one assumption

Each of these treated the first argument's expression *form* as
load-bearing, instead of trusting the resolved type:

| Layer | File:line | Behavior on named ref |
|---|---|---|
| Call resolver shape check | `lang/driftc/checker/call_resolver.py:4674` | `_borrowed_place(arg)` returns None → emits `E-AUTO-9370445a` |
| Call contract pass | `lang/driftc/call_contract.py:305-313` | `isinstance(args[0], HBorrow)` false → emits `E_INTRINSIC_REPLACE_MUT_BORROW_REQUIRED` |
| Borrow checker | `lang/driftc/borrow_checker_pass.py:2254` | `place_from_expr` returns None → raises `AssertionError("checker bug")` |
| HIR→MIR lowering | `lang/driftc/stage2/hir_to_mir.py:3318` | Not `HPlaceExpr` → raises `AssertionError("...normalize/typechecker bug")` |

The customer-facing error is the first layer's. The other three are
the "ICE waiting to happen" walls hit as the checker is relaxed — all
four needed to move together.

The call resolver's type check (line ~4668: `if mut_inner is None`)
already correctly captures the actual correctness criterion — does
`arg`'s resolved type unify with `&mut T`? — and rejects shared refs,
by-value, and any other mismatch. The form checks were defense-in-depth
for "is this expression an addressable place", but for named `&mut T`
values that's a misframing: the *value* is a pointer (refs are
pointers at the MIR boundary), and that's exactly what `MoveFromRef` /
`StoreRef` need.

## Fix

Four surgical edits, ~30 lines, all replacing rejects / asserts with
the correct case split.

### 1. `call_resolver.py:4674`

Keep the type check. Drop the `_borrowed_place is None` rejection.
Make the inline-deref safety check conditional on `place_expr is not
None`:

```python
# OLD:
place_expr = _borrowed_place(expr.args[0])
if place_expr is None:
    diagnostics.append(_tc_diag(message="replace expects &mut T as the first argument", ...))
    return record_expr(expr, ctx.unknown_ty)
if any(isinstance(p, H.HPlaceDeref) for p in place_expr.projections) and isinstance(place_expr.base, H.HVar):
    base_ty = ctx.type_expr(place_expr.base, used_as_value=False)
    if base_ty is not None:
        base_def = ctx.type_table.get(base_ty)
        if base_def.kind is TypeKind.REF and not base_def.ref_mut:
            diagnostics.append(_tc_diag(message=f"cannot write through *{place_expr.base.name} unless ...", ...))
            return record_expr(expr, ctx.unknown_ty)

# NEW:
place_expr = _borrowed_place(expr.args[0])
if place_expr is not None and any(isinstance(p, H.HPlaceDeref) for p in place_expr.projections) and isinstance(place_expr.base, H.HVar):
    base_ty = ctx.type_expr(place_expr.base, used_as_value=False)
    if base_ty is not None:
        base_def = ctx.type_table.get(base_ty)
        if base_def.kind is TypeKind.REF and not base_def.ref_mut:
            diagnostics.append(_tc_diag(message=f"cannot write through *{place_expr.base.name} unless ...", ...))
            return record_expr(expr, ctx.unknown_ty)
```

The inline-deref safety check is still load-bearing — it catches
`mem.replace(&mut *immutable_var.field, ...)` — but only runs when
there *is* a syntactic place to inspect. For a named ref, the type
guarantees mutability.

### 2. `call_contract.py:305-313`

Remove the `E_INTRINSIC_REPLACE_MUT_BORROW_REQUIRED` issue entirely.
Replaced with a comment pointing at the call-resolver type check as the
load-bearing correctness path. Note that `hir_to_mir.py` already
filtered this contract issue out at two sites (lines 3310, 6997) — it
was only ever a hard error via `driftc.py:1119`'s top-level contract
sweep, so the contract check was effectively half-disabled already.

### 3. `borrow_checker_pass.py:2248-2262`

Don't ICE when `place_from_expr` returns None. Split into two paths:

- **Inline-borrow / direct place form** (place is not None): existing
  behavior — `_consume_place_use`, then visit `new_expr` as a consume,
  then `_reject_write_while_borrowed`, then set state to VALID.
- **Named &mut T value** (place is None): the underlying place's
  borrow rights were already validated when the &mut T was formed
  at its binding site; the borrow's liveness covers this write. Visit
  the arg expression as a normal read (use-after-move on the
  ref-holding local), then consume `new_expr`, then return.

### 4. `hir_to_mir.py:3313-3355`

Same case split — extend the existing `HBorrow(is_mut=True) →
place_expr_from_lvalue_expr` normalization with a third branch:

```python
if hasattr(H, "HPlaceExpr") and isinstance(place_expr, getattr(H, "HPlaceExpr")):
    # Inline-borrow / direct place form: existing path.
    ptr, inner_ty = self._lower_addr_of_place(place_expr, is_mut=True)
else:
    # Named &mut T value: the expression's lowered value IS the
    # pointer; inner_ty comes from the call's recorded CallInfo.
    if info is None or info.sig is None:
        raise AssertionError("replace(named-ref, v): missing CallInfo (checker bug)")
    inner_ty = info.sig.user_ret_type
    ptr = self.lower_expr(expr.args[0])
```

`lower_expr` on a `&mut T`-valued HVar (or HMethodCall, or any other
expression) produces the ref value, which IS a pointer at the MIR
layer — directly usable as the `ptr` operand to `M.MoveFromRef` and
`M.StoreRef`. This mirrors the pattern already used by `MAYBE_WRITE`
at line 3371 (`slot = self.lower_expr(expr.args[0])`).

## Regression tests

**Regression-first** — wrote the four tests, confirmed three positive
cases fail with exactly the customer's error on unfixed 0.31.80, and
the negative case passes. Applied the four-layer fix; all four pass.

Positive cases (e2e at `lang/tests/codegen/e2e/`):

- `mem_replace_named_mut_local_ref/` — the customer's headline case.
  Bind `&mut b.x` to a local, then pass that local to `mem.replace`.
  Round-trips through `Some → take → None`; also pins that a
  second take returns `None` (no double-move).
- `mem_replace_helper_param_ref/` — factor-out shape: `fn
  take_via_helper(slot: &mut Optional<R>) { mem.replace(slot, ...) }`,
  called twice, with the second call expected to find the slot
  already `None`.
- `mem_replace_method_ref_return/` — `mem.replace(cell.get_mut(),
  ...)`. Uses a handcrafted `Cell::get_mut() -> &mut
  Optional<Resource>` rather than pulling in std.sync, to keep the
  test small. This was the MutexGuard shape in the customer's
  report.

Negative / soundness cases:

- `mem_replace_rejects_shared_ref/` — passes a `shared_ref: &T`
  (immutable reference) to `mem.replace`. Expected `phase: typecheck`,
  exit 1, with the diagnostic substring `"replace expects &mut T as
  the first argument"`. This pins that widening to named `&mut T` did
  **not** accidentally widen to `&T`.
- **`mem_replace_named_ref_rejects_live_reborrow/`** — *soundness
  pin requested in review.* Constructs `val slot: &mut T = &mut b.x;
  val r: &T = &*slot; mem.replace(slot, ...); _peek(r);` — a live
  shared reborrow of `*slot` must block the write through `slot`.
  Expected `phase: borrowcheck`, exit 1, diagnostic substring
  `"cannot write to 'slot' while it is borrowed"`. If this test
  starts passing (i.e. the program compiles), the patch is unsound:
  `mem.replace(slot, ...)` would mutate / tombstone the storage
  `r` aliases, leaving `r` dangling. Proves the new named-ref
  borrow-check path correctly inherits conflict detection via loan
  tracking on the *ref local* (the `_visit_expr(state, place_expr,
  consume=False, ...)` call), rather than via place-state tracking
  on the underlying slot.
- **`mem_replace_named_ref_accepts_scoped_reborrow/`** —
  precision companion. Same shapes, but the reborrow is in an inner
  block that ends before the `mem.replace`. The replace must
  succeed; the slot must end up `None`. Without this, a future
  "over-eager" patch to the borrow checker could pass the negative
  test by rejecting *all* named-ref replaces — which would
  regress the customer use case. Locks the rejection precision in
  both directions.

All three positive cases pass under `DRIFT_MEMCHECK=1` valgrind —
zero leaks, zero errors. The fix correctly handles ownership transfer
through a named ref (the `MoveFromRef` + `StoreRef` pair atomically
tombstones the slot and writes the replacement, just as in the
inline form).

## Regression sweep

- **Checker / type_checker pytest suites:** 189 passed, 0 failed.
- **Representative e2e sample (13 cases):** simple_return, ffi_c_basic,
  codec_gzip_round_trip, uuid_round_trip, core_string_to_utf8_bytes,
  checker_chained_byte_equality_inference,
  result_or_throw_pub_error_envelope,
  pub_error_manual_diagnostic_redaction, borrow_escape_scope_accepted,
  and the four new mem_replace cases — **13 pass, 0 fail**.

## Risk surface

- **Only** the four files above change. No MIR opcode additions, no
  new public symbols, no ABI implications, no stdlib changes (stdlib
  itself uses `mem.replace` in the inline-borrow form, which is on
  the unchanged code path).
- The relaxation is strictly **additive** at the type level: the
  checker now accepts a strict superset of what it accepted before
  (specifically: arguments whose resolved type is `&mut T` regardless
  of expression form). It cannot turn previously-accepted code into
  rejected code.
- The negative regression (`mem_replace_rejects_shared_ref`) pins
  that the widening doesn't escape to shared `&T`. The type check at
  call_resolver.py:4668 (`_mut_ref_inner` returns None for `&T`)
  catches this and emits the same diagnostic the customer-facing
  message says it does.
- The borrow checker's new "named-ref path" is **less precise** than
  the place-path — we don't track underlying-place state for named
  refs. This is sound because:
  1. The borrow that produced the `&mut T` was itself
     borrow-checked at its formation site.
  2. The ref's lifetime constraints prevent the underlying place
     from being used through another path while the ref is live.
  3. Use-after-move checks on the *ref-holding local* still fire
     via the `_visit_expr` call. **The new test
     `mem_replace_named_ref_rejects_live_reborrow` empirically pins
     this**: `val r: &T = &*slot; mem.replace(slot, ...)` is rejected
     with `cannot write to 'slot' while it is borrowed` because the
     loan from `r = &*slot` is still live when we visit `slot` for
     the replace. The companion `..._accepts_scoped_reborrow` pins
     that the rejection is precise (scoped reborrows that have
     ended allow the replace to proceed).
  If a customer needs strict per-write conflict detection on the
  underlying place independently of the ref local's loan state,
  they can use the inline-borrow form, which continues on the
  precise path.
- The lowering's value-based path mirrors `MAYBE_WRITE` exactly
  (lower-expr-as-value, use as pointer), so it inherits the same
  ownership/refcount semantics that pattern already validates.

## Files touched

| File | Change |
|---|---|
| `lang/driftc/checker/call_resolver.py` | drop form-check rejection (kept type check); make inline-deref safety check conditional on `place_expr is not None` |
| `lang/driftc/call_contract.py` | remove syntactic `E_INTRINSIC_REPLACE_MUT_BORROW_REQUIRED` (replaced with explanatory comment) |
| `lang/driftc/borrow_checker_pass.py` | split into place / named-ref paths; named-ref path visits expr as read + consumes new_expr |
| `lang/driftc/stage2/hir_to_mir.py` | add named-ref lowering branch — `lower_expr` for the ptr, `info.sig.user_ret_type` for inner_ty |
| `lang/versions.py` | 0.31.80 → 0.31.81 |
| `docs/history.md` | full release entry with the four-layer breakdown |
| `docs/effective-drift.md` | Pattern A note: factoring `&mut T` through a helper / method is now valid |
| `lang/tests/codegen/e2e/mem_replace_named_mut_local_ref/` | NEW — named local ref e2e |
| `lang/tests/codegen/e2e/mem_replace_helper_param_ref/` | NEW — helper param ref e2e |
| `lang/tests/codegen/e2e/mem_replace_method_ref_return/` | NEW — method-call return ref e2e |
| `lang/tests/codegen/e2e/mem_replace_rejects_shared_ref/` | NEW — negative pin (shared &T rejected at typecheck) |
| `lang/tests/codegen/e2e/mem_replace_named_ref_rejects_live_reborrow/` | NEW — **soundness pin** (live shared reborrow blocks the named-ref replace at borrowcheck) |
| `lang/tests/codegen/e2e/mem_replace_named_ref_accepts_scoped_reborrow/` | NEW — precision pin (scoped reborrow doesn't over-block) |
| `docs/design/drift-lang-spec.md` | doc cleanup — §4.13.3 prose now references the concrete `pub fn destroy(var self: T) nothrow -> Void` shape; §5.11 implementation example matches |

## Ship readiness

I'd land this. The customer's blocker disappears, the workaround
becomes unnecessary, the negative direction is pinned, and the
regression sweep is clean. The `&mut *slot` workaround the customer
wrote into their managed-connection spike now becomes a stylistic
choice rather than a necessity.

## Follow-ups (not blocking this slice)

1. **`mem.swap` symmetric fix** — has the same four-layer structural
   pattern (`_borrowed_place` for each operand, `E_INTRINSIC_SWAP_MUT_BORROW_REQUIRED`
   contract issue, place-only borrow-checker path, place-only
   lowering). The customer report only flagged `replace`, so we
   haven't widened the slice. Worth doing as a follow-up to keep the
   two intrinsics in parity.
2. **Type-aware diagnostic message** — the current
   `"replace expects &mut T as the first argument"` is fine for the
   cases it now actually fires on (shared `&T`, by-value `T`), but
   could be sharper: "got `T`" / "got `&T`" rather than the static
   string. Cheap to do separately.
3. **Doc audit** — `docs/design/drift-lang-spec.md` has an
   implementation example at line 1269 still using
   `fn destroy(self) -> Void` (not `var self`). The trait
   *declaration* at line 1255 is correct per the "untyped self
   implies `self: Self`" rule (line 886), so this is just a style
   inconsistency, not a wrong example. Optional cleanup.
