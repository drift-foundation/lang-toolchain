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

## Fix

### Primary fix: `types_core.py:1395-1398`

```python
# BEFORE (wrong — only checks element drop):
if td.kind is TypeKind.ARRAY:
    needs = bool(td.param_types) and self.has_drop(td.param_types[0])

# AFTER (correct — array always needs drop for backing buffer):
if td.kind is TypeKind.ARRAY:
    needs = bool(td.param_types)
```

This aligns `has_drop` with the ARC pass's `_type_needs_drop` (line 88-90 of `string_arc.py`) which already unconditionally returns True for all arrays.

### Verification

The LLVM codegen's `_emit_drop_value` already handles structs with array fields correctly (lines 7619-7631 → recursive field drop → lines 7598-7613 for arrays → `drift_free_array`). The only issue is the early-exit guard at line 7568. Once `has_drop` returns the correct value, the existing codegen logic will emit the right cleanup code.

### Blast radius

- `has_drop` is used broadly: type_checker, codegen, MIR lowering
- Changing it affects which types are considered "needing destruction"
- `Array<Int>` (and `Array<Uint>`, `Array<Bool>`, etc.) will now correctly report `has_drop = True`
- Structs/variants containing such arrays will transitively report `has_drop = True`
- This is the correct semantic — it was already correct in the ARC pass but inconsistent in `types_core`

### Tests to run

- `algo_sort_range_basic` and `algo_swap_sanity` under `DRIFT_MEMCHECK=1` (should now pass)
- All other `algo_sort_*` tests (should remain passing)
- Full e2e suite to catch regressions from broader `has_drop` change
- Any test involving struct-with-array or variant-with-array patterns

## Collateral: ARC pass redundancy note

The ARC pass (`string_arc.py`) maintains its own `_type_needs_drop` that diverges from `types_core.has_drop`. After the fix, these should be consistent. Consider consolidating to a single source of truth in a future cleanup.
