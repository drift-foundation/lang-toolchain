# Drift TODO

## Runtime and semantics

## Frontend and IR
- Define a minimal typed IR (ownership, moves, error edges) with a verifier.
- Lower the current AST/typechecker output into the IR; add golden tests for the lowering.
- Prototype simple IR passes (dead code, copy elision/move tightening).
- Draft SSA MIR schema (instructions, φ, error edges, drops) and map DMIR → SSA lowering.
- Plan cross-module error propagation: define backtrace handle format and how `Error` travels across module boundaries during SSA/LLVM work.
- Wire `^` capture so unwinding records per-frame locals and backtrace handles in `Error`
- Support `source_location()` intrinsic returning `SourceLocation` (file, line).

## Docs
- Add short “how to read IR” note once the IR spec is drafted.
- Expand DMIR spec with canonicalization rules (naming, ordering) and examples, including:
  - Ordering: consistent ordering of module items, let bindings, catch clauses, etc., so the serialized DMIR is stable across compiler runs.
  - Examples: concrete DMIR snippets showing surface code → DMIR (including ternary, try/else, struct/exception constructors), so implementers/verifiers have reference inputs/outputs.

