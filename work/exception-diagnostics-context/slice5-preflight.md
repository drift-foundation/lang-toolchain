# Slice 5 Preflight: DV Public Removal + ABI 13

**Status:** preflight — no code changes yet. **K signed off 2026-05-03 with substantial scope expansion** — see §12. The original §1 inventory still applies; §2 decisions are now answered (with §12 superseding §2 where they diverge). The pre-implementation deliverable is now a **language-migration spec draft** (see §13), not direct DV deletion. Awaiting K's go-ahead on §13 before any edits begin.

**Scope:** final removal of the public `DiagnosticValue` surface and the user-visible `e.attrs[...]` / `e.captures[...]` API. Public exception model becomes JSON text only:

  - `e.encode_compact()`
  - `e.params.encode_compact()`
  - `e.context.encode_compact()`
  - `e.params.get("k").as_*()`

No `std.json` runtime dependency. No context cursor in this slice unless trivial after deletion.

---

## 1. Inventory

### 1.1 Public `DiagnosticValue` surface in stdlib

**`stdlib/std/core/core.drift`** (canonical exports + builtin trait surface):
- Export block lines 26–27: `DiagnosticEntry`, `diagnostic_entry`.
- Type prelude / reserved-names entries already noted as legacy in spec §3.2 / §9.4.
- `pub trait Diagnostic { fn to_diag(&Self) -> DiagnosticValue }` at `core.drift:111-115`.
- `pub struct DiagnosticEntry { key: String, value: DiagnosticValue }` at `core.drift:124-127`.
- `pub fn diagnostic_entry(...)` at `core.drift:130-132`.
- `implement Diagnostic for {Int, Uint, Bool, Float, String, DiagnosticValue}` at `core.drift:563-595`.
- `implement Throw for {Int, Uint, Bool, Float, String, DiagnosticValue}` at `core.drift:600-631` — all wrap `ResultError(dv = DiagnosticValue::...)`.
- `implement copy_mod.Copy for {DiagnosticValue, DiagnosticEntry}` at `core.drift:522, 525`.
- `implement Frozen for DiagnosticValue` at `shareable.drift:193`.

**`stdlib/std/err/err.drift`** (universal Error vehicle):
- `pub exception ResultError(dv: DiagnosticValue)` at line 52 — single most load-bearing DV consumer in stdlib.
- `implement Diagnostic for IteratorOpId` at line 66.

**Per-module `Diagnostic` impls (12 stdlib modules):** `codec`, `random`, `text` (×2 — `Utf8Error`, `TextError`), `time`, `net`, `regex`, `crypto`, `parse`, `io`, `json` (`JsonErrorData`), `err` (`IteratorOpId`). All follow the same shape: `to_diag(&Self) -> DiagnosticValue::String(self.tag)` plus `Throw::throw_self → throw ResultError(dv = ...)`.

**`stdlib/std/log/log.drift`** (orthogonal but DV-typed):
- `pub trait Debuggable { fn to_debug(&Self) -> DiagnosticValue }` at line 121-125 — separate trait, uses DV as return type.
- 7 stdlib `Debuggable` impls (Int/Uint/Bool/Float/String/DiagnosticValue/etc.).

**`stdlib/std/json/json.drift`** (transitional adopter):
- `implement Diagnostic for JsonErrorData` at line 115.

### 1.2 Compiler lowerings still requiring DV machinery

**MIR op classes (`stage2/mir_nodes.py`)** — all 7 still defined and registered:
- `ConstructDV` (line 1233) — DV literal-promotion + HDVInit lowering.
- `ErrorAddAttrDV` (line 1258) — Slice-1 throw-side legacy DV path.
- `ErrorAddLocalDV` (line 1267) — `^`-capture DV path.
- `ErrorAttrsGetDV` (line 1331) — `e.attrs[k]` reader.
- `ErrorCapturesGetDV` (line 1342) — `e.captures[fr][k]` reader.
- DV scalar accessors: `DVAsInt`, `DVAsBool`, `DVAsFloat`, `DVAsString`, `DVAsObject`, `DVGetField`, `DVLen`, `DVEntries`, `DVKind`, `DVIndex` — used by `_dv_to_json_text` and the `e.attrs[k].as_int()` chain.

**HIR→MIR lowering hooks (`stage2/hir_to_mir.py`):**
- `_capture_to_dv` (line 874) — typed value → DV at `^`-capture site.
- `_construct_error_from_exception_init` — emits `ConstructError(payload=dv1)` + `ErrorAddAttrDV(value=dvN)` for legacy DV path AFTER the new JSON path. Pre-existing complex K28-aftermath ownership protocol at lines 6042-6062.
- `_emit_captured_locals` — emits `ErrorAddLocalDV` for legacy DV path AFTER the new JSON frame append.
- DV intrinsic method dispatch (line 4579+) for `as_int`/`as_bool`/`as_float`/`as_string`/`as_object`/`get`/`index`/`kind`/`len`/`entries`.
- `e.attrs[k]` HIndex special-case in type checker (`type_checker.py:8728`) + HIR→MIR (`hir_to_mir.py:2856, 2923`).
- `e.captures[fr][k]` HIndex special-case (parallel sites).

**Throw-side params/context build still uses `_dv_to_json_text`:**
- `_build_throw_params_json` and `_build_throw_context_frame_json` clone field DVs and project via the transitional `_dv_to_json_text` helper. Slice 1/2/4A all depend on this.

**Codegen (`llvm_codegen.py`):** all DV ops have LLVM lowerings + a block of LLVM declarations for DV runtime helpers (lines 1543-1563).

`grep -rn "M\.ConstructDV\|M\.ErrorAdd...DV..." | wc -l` → **45** active references across the compiler.

### 1.3 Runtime helpers + storage

