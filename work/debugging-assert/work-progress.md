# Debugging + Assert Work Progress

## Scope (from spec)
- Add always-on `assert(cond)` and `assert_msg(cond, msg)` that are nothrow and never compiled out.
- On failure: print message + file:line + Drift stack trace, then abort.
- Requires real debug info (no “native-only backtrace”).

## Decisions to Lock
- Debug info strategy: **Option A (DWARF)**.
- Assert lowering: expand `assert(cond)` → `std.core.assert_loc(cond, file, line)` (frozen).
- Output format: minimal but stable (assertion failed + optional msg + file:line + stack trace).

## Implementation Plan

### 0) Choose debug info strategy
- **Done:** DWARF (Option A) + libdw/libunwind.

### 1) Compiler: emit debug metadata
- Record function ranges and line mappings during codegen.
- Emit line tables + function symbols in LLVM.
- Ensure required flags on IR module (debug info version, etc.).
- Use LLVM DIBuilder (or equivalent) to attach:
  - `DICompileUnit`, `DIFile`
  - `DISubprogram` per function
  - `DILocation` per instruction (at least on call sites + asserts)
- Wire `-g`-style emission path through `driftc` / codegen.
- Add a low-level test to assert `!dbg` locations and `DICompileUnit` exist.

### 2) Runtime: stack trace resolution
- Add runtime resolver using libdw + libunwind to resolve PCs.
- Provide `drift_debug_print_stacktrace()` runtime entry.
- Decide output format (function name + file:line, one frame per line).
- Add a small runtime unit test for resolver if feasible.
- Ensure stacktrace works in optimized builds (best effort; ok if some frames missing).

### 3) std.core assert surface
- Add `assert` and `assert_msg` declarations in std.core.
- Add `assert_loc(cond, file, line, msg?)` internal helper (compiler expansion target).
- Ensure `assert` is nothrow and always enabled (no debug/release branch).
- Define ABI for `assert_loc` (string literal + line number types).

### 4) Compiler lowering for assert
- Parser recognizes `assert(expr)` and `assert_msg(expr, msg)`.
- Lowering expands to `std.core.assert_loc` with file/line.
- Ensure file/line constants are correct for nested/inline call sites.
- Ensure `assert` is not optimized away even in release.

### 5) E2E tests
- `assert_basic_fail`: failing assert prints file:line and function name.
- `assert_call_chain`: nested calls show stack trace order.
- `assert_msg`: message included.
- Ensure tests pass in optimized builds (if applicable).
- Add driver test to confirm `assert` lowers to `assert_loc` with correct file/line.

## Notes / Risks
- Don’t ship assert without full debug info (spec requirement).
- Keep output stable to avoid brittle tests.
- Ensure stack trace respects inlining (best-effort acceptable).

## Progress
- [x] Decision on debug info strategy
- [ ] Compiler debug metadata emission
- [ ] Runtime stack trace resolver
- [ ] std.core assert surface
- [ ] Compiler assert lowering
- [ ] E2E tests

