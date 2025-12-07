## Real type checker plan — lang2

### Goal
Replace the legacy string/tuple/type-guess shims with a real front-end type checker that produces TypeIds and `FnSignature`s, and drives call/type/throw checks off that single source of truth.

### Baseline (current)
- Checker stub prefers TypeIds in `FnSignature` (`param_type_ids`, `return_type_id`, `declared_can_throw`, flags) and falls back to legacy raw shapes only when ids are missing.
- `FnInfo` owns `signature` and an `inferred_may_throw` flag; checker does a shallow HIR walk to mark may-throw and emit missing-throws diagnostics.
- Try-sugar validation uses `TypeKind.FNRESULT` against `signature.return_type_id`.
- No real type resolution yet; signatures are still often built from raw strings/tuples via stub resolution.

### Planned work
1. **Minimal type resolver** ✅
   - Added `lang2/type_resolver.py` to resolve declared types (`Int`, `Bool`, `String`, `Error`, `FnResult<...>`) into a shared `TypeTable` and build `FnSignature` with `param_type_ids`, `return_type_id`, `declared_can_throw`, `loc`, flags. Raw fields remain for legacy/tests.

2. **Driver integration** ✅ (stub)
   - `compile_stubbed_funcs` now calls the resolver (using a fake decl shim) when no signatures are supplied, so tests get TypeIds without hand-built signatures. A real front end should supply decls with parsed types/throws.

3. **Checker consumption** ⏳
   - Prefers TypeIds in signatures; falls back only when missing. Try-sugar checks use TypeIds. `_is_fnresult_return` still exists as a legacy fallback until all callers supply TypeIds.
   - Helper `check_call_signature` added (not yet wired) to enforce arity/type via `FnInfo.signature` + TypeIds.

4. **Call/arity/type checks**
   - Add a `check_call` helper that consults `FnInfo.signature` + `TypeTable` to enforce arity, set call result types, and mark throwing calls. Wire this into the appropriate pass once expression typing is available.

5. **Diagnostics**
   - Centralize throw diagnostics: after body walk, emit “must be declared throws” when `inferred_may_throw && !declared_can_throw`; optional warning on “declared throws but no throw”.
   - Ensure call-site throw checks use `call_may_throw` derived from `FnInfo.signature`.

### Deferred
- Full expression/type inference and unification.
- Real exception catalog integration with spans.
- Removal of legacy raw type fields and bool-map shims once the new checker is in place.
