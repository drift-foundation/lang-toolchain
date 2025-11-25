# Drift TODO

## Frontend and IR
- MIR verifier:
  - Add deeper type checks: enforce operand/result types for all instructions; verify `copy` only on `Copy` types (needs a richer type environment or Copy-trait info).
  - Revisit Call shape once error plumbing is fully settled: require normal+error edges on calls to reflect Drift’s implicit `Result<T, Error>` model.
- Serialization:
  - DMIR (ANF-like, structured) serializer for signing (canonical, deterministic).
  - SSA MIR printer/serializer for debugging and golden tests (internal use, not signed).
- Start DMIR→MIR→LLVM integration (incremental):
  - Add lowering for array/field get/set and full try/catch (exception constructors with full payloads, not just `msg` stubs).
- Closures/lambdas (implementation):
  - Lower closure literals: build env structs, emit `{env_ptr, call_ptr}` values, and generate callable thunks.
  - MIR/LLVM: represent non-capturing closures as thin pointers; capturing closures as fat pointers; ensure env drop is emitted once.
  - Callable interface wiring: desugar `cb.call(...)` and/or `cb(...)` to the callable thunk; honor `ref`/`ref mut`/by-value usage in the verifier.
  - Add borrow-capture support once borrow/lifetime checking is in place.
- MIR verifier:
- Codegen coverage:
  - Add more MIR→LLVM codegen cases beyond the current set: branches with real error edges (try/catch), struct/array ops, and additional arithmetic/comparison coverage.
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
