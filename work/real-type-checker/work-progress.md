## Real type checker plan — lang2

### Goal
Replace the legacy string/tuple/type-guess shims with a real front-end type checker that produces TypeIds and `FnSignature`s, and drives call/type/throw checks off that single source of truth.

### Baseline (current)
- Checker stub prefers TypeIds in `FnSignature` (`param_type_ids`, `return_type_id`, `declared_can_throw`, flags) and falls back to legacy raw shapes only when ids are missing. Guard prevents TypeId-carrying signatures without the matching shared `TypeTable`; string TypeId is shared across HIR/SSA now.
- `FnInfo` owns `signature` and an `inferred_may_throw` flag; checker does a shallow HIR walk to mark may-throw and emit missing-throws diagnostics.
- Try-sugar validation uses `TypeKind.FNRESULT` against `signature.return_type_id`.
- Minimal resolver supplies TypeIds for declared param/return types via `TypeTable`; legacy string/tuple paths remain only for old shims.

### Planned work
1. **Minimal type resolver** ✅
   - Added `lang2/type_resolver.py` to resolve declared types (`Int`, `Bool`, `String`, `Error`, `FnResult<...>`) into a shared `TypeTable` and build `FnSignature` with `param_type_ids`, `return_type_id`, `declared_can_throw`, `loc`, flags. Raw fields remain for legacy/tests. Parser adapter now builds decls from `FunctionDef` (params/return) and feeds them into the resolver; auto-resolution is used when no signatures are supplied.

2. **Driver integration** ✅ (stub)
   - `compile_stubbed_funcs` now calls the resolver (using a fake decl shim) when no signatures are supplied, so tests get TypeIds without hand-built signatures. A real front end should supply decls with parsed types/throws.
   - The shared `TypeTable` from the resolver (or caller) is threaded into `Checker` even when signatures are provided, so `TypeId`/`TypeKind` queries stay coherent end-to-end. Guard added: signatures carrying TypeIds must be accompanied by the shared TypeTable.

3. **Checker consumption** ✅ (core plumbing)
   - Prefers TypeIds in signatures; falls back only when missing. Try-sugar checks use TypeIds. `_is_fnresult_return` still exists as a legacy fallback until all callers supply TypeIds.
   - Helper `check_call_signature` added; HIR walk enforces call arity for plain calls via `FnInfo.signature`. Arg type checks are deferred until expression typing exists.
   - `parse_drift_to_hir` now returns the shared `TypeTable` so Drift-source paths can thread it into `Checker`, keeping TypeId/TypeKind coherent.

4. **Call/arity/type checks**
   - `check_call_signature` exists and is wired over HIR calls; arity is enforced and basic param-type equality is attempted when arg types can be inferred (literals/simple calls/Result.Ok). Full expression typing still deferred.

5. **Diagnostics**
   - Centralize throw diagnostics: after body walk, emit “must be declared throws” when `inferred_may_throw && !declared_can_throw`; optional warning on “declared throws but no throw”.
   - Ensure call-site throw checks use `call_may_throw` derived from `FnInfo.signature`.

### Deferred
- Full expression/type inference and unification.
- Real exception catalog integration with spans.
- Removal of legacy raw type fields and bool-map shims once the new checker is in place.
