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

## Site 2 — format_int

The `format_int` string leak from the original app report was not
reproduced in isolation.  It may be a downstream consequence of the
same scope-exit miss (the string is inside a `DiagnosticValue::String`
variant), or a separate issue.  To be re-tested on the app after the
0.27.145 fix.

## Reproducer

Compiler-level: `issues/scope-drop-heap-leak/repro_clone_deep.drift`
reproduces Site 1.  3 calls × clone_deep in conditional-move match →
2,496 bytes definitely lost before fix, 0 after.

Regression tests:
- `lang/tests/memcheck/test_scope_drop_conditional_move.py` — valgrind
  pinned (primary)
- `lang/tests/codegen/e2e/scope_drop_conditional_move/` — functional
  (exit code only)
