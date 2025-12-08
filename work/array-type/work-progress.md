## 0. Non-negotiable invariants (copy these, don’t re-invent)

From the blueprint: 

* **Runtime / ABI**

  * `Array<T>` header is:
    `struct DriftArrayHeader { drift_size len; drift_size cap; void *data; }`
  * `drift_size` is the C side of Drift’s `Size` type.
  * Helpers:

    ```c
    void *drift_alloc_array(size_t elem_size,
                            size_t elem_align,
                            drift_size len,
                            drift_size cap);

    noreturn void drift_bounds_check_fail(drift_size idx,
                                          drift_size len);
    ```
* **LLVM layout**

  * A single size type:

    ```llvm
    %drift.size = type i64   ; on 64-bit, or i32 on 32-bit
    ```
  * Arrays:

    ```llvm
    %drift.Array$T = type { %drift.size, %drift.size, T* }
    ; fields: len, cap, data
    ```
* **MIR nodes**

  * `ArrayLit { elem_ty: TypeId, elements: [ValueId] }`
  * `ArrayIndexLoad { elem_ty: TypeId, array: ValueId, index: ValueId }`
* **Lowering rules**

  * Literals call `drift_alloc_array`, store elements with GEP, then build `%drift.Array$T` via `insertvalue`.
  * Indexing extracts `len` and `data`, converts index to `%drift.size`, checks negative or `idx >= len`, calls `drift_bounds_check_fail` on OOB, then GEP+load.

Everything else is plumbing to make the above true in lang2.

### Progress so far (lang2)
- Type core: added `TypeKind.ARRAY` and `TypeTable.new_array`.
- Parser/AST/HIR: grammar already had array literals/indexing; AstToHIR now lowers array literals to `HArrayLiteral`.
- Checker: shallow inference/validation for array literals, indexing, and indexed assignments; diagnostics for mixed element types, empty literal without type, non-Int index, non-array indexing, and assignment type mismatch. New checker tests cover these cases.

---

## 1. Spec / type system alignment

**Goal:** Make lang2’s type layer agree with the blueprint and the String spec.

1. Add/confirm in spec:

   * `Array<T>.len: Size`
   * `Array<T>.capacity(): Size`
   * Indexing `xs[i]`:

     * `i: Int` (signed).
     * Bounds-checked exactly like Strings; OOB ⇒ standard index-out-of-bounds `Error`. 
2. In the lang2 type system:

   * Register `Array` as a **built-in generic**:

     * `Array<T>` → “struct-like” concrete type with 3 fields (len, cap, data).
   * Wire `Size` so there’s a clean mapping:

     * Drift `Size` ↔ `%drift.size` ↔ `drift_size`.
   * Decide now: `Array<T>` is always owning and heap-backed (same as old design). No slices, no stack semantics baked into the type.

**Deliverable:** A short “Array” section in the language spec that mirrors the blueprint, no new ideas.

---

## 2. Parser + AST + HIR

**Goal:** lang2 can *parse* all the necessary array constructs, and HIR carries enough info to feed MIR.

1. **Type syntax**

   * Extend type grammar to support:

     ```text
     TypeRef ::= ... | "Array" "<" TypeRef ">"
     ```
   * AST:

     ```python
     @dataclass
     class ArrayTypeRef(TypeRef):
     	elem: TypeRef
     ```
   * If you plan to keep `T[]` sugar, have stage1 rewrite `T[]` → `Array<T>` early; do not fork semantics.

2. **Expressions**

   * Array literals:

     * Syntax: `[e1, e2, e3]` → `ArrayLitExpr(elements: list[Expr])`.
     * Type inference rule: all `ei` must unify to the same element type `T`.
   * Indexing:

     * Syntax: `a[i]` → `IndexExpr(container: Expr, index: Expr)`.
     * For assignments: `a[i] = v` → same AST node in lvalue position, or a distinct `IndexAssignExpr`, your choice.

3. **HIR**

   * Preserve `ArrayTypeRef` and `ArrayLitExpr` / `IndexExpr` in HIR.
   * Make sure type arguments (`T`) are attached early so MIR’s `elem_ty: TypeId` is trivial to fill.

**Deliverable:** Parser tests:

