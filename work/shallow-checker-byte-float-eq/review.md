# Code review: shallow checker — `Byte` and `Float` comparisons infer `Bool`

**Branch / commit:** working tree against `main` (0.31.79 → 0.31.80 bump
in `lang/versions.py`). ABI 14 unchanged.
**Severity:** LANGUAGE_BUG, customer-reported papercut (one-token
workaround was always available). Not blocking.
**Reporter:** bookkeeper team (see
`~/src/pushcoin/work/customers-snapshot-handler/ask-toolchain-type-inference-and-chain.md`).

## TL;DR

`val ok = bytes[0] == cast<Byte>(0x1F) and bytes[1] == cast<Byte>(0x8B)`
(magic-byte / signature-check shape) failed to compile in 0.31.79 with
`E-AUTO-84b36a12 — if condition must be Bool` at the downstream
`if ok { ... }` site, because the shallow inference rule for binary
comparisons whitelisted `Bool / Int / Uint / Uint64 / String` but not
`Byte`. The `val` therefore typed as `Unknown` and the misleading
diagnostic surfaced at the use site instead of the binding. Float was
also missing despite the in-source fall-through diagnostic claiming it
was supported. Fix is **~15 lines** in
`lang/driftc/checker/__init__.py::_TypingContext`: cache `_byte_type`,
add two scalar branches in the HBinary path. No ABI / codegen /
primary-checker change.

## Customer-visible repro (verbatim from the ask)

```drift
val bytes = core.string_to_utf8_bytes(&src);
val v3_ok =
    bytes.len == 3
    and bytes[0] == cast<Byte>(65)
    and bytes[1] == cast<Byte>(66)
    and bytes[2] == cast<Byte>(67);
if v3_ok { ... }                    // ← E-AUTO-84b36a12 here
```

Customer workaround: `val v3_ok: Bool = ...` (annotated type masks the
shallow miss).

## Narrowing

Confirmed each axis independently before touching code:

| Shape | 0.31.79 |
|---|---|
| `val ok = bytes.len == 3;` *(no cast, no chain)* | ✅ compiles |
| `val ok = bytes[0] == cast<Byte>(65);` *(single Byte==Byte)* | ❌ E-AUTO-84b36a12 |
| `val ok = bytes.len == 3 and bytes.len == 3;` *(Int and, no Byte)* | ✅ |
| `val ok = bytes.len == 3 and bytes[0] == cast<Byte>(65);` | ❌ |
| `val ok = bytes[0] == cast<Byte>(65) and bytes[1] == cast<Byte>(66);` | ❌ |

The bug is **not** in the `and` reducer (case C compiles fine; same `and`
shape, plain Int comparisons on both sides). The bug is in the
**Byte == Byte** rule of the shallow inference — chaining with `and` just
propagates the `None` upward to the `val`.

Probing the other scalar primitives:

| Type | Shallow infers Bool? | Primary checker accepts? | Codegen works? |
|---|---|---|---|
| Byte | ❌ (this fix) | ✅ | ✅ |
| Float | ❌ (this fix) | ✅ | ✅ |
| Int32 | ❌ | ✅ | ❌ ICE in LLVM v1 |
| Uint32 | ❌ | ✅ | ❌ ICE in LLVM v1 |

→ Fix scope is **Byte + Float**. Int32 / Uint32 deliberately not
unblocked here (codegen is broken on a separate track; helping them
through inference would just convert a checker diagnostic into a less
actionable codegen ICE, against K's "keep unsupported rejected"
principle).

## Root cause

`lang/driftc/checker/__init__.py:1941-1992` — the `HBinary` arm of
`_TypingContext._infer_expr_type`. The whitelist handles Bool /
Int / Uint / Uint64 / String comparisons explicitly; everything else
falls through to a `None` return with a generic rejection diagnostic at
line 1987 ("==/!= are only supported for Bool, Int, Float, String in v1")
— and **that diagnostic message already promised Float was supported**,
which is the smoking gun that the rule had drifted from the docstring.
Byte just wasn't on anyone's radar (likely because Byte arithmetic isn't
a real surface in v1, but Byte equality very much is — bytewise input
parsing, magic-byte sigs, row-decode boundaries).

The shallow rule's `None` return means downstream `val ok = …` records
`Unknown` for `ok`. The `if` condition validator at the use site
correctly rejects `Unknown` (Bool was expected) but reports it where
the symptom surfaces, not where it originated. That diagnostic is
correct as written; the problem is that the binding should have typed
as `Bool` in the first place.

## Fix

Two edits to `lang/driftc/checker/__init__.py`.

1. Cache the Byte scalar TypeId alongside the existing scalars
   (one line in the init block, line ~387):

   ```py
   self._byte_type = _find_named(TypeKind.SCALAR, "Byte") or self._type_table.ensure_byte()
   ```

2. Add two branches to the HBinary inference path, matching the existing
   shape of the Int / Uint / Uint64 branches (after line 1981):

   ```py
   # Byte: comparison only (no arithmetic in v1; callers cast to
   # Int/Uint first). Closing the gap that made magic-byte checks
   # (`bytes[0] == cast<Byte>(0x1F) and ...`) infer Unknown and
   # misreport `if condition must be Bool` at the use site
   # instead of at the val binding.
   if left_ty == checker._byte_type and right_ty == checker._byte_type:
       if expr.op in comparison_ops:
           return checker._bool_type
       return None

   # Float: comparison + arithmetic. The fall-through rejection
   # diagnostic below already claimed Float was supported for
   # ==/!=; this aligns the shallow inference with that claim
   # and with the primary type-checker.
   if left_ty == checker._float_type and right_ty == checker._float_type:
       if expr.op in comparison_ops:
           return checker._bool_type
       return checker._float_type
   ```

