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

- Pinned only.
- Implementation not started.
