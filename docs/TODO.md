# Drift TODO

## Frontend and IR
- Modules/interop: Cross-module support adds a few complexities. It’s an ABI and tooling problem: agreeing on data layouts (field layout/alignment/ownership), calling conventions, and error/backtrace representation so modules built separately can interoperate safely. That’s why it’s deferred until we’ve locked the single-module MIR.
		- Error propagation ABI: errors need a stable, module-independent representation (layout, ownership rules) so they can cross boundaries without being interpreted incorrectly.
		- Backtrace/handles: stack info must survive module hops—either as an opaque handle or normalized form that can be merged/symbolicated later.
		- Symbol resolution: calls across modules may need indirection (import tables) and consistent naming/versioning for functions/structs/exceptions.
		- Codegen/linking: MIR → LLVM may need to emit module exports/imports, and ensure calling conventions/ownership semantics match across modules.
		- Verification: cross-module calls need type/ABI checks; error edges must be compatible across module versions.
- Define a minimal typed IR (ownership, moves, error edges) with a verifier.
- Lower the current AST/typechecker output into the IR; add golden tests for the lowering.
- Prototype simple IR passes (dead code, copy elision/move tightening).
- Draft SSA MIR schema (instructions, φ, error edges, drops) and map DMIR → SSA lowering.
- Plan cross-module error propagation: define backtrace handle format and how `Error` travels across module boundaries during SSA/LLVM work.
- Wire `^` capture so unwinding records per-frame locals and backtrace handles in `Error`
- Support `source_location()` intrinsic returning `SourceLocation` (file, line).
