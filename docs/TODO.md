# Drift TODO

## Frontend and IR
- Pre-req:
	- Decide the SSA MIR instruction set and control-flow shape (blocks, φ, call, raise/return, drop, alloc?).
		- Core instructions (typed):
			- const (ints/floats/bools/strings)
			- move (ownership transfer)
			- copy (for copyable types)
			- call (direct), with explicit normal target block and error target block or a returned Error edge
            - struct_init, field_get, field_set (if mutable)
            - array_init, array_get, array_set (with bounds checking policy)
            - alloc/stack_alloc for locals if needed (or treat all as values)
            - drop (inserted by ownership/liveness analysis)
            - unary/binary ops
        - Modules/interop: For now, intra-module; cross-module later.
        - Verifier: Check SSA form (definitions dominate uses), type consistency, correct block params arity, no use-after-move, drop rules, and that all control-flow paths end in return or raise.
    - Sketch one or two end-to-end examples (surface → DMIR → SSA MIR) to validate the design.
- Define a minimal typed IR (ownership, moves, error edges) with a verifier.
- Lower the current AST/typechecker output into the IR; add golden tests for the lowering.
- Prototype simple IR passes (dead code, copy elision/move tightening).
- Draft SSA MIR schema (instructions, φ, error edges, drops) and map DMIR → SSA lowering.
- Plan cross-module error propagation: define backtrace handle format and how `Error` travels across module boundaries during SSA/LLVM work.
- Wire `^` capture so unwinding records per-frame locals and backtrace handles in `Error`
- Support `source_location()` intrinsic returning `SourceLocation` (file, line).