**`lang/compiler_infra/error_dummy.h/c`:**
- `struct DriftError` carries BOTH the legacy DV fields (`attrs`, `attr_count`, `frames`, `frame_count`) AND the JSON fields (`params_json`, `context_json`).
- DV-shaped helpers: `drift_error_add_attr_dv`, `drift_error_add_local_dv`, `__exc_attrs_get`, `__exc_attrs_get_dv`, `__exc_captures_get_dv`, `drift_error_get_attr`, `drift_error_new_with_payload`.
- `drift_error_release` walks both legacy and JSON fields.

**`lang/compiler_infra/diagnostic_runtime.h/c`** (DV runtime itself):
- ~20 helpers: `drift_dv_{missing,null,bool,int,float,string,array,object,object_from_entries,clone,release,get,index,kind,as_int,as_bool,as_float,as_string,as_object,get_field,len,entries}`.
- `struct DriftDiagnosticValue` (24-byte tagged union).
- `struct DriftDiagnosticEntry`, `struct DriftDiagnosticArray`, `struct DriftDiagnosticObject`, `struct DriftDiagnosticField`.

### 1.4 Tests still using DV public surface

`grep -rln "e\.attrs\[\|e\.captures\[\|DiagnosticValue::\|to_diag\b"` returns **96 files**. Sampled:

- `lang/tests/driver/test_exception_alias_resolution.py`, `test_exception_params_json.py` (legacy-additivity baseline), `test_diagnostic_byref.py`, `test_dv_entries_negative.py`, `test_external_consumer.py`, `test_lambda_catch_binder_capture_discovery.py`, `test_map_literal_move_canonicalization.py`, `test_std_log_api_smoke.py`, `test_try_trait_visibility.py`, `test_inline_try_catch_attrs_lang_bug.py`, `test_exception_params_cursor.py` (uses `DiagnosticValue::Null()` for null-fields probe), `test_exception_context_json.py`.
- `lang/tests/codegen/e2e/`: ~20 e2e Drift sources using `e.attrs[]`, `DV::Object`, `DiagnosticEntry`, etc.
- `lang/tests/stage2/test_dv_*` (2 files) — internal DV ownership tests.

### 1.5 Doc surface

`grep -rln "DiagnosticValue\|to_diag" docs/` → 8 files: `dmir-spec.md`, `drift-lang-abi.md`, `drift-lang-spec.md`, `effective-drift.md`, `history.md`, `articles/drift_vs_rust_error_handling.md`, `articles/drift-compiler-architecture.md`, `design/spec-change-requests/drift-error-diagnostic-refactor.md`.

---

## 2. Decision Points (K asked)

### 2.1 Direct JSON text projection vs renamed `to_json_text` trait?

**Recommendation: renamed `to_json_text` trait.** `pub trait Diagnostic { fn to_json_text(self: &Self) nothrow -> String }`.

