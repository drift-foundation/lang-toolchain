# Drift Debugging + Assert (Spec Change Request)

## Goal
Ship `assert(...)` exactly once, with real debugging support:
- call stack shows Drift function names
- file/line locations
- no split between “debug” and “release” behavior

## Scope
This proposal ties `assert` to proper debug info. We do **not** add a minimal
assert that only prints native backtraces.

## Requirements
- `assert(cond)` is **nothrow** and **always enabled** (all build modes).
- On failure: print message + Drift stack trace (with file/line).
- Works for optimized builds (best effort).

## Debug Info Strategy
Pick **one** of these:

### Option A: DWARF (preferred if we want debugger tooling)
- Emit DWARF with line tables + function symbols.
- Runtime resolves PCs to file:line via `libdw`/`libunwind` or similar.
- Pros: integrates with standard debuggers.
- Cons: larger compiler/runtime work and platform dependencies.

### Option B: Custom map (lighter, Drift‑only)
- Compiler emits a `drift.debug.map` section:
  - function range → name
  - PC range → file/line
- Runtime uses a lightweight resolver on the map.
- Pros: portable, stable across platforms.
- Cons: separate tooling for external debuggers.

## Assert API (frozen surface)
- `assert(cond: Bool) nothrow -> Void`
- `assert_msg(cond: Bool, msg: String) nothrow -> Void`
- compiler expands `assert(cond)` → `std.core.assert_loc(cond, file, line)` once debug info exists.

## Runtime behavior
On failure:
1) Print `assertion failed` + optional message
2) Print file:line of assert site
3) Print Drift stack trace with resolved symbols
4) Abort

## Compiler changes
- Emit debug info (DWARF or custom map).
- Record mapping from generated code ranges to:
  - module
  - function name
  - source file and line
- Expand `assert` to `assert_loc` with file/line constants.

## Tests
- Unit: parser/ast accepts `assert(...)`.
- E2E: failing assert prints a stack trace containing:
  - function name
  - file:line
- E2E: nested call chain shows correct order (topmost assert site).

## Non‑goals (MVP)
- Variable inspection
- Debugger breakpoints / stepping

## Decision
Do not ship assert without real debug info. Implement debug info first, then assert.