### Work Progress (Debugging Session)
- Diagnosed gdb stepping skip: line 7 missing from DWARF line table, so `next` jumped from line 6 to 8 and `p` was out of scope.
- Found `p` had `DILocalVariable`/`dbg.value` but no address-backed location, which triggered “optimized out” for aggregates.
- Fixes applied:
- Preserve return spans during string ARC rewriting. (`lang2/driftc/stage2/string_arc.py`)
- Emit debug keepalive storage for SSA locals so line-table entries exist on defining lines. (`lang2/codegen/llvm/llvm_codegen.py`)
- Emit `dbg.declare` for keepalive allocas so gdb can materialize structs. (`lang2/codegen/llvm/llvm_codegen.py`)
- Tests added:
- `lang2/codegen/llvm/tests/test_llvm_codegen_debug_return_span.py`
- `lang2/codegen/llvm/tests/test_llvm_codegen_debug_keepalive.py`
- Verified outcome: line 7 appears in `readelf --debug-dump=decodedline` and `gdb` can stop on line 7 and `print p` shows `Point {x: 6, y: 39, label: String}`.
- Added debug channels to trace type issues:
- `lambda_capture` logs capture ids and their types during capture analysis.
- `dbg_unknown_types` logs unknown types encountered in debug info emission.
- Investigation: UNKNOWN types are not from captures, but from temps created around try/call lowering (`__try_expr_tmp*`, `__try_err*`, `__call_ok*`) in the hidden lambda `__lambda_cb_main_0_0` at `examples/debug_1/main.drift:22`.
- Callsite logs show `__lambda_cb_main_0_0` records callsite info for `handle`, but the `user_ret_type` used for debug emission remains `UNKNOWN` at MIR/debug emission time.
- LLVM IR shows concrete return types for the lambda (e.g., `FnResult_Int_Error`), so the UNKNOWN types are likely a type propagation gap in MIR/type info rather than in codegen IR.
- Ran regression test to confirm current state: `lang2/tests/driver/test_ssa_load_before_store_regression.py::test_emit_ir_while_capture_move_in_loop_no_ssa_crash` (passed).
- Root cause found: hidden lambdas were not copying `lower._local_types` into `builder.func.local_types`, so later debug emission saw `UNKNOWN` for try/call temps. Fixed by syncing `local_types` for hidden lambdas and captureless lambdas (same as the normal function path).
- Added regression test: `lang2/tests/driver/test_hidden_lambda_local_types.py` to ensure try/call temps in hidden lambdas have concrete types.
- Debug IR failure root cause: explicit ref captures in hidden lambdas kept value types from origin bindings (`Cell`) instead of ref types from env field types, causing `llvm.dbg.value` type mismatches.
- Fix: for explicit ref captures (`captures(x)`), prefer env field ref types over origin binding value types when seeding hidden lambda binding types.
- Added regression test: `lang2/tests/driver/test_hidden_lambda_capture_local_types_ref.py`.
- New regression (e2e): `hashmap_iter_all` fails in clang with `llvm.dbg.value` type mismatch (`%t35` is `i1` but debug metadata expects `i64`).
- IR confirms `DILocalVariable(name: "done")` has type `Int` in `run`, while the value is a `Bool` (i1).
- Typechecker logs show `let done id=5 type=Bool fn=run`, so the mismatch is introduced after typecheck (local type propagation/debug metadata).
- HIR confirms `done` is initialized with `HLiteralBool(value=False)` at `main.drift:16`.
- Next step: trace where `func.local_types["done"]` becomes `Int` during lowering/debug emission (likely local-types propagation path, possibly around bool literal handling or post-ARC local type fill).
- Added `local_types_trace` debug channel (narrow) to log when `done` local type is set in HIR->MIR (`HLet`/`HAssign`) and when post-ARC `StoreLocal` backfills types. Use `DRIFT_DEBUG='{\"local_types_trace\": true}'`.
- `local_types_trace` now also logs when `HLiteralBool` is mapped to a non-Bool type via `_expr_types` in `_infer_expr_type`.
- `local_types_trace` now logs if the type checker records a non-Bool type for an `HLiteralBool` (with node_id/span).
- `local_types_trace` now also prints `HLiteralBool` node ids + types from the typed HIR just before lowering (to verify expr_types alignment).
- `local_types_trace` now detects duplicate `node_id` usage across HIR expressions (logs both kinds/spans).
- `local_types_trace` now validates duplicate expr `node_id`s right after typechecker rewrites (`_apply_fnptr_consts`) to pinpoint where ids diverge.
- `local_types_trace` now also validates duplicate expr `node_id`s immediately after `normalize_hir` (pre-typecheck) for `main::run`.
- Added `local_types_trace` scan at `post_callinfo` (after callsite alignment/validation) to narrow when duplicate `node_id`s first appear; pending rerun to see whether duplicates show up there vs. later (currently only seen at `pre_lowering`).
- Rerun `hashmap_iter_all` confirms duplicates already appear at `post_callinfo` (e.g., node_id 59 `HLiteralBool` vs `HBinary`), so corruption happens during callsite alignment/validation (or immediately adjacent code in that block).
- Added `scan=post_checker` after `Checker.run_by_id`; rerun shows duplicates already present there. This pins the corruption to `Checker.run_by_id` (likely its `Checker.__init__` path that calls `_normalize_and_collect_catch_arms`, which runs `normalize_hir` and reassigns node_ids on shared HIR).
- Verified `pre_checker_body_shared=True` for `main::run`, meaning `typed_fn.body` is the same object as `normalized_hirs_by_id[fn_id]`. This makes `Checker`’s internal `normalize_hir` call a likely source of node_id corruption (partial renumbering via shared nodes).
- Updated `Checker._normalize_and_collect_catch_arms` to skip `normalize_hir` and collect catch arms directly, avoiding mutation of shared HIR during checker initialization.
- Provenance metadata idea (pending approval):
- Add an optional side table keyed by `TypeId` or by `ExprId/CallsiteId` that records the phase/stage that determined the type (e.g., `typecheck`, `instantiation`, `mir_infer`, `codegen`, `debug_patch`) plus confidence and optional source span.
- Keep this separate from `TypeId`/TypeDef hashing to avoid breaking interning or equality. This is a future enhancement, not yet implemented.
