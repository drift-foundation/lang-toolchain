# Slice 7 Follow-ups

Captures items surfaced during the Slice 7a (DV public-surface
deletion + Debuggable / ResultError migration; landed at 0.31.62
on 2026-05-05) corpus migration that are out-of-scope for 7a but
need to land alongside or before Slice 7b (full DV runtime / lowering
removal + ABI 13 closure).

## Variant-field projection double-quoting (synthesized Diagnostic DV bridge)

**Symptom.**  After Slice 7a's typed-catch projection migration, an
iterator-invalidated test like:

```drift
} catch std.err:IteratorInvalidated(e) {
    val o_code = match e.params.get("op_id").as_int() {
        Some(v) => { (v != 2) ? 12 : 0 },
        None => { 13 }
    };
    o_code
}
```

returns `None` from `e.params.get("op_id").as_int()` even when the
underlying `op_id: IteratorOpId` variant tag is the expected
`Len = 2`.  Probing `e.params.encode_compact()` shows
`{"container_id":"std.containers:Array","op_id":"2"}` — the variant
tag value is QUOTED.  The accessor's "type-mismatch returns None"
behavior fires (cursor sees `JsonCursor::StringVal("2")`, not
`JsonCursor::IntVal(2)`).

**Cause.**  The synthesized `Diagnostic.to_json_text` body emitted by
`_synthesize_auto_diagnostic_impls` calls each field's own
`to_json_text` impl.  For `IteratorOpId` (manual Diagnostic on a
variant) the impl returns the raw canonical JSON int: `"2"` (a
1-character String).  But the throw-side params-JSON projection in
`_construct_error_via_synthesized_diagnostic` (or the type_checker's
auto-promotion at site A) wraps non-scalar field values in
`DV::String(value.to_json_text())`, which then JSON-encodes the
already-canonical body — emitting `"\"2\""`.

**Where the auto-promotion lives.**  `lang/driftc/type_checker.py`
around line 9533-9550 (the HExceptionInit per-field DV-attachment
auto-promotion path); the parallel splice in
`_build_throw_params_json` then takes the DV-typed value and projects
it back through `_dv_to_json_text`, which round-trips `DV::String("2")`
as `"\"2\""`.

**Fix direction (Slice 7b).**  Move synthesized `pub error` throws
off the DV bridge entirely — emit `to_json_text` calls directly,
then a single `M.ExcSetParamsJson` over the assembled `{ "k": v, ... }`
document with NO intermediate DV wrap.  Drop the legacy
`ConstructError(payload, attr_key) + ErrorAddAttrDV(...)` path for
synthesized errors; that path was kept as the internal bridge during
7a but has no consumer once `e.attrs[k]` is rejected at the checker
boundary (see `E_EXC_ATTRS_REMOVED`, landed in 7a).

Test pins held in 7a with the op_id check disabled (in-source
comment in `array_range_len_invalidated/main.drift` and siblings
that reference op_id).  Re-enable those checks once 7b lands.

## Multi-field typed-catch borrow-checker state

**Symptom.**  In specific test shapes (most reliably the original
`std_json_strict_chain` corpus shape, deleted in 7a), multiple
`if e.field_X != "..."` / `if e.field_Y != ...` reads against the
SAME catch-arm binder fire `borrow_checker_pass.py:_consume_place_use`
diagnostic `use of uninitialized 'e'` (no diag code; phase=
"typecheck"; emitter at borrow_checker_pass.py line ~811).

**Repro shape (when available).**  Several sequential
`try { ... } catch X(e) { if e.f1 != ... { return N }; if e.f2 != ... { return M } }`
arms, possibly inside a `match` scrutinee return-path.  Synthetic
4-field repro doesn't trip it — the actual std_json_strict_chain
nesting was the smallest failing case observed.

**Hypothesis.**  Slice 6's typed-catch model registers the binder's
`binding_id` in `_typed_catch_binders` so HField projection routes
through schema-typed lowering.  The borrow_checker's place-state
machinery may not recognize the typed-projection HIR shape as a
"valid use" of `e` — the catch binder gets seeded VALID at arm
entry (borrow_checker_pass.py line ~2655) but downstream typed
projection on `e` may dirty its tracked place state in a way that a
subsequent unrelated `e.fK` read sees UNINIT.

**Status.**  Test deleted in 7a (was DV-attrs-specific anyway).  No
test currently pins the bug.  Worth a regression test under
`work/typed-catch-borrow-state/` before 7b touches the
typed-projection lowering path, since 7b's variant fix above will
likely re-flow through the same place-state checks.

## DV runtime exports + HIR lowering retirement (Slice 7b core)

After 7a:

- User-source `Error.attrs[k]` and `Error.captures[fr][k]` are
  rejected at the checker (`E_EXC_ATTRS_REMOVED` /
  `E_EXC_CAPTURES_REMOVED`) for non-stdlib callers.
- Stdlib has no internal use of those surfaces (verified by grep
  during the 7a corpus sweep).
- `_emit_index_error_throw` in `hir_to_mir.py` and
  `drift_bounds_check_fail` in `array_runtime.c` were updated to
  populate the canonical `params_json` alongside the legacy DV-attrs
  attachment.

What 7b should retire:

1. **HIR→MIR lowering paths in `hir_to_mir.py`** that emit
   `M.ErrorAttrsGetDV` / `M.ErrorCapturesGetDV` for the user-source
   `e.attrs[k]` / `e.captures[fr][k]` HIR shapes — 4 emission sites
   around lines 2940-3025.  Currently kept alive for the stage2
   unit tests (`test_hir_to_mir_diagnostic_value.py`,
   `test_llvm_codegen_diagnostic_value.py`,
   `test_llvm_codegen_optional_ops.py`) — those tests need to be
   migrated or deleted alongside the lowering-path retirement.
2. **MIR ops `ErrorAttrsGetDV` / `ErrorCapturesGetDV`** in
   `stage2/mir_nodes.py` once their lowering paths are gone.
3. **Runtime exports** `__exc_attrs_get_dv` / `__exc_captures_get_dv`
   in `lang/compiler_infra/error_dummy.h/c` once nothing references
   them.
4. **Drop the legacy DV-attrs path on synthesized `pub error`
   throws** in `_construct_error_via_synthesized_diagnostic` —
   replace with direct `to_json_text` + `ExcSetParamsJson` (the
   variant-field fix above lands here).
5. **Retire `_dv_to_json_text` and the `ConstructDV` / `HDVInit`
   surface** once nothing else depends on them.
6. **Delete `DiagnosticValue` and `DiagnosticEntry`** from std.core
   entirely (currently they're just compiler-internal but the names
   still exist in `core.drift`).
7. **ABI bump 13 → 14** when the runtime exports retire.

Slice 7a held ABI 13 + compiler 0.31.62 stable; the runtime archive
still ships the legacy DV helpers.
