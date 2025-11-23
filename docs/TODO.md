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
  - Refactor MIR call/error edge model to make lowering straightforward (current Call carries embedded normal/error edges but does not return an error value; consider simplifying calls or adjusting error representation before lowering).
  - Straight-line lowering:
    - Implement minimal AST/DMIR → MIR lowering for params, literals, moves, calls, struct/exception/array init, return (no control flow).
    - Add MIR golden tests via the MIR printer (e.g., simple add function).
  - Add a minimal MIR→LLVM emitter stub for that subset (ints, calls, return, basic error edge handling) to prove end-to-end.
  - Extend lowering to conditionals/ternary and try/else once the verifier is richer.
  - Add lowering for array/field get/set, raises, and try/catch.
- Add tests/lowering from DMIR.
- Modules/interop: Cross-module support adds a few complexities. It’s an ABI and tooling problem: agreeing on data layouts (field layout/alignment/ownership), calling conventions, and error/backtrace representation so modules built separately can interoperate safely. That’s why it’s deferred until we’ve locked the single-module MIR.
		- Error propagation ABI: errors need a stable, module-independent representation (layout, ownership rules) so they can cross boundaries without being interpreted incorrectly.
		- Backtrace/handles: stack info must survive module hops—either as an opaque handle or normalized form that can be merged/symbolicated later.
		- Symbol resolution: calls across modules may need indirection (import tables) and consistent naming/versioning for functions/structs/exceptions.
		- Codegen/linking: MIR → LLVM may need to emit module exports/imports, and ensure calling conventions/ownership semantics match across modules.
		- Verification: cross-module calls need type/ABI checks; error edges must be compatible across module versions.
- Lower the current AST/typechecker output into the IR; add golden tests for the lowering.
- Prototype simple IR passes (dead code, copy elision/move tightening).
- Plan cross-module error propagation: define backtrace handle format and how `Error` travels across module boundaries during SSA/LLVM work.
- Wire `^` capture so unwinding records per-frame locals and backtrace handles in `Error`
- Support `source_location()` intrinsic returning `SourceLocation` (file, line).
- Roadmap: plan LLVM backend integration via a minimal C shim using the LLVM C API, with Drift calling the shim through FFI to validate C interop while self-hosting.
