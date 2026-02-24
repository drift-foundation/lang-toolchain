# Memory Leak: Array backing buffer not freed inside struct locals

**Date**: 2026-02-24
**Failing tests**: `algo_sort_range_basic`, `algo_swap_sanity`
**Symptom**: exit code 97 under `DRIFT_MEMCHECK=1` (valgrind `--error-exitcode=97`)
**Unrelated to**: lexical-scope hardening changes on `const-func-scope` branch

---

## Valgrind output (both tests identical pattern)

```
HEAP SUMMARY:
    in use at exit: 24 bytes in 1 blocks
  total heap usage: 1 allocs, 0 frees, 24 bytes allocated

24 bytes in 1 blocks are definitely lost in loss record 1 of 1
   at 0x4914AA9: posix_memalign (vg_replace_malloc.c:2226)
   by 0x4093608: drift_alloc_array (in .../a.out)
   by 0x4005FC0: main (main.drift:15)

ERROR SUMMARY: 1 errors from 1 contexts
```

1 alloc, 0 frees. The `Array<Int>` backing buffer allocated via `drift_alloc_array` is never freed.

## Reproducer

Both tests define a user struct wrapping an owned `Array<Int>`:

```drift
struct Range { data: Array<Int> }

fn main() nothrow -> Int {
    var r = Range(data = [1, 2, 3]);
    // ... use r ...
    return r.data[0] * 100 + r.data[1] * 10 + r.data[2];
}
```

The `Range` local `r` goes out of scope at the end of `main`. The array backing buffer is never freed.

**Contrast with passing tests**: `algo_sort_random`, `algo_sort_duplicates`, etc. use `var arr = [3, 1, 4, 2]` — a bare `Array<Int>` local, not wrapped in a struct. These pass under valgrind because bare array locals are handled correctly by `_drop_all_arrays`.

## Root cause

**File**: `lang/driftc/core/types_core.py`, line 1395-1398

```python
if td.kind is TypeKind.ARRAY:
    needs = bool(td.param_types) and self.has_drop(td.param_types[0])
    self._needs_drop_cache[tid] = needs
    return needs
```

`has_drop(Array<Int>)` returns `has_drop(Int)` = **False**. This is incorrect — every array owns a heap-allocated backing buffer via `drift_alloc_array` that must be freed via `drift_free_array` regardless of whether the element type needs drop.

### Full chain

1. **ARC pass** (`string_arc.py:88-90`): Correctly identifies `Array<Int>` as needing drop (unconditional `return True` for all arrays). Therefore `Range` is also correctly identified as destructible. The ARC pass emits `ZeroValue + StoreLocal + DropValue(r, Range)`.

2. **LLVM codegen** (`llvm_codegen.py:2162-2164`): Receives `DropValue(r, Range)`. Calls `_emit_drop_value(Range, r)`.

3. **`_emit_drop_value`** (`llvm_codegen.py:7568`): Guard check calls `_type_needs_drop(Range)`.

4. **`_type_needs_drop`** (`llvm_codegen.py:7549-7552`): Delegates to `type_table.has_drop(Range)`.

5. **`has_drop(Range)`** (`types_core.py:1399-1405`): Checks `any(has_drop(fty) for fty in inst.field_types)` = `has_drop(Array<Int>)`.

6. **`has_drop(Array<Int>)`** (`types_core.py:1395-1398`): Returns `has_drop(Int)` = **False**.

7. **Result**: `_emit_drop_value` returns early at line 7568. No `drift_free_array` is emitted. The `ZeroValue + StoreLocal` from the ARC pass ARE lowered (they zero the struct, including the array header), but the backing buffer pointer is orphaned.

### LLVM IR evidence (main function return path)

```llvm
; ARC pass emitted ZeroValue+StoreLocal (zeroes the struct, orphans the buffer)
%__arc1 = load %Struct_main_Range, %Struct_main_Range* %r__addr
%__arc2 = insertvalue %Struct_main_Range zeroinitializer, %DriftArrayHeader zeroinitializer, 0
store %Struct_main_Range %__arc2, %Struct_main_Range* %r__addr
; DropValue was emitted but _emit_drop_value returned early — no drift_free_array call
ret i64 %t85
```

---

## Fix 1: `types_core.py:1395-1397` — has_drop(Array) [APPLIED]

```python
# BEFORE (wrong — only checks element drop):
if td.kind is TypeKind.ARRAY:
    needs = bool(td.param_types) and self.has_drop(td.param_types[0])
    self._needs_drop_cache[tid] = needs
    return needs

# AFTER (correct — array always needs drop for backing buffer):
if td.kind is TypeKind.ARRAY:
    self._needs_drop_cache[tid] = True
    return True
```

This aligns `has_drop` with the ARC pass's `_type_needs_drop` (line 88-90 of `string_arc.py`) which already unconditionally returns True for all arrays.