* `Array<Int>`, `Array<String>`, nested `Array<Array<Int>>`.
* `[1, 2, 3]` literal.
* `xs[i]` and `xs[i] = v`.
* Negative cases: malformed generics, empty literal without annotation, `xs[]`, etc.

---

## 3. Checker: type rules for arrays

**Goal:** Arrays become real, fully type-checked values.

1. **Types**

   * When resolving `ArrayTypeRef(elem)`, ensure `elem` is a valid type; produce a canonical `TypeId` representing `Array(elem_ty)`.

2. **Literals**

   * For `[e1, e2, ...]`:

     * Infer each `ei` type.
     * Unify to a single `T`; if they don’t unify → diagnostic “array literal element types don’t match.”
     * Assign expression type `Array<T>`.
   * Special-case empty literals:

     * Either disallow `[]` for now, or require explicit type: `([]: Array<Int>)`. Pick one and enforce.

3. **Indexing**

   * For `a[i]`:

     * Ensure `a: Array<T>` for some `T`.
     * Ensure `i: Int` (your language-level `Int`).
     * Result type: `T`.
   * For `a[i] = v`:

     * All of the above, plus ensure `v: T` (or coercible to `T` under your usual rules).

4. **len / capacity**

   * If you expose methods now:

     * `a.len` and `a.capacity()` must have type `Size`.
   * Or, if you use builtin functions (`len(a)`, `cap(a)`):

     * Check argument `a: Array<T>` and assign return type `Size`.

**Deliverable:** Checker tests that assert:

* Correct types for `[1, 2]`, `[x, y]` where `x, y: String`.
* Rejection of mixed-type literal `[1, "x"]`.
* `a[i]` type is `T`.
* Wrong index type or non-Array container is rejected.

---

## 4. MIR design for arrays

**Goal:** Add the exact MIR nodes from the blueprint, plus the missing store/len/cap bits.

Blueprint gives you: `ArrayLit` and `ArrayIndexLoad`. 

I’d define the set as:

* `ArrayLit { elem_ty: TypeId, elements: [ValueId] }`
* `ArrayIndexLoad { elem_ty: TypeId, array: ValueId, index: ValueId }`
* `ArrayIndexStore { elem_ty: TypeId, array: ValueId, index: ValueId, value: ValueId }`
* `ArrayLen { array: ValueId } -> Size`
* `ArrayCap { array: ValueId } -> Size` (if you expose capacity at surface level)

**Lowering from HIR:**

* Literal expression `[e1, e2]`:

  * Lower `e1, e2` to MIR values `v1, v2`.
  * Emit `ArrayLit{ elem_ty=T, elements=[v1, v2] }`.
* `a[i]` in rvalue position → `ArrayIndexLoad{ elem_ty, array=a_val, index=i_val }`.
* `a[i] = v` → `ArrayIndexStore`.
* `len(a)` or `a.len` → `ArrayLen(array=a_val)`.
* `cap(a)` / `a.capacity()` → `ArrayCap`.

**SSA verifier:**

* Enforce that `array` has type `Array<elem_ty>`.
* Enforce that `index` has type `Int` at MIR level (will downcast in LLVM).

---

## 5. Runtime / ABI implementation

**Goal:** Provide the C side exactly as described.

1. Implement:

   ```c
   typedef size_t drift_size;

   typedef struct DriftArrayHeader {
   	drift_size len;
   	drift_size cap;
   	void      *data;
   } DriftArrayHeader;

   void *drift_alloc_array(size_t elem_size,
                           size_t elem_align,
                           drift_size len,
                           drift_size cap);

   noreturn void drift_bounds_check_fail(drift_size idx,
                                         drift_size len);
   ```

2. Policy decisions:

   * `drift_alloc_array`:

     * allocate `elem_size * cap` bytes, aligned to `elem_align`.
     * set `len` and `cap` in the header accordingly.
     * return pointer to `data` (per blueprint; header is carried separately in the `%drift.Array$T` struct). 
   * `drift_bounds_check_fail`:

     * construct an “index out of bounds” `Error` and `throw` it, *or* abort if your unwinding isn’t fully wired yet. But the signature and no-return behavior must be correct now.

