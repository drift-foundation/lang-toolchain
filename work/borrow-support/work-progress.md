# Borrow Support — Work Progress

Goal: full end-to-end MVP support for `&T` / `&mut T`, with real coverage (notably `&String` passed into functions), aligned with `docs/design/drift-lang-spec.md`.

This tracker is authoritative for what we consider “done” vs “still missing” for borrow support.

---

## MVP Semantics (Locked)

### Representation
- `&T` / `&mut T` is a **non-null pointer to storage of `T`**.
- Type system representation: `TypeKind.REF { inner: T, mut: bool }`.

### Borrowable expressions (“addressable places”)
- `&x` / `&mut x` is only valid when `x` is an **addressable place** (local, param, field, index, deref-place later).
- `&(some_call())` is **rejected** in MVP unless we explicitly implement temporary materialization.

### Mutability rule (MVP)
- `&mut x` requires `x` is declared `var` (not `val`).
- Only enforce a **local / within-expression** exclusivity rule for MVP (no full lifetime analysis yet).

### What “full support” means (MVP)
You can:
- create a ref: `&x`, `&mut x`
- pass refs into functions
- store refs in locals
- dereference refs for reads/writes (`*p` and `*p = v`)

Restrictions we keep explicit (reject with diagnostics):
- returning references to locals (until we have a lifetime model)
- storing references into long-lived heap objects (future)
- capturing refs in closures (future)

---

## Status

### Done
- Surface syntax:
  - Parse `&x` / `&mut x` in expressions.
  - Parse unary deref `*p` and allow deref as an assignment target (`*p = v`).
- Stage0/Stage1:
  - Thread `val`/`var` mutability into HIR (`HLet.is_mutable`).
  - HIR supports deref via `UnaryOp.DEREF`.
- Type system:
  - Resolve type expressions `&T` / `&mut T` to `TypeKind.REF` (shared helper `resolve_opaque_type`).
- Typed checker:
  - Type `&T` / `&mut T` as `Ref` / `RefMut`.
  - Enforce MVP borrow rules:
    - borrow operand must be an addressable local/param (reject rvalues)
    - `&mut` requires `var`
    - within-statement exclusivity for borrows of the same place (Place-keyed; future-proof for projections)
  - Type deref `*p` and enforce `*p = v` requires `&mut`.
- Borrow checker (CFG/dataflow scaffold):
  - Treat `*p` as an lvalue place (deref projection) so `*p = v` is accepted.
- MIR:
  - Lower `HBorrow(&local)` → `AddrOfLocal(local, is_mut=...)`.
  - Lower `*p` → `LoadRef(inner_ty=...)`.
  - Lower `*p = v` → `StoreRef(inner_ty=...)`.
  - Allow builtin `len/byte_length` to accept `&String` / `&Array<T>` by implicit deref at the builtin boundary (no global autoderef).
- SSA:
  - Detect address-taken locals (`AddrOfLocal`) and keep them as real storage (do not SSA-rename `LoadLocal`/`StoreLocal`).
- LLVM/codegen:
  - Lower `&T` / `&mut T` to typed pointers (`T*`) in LLVM IR (clang-friendly).
  - Materialize address-taken locals as `alloca` + `load`/`store`.
  - Lower `AddrOfLocal`, `LoadRef`, `StoreRef`.
  - Force LLVM emission order to start with the function entry block so entry allocas are guaranteed to be in the real LLVM entry block.
- Tests:
  - Added e2e coverage:
    - `lang2/codegen/tests/e2e/borrow_string_param` (`&String` passed into a function).
    - `lang2/codegen/tests/e2e/borrow_mut_int` (`&mut Int` + `*p = *p + 1`).
    - negative cases: `&mut` of `val`, borrow of rvalue.

### Remaining Work

#### 1) Places beyond locals
- Introduce a canonical “place” shape at the stage1/stage2 boundary:
  - `HPlace`: `Local(name) | Field(base, field) | Index(base, idx) | Deref(base) | …`
- Extend borrow/deref lowering to cover:
  - `&s.field`
  - `&arr[i]`
  - nested projections

#### 2) Temporary materialization
- Keep rejecting `&(rvalue)` for MVP.
- Optional extension: materialize rvalues into a hidden local, then borrow the temp.

#### 3) Stronger tests (non-blocking)
- Add targeted tests for:
  - borrow conflict detection (shared vs mut) within one statement
  - place borrowing once fields/indexes are supported

---

## Notes / Known Sharp Edges
- No lifetimes yet: reject reference escape patterns (returning refs, storing in long-lived objects, closure capture).
- No autoref/autoderef in MVP: keep call sites explicit until semantics are stable.