Net diff: **~15 lines**, all additive, all under existing scalar
patterns. No new imports, no new public surface.

### Things I considered and decided against

- **Folding Byte/Float into a generic "scalar-eq returns Bool" rule.**
  The existing per-scalar pattern is repetitive but explicit; the
  arithmetic surfaces differ (Byte has none in v1, Float has full set,
  Int/Uint have full set). A unified table would require introducing
  policy data the rest of the checker doesn't use yet. Rejected as
  over-abstraction for two new entries.
- **Adding Int32 / Uint32 here.** Per the table above, codegen v1 ICEs.
  Out of scope — separate codegen track. Adding them to the shallow
  whitelist would degrade the error UX, not improve it.
- **Improving the at-the-use-site diagnostic.** K's third ask. With the
  rule fix in place, the customer's shape no longer trips the misleading
  diagnostic at all. Improving the "Unknown condition" message for the
  remaining class of cases (truly ill-typed RHS with no matching scalar
  branch) is worth doing but is non-trivial (what to say when the
  binding *itself* can't be typed?) — deferred to a follow-up. Not
  blocking the customer fix.

## Regression tests

**Regression-first**, per project policy. With the new tests applied
against unfixed 0.31.79, three of four unit tests and the e2e case fail
with exactly `E-AUTO-84b36a12 — if condition must be Bool`. Applying
the checker fix flips them green.

- `lang/tests/checker/test_byte_float_comparison_inference.py` — 4 unit
  tests:
  - `test_byte_eq_binding_infers_bool_for_if_condition` — the
    one-liner version of the customer report.
  - `test_chained_and_over_byte_eq_infers_bool` — the operative
    chained `and` shape.
  - `test_float_eq_binding_infers_bool_for_if_condition` — pins
    the parallel Float fix.
  - `test_array_byte_eq_stays_rejected_with_specific_diagnostic`
    — **negative pin**: `Array<Byte> == Array<Byte>` must still
    be rejected after the Byte fix (extending Byte must not
    widen the accepted-equality surface). Asserts the specific
    `"==/!= are only supported for ..."` diagnostic substring,
    not just `len(diags) > 0` — a weaker check would pass on
    any unrelated setup failure (type-resolution, scope, etc.)
    and would silently miss the regression we actually care
    about. The assertion failure message tells the next
    maintainer how to react in either direction (reworded
    diagnostic vs. widened surface).
- `lang/tests/codegen/e2e/checker_chained_byte_equality_inference/` —
  exact customer shapes through the full pipeline:
  - Unannotated chained byte equality.
  - Parenthesized form (rules out precedence regression).
  - Gzip magic-byte check (`bytes[0] == cast<Byte>(0x1F) and ...`).
  - Single byte equality bound to a val.
  - Negative path (byte inequality returns false correctly).
  - `Float == Float`.

## Regression sweep

- **Checker / type_checker pytest suites:** 189 passed, 0 failed
  (`PYTHONPATH=… pytest lang/tests/checker/ lang/tests/type_checker/`).
- **Representative e2e sample (8 cases):** simple_return, ffi_c_basic,
  codec_gzip_round_trip, uuid_round_trip, uuid_v3_vectors,
  uuid_v4_shape, core_string_to_utf8_bytes, and the new
  checker_chained_byte_equality_inference — 8 pass, 0 fail.

## Risk surface

Narrow:

- **Only** `_TypingContext._infer_expr_type` changes. The primary
  type-checker, HIR→MIR lowering, codegen, runtime, and the linker
  contract are all untouched. There is no ABI implication and the
  fix is invisible to packages.
- The new branches strictly **add** acceptance for previously-rejected
  expressions. They cannot turn previously-accepted code into rejected
  code.
- The negative test (`Array<Byte> == Array<Byte>` still rejected) pins
  that the change doesn't accidentally widen the equality surface
  past scalars.
- No primary-checker bypass: the primary `Checker` already accepted
  Byte and Float comparisons (verified — both produce working binaries
  when the customer's `: Bool` annotation is applied). We're only
  closing the secondary inference's blind spot.

## Files touched

| File | Change |
|---|---|
| `lang/driftc/checker/__init__.py` | +1 line scalar cache, +12 lines two HBinary branches |
| `lang/versions.py` | 0.31.79 → 0.31.80 |
| `docs/history.md` | release note (with the deferred-diagnostic rationale) |
| `lang/tests/checker/test_byte_float_comparison_inference.py` | NEW — 4 unit regressions |
| `lang/tests/codegen/e2e/checker_chained_byte_equality_inference/` | NEW — e2e regression |

## Ship readiness

I'd land this. The customer's blocker is gone, the negative direction
is pinned, the regression sweep is clean, and the deliberate
out-of-scope items (Int32/Uint32 codegen, diagnostic improvement) are
called out in history.md and below.

## Follow-ups (not blocking this slice)

1. **Diagnostic improvement** for the remaining "binding inferred
   Unknown → `if` rejects Unknown" class of cases. Move the error to
   the binding site with a "could not infer type of `X`; add an
   explicit type annotation" message when the RHS truly cannot be
   typed (rather than just landing on a non-Bool because we didn't
   handle Byte). Touches the `if` condition validator and probably
   `HLet` type recording.
2. **Int32 / Uint32 codegen** — separate track. LLVM v1 ICE on
   `integer binop requires matching Int/Uint operands (have i32, i32)`.
   Once that's fixed, the shallow whitelist should extend to cover
   these too.
3. **Wider scalar comparison audit** — `Char`/`Rune` if/when added; any
   other scalar types where the shallow path lags the primary checker.
