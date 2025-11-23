# Drift TODO

## Runtime and semantics
- Wire `^` capture so unwinding records per-frame locals and backtrace handles in `Error`.
- Replace the simple call-name stack capture with proper backtrace handles once the runtime grows them.
- Support `source_location()` intrinsic returning `SourceLocation` (file, line).
- Add runtime tests for event-specific catches, rethrow, and inline `try expr else` type/behaviour checks; include negative cases (parse/type/runtime failures).

## Frontend and IR
- Define a minimal typed IR (ownership, moves, error edges) with a verifier.
- Lower the current AST/typechecker output into the IR; add golden tests for the lowering.
- Prototype simple IR passes (dead code, copy elision/move tightening).
- Draft SSA MIR schema (instructions, φ, error edges, drops) and map DMIR → SSA lowering.

## Docs
- Add short “how to read IR” note once the IR spec is drafted.
- Expand DMIR spec with canonicalization rules (naming, ordering) and examples.
