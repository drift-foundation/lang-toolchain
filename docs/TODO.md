# Drift TODO

## Frontend and IR
- MIR verifier:
  - Add cross-block dominance/phi validation (propagate defs/types across CFG; ensure block params align with predecessor args and uses are dominated by defs). [partially done: edge arg/param validation; still need full dominance/dataflow]
  - Add deeper type checks: enforce operand/result types for all instructions, and verify copy only on Copy types (you may need a type environment or Copy-trait info).
	To enforce it properly we need a small dataflow pass: compute in/out def/type sets per block via the params, iterate to a fixed point across predecessors, and then validate that every use is either defined in the block or comes through its params (and that all predecessors supply matching args for each param).
  - Revisit Call shape once error plumbing exists: require normal+error edges on calls to reflect Drift’s implicit `Result<T, Error>` model.
- Serialization:
  - DMIR (ANF-like, structured) serializer for signing (canonical, deterministic).
  - SSA MIR printer/serializer for debugging and golden tests (internal use, not signed).
- Start DMIR→MIR→LLVM integration (incremental):
  - Extend lowering to try/else and try/catch once the verifier is richer. (Conditionals/ternary are covered.)
  - Add lowering for array/field get/set, raises, and try/catch.
- Closures/lambdas (implementation):
  - Lower closure literals: build env structs, emit `{env_ptr, call_ptr}` values, and generate callable thunks.
  - MIR/LLVM: represent non-capturing closures as thin pointers; capturing closures as fat pointers; ensure env drop is emitted once.
  - Callable interface wiring: desugar `cb.call(...)` and/or `cb(...)` to the callable thunk; honor `ref`/`ref mut`/by-value usage in the verifier.
  - Add borrow-capture support once borrow/lifetime checking is in place.
- MIR verifier:
  - Finish dominance/dataflow: ensure uses are dominated by defs across CFG, tighten block param/arg checks, and enforce type consistency with propagated info.
- Codegen coverage:
  - Add runtime/codegen tests that exercise call normal/error edges (try/else, try/catch) once error edges/raise have a real lowering in MIR→LLVM.
  - Increase MIR→LLVM codegen cases beyond the current sum_int64s to cover more ops/branches/returns.
- Modules/interop: Cross-module support adds a few complexities. It’s an ABI and tooling problem: agreeing on data layouts (field layout/alignment/ownership), calling conventions, and error/backtrace representation so modules built separately can interoperate safely. That’s why it’s deferred until we’ve locked the single-module MIR.
		- Error propagation ABI: errors need a stable, module-independent representation (layout, ownership rules) so they can cross boundaries without being interpreted incorrectly.
		- Backtrace/handles: stack info must survive module hops—either as an opaque handle or normalized form that can be merged/symbolicated later.
		- Symbol resolution: calls across modules may need indirection (import tables) and consistent naming/versioning for functions/structs/exceptions.
		- Codegen/linking: MIR → LLVM may need to emit module exports/imports, and ensure calling conventions/ownership semantics match across modules.
		- Verification: cross-module calls need type/ABI checks; error edges must be compatible across module versions.
- Prototype simple IR passes (dead code, copy elision/move tightening).
- Plan cross-module error propagation: define backtrace handle format and how `Error` travels across module boundaries during SSA/LLVM work.
- Wire `^` capture so unwinding records per-frame locals and backtrace handles in `Error`
- Support `source_location()` intrinsic returning `SourceLocation` (file, line).
- Roadmap: plan LLVM backend integration via a minimal C shim using the LLVM C API, with Drift calling the shim through FFI to validate C interop while self-hosting.