- Preserves user extensibility — user-defined exception field types remain projectable.
- Naming: keep `Diagnostic` (per K's earlier directive). Method name `to_json_text` is honest about the return shape (vs the prior `to_diag` / mooted `to_json -> JsonNode` which would re-introduce a JSON value type).
- Stdlib provides built-in impls for `Int`, `Uint`, `Bool`, `Float`, `String` (and possibly `DiagnosticValue` if it stays internal — see 2.2).
- Throw-side params build replaces `_dv_to_json_text` with direct `Diagnostic.to_json_text(&field)` calls. No DV intermediate.

**Tradeoff with "direct projection":** A direct compiler-side projection (no trait) would limit exception fields to a closed set of primitives; user types couldn't be exception fields without a wrapper. Too restrictive for a release surface.

### 2.2 Keep internal-only `DiagnosticValue` or delete it completely?

**Recommendation: keep DV INTERNAL for this slice; delete only the public surface.**

Rationale:
- `pub exception ResultError(dv: DiagnosticValue)` in `std.err` is the universal catchall throw vehicle. ~12 stdlib modules `throw std.err:ResultError(dv = DiagnosticValue::String(self.tag))` from their `Throw` impls. Migrating ResultError off DV is a separate large change.
- Deleting DV completely also forces removal of `std.log.Debuggable` (separate trait that returns DV); that's an orthogonal concern not in this slice's scope.
- Internal DV doesn't appear in user-facing exception field projections (Slice 5 throw-lowering switches to `Diagnostic.to_json_text` directly, bypassing DV).

**What "internal" means concretely:**
- `DiagnosticValue` builtin variant stays.
- `DiagnosticEntry`, `diagnostic_entry`: REMOVE from `std.core` exports; either delete entirely OR mark internal (`@internal` if Drift supports such; otherwise leave un-exported).
- `e.attrs[k]` / `e.captures[fr][k]`: type-check ERROR with migration diagnostic.
- Storage in `DriftError`: legacy `attrs` / `frames` fields REMOVED (ABI 13 reshape).
- Throw-side `ErrorAddAttrDV` / `ErrorAddLocalDV` emission REMOVED — only the JSON-text path remains.
- DV scalar accessors (`DVAs*`, `DVKind`, `DVIndex`, etc.) and `_dv_to_json_text` REMOVED from compiler. The transitional bridge dies.
- Internal stdlib continues using `DiagnosticValue::String(tag)` inside `std.err:ResultError(dv = ...)` AS A PRIVATE PAYLOAD; no public observation.

### 2.3 What replaces `Diagnostic` as the user projection trait?

**Recommendation: same name, new method shape.** `pub trait Diagnostic { fn to_json_text(self: &Self) nothrow -> String }`.

- User-defined exception field types implement `to_json_text` returning RFC-8259 JSON text (a complete value: `42`, `true`, `"escaped"`, `null`, `{...}`, `[...]`).
- Stdlib built-in impls cover primitives:
  - `Int`/`Uint` → decimal text.
  - `Bool` → `"true"`/`"false"`.
  - `Float` → `format_float(self)`.
  - `String` → `_json_quote_string(self)` (already exists in `std.core`).
- Per-module exception-field types (CodecError, NetError, etc.) re-implement: `to_json_text` returns `_json_quote_string(self.tag)` or a `{...}` object literal.

**Migration churn for stdlib `Diagnostic` impls:** 13 impls × ~3 lines each = ~40 lines of mechanical edits. Manageable.

---

## 3. Proposed ABI 13 `DriftError` Layout

```c
struct DriftError {
    drift_error_code_t code;          // u64; field 0 (unchanged from ABI 12)
    struct DriftString event_fqn;     // field 1 (unchanged)
    struct DriftString params_json;   // field 2 — formerly at field 6 in ABI 12
    struct DriftString context_json;  // field 3 — formerly at field 7
    void *stack;                      // field 4 — opaque backtrace (unchanged)
};
```

**Removed:** `attrs`, `attr_count`, `frames`, `frame_count` (4 fields, 32 bytes on x86_64).

**Compiler-emitted `extractvalue` indices change:**
- `M.ErrorEvent` extracts field 0 (code) — unchanged.
- `M.ErrorEventFqn` extracts field 1 — unchanged.
- No other extractvalues today; all params/context access goes through `drift_error_*_json` helpers which are layout-agnostic on the call side.

**LLVM type alias (`%DriftError`)** in codegen prelude becomes `{ u64, %DriftString, %DriftString, %DriftString, ptr }`.

---

## 4. Runtime Helper Delete / Add List

### Delete (`lang/compiler_infra/error_dummy.h/c`)
- `drift_error_add_attr_dv`
- `drift_error_add_local_dv`
- `drift_error_get_attr`
- `__exc_attrs_get` (and its DV variant)
- `__exc_attrs_get_dv`
- `__exc_captures_get_dv`
- `drift_error_new_with_payload`
- `drift_error_new_dummy` (unused legacy ctor; verify before delete)

### Delete (`lang/compiler_infra/diagnostic_runtime.h/c`)
- All DV public C entry points (`drift_dv_*`) IF compiler stops emitting them.
- BUT — internal DV in `std.err:ResultError(dv = ...)` still routes through `drift_dv_string`, `drift_dv_int`, `drift_dv_clone`, `drift_dv_release` for stdlib's own `Throw` impls. Decision needed: either keep these as private-but-linked, or migrate `ResultError` to a String-payload shape.

**Recommendation:** **keep `diagnostic_runtime.{h,c}` minimal-internal**. Mark all symbols `__attribute__((visibility("hidden")))` or similar; stop declaring them in compiler-emitted LLVM IR. They link only via stdlib `Throw` impls. ResultError migration is out of Slice 5 scope.

### Add
- None. The JSON path uses existing `drift_error_get_params_json`, `drift_error_set_params_json`, `drift_error_get_context_json`, `drift_error_append_context_frame` — all already in ABI 12.

---

## 5. Compiler / MIR Op Delete / Add List

### Delete (`stage2/mir_nodes.py` + `__init__.py` + `string_arc.py` + `llvm_codegen.py`)
- `M.ErrorAddAttrDV`
- `M.ErrorAddLocalDV`
- `M.ErrorAttrsGetDV`
- `M.ErrorCapturesGetDV`
- `M.DVKind` (transitional bridge — used only by `_dv_to_json_text`)
- `M.DVIndex` (same)
- `M.DVAsInt`, `M.DVAsBool`, `M.DVAsFloat`, `M.DVAsString`, `M.DVAsObject`, `M.DVGetField`, `M.DVLen`, `M.DVEntries` — used by `e.attrs[k].as_int()` chain (going away) and `_dv_to_json_text` (going away).
- `M.ConstructDV` — IF stdlib `ResultError` migration completes too. Otherwise KEEP for stdlib internal use.

### Delete (HIR→MIR lowerings)
- `_capture_to_dv` helper.
- `_dv_to_json_text` (in std.core).
- `_json_quote_string` and `_byte_*` helpers stay — used by the new `Diagnostic.to_json_text` impls.
- `_build_throw_params_json` rewires: collect `Diagnostic.to_json_text(&field)` Strings instead of cloning DVs and calling `_dv_to_json_text`.
- `_build_throw_context_frame_json` rewires similarly for `^`-captured locals.
- `_construct_error_from_exception_init` legacy DV emission block (lines ~6235-6254) deleted.
- `_emit_captured_locals` legacy DV emission deleted.
- DV intrinsic method dispatch block (line 4579+).
- `e.attrs[k]` HIndex special-case in type checker + HIR→MIR.
- `e.captures[fr][k]` HIndex special-case.

### Add
- Type-checker rejection diagnostic for `e.attrs[k]` — clear migration message:

  > `error: e.attrs[k] is removed in ABI 13. Use e.params.get(k).as_*() or e.params.encode_compact() — see migration notes in docs/history.md.`

- Same for `e.captures[fr][k]`.
- Trait-method rename for `Diagnostic`: `to_diag → to_json_text`, return-type change `DiagnosticValue → String`.
- Per-stdlib-module `Diagnostic` impl edits (13 modules + std.core builtins).

### Keep
- `M.ExcGetParamsJson`, `M.ExcSetParamsJson`, `M.ExcGetContextJson`, `M.ExcAppendContextFrame`, `M.ErrorEvent`, `M.ErrorEventFqn` — all the JSON-path infrastructure from Slices 1-3.
- `M.ConstructError` — still needed (codegen calls `drift_error_new`).
- `core.JsonCursor` + accessors (Slice 4A).
- `_json_quote_string` and the byte/hex helpers (used by the new throw-side JSON build via `to_json_text` for String fields).

---

## 6. Test Migration List

### Positive tests (must continue passing)
- `test_exception_params_json.py` (6 tests) — mostly unchanged; the legacy-additivity baseline `test_old_attrs_path_still_works_additive` becomes a NEGATIVE test (see below).
- `test_exception_context_json.py` (5 tests).
- `test_exception_envelope_json.py` (5 tests).
- `test_exception_params_cursor.py` (6 tests) — but `test_explicit_null_distinguished_from_missing` uses `throw DvErr(payload = DiagnosticValue::Null())`. This needs migration: build a null params value through a different route (e.g., a user type whose `to_json_text` returns the literal `"null"`).

### Negative tests (NEW — assert removed surface fails with diagnostic)
- `e.attrs[k]` → expect `E_DV_PUBLIC_REMOVED` (or similar) diagnostic mentioning the migration path.
- `e.captures[fr][k]` → same.
- `DiagnosticValue::Int(...)` in user code → `cannot resolve DiagnosticValue` diagnostic with migration hint.
- `to_diag(&self) -> DiagnosticValue` user impl → diagnostic pointing at new `to_json_text -> String` shape.

### Tests requiring rewrite or deletion
- `test_diagnostic_byref.py` — DV-by-ref intrinsic test; obsolete.
- `test_dv_entries_negative.py` — DV entries access; obsolete or moved to internal-only suite.
- `test_lambda_catch_binder_capture_discovery.py` — uses `e.attrs[k]`; rewrite to `e.params.get(k)`.
- `test_map_literal_move_canonicalization.py` — uses DV literal; check if still relevant.
- `test_external_consumer.py` — package consumer test using DV; rewrite to JSON.
- `test_try_trait_visibility.py` — Throw trait test; revise.
- `test_std_log_api_smoke.py` — log.Debuggable smoke; orthogonal but uses DV. Keep as long as Debuggable stays.
- `lang/tests/codegen/e2e/exception_attrs/`, `exception_dv_attr_no_leak/`, `exception_dv_object_rvalue_entries_no_leak/`, `exception_string_attr_concat_double_catch_no_corruption/`, `exception_attrs_reject_nonprimitive/`, `exception_result_error_param_*` (×3) — all DV path. Either delete or migrate to JSON-equivalent shapes.
- `lang/tests/codegen/e2e/iterator_op_id_mapping/`, `om_*_diag_entry/` (×4), `pkgb_struct_ctor_diag_entry/`, `index_error_payload_oob/` — exercise DiagnosticEntry / DV constructors. Migrate or delete.
- `lang/tests/stage2/test_dv_deref_clone.py`, `test_dv_string_arc_release.py` — internal DV ownership; keep if internal DV stays, otherwise delete.

### Estimated test churn
- ~25–30 test files rewrite or delete.
- ~15 e2e Drift sources rewrite or delete.
- 4–6 NEW negative tests for removed-surface diagnostics.

---

## 7. Versioning Plan

- `DRIFTC_VERSION` 0.31.53 → **0.32.0** (minor version bump — public API break).
- `DRIFT_RT_ABI_VERSION` 12 → **13** (DriftError layout change + helper symbol set change).
- ABI stamp regression test `test_abi_version_stamp.py` regenerates expectations automatically.
- Spec docs: `drift-lang-abi.md` §2 banner moves "Migration state" → "ABI 13 final" wording. `drift-lang-spec.md` §5.13 / §14 lose their "legacy" footnotes for DV. `dmir-spec.md` updates to drop DV ops.
- `history.md`: 2026-05-XX Slice 5 entry — comprehensive listing of what was removed.

---

## 8. Risk List

### High risk
1. **Throw-side ownership**. Slice 1's `_build_throw_params_json` carefully clones DVs (M.CopyValue) and explicitly `M.DropValue`s them after `_dv_to_json_text` calls — the throw unwinds before scope-drop fires. Replacing with direct `Diagnostic.to_json_text(&field)` calls means the field VALUE is borrowed (no DV intermediate), so we don't need the clone+drop. BUT: if the field expression is an rvalue (e.g., `throw E(name = format_int(x))`), the rvalue String must be properly dropped on the throw path. The K28-aftermath ownership protocol comments (`hir_to_mir.py:6042-6062`) describe a brittle two-mechanism release scheme; the new path needs an equivalent audit.

2. **`std.err:ResultError(dv = DiagnosticValue)` continues to be the universal catchall**. `or_throw` and the `Throw` trait impls all wrap into `ResultError` with a `dv` field. Keeping DV as internal payload means ResultError's shape doesn't change — but means the universal-error-vehicle pattern isn't fully migrated. Future cleanup. Document explicitly so reviewers know.

3. **`std.log.Debuggable`** trait returns `DiagnosticValue`. If we delete public DV, std.log breaks. Keep `std.log` private to its module (no export of `Debuggable`) OR migrate `to_debug` → `to_json_text` too. Recommend: migrate, since it's the same pattern; ~7 stdlib impls to update. Adds ~30 lines of churn.

4. **`stdlib/std/json/json.drift:115` `implement Diagnostic for JsonErrorData`** — std.json itself uses DV-via-Diagnostic. Migrating to `to_json_text` means JsonErrorData implements its own JSON-text projection (recursive via std.json's encoder, since std.json HAS access to JsonNode). Solvable but worth noting.

### Medium risk
5. **Removed-surface diagnostic quality**. `e.attrs[k]` has been a documented public surface for many releases. The compile-error message must be actionable: tell the user what to write instead. Recommend a dedicated `E_DV_PUBLIC_REMOVED` code with examples.

6. **Test migration is large but mechanical**. Risk of incidentally exercising a new bug while rewriting; bias toward minimal mechanical edits and verify each migrated test passes the equivalent JSON shape.

7. **DriftError struct field order**. Existing pre-ABI-13 binaries don't exist (Slice 4A is 0.31.53 / ABI 12). But any consumer compiled against ABI 12 must be rebuilt. Document in release notes.

### Low risk
8. **Unused-symbol cleanup**. `drift_error_new_dummy`, `__exc_attrs_get` — verify zero callers before deleting.

9. **DV builtin reserved-names list** in spec §9.4. Already marked legacy; can be deleted.

10. **`docs/effective-drift.md`** has a section showing `e.attrs[k].as_int()` — needs rewrite to `e.params.get(k).as_int()`.

---

## 9. Sequencing (Suggested Order)

1. **Spec/ABI docs** — update first (banner ABI 13 final; remove DV public surface from spec; rewrite §14 user-facing examples).
2. **`Diagnostic` trait shape change** in `std.core` (`to_diag → to_json_text -> String`).
3. **Stdlib `Diagnostic` impl rewrites** (12 modules + 5 builtins).
4. **`std.log.Debuggable` migration** (if going).
5. **HIR→MIR throw-side rewrite** (replace `_dv_to_json_text` projection with direct `to_json_text` calls); audit ownership.
6. **HIR→MIR `_emit_captured_locals` rewrite** (same).
7. **Type-checker rejection diagnostics** for `e.attrs[k]` / `e.captures[fr][k]` / `to_diag` impls.
8. **MIR op deletions** (cascading — codegen, string_arc, dispatch removals).
9. **Runtime header/struct reshape** (DriftError field removals).
10. **Runtime helper deletions**.
11. **Codegen LLVM declarations cleanup** (drop DV helper declarations).
12. **`std.core` exports cleanup** (remove `DiagnosticEntry`, `diagnostic_entry`).
13. **Test migration**: rewrite positive tests, add negative tests, delete obsolete.
14. **ABI stamp** + version bumps.
15. **Full regression matrix**.

Total scope estimate: significant — multi-day implementation. ~40–60 file edits across stdlib + compiler + runtime + tests.

---

## 10. What I Need From You Before Starting

1. **Confirm decision 2.1**: `to_json_text` trait (recommended) vs direct projection?
2. **Confirm decision 2.2**: keep DV internal (recommended) vs delete completely?
3. **Confirm decision 2.3**: keep trait name `Diagnostic` (recommended) vs rename?
4. **Confirm 2.2 sub-question**: `std.err:ResultError(dv = DiagnosticValue)` stays as internal carrier (recommended) vs migrate to String payload?
5. **Confirm `std.log.Debuggable` disposition**: migrate `to_debug → to_json_text` alongside (recommended) vs leave for separate track?
6. **Confirm sequencing**: top-down spec/trait-first (recommended) vs runtime-first?

Once confirmed, plan is committed and execution begins in the order above. Pure preflight at this point — no code changed.

---

## 11. Review Notes (Codex, 2026-05-03)

**Review status:** preflight is useful and directionally right, but do not start implementation until the blockers below are resolved. The largest risk is not deleting code; it is accidentally claiming "DV is gone" while retaining a public or package-visible path that still names `DiagnosticValue`.

### 11.1 Blockers to settle before edits

1. **`ResultError(dv: DiagnosticValue)` cannot stay public if DV is removed from user surface.**

   The preflight recommends keeping `std.err:ResultError(dv = DiagnosticValue)` as an internal carrier. That is only viable if the type and constructor are not nameable through public APIs or package signatures. If `ResultError` remains a public exception with a public/constructible `dv: DiagnosticValue` field, then DV is still public, even if `DiagnosticEntry` and `e.attrs` are gone.

   Required decision:
   - Either migrate `ResultError` to a JSON-text payload in Slice 5, or
   - make `ResultError` and/or its `dv` field genuinely internal to stdlib/compiler use with a proven visibility mechanism and negative tests showing user code cannot name or construct the DV path.

   "Keep DV internal" needs a concrete enforcement mechanism, not just docs. I did not see a general `@internal` surface in current Drift source; private-by-module exists, but a public exception field of a private/internal type may still leak through exports unless explicitly rejected.

2. **Raw `to_json_text() -> String` needs a safe user-authoring story.**

   Keeping the trait name `Diagnostic` and changing the method to `to_json_text(&Self) -> String` is reasonable, but returning raw JSON text means user impls can produce invalid JSON and corrupt `e.encode_compact()` by splicing bad text. More importantly, user-defined types need a sanctioned way to quote string fields without importing `std.json` or duplicating escape logic.

   Required decision:
   - Export a small helper from `std.core` for JSON value construction/escaping, e.g. `diagnostic_json_string(&String) -> String`, plus simple helpers for null/bool/number if useful; or
   - explicitly make `to_json_text` an expert/unsafe-style contract where malformed JSON is caller responsibility.

   Recommendation: add a small public `std.core` helper surface for JSON text literals. Keep it text-only and independent of `std.json`.

3. **`std.log.Debuggable` is public DV surface unless migrated or scoped internal.**

   The preflight marks `std.log.Debuggable` as orthogonal, but it returns `DiagnosticValue` and is used by public logging APIs accepting `HashMap<String, DiagnosticValue>`. If next release's objective is "no DV in user code", this cannot remain as-is.

   Required decision:
   - Include `std.log` migration in Slice 5, or
   - explicitly defer it and accept that DV remains public through logging APIs.

   Recommendation: migrate logging's projection trait and public attr maps to JSON text in Slice 5 if the release objective is truly "no public DV".

4. **ABI 13 `stack` field is proposed but not present today.**

   Current `DriftError` has no `stack` field; `e.encode_compact()` emits `"stack":null` directly. Adding `void *stack` in ABI 13 is a new layout/ownership surface without a current producer/consumer.

   Required decision:
   - Add `stack` to ABI 13 now as a reserved null/opaque slot and define release semantics, or
   - keep ABI 13 layout to `{ code, event_fqn, params_json, context_json }` and leave stack as a computed `null` envelope field until the stack track lands.

   Recommendation: do not add a runtime `stack` field in Slice 5 unless stack capture also lands. Keep the envelope key `"stack": null`; do not grow the C layout for unused state.

5. **Compiler deletion list must distinguish "public DV removed" from "internal DV retained".**

   If `ResultError` or `std.log` keeps any internal DV use, then `M.ConstructDV` and parts of the DV runtime/codegen cannot be deleted yet. The preflight currently lists DV op deletion conditionally in places, but the execution list should be made explicit after the internal-DV decision.

   Required decision:
   - If DV is fully deleted: remove `M.ConstructDV`, DV constructors, DV runtime helpers, and all stdlib uses.
   - If DV is internal-retained: delete only public-read/exception-storage ops (`ErrorAddAttrDV`, `ErrorAddLocalDV`, `ErrorAttrsGetDV`, `ErrorCapturesGetDV`, DV accessor methods used by `e.attrs[...]`), and keep the minimal constructor/drop/clone path needed by stdlib internals.

### 11.2 Sequencing adjustments

Recommended order after decisions:

1. Lock the public contract: `Diagnostic` trait shape, `ResultError` disposition, `std.log` disposition, and ABI 13 layout.
2. Add failing negative tests for removed public DV surfaces before deleting implementation:
   - `DiagnosticValue::Int(...)` from user code.
   - `DiagnosticEntry` / `diagnostic_entry` from user code.
   - user `to_diag(...) -> DiagnosticValue` impl.
   - `e.attrs[...]` and `e.captures[...]`.
   - public `std.err:ResultError(dv = ...)` if it is intended to be internal.
3. Add positive tests for replacement projection:
   - custom user type implementing `Diagnostic.to_json_text`.
   - custom type returning JSON `null`.
   - custom type returning an object/array text, proving params splice as JSON value, not quoted string.
4. Migrate stdlib projection traits and throw lowering.
5. Remove legacy exception storage/helpers and bump ABI to 13.
6. Clean obsolete DV tests/docs.

This order keeps the LANGUAGE_BUG/regression-first discipline intact and prevents a large deletion pass from masking missing diagnostics.

### 11.3 Review answers to K's six questions, pending user confirmation

1. **Projection trait:** yes to `Diagnostic.to_json_text(&Self) -> String`, but only with a small public `std.core` JSON-text helper surface or an explicit "caller must return valid JSON" contract.
2. **Internal DV:** acceptable only if internal means enforced. Public `ResultError(dv: DiagnosticValue)` is incompatible with the stated no-public-DV release goal.
3. **Trait name:** keeping `Diagnostic` is reasonable. It avoids renaming every concept while still changing the method name away from `to_diag`.
4. **`ResultError` carrier:** do not rubber-stamp keeping the DV carrier until visibility/package leakage is proven. Prefer migrating it to JSON text unless that is too large.
5. **`std.log.Debuggable`:** migrate in Slice 5 if release objective is no public DV. Otherwise document that DV survives in logging and the objective is narrower than "no DV".
6. **Sequencing:** top-down contract/tests first, then implementation. Runtime-first would create churn without pinning the user-visible breakage diagnostics.

---

## 12. K Sign-Off + Scope Expansion (2026-05-03)

K confirmed all six decision points and substantially expanded the slice. The dominant new direction is the introduction of `pub error` as a first-class language concept; Slice 5 is therefore primarily a **language/API migration**, not a runtime substrate patch.

### 12.1 Confirmed decisions (with K's reasoning)

**Decision 2.1 — Projection trait shape: CONFIRMED `Diagnostic.to_json_text(&Self) -> String`, with a NEW public `std.core` JSON-text helper surface.**

- Direct compiler-only projection rejected as too closed for user-defined exception field types.
- Trait return type is canonical JSON value text — must be a complete JSON value (`42`, `true`, `"escaped"`, `null`, `{...}`, `[...]`).
- **NEW:** Slice 5 must add a small sanctioned `std.core` helper surface so users don't hand-roll JSON escaping:
  - `pub fn diagnostic_json_string(s: &String) nothrow -> String` — minimum viable.
  - `pub fn diagnostic_json_null() nothrow -> String`.
  - `pub fn diagnostic_json_bool(v: Bool) nothrow -> String`.
  - (Future: `diagnostic_json_field(key, value_json)` if useful.)
- Helper surface is text-only; no `std.json` dependency, no `JsonNode`.

**Decision 2.2 — DV disposition: CONFIRMED prefer DELETE COMPLETELY; only temporary unreachable scaffolding may remain, deletion-ledgered.**

- "Keep DV internal forever" rejected: still leaks DV as a supported model.
- Any internal remainder must be (a) non-user-nameable, (b) non-emitted for new exception paths, (c) deletion-ledgered, (d) targeted for removal in the immediate follow-up. This is a temporary state during the multi-step migration, not a long-term posture.
- The §11.1 blocker about "internal-DV needs enforced visibility" is resolved by deleting completely rather than enforcing visibility.

**Decision 2.3 — Trait name: CONFIRMED keep `Diagnostic`.**

- Rationale: trait name describes the role ("how do I describe this value as JSON diagnostic data?"), not the transport. With `pub error ParseError { ... }`, `ParseError` is the error datatype; `Diagnostic` is the projection capability.
- Renames rejected: `JsonProjectable` / `JsonDiagnostic` overfit current representation.
- What changes is the method/signature shape (`to_diag → to_json_text`) and the return type (`DiagnosticValue → String`), NOT the trait name.

**Decision 2.4 — `ResultError` disposition: CONFIRMED demoted to fallback adapter; primary path is `or_throw()` throws the concrete error type.**

- Rejected: keeping `ResultError(dv: DiagnosticValue)` as either public or internal carrier.
- Rejected: naively migrating to `ResultError(error: String)` — would force structured JSON to be escaped as a string literal.
- **Preferred runtime model:** `Result<T, ParseError>.or_throw()` throws `ParseError` directly, so catch-by-type routing remains precise: `catch ParseError(e) { ... }`.
- If `ResultError` survives at all, it is generic-fallback only and (if kept) must hold its payload as JSON value text — not a quoted string. Public APIs should be `throw_result_error(err: impl Diagnostic)` or `ResultError::from_diagnostic(&err)`; users should NOT have to construct it directly.
- Slice 5 plans for `ResultError` to be a fallback adapter; user-facing migration steers everyone onto `pub error` + concrete-type catch.

**Decision 2.5 — `std.log.Debuggable`: CONFIRMED migrate in Slice 5, but as a separate trait with its own method name.**

- Leaving `Debuggable` returning `DiagnosticValue` would keep DV public via logging, contradicting the "no DV in user code" objective.
- Do NOT merge into `Diagnostic`. Diagnostic projection (errors) and debug projection (logs) MAY diverge on redaction rules — keep them separate so the contract can evolve independently.
- **Trait shape:**
  ```drift
  pub trait Debuggable {
      fn to_debug_json_text(self: &Self) nothrow -> String;
  }
  ```
- Method name `to_debug_json_text` (not `to_json_text`) — slight preference to avoid dispatch ambiguity if a type implements both, AND signals the different audience.

**Decision 2.6 — Sequencing: CONFIRMED top-down spec/trait-first.**

- Runtime-first sequencing rejected for Slice 5: it would preserve the wrong concepts (`ResultError`, `DiagnosticValue`, throw-side DV projection) too long.
- Confirmed order:
  1. Spec + public contract.
  2. Failing positive tests for the new public model.
  3. Failing negative tests for removed DV surfaces.
  4. Trait / stdlib migration.
  5. Compiler checker / lowering support.
  6. Runtime / helper deletion + ABI 13 bump.
  7. Cleanup docs / history / memory.

K is firm: "Slice 5 is primarily a language/API migration, not a runtime substrate patch."

### 12.2 NEW: `pub error` language direction

**This is the largest scope expansion.** The user-facing model shifts from "ResultError as the main bridge" to "error datatypes are the bridge."

**Canonical declaration:**
```drift
pub error ParseError {
    message: String,
    offset: Int,
}
```

**Semantic rules:**
1. `pub error` is a normal value type. It can be constructed, copied (per its field types), passed around without throwing.
2. Usable as the `Err` type in `Result<T, ParseError>`.
3. **Throwable** when needed; **catchable by type** when thrown:
   ```drift
   try { ... } catch ParseError(e) { ... }
   ```
4. **Knows how to be exception-friendly:** synthesized JSON projection by default, when all fields are JSON/diagnostic-projectable. Manual `Diagnostic` impl override is the escape hatch.
5. **`Result<T, ParseError>.or_throw()` throws `ParseError` directly** — NOT `ResultError(error = ...)`. Catch routing stays precise.
6. **Synthesis only works** when all fields are JSON/diagnostic-projectable. Otherwise the compiler emits a targeted error directing the user to either make the field projectable or implement the manual projection.

**Design rules for the spec draft (K-pinned):**

1. **`pub error` is the canonical declaration keyword.** `pub exception` may be a transitional alias / deprecated spelling for one release if it reduces migration risk, but the long-term user-facing concept is `error`.
2. **`Result<T, E>` should require `E` to be an error datatype** (long-term preference: strict). At minimum, `.or_throw()` must require it during the transition.
3. **Default JSON projection is synthesized** for `error` types. Canonical field ordering. For `pub error ParseError { message: String, offset: Int }`, default `params` is `{"message":"...","offset":12}`.
4. **Synthesis fails closed:** non-projectable fields → targeted compile error.
5. **Manual projection remains the escape hatch:** users can use `std.json` inside their `to_json_text` impl to build any structure, then return serialized JSON text. No `std.json` / `JsonNode` crosses the runtime boundary — only the final `String`.
6. **Runtime/storage boundary unchanged:** canonical JSON String only. No JsonNode, no JsonObject, no JsonHandle in the runtime/C envelope.
7. **`ResultError` is demoted.** Keep only as a generic adapter or compatibility surface; do not build Slice 5 around users writing `ResultError(error = ...)`. No `_trusted`-style JSON constructors as normal user API.

**Envelope semantics clarified:**

For a thrown value of:
```drift
pub error ParseError { message: String, offset: Int }
```

with code `12345`, the envelope is:
```json
{
  "event_code": 12345,
  "event_fqn": "my.pkg:ParseError",
  "params": {"message":"bad input","offset":12},
  "context": [],
  "stack": null
}
```

- `event_fqn`: fully-qualified error type name — which error happened.
- `event_code`: compact stable routing identity for catch dispatch.
- `params`: fields of the error value.
- `context`: captured `^`-unwind frames.
- `stack`: deferred / backtrace slot (kept null per Codex §11.1.4 — do not grow C layout for unused state).
- ABI/runtime field name `event_*` stays even though docs may eventually use "error_fqn" / "error_code" wording. Renaming is not worth the churn.

### 12.3 Codex blockers — resolution status

- **§11.1.1 (ResultError public DV):** RESOLVED by 2.4 + `pub error` migration. ResultError demoted; primary path uses concrete error types so DV doesn't leak through it.
- **§11.1.2 (raw to_json_text → String safety):** RESOLVED by 2.1 helper surface. `diagnostic_json_string` / `diagnostic_json_null` / `diagnostic_json_bool` give users sanctioned escape primitives.
- **§11.1.3 (Debuggable public DV):** RESOLVED by 2.5. Migrated in Slice 5 as a separate trait with `to_debug_json_text -> String`.
- **§11.1.4 (ABI 13 stack field):** RESOLVED — defer. Keep ABI 13 layout to `{ code, event_fqn, params_json, context_json }`; envelope key `"stack": null` continues to be emitted as a literal in `e.encode_compact()`.
- **§11.1.5 (DV op deletion list — public vs internal):** RESOLVED by 2.2 (delete completely). All DV ops are removable; remaining transitional scaffolding (if any) is deletion-ledgered for the immediate follow-up.

### 12.4 Surface impact summary (post-K decisions)

| Surface | Pre-Slice-5 | Post-Slice-5 |
|---|---|---|
| Error datatype declaration | `pub exception E { ... }` | `pub error E { ... }` (canonical); `pub exception` transitional alias for one release |
| `Result<T, E>.or_throw()` | wraps as `ResultError(dv = ...)` | throws `E` directly |
| Catch routing | `catch ResultError(e) { ... }` + DV pattern-match on `e.attrs[...]` | `catch E(e) { ... }` precise by type |
| `Diagnostic` trait | `fn to_diag(&Self) -> DiagnosticValue` | `fn to_json_text(&Self) nothrow -> String` |
| Default error JSON | none (manual `to_diag` impl required) | synthesized when all fields projectable |
| `Debuggable` trait | `fn to_debug(&Self) -> DiagnosticValue` | `fn to_debug_json_text(&Self) nothrow -> String` |
| `e.attrs[k]` | DV reader | REMOVED — `e.params.get(k).as_*()` |
| `e.captures[fr][k]` | DV reader | REMOVED — `e.context.encode_compact()` (Slice 5); `e.context.get(...)` (Slice 4B, deferred) |
| `DiagnosticValue` (user code) | public type | REMOVED — type-checker rejects |
| `DiagnosticEntry` / `diagnostic_entry` | public type + helper | REMOVED |
| `ResultError` | universal catchall with `dv: DiagnosticValue` | demoted; generic fallback adapter only |
| `std.core` JSON helpers | none (DV builders) | NEW: `diagnostic_json_string`, `diagnostic_json_null`, `diagnostic_json_bool` |
| ABI | 12 (additive layout: code, event_fqn, JSON fields + legacy DV fields) | **13** — drops legacy DV fields entirely |
| Compiler version | 0.31.53 | **0.32.0** (public API break) |

---

## 13. Next Deliverable: Spec Draft (awaiting K's go)

Per the confirmed top-down sequencing (§12.1 / 2.6), the next concrete artifact is a **`pub error` + Diagnostic spec draft**, not implementation code. The draft is the contract that subsequent test work (positive + negative) and implementation hangs off of.

**Proposed spec-draft structure** (would live at `work/exception-diagnostics-context/slice5-spec.md` or directly in `docs/design/spec-change-requests/drift-error-diagnostic-refactor.md` extension):

1. **`pub error` declaration syntax** — grammar additions; alias status of `pub exception`.
2. **Type semantics** — `pub error` as a value type; constructor; copy/move; storage class; throwability.
3. **`Result<T, E>` constraint** — `E: Error`-trait or compiler-recognized `pub error` (decide which).
4. **Catch routing** — `catch E(e) { ... }` semantics; how the runtime dispatches on `event_fqn` / `event_code`.
5. **`or_throw()` semantics** — direct throw of `E`; ResultError fallback path (if any).
6. **`Diagnostic` trait** — trait definition; method `to_json_text`; built-in impls for primitives; user impl rules; canonical JSON-value contract.
7. **Synthesized JSON projection** — when synthesis fires; field ordering; non-projectable-field error (`E_PUB_ERROR_FIELD_NOT_PROJECTABLE` or similar).
8. **`std.core` JSON-text helpers** — exact public surface; nothrow contract; escape rules.
9. **`Debuggable` trait** — trait definition; method `to_debug_json_text`; how it differs from `Diagnostic`.
10. **`ResultError` disposition** — final shape; fallback usage rules; deprecation timeline.
11. **`std.log` migration** — public attr maps; `HashMap<String, DiagnosticValue>` → `HashMap<String, String>` (JSON text).
12. **Envelope shape** — restated for `pub error`; event_fqn = fully-qualified type name; event_code stable routing.
13. **Removed surfaces** — full list with diagnostic codes (`E_DV_PUBLIC_REMOVED`, `E_DIAG_DV_RETURN_TYPE_REJECTED`, `E_PUB_EXCEPTION_DEPRECATED` if applicable, etc.).
14. **Migration guide** — minimum keystrokes to migrate a typical `pub exception E { ... }` + `to_diag` impl into `pub error E { ... }` (with examples).
15. **ABI 13** — final DriftError layout; stamp regeneration.

**Open spec questions that need K's resolution before drafting:**

1. **`pub exception` alias status:** is it (a) a hard-deprecated alias for one release with a warning, (b) a compile error in Slice 5, or (c) silently rewritten to `pub error`? Recommendation: option (a) — release 0.32.0 emits a deprecation warning; option (b) at 0.33.0.
2. **`Result<T, E>` constraint enforcement:** strict-now or transition? K's words: "long-term preference is the stricter rule." Concretely: does Slice 5 enforce `E: pub-error-or-Error-trait` for ALL `Result<T, E>` instances, or only for `or_throw()` call sites (the looser transitional rule)?
3. **`Error` trait existence:** does `pub error` desugar to `struct E { ... } implement Error for E { ... }`, or is `pub error` a distinct kind in the type system? Suggest desugar — keeps the type system simpler — but K may have a view.
4. **Synthesis algorithm pinning:** lex-utf8 field ordering (consistent with throw-side params today)? Or declaration order? Suggest lex-utf8 for byte-identical reproducibility with the current substrate.
5. **`pub error` field type set for synthesis-success:** primitives (Int/Uint/Bool/Float/String) + any `T: Diagnostic`? Or does composition of `pub error` count (a field of type `pub error E2`)? Suggest `T: Diagnostic` is the composability rule; `pub error` types automatically satisfy it via synthesized impl.
6. **Catch-by-supertype:** can a `catch SomeMarkerTrait(e)` catch any thrown `pub error` implementing the marker? Out of Slice 5 scope, but the spec should explicitly state the deferral.
7. **ResultError fallback shape:** if `ResultError` survives at all, is it `pub error ResultError { error_json: String }` (with a JSON-value-text contract on the field), or is it removed entirely and replaced by per-call-site coercion to a synthesized `GenericError`? Recommendation: keep as a deprecated `pub error` with a JSON-text payload; drop in 0.33.0.

**My recommended next step:** I draft a v0 of the spec at `work/exception-diagnostics-context/slice5-spec.md` covering items 1–15 above with my answers to the 7 open questions inline, marked clearly as "DRAFT — awaiting K". K reviews the spec; once locked, we proceed to step 2 of K's confirmed sequencing (failing positive tests).

**No code edits to the live tree** until K signs off on §13 — confirms or revises the spec draft direction. Same regression-first / spec-first discipline that worked for Slices 1–4A.
