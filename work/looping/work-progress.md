# Looping Work Progress

## Goal

Land ergonomic loop syntax for common counted/index iteration without changing iterator mechanics.

## Pinned MVP: Counted/Index `for`

### Syntax

- `for var i = 0; i < xs.len; i += 1 { ... }`
- `for val i = 0; i < limit; i += 1 { ... }`
- explicit type variant allowed:
  - `for Int i = 0; i < xs.len; i += 1 { ... }`

### Semantics

1. Lowering equivalent to:
   - init once
   - pre-check condition each iteration
   - execute body
   - execute step
   - repeat
2. `continue` jumps to step, then condition.
3. `break` exits loop directly.
4. Scope:
   - loop variable declared in init is scoped to the `for` loop.
5. Step expression is required in MVP.

### Mutation Operators in Scope

- Support `+=` for MVP loop ergonomics.
- `++/--` are deferred (not in this MVP pin).

## Non-Goals (This Pin)

- Iterable `for` syntax (`for val x : iterable { ... }`) is a separate follow-up.
- Iterator protocol changes are out of scope.
- No `for (...)` parenthesized syntax; Drift style remains without parens.

## Pinned: Iterator Consolidation

1. `Iterable` is the canonical trait-level contract for iteration entry (`iter`).
2. `SinglePassIterator::next()` remains the core stepping protocol.
3. Method-call `.iter()` remains supported as ergonomic sugar, but should resolve via `Iterable` implementations.
4. Container-specific ad-hoc iteration entry points should converge on the same trait contract.
5. Mutable iteration is a follow-up track using the same model (`&mut` source + mutable item types), not a separate ad-hoc protocol.

## Pinned: Iterable `for` Shortcut Semantics

1. Common shortcut form is supported:
   - `for val x : source { ... }`
2. Shortcut lowering uses immutable iteration by default:
   - `source` -> `source.iter()` via `Iterable`.
3. Mutable iteration must be explicit (no implicit mutable shortcut):
   - `for val x : source.iter_mut() { ... }` (or `for var x : ...` when rebinding is needed).
4. This keeps read-only loops concise and makes mutation intent explicit at callsite.

## Test Plan (Regression-first)

1. Parse/grammar:
   - valid forms above parse successfully.
   - invalid missing-part forms produce clear diagnostics.
2. Codegen/e2e:
   - simple sum loop correctness.
   - `continue` executes step semantics correctly.
   - `break` exits without executing subsequent body iterations.
3. Scope checks:
   - init variable not visible outside loop.

## Status

### Completed

1. Grammar/parser support landed for:
   - counted/index `for`:
     - `for var i = 0; i < n; i += 1 { ... }`
     - `for val i = 0; i < n; i += 1 { ... }`
     - `for Int i = 0; i < n; i += 1 { ... }`
   - iterable shortcut `for`:
     - `for val x : xs { ... }`
     - `for Int x : xs { ... }`
   - legacy `for x in xs { ... }` remains supported.
2. Stage0/stage1 AST/HIR plumbing added for:
   - mutable/typed iterable binders
   - counted loop init metadata (mutable + optional type).
3. Counted-loop lowering implemented with correct step ordering around `continue`.
4. Counted-loop binding scoping fixed:
   - init variable no longer leaks outside loop scope.
5. Regression coverage added:
   - parser unit tests for valid/invalid counted + colon forms
   - stage1 scope regression test for counted-loop init binding
   - codegen e2e:
     - `for_loop_colon_sum_int`
     - `for_count_loop_sum_int`
     - `for_count_loop_continue_break`
     - `for_count_nested_continue_break`
     - `for_count_outer_continue_step`
     - `for_iter_colon_typed_mismatch`
     - `for_count_typed_init_mismatch`
     - `for_count_loop_scope_unknown_name`
6. Unknown-name diagnostics pinned and stabilized for loop scope errors:
   - Added driver regression:
     - `lang/tests/driver/test_unknown_name_diagnostic.py`
   - Added checker `E-UNKNOWN-NAME` for unresolved user-style local names in function scope.
   - Hardened checker to avoid false positives in shallow/incomplete inference contexts:
     - do not treat call callee var nodes as unknown locals in generic traversal
     - suppress unknown-name reporting in lambda-body shallow inference
     - suppress unknown-name reporting while traversing `try`/`match` arm blocks
     - keep unknown-name checks active for normal function-scope traversal.
7. Validated previously regressed e2e families after unknown-name integration:
   - closures/try-catch lambda
   - concurrency cancel/spawn timeout cases
   - exception/index error payload cases
   - match binder/type-args cases

### Remaining

1. Iterator protocol consolidation track (follow-up branch, not a blocker for closing this branch):
   - formalize `Iterable` as canonical `iter()` entry in docs/spec notes
   - converge container docs/examples on `iter()/iter_mut()` naming.

### Branch Status

- Full suite validation passed.
- Looping MVP scope is complete.
- Branch is ready to close and resume JSON-focused work.
