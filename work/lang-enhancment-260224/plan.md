# Plan: language enhancements from crypto implementation feedback (2026-02-25)

Owner: Klaudia
Order: 1 -> 2 -> 3 (strict)
Mode: regression-first for LANGUAGE_BUG items

## Context
While implementing `sha256`/`hmac_sha256`/`constant_time_eq`, three language/toolchain gaps were identified:
1. Checker does not infer `Uint` result types for binary operations.
2. Checker does not consistently resolve `match` binders to payload types.
3. No const/static array support (ergonomic/perf gap for lookup tables).

We will address all three in this branch, in the exact order above.

---
## Item 1 (High): Uint binary-op inference in checker

### Problem
Checker inference handles `Int op Int -> Int` but misses `Uint op Uint -> Uint` in binary op inference flow, forcing explicit annotations/casts in valid `Uint` code.

### Classification
LANGUAGE_BUG

### Required sequence
1. Add minimal failing regression(s) first.
2. Confirm failure on current behavior.
3. Implement checker fix.
4. Confirm regressions pass.
5. Run targeted suite.

### Regressions to add
- Driver tests for inferred type through:
  - shift: `val r = u >> cast<Uint>(n)`
  - bitwise: `val x = a | b`, `a & b`, `a ^ b`
  - arithmetic used in crypto path: `+` and `-` (where currently legal for Uint)
- One end-to-end smoke in codegen/e2e using chained Uint expressions without explicit annotations.

### Implementation guidance
- Update checker binary-op type inference branch to include `Uint` parallel to existing `Int` logic where semantics are already supported.
- Do not widen semantics beyond existing legal operators.
- Keep diagnostics for illegal op combinations unchanged.

### Guardrails
- No stdlib workaround edits to hide checker behavior.
- Ensure boundary consistency (checker -> stage2 -> MIR validate -> LLVM lowering) for shapes touched by inferred types.

---
## Item 2 (High): match binder payload type propagation

### Problem
In `match` over variants/results, binder names (e.g. `core.Result::Ok(vals)`) are not consistently typed in checker context. This causes valid payload operations (e.g. indexing Array payload) to fail with unknown-name/type errors.

### Classification
LANGUAGE_BUG

### Required sequence
1. Add minimal failing regression(s) first.
2. Confirm failure.
3. Fix checker binder typing.
4. Confirm pass.
5. Run targeted suite.

### Regressions to add
- Driver test: `match Result::Ok(vals)` where `vals: Array<Byte>` and direct indexing is used.
- Driver test: nested match binder payload typing (Result inside Result or variant payload path).
- Negative test: binder misuse still reports clear checker diagnostic (no internal errors).
- Optional e2e: reproducer modeled on codec decode + index access path.

### Implementation guidance
- Ensure binder type is derived from scrutinee arm payload type and injected into local typing context for arm scope.
- Ensure lexical scope remains correct (binder only inside its arm).

### Guardrails
- Keep ownership/borrow diagnostics stable.
- No test rewrites that avoid binders just to pass.

---
## Item 3 (Medium): const/static arrays (lookup table support)

### Problem
Lack of const/static composite arrays forces large branch chains (e.g., SHA-256 constants), hurting readability and performance.

### Classification
Language enhancement (not blocking correctness)

### Scope (MVP for this item)
Support read-only constant arrays usable from function/module scope for index-based lookup.

### Design constraints
- Immutable only.
- Element types: start with scalar/byte-friendly types needed for tables (`Byte`, `Int`, `Uint`, maybe `Uint64` if already stable).
- No runtime mutation; no `&mut` borrow of const arrays.
- Deterministic compile-time initialization only.

### Required sequence
1. Write design note + explicit supported/unsupported matrix.
2. Add positive and negative regressions before implementation.
3. Implement parser/checker/stage2/codegen path.
4. Validate end-to-end and docs.

### Regressions to add
Positive:
- module-level const array lookup by index.
- function-local const array lookup reuse at multiple call sites.
- crypto-table-like lookup smoke.

Negative:
- non-literal/non-const initializer rejected.
- mutation attempts rejected.
- unsupported element types rejected with clear diagnostics.

### Guardrails
- Must not contradict existing const semantics docs/tests.
- Update contract comments/messages/tests where behavior expands.

---
## Cross-cutting validation checklist (mandatory)
For each item before marking done:
1. Minimal failing regression added and pinned.
2. Root-cause compiler fix landed.
3. Regression now passes.
4. At least one negative contract test present (clear diagnostic).
5. No workaround-driven stdlib edits to mask compiler defects.
6. Boundary contract guardrails updated where type/boundary behavior changed.

---
## Item 4 (Post): Remove stdlib workarounds using new compiler enhancements

### Problem
The crypto/codec stdlib code written before Items 1-3 contains workarounds for compiler limitations that should be cleaned up once the fixes land:
- Explicit `: Uint` type annotations on val bindings in `_ror32` (workaround for Item 1).
- `codec.hex_encode(&vals)` comparison instead of direct `vals[index]` in e2e tests (workaround for Item 2).
- 64-branch `_sha256_k(i: Int)` if-chain instead of const array lookup (workaround for Item 3).

### Classification
Cleanup (depends on Items 1-3)

### Required sequence
1. After Items 1-3 land and full farm is green.
2. Remove explicit `: Uint` annotations from `_ror32` in `crypto.drift` — verify inference handles it.
3. Replace `_sha256_k` if-chain with `const K: Array<Uint> = [...]` lookup.
4. Optionally update e2e tests to use direct match binder indexing where it improves clarity.
5. Re-run all crypto/codec e2e tests.

### Guardrails
- No functional changes — output must be byte-identical for all vectors/round-trips.
- Each cleanup verified independently before moving to next.

---

## Suggested execution checkpoints
- Checkpoint A: Item 1 complete + targeted tests green.
- Checkpoint B: Item 2 complete + targeted tests green. **Hard stop** — full farm run + snapshot.
- Checkpoint C: Item 3 complete + targeted tests green + docs synchronized.
- Checkpoint D: Item 4 complete + crypto/codec e2e green.

## Out of scope
- General-purpose constexpr evaluator beyond const-array initialization needed here.
- Broader generic constant containers beyond planned MVP.
