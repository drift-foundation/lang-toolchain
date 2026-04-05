# Scope-drop leak for heap-backed values (clone_deep HashMap, format_int string)

## Status: open
## Reported: 2026-04-04
## Compiler: 0.27.144 / ABI 7

## Summary

Two source-level scope-drop leaks remain after the package-boundary leak fixes
in 0.27.144. Both scale linearly with request count and reproduce without any
package boundary — they are compiler scope-exit destructor bugs for heap-backed
values.

## Sites

### 1. `JsonNode.clone_deep` — HashMap backing arrays not freed

```
528 (384 direct, 144 indirect) bytes in 3 blocks are definitely lost
   at posix_memalign (array_runtime.c:62)
   by HashMapCore::ensure_capacity
   by HashMapCore::insert
   by _clone_deep_impl
   by JsonNode::clone_deep
   by pushcoin.bookkeeper::_build_response (app.drift:360)
```

A `val` holding a cloned JsonNode with internal HashMap goes out of scope
without the destructor freeing the HashMap's backing arrays.

### 2. `fmt.format_int` — concatenated string not freed

```
60 bytes in 3 blocks are definitely lost
   by drift_string_alloc (string_runtime.c:35)
   by drift_string_concat (string_runtime.c:164)
   by std.format::format_int
   by pushcoin.bookkeeper::BookkeeperApp::_do_submit (app.drift:163)
```

A string temp from `format_int` is used but not released on scope exit.

## Root cause (Site 1 — clone_deep)

`string_arc.py` `initialized_at_return` uses `assigned_in` (intersection
of all predecessor paths) to decide which destructible locals to destroy
at function exit.  For a variant local assigned inside a match arm (not
all paths), the intersection excludes it.  The PHI at the join provides
zeroinitializer for uninitialized paths, and variant destroy on tag 0 is
a no-op, so the destroy would be safe — but it was never emitted.

## Fix (0.27.145)

`string_arc.py:1242` — after computing `initialized_at_return`, widen it
by scanning predecessors for **variant-typed** destructible locals assigned
on some paths but not all, excluding any moved on any predecessor.

**Scope: variant types only.**  The safety argument depends on variant
tag-dispatch destroy where tag 0 is a no-op.  Non-variant conditionally-
initialized destructible locals (e.g. structs with destructors) are NOT
covered by this fix and remain an open class.

## Site 2 — format_int / DiagnosticValue::String

**OPEN — ownership model bug, not a point fix.**

The string produced by `fmt.format_int()` leaks when wrapped in
`DiagnosticValue::String(...)` and placed in a map literal passed to
a logger call.  Three attempted fixes (0.27.146, 0.27.147, and a
codegen-only release) all failed:

- 0.27.146: codegen release after drift_dv_string → double-free
  (HashMap cleanup also releases the same string)
- 0.27.147: drift_dv_string_move (no retain) for owned temps →
  same double-free (exception fields misclassified as owned)
- 0.27.147 + owns_string_arg: MIR-level ownership bit → still
  double-free (the aliasing between DV and HashMap is not resolved
  by choosing retain vs move at construction time)

The root cause is a missing ownership contract across:
- expression lowering (who owns the string temp?)
- DV construction (retain vs move vs borrow?)
- container insertion (does HashMap clone/retain the value?)
- scope exit (what releases what?)
- container cleanup (HashMap::clear vs DV::release ordering)

This is classified as LANGUAGE_BUG in the ownership model, not a
codegen or runtime point fix.  Reverted to 0.27.145 baseline.

20 bytes/request.  Low severity individually but proves the
ownership model gap.

## Fix (0.27.148) — deref-clone only

`hir_to_mir.py`: LoadRef from `*(&T)` now marks the result as
ref-aliased for non-bitcopy types, and `_copy_if_ref_alias` handles
`TypeKind.DIAGNOSTICVALUE`.  This emits `CopyValue`/`drift_dv_clone`
when `to_debug(&DiagnosticValue)` returns `*self`, preventing the
two-owner aliasing that caused the double-free.

**Fixes:** double-free / UAF crash (the P0).

## Fix (0.27.149) — ConstructDV visibility in string_arc

`string_arc.py _iter_used_values`: added `ConstructDV` so its args
participate in string use-count tracking.  string_arc's existing
last-use release machinery (`_note_use` at use_count==0 for values
in `owned_values`) now emits `StringRelease` for owned creator temps
consumed by `ConstructDV(String)`.  Borrowed locals (LoadLocal,
LoadRef) are NOT in `owned_values` and are unaffected — their
scope-exit release remains the only cleanup path.

One line added to `_iter_used_values`.  No new MIR ops, no runtime
changes, no ABI bump, no codegen heuristics.

**Fixes:** the 20-byte/request string leak from `drift_dv_string`
retaining without a matching caller release.

Both the P0 crash (deref-clone) and the leak (string_arc visibility)
are now resolved.

## Reproducer

Compiler-level: `issues/scope-drop-heap-leak/repro_clone_deep.drift`
reproduces Site 1.  3 calls × clone_deep in conditional-move match →
2,496 bytes definitely lost before fix, 0 after.

Regression tests:
- `lang/tests/memcheck/test_scope_drop_conditional_move.py` — valgrind
  pinned (primary)
- `lang/tests/codegen/e2e/scope_drop_conditional_move/` — functional
  (exit code only)