3. Make sure `Size` in the language maps cleanly to `drift_size` at ABI boundary.

---

## 6. LLVM lowering (MIR → LLVM IR)

Here you basically just transcribe the blueprint into your lowering code. 

### 6.1 Array literals

For `ArrayLit { elem_ty, elements=[v0, v1, ...] }`:

1. Evaluate `v0..vn` to LLVM values of type `T`.

2. Compute constants:

   * `len = n : %drift.size` (const).
   * `cap = len`.

3. Call allocator:

   ```llvm
   %raw = call ptr @drift_alloc_array(
       i64         %elem.size,   ; constant for sizeof(T)
       i64         %elem.align,  ; constant for alignof(T)
       %drift.size %len.const,
       %drift.size %cap.const
   )
   %data = bitcast ptr %raw to T*
   ```

4. Store elements:

   ```llvm
   ; For each i in [0..n-1]:
   %i.const   = <const %drift.size i>
   %elem.ptr  = getelementptr inbounds T, T* %data, %drift.size %i.const
   store T %val.i, T* %elem.ptr, align %elem.align
   ```

5. Build `%drift.Array$T`:

   ```llvm
   %tmp0 = insertvalue %drift.Array$T undef, %drift.size %len.const, 0
   %tmp1 = insertvalue %drift.Array$T %tmp0, %drift.size %cap.const, 1
   %arr  = insertvalue %drift.Array$T %tmp1, T* %data, 2
   ```

6. Zero-length case:

   * `len = cap = 0`.
   * `drift_alloc_array` may return null `data`; that’s fine since there will be no indexing.

### 6.2 Indexed loads

Given MIR: `ArrayIndexLoad { elem_ty=T, array=a, index=i }`.

Translate:

1. Extract fields:

   ```llvm
   %len  = extractvalue %drift.Array$T %arr, 0 ; %drift.size
   %data = extractvalue %drift.Array$T %arr, 2 ; T*
   ```

2. Index conversion (Int → %drift.size) and negative check:

   ```llvm
   ; %idx : %drift.int (signed Int)
   %is.neg   = icmp slt %drift.int %idx, 0
   %idx.size = zext %drift.int %idx to %drift.size
   ```

3. Bounds check:

   ```llvm
   %too.big = icmp uge %drift.size %idx.size, %len
   %oob     = or i1 %is.neg, %too.big

   br i1 %oob, label %oob.block, label %ok.block

   oob.block:
       call void @drift_bounds_check_fail(%drift.size %idx.size,
                                          %drift.size %len)
       unreachable
   ```

4. Safe load in `ok.block`:

   ```llvm
   %elem.ptr = getelementptr inbounds T, T* %data, %drift.size %idx.size
   %elem     = load T, T* %elem.ptr, align <alignof(T)>
   ```

### 6.3 Indexed stores

`ArrayIndexStore` is the same except you emit a `store` instead of `load`.

### 6.4 len / cap

* `ArrayLen`:

  * `extractvalue %drift.Array$T %arr, 0`.
* `ArrayCap`:

  * `extractvalue %drift.Array$T %arr, 1`.

---

## 7. Tests and migration

Since you’re going deep, don’t skimp on tests; port them aggressively.

1. **Unit tests (parser + checker)**:

   * Array type parsing and unification.
   * Array literal typing, including failure modes.
   * Indexing type rules.

2. **MIR tests**:

   * HIR → MIR for a few patterns:

     * `let xs = [1, 2, 3]; xs[1]`
     * `xs[i] = xs[i] + 1`
     * `let n = len(xs);`

3. **LLVM/codegen tests**:

   * Golden IR tests for simple cases:

     * Literal `[1, 2]` produces the expected `drift_alloc_array` call and stores.
     * Index load emits the exact `icmp`/`or`/branch shape plus the call to `drift_bounds_check_fail`.

4. **Runtime tests**:

   * Valid indexing: `xs = [10, 20, 30]; assert(xs[1] == 20)`.
   * Bounds failure: `xs[3]` and `xs[-1]` must raise the proper `Error` / abort consistently.

5. **Old lang → lang2 parity**:

   * Pick a couple of old lang array e2e tests (especially ones that index in loops) and port them verbatim to lang2; they become your regression suite for future refactors.