## Fix 2: `llvm_codegen.py:7856` — null-guard in interface drop helper [APPLIED]

Added vtable null check at the top of `_ensure_interface_drop_helper()`. When `iface_vtable` is null (zeroed interface value), the helper jumps directly to `iface_free_done`, skipping both the drop function call and the `drift_iface_free` call.

**Intentional defensive behavior**: if a malformed value had `vtable=null` but owned heap data, that data would leak rather than crash. This is acceptable — the guard exists for crash-avoidance on zero-initialized values, not for handling arbitrary malformed state.

## Fix 3: `string_arc.py` — drop-before-reassign for destructibles [APPLIED]

Sweep found that destructible locals (structs/variants containing arrays) lacked drop-before-reassign on StoreLocal, unlike array locals (line 735-738) and string locals (line 802-812).

### Three-tier model

Destructible locals are partitioned by `_is_nullsafe_drop`:

- **Nullsafe types** (plain structs whose fields recursively resolve to Array/String/Interface/Error — no explicit `destructor_fns`): zero-initialized in the entry block, unconditional drop-before-reassign. Structurally identical to the array/string paths. No conditional-init gap.

- **Non-nullsafe types** (Arc, Mutex, types with `destructor_fns`): NOT zero-initialized. Drop-before-reassign is gated by `initialized_destructibles` (seeded from `assigned_in[block]`, definite-init intersection semantics). Conservative: at merge points after conditional init, first StoreLocal won't drop old value — potential leak on the initialized-path subset. Acceptable because these types use RAII-style init-at-declaration in practice.

- **Arrays / Strings**: unchanged, handled by existing paths (`_drop_array_local`, `_release_local`).

---

## Sweep findings

### Safety sweep of all drop predicates across codebase

| File | Function | Array handling | Status |
|------|----------|---------------|--------|
| `types_core.py` | `has_drop()` | **FIXED** → True | Applied |
| `string_arc.py` | `_type_needs_drop()` | Correct (unconditional True) | OK |
| `hir_to_mir.py` | `_needs_runtime_drop()` | Delegates to `has_drop()` | OK (inherits fix) |
| `llvm_codegen.py` | `_type_needs_drop()` | Delegates to `has_drop()` via Tier 1 | OK (inherits fix) |
| `llvm_codegen.py` | `_type_needs_drop()` Tier 3 fallback | **Incomplete** — defaults False for non-SCALAR | LOW — unreachable in v1 |

### Drop-before-reassign coverage

| Local type | StoreLocal drop | Status |
|------------|----------------|--------|
| `array_locals` (bare arrays) | `_drop_array_local` (line 735) | OK |
| `string_locals` | `_release_local` (line 802) | OK |
| `destructible_locals` (structs/variants with drop fields) | **FIXED** → `_drop_destructible_local` (line 739) | Applied |

### MIR lowering (`hir_to_mir.py`) — all paths safe

- `_register_drop_local` — now correctly registers Array-containing types
- `_emit_scope_drops` — return/break/continue all call cleanup
- Move semantics — `_moved_locals` tracking unaffected
- Copy semantics — Array remains non-Copy (`copy_status` returns False)
- Match binder extraction — now correctly registers array binders for drop

---

## Verification (targeted, all under DRIFT_MEMCHECK=1)

| Test | Result |
|------|--------|
| `struct_array_field_drop` (new regression) | ok |
| `struct_array_reassign_drop` (new regression) | ok |
| `algo_sort_range_basic` (was failing) | ok |
| `algo_swap_sanity` (was failing) | ok |
| `algo_sort_random` (negative control) | ok |
| `algo_sort_duplicates` (negative control) | ok |
| `algo_sort_sorted` (negative control) | ok |
| `algo_sort_reverse` (negative control) | ok |
| `array_reassign_drop` (negative control) | ok |
| `callback_arc_mutex_full_mutation` (regression check) | ok |
| `callback_move_capture_arc_lifetime` (regression check) | ok |
| `callback_move_capture_nested_callback` (regression check) | ok |
| `callback_move_capture_replace_state` (regression check) | ok |

**Pending**: Full e2e suite on farm.

---

## Follow-up (not in this branch)

1. **Codegen fallback hardening**: `llvm_codegen.py:7557-7563` Tier 3 fallback defaults to False for non-SCALAR. Currently unreachable (Tier 1 always fires in v1 path), but should be made fail-loud or aligned with `types_core.has_drop`.

2. **ARC / types_core consolidation**: `string_arc.py` maintains its own `_type_needs_drop` that is now consistent with `types_core.has_drop`, but having two sources of truth is fragile. Consider consolidating.

3. **Interface drop null-safety**: Interface/callback drop helpers crash on zeroed values. Arrays and strings are null-safe. Consider adding a null guard to the interface drop helper for robustness.
