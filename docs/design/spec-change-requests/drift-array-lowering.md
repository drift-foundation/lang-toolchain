# MIR → LLVM Lowering for Array Literals and Indexing  
(Updated for `Size` type, consistent with String spec)

This document defines how `Array<T>` literals and indexed loads lower
from MIR to LLVM IR, and the required runtime ABI. All size-related
fields (`len`, `cap`, index bounds) use the language-level `Size` type,
whose concrete representation is the target-dependent `%drift.size`.

---

## 1. Runtime / ABI Model

Arrays share the same size/index model as Strings.

### 1.1 Language-level types

- `Array<T>.len: Size`
- `Array<T>.capacity(): Size`
- Indexing: `xs[i]`, where `i: Int`, is checked against `Size`.

### 1.2 Runtime C definitions

```c
#include <stddef.h>
#include <stdint.h>

typedef size_t drift_size;        // mirror of Drift's Size

typedef struct DriftArrayHeader {
	drift_size len;
	drift_size cap;
	void      *data;  // T[len]
} DriftArrayHeader;

void *drift_alloc_array(size_t elem_size,
                        size_t elem_align,
                        drift_size len,
                        drift_size cap);

noreturn void drift_bounds_check_fail(drift_size idx,
                                      drift_size len);
````

No fixed-width integers are used for array metadata.

---

## 2. LLVM Representation

Define a single target-selected integer type:

```llvm
; Chosen by backend:
;   64-bit target → i64
;   32-bit target → i32
%drift.size = type i64
```

Array layout:

```llvm
%drift.Array$T = type { %drift.size, %drift.size, T* }
; fields: len, cap, data
```

---

## 3. MIR Nodes

### 3.1 Array literals

```text
ArrayLit {
    elem_ty: TypeId,
    elements: [ValueId]
}
```

### 3.2 Indexed loads

```text
ArrayIndexLoad {
    elem_ty: TypeId,
    array:   ValueId,
    index:   ValueId   ; Int
}
```

---

## 4. Lowering Array Literals to LLVM IR

Steps:

1. Evaluate all element expressions left → right in MIR.
2. Compute compile-time constants:

   * `len = elements.len() : %drift.size`
   * `cap = len`
3. Call runtime allocator:

```llvm
%raw = call ptr @drift_alloc_array(
    i64         %elem.size,
    i64         %elem.align,
    %drift.size %len.const,
    %drift.size %cap.const
)
%data = bitcast ptr %raw to T*
```

4. Store elements using GEP and `store`:

```llvm
%elem.ptr.i = getelementptr inbounds T, T* %data, %drift.size i
store T %value.i, T* %elem.ptr.i, align %elem.align
```

5. Construct `%drift.Array$T`:

```llvm
%tmp0 = insertvalue %drift.Array$T undef, %drift.size %len.const, 0
%tmp1 = insertvalue %drift.Array$T %tmp0, %drift.size %cap.const, 1
%arr  = insertvalue %drift.Array$T %tmp1, T*           %data,      2
```

Zero-length case: `len == 0`, runtime may return null or dummy pointer — safe since indexing is impossible.

---

## 5. Lowering Indexed Loads (with Bounds Checks)

Given MIR:

```text
ArrayIndexLoad { elem_ty=T, array=a, index=i }
```

### 5.1 Extract fields

```llvm
%len  = extractvalue %drift.Array$T %arr, 0 ; %drift.size
%data = extractvalue %drift.Array$T %arr, 2 ; T*
```

### 5.2 Lower index (`Int`) to `%drift.size`

```llvm
; %idx : %drift.int   (signed Int)
%is.neg   = icmp slt %drift.int %idx, 0
%idx.size = zext %drift.int %idx to %drift.size
```

### 5.3 Bounds check

```llvm
%too.big = icmp uge %drift.size %idx.size, %len
%oob     = or i1 %is.neg, %too.big

br i1 %oob, label %oob.block, label %ok.block

oob.block:
    call void @drift_bounds_check_fail(%drift.size %idx.size,
                                       %drift.size %len)
    unreachable
```

### 5.4 Safe load

```llvm
ok.block:
    %elem.ptr = getelementptr inbounds T, T* %data, %drift.size %idx.size
    %elem     = load T, T* %elem.ptr, align alignof(T)
```

Result `%elem` is the MIR result value.

---

## 6. Spec Notes

To keep the main language spec consistent:

* All collection lengths/capacities use `Size`.
* Indexing uses runtime-checked semantics identical to Strings.
* Bounds violations produce a standard “index-out-of-bounds” error.

ABI/runtime use `drift_size`.
LLVM backend uses `%drift.size`.

