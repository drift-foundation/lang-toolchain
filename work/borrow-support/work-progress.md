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
 - Reborrows are supported: `&*p` / `&mut *p`.
 - Shared borrows of rvalues are supported via temporary materialization: `&(<expr>)` becomes `val tmp = <expr>; &tmp`.

### Mutability rule (MVP)
- `&mut x` requires `x` is declared `var` (not `val`).
- Only enforce a **local / within-expression** exclusivity rule for MVP (no full lifetime analysis yet).

### What “full support” means (MVP)
You can:
- create a ref: `&x`, `&mut x`
- pass refs into functions
- store refs in locals
- dereference refs for reads/writes (`*p` and `*p = v`)

### Lifetime + closures (Pinned policy; MVP-friendly, no new syntax)
- Borrowed return support:
  - `returns &U` / `returns &mut U` is allowed only when every returned reference is **derived from a reference parameter**
    (same base parameter, plus projections like `.field`, `[i]`, `*p`).
  - Returning borrows of locals/temporaries is rejected.
  - If multiple reference parameters exist, all returns must derive from the **same** parameter (otherwise reject as ambiguous).
- Ref storage:
  - Refs may be stored in **local variables** (within a scope), but remain subject to freeze/escape rules.
  - Refs stored into structs/globals/heap objects are rejected in MVP.
- Freeze while borrowed:
  - When a reference is taken to an element/place derived from a base owner (e.g. `items[i]`), the base owner is considered **frozen**
    (operations that can invalidate element storage are rejected) for as long as the borrow is live.
  - MVP liveness: lexical (“until end of scope”) for local ref variables.
- Aliasing rule (per-place):
  - While `&place` (shared borrow) is live, writes to `place` (or overlapping projections) are rejected.
  - While `&mut place` (mutable borrow) is live, any other read/write borrow of `place` (or overlapping projections) is rejected.
  - Intuition: many readers or one writer, never both.
- Projection overlap (MVP precision):
  - Constant indices are distinguished: `items[0]` and `items[1]` are treated as disjoint for conflict checks.
  - Unknown/non-constant indices are treated as overlapping everything (`IndexProj.ANY` overlaps any index).
- Closures:
  - Two classes of closures:
    - Borrowing closures (capture `&`/`&mut` or take `&` params): **non-escaping only**, allowed only in compiler-known non-escaping contexts.
      During such a call, borrowed bases are frozen for the duration of the call.
    - Escaping closures: **by-value only** (cannot capture or accept `&`/`&mut`, cannot return refs).
  - Borrowing closures cannot be stored/returned/captured or passed to unknown functions in MVP.

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
  - Projection-aware overlap:
    - Fields with different names are disjoint (`x.a` vs `x.b`).
    - Constant indices are disjoint (`arr[0]` vs `arr[1]`).
    - Unknown indices overlap everything (`arr[i]` overlaps `arr[0]`).
    - Prefix overlap counts (`x` overlaps `x.a` and `x[0]`).
  - Freeze while borrowed:
    - Reject writes to any place overlapping a live loan (no “dropping loans on assignment”).
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
    - `lang2/codegen/tests/e2e/borrow_struct_field_local` (`&mut p.x` where `p` is a local struct).
    - `lang2/codegen/tests/e2e/borrow_struct_field_param` (`&mut (*p).x` where `p: &mut Struct`).
    - `lang2/codegen/tests/e2e/borrow_struct_field_disjoint_write_ok` (borrow `&p.x` then write `p.y` succeeds).
    - `lang2/codegen/tests/e2e/borrow_struct_field_write_rejected` (borrow `&p.x` then write `p.x` is rejected).
    - `lang2/codegen/tests/e2e/borrow_struct_overwrite_rejected` (borrow `&p.x` then overwrite `p` is rejected).
    - negative cases: `&mut` of `val`, borrow of rvalue.
  - Added unit coverage:
    - Write-while-borrowed is rejected.
    - Const-vs-unknown index overlap rules.
    - `BorrowChecker.from_typed_fn` preserves param vs local `PlaceKind`.

### Remaining Work

#### 1) Places beyond locals
- Introduce a canonical “place” shape at the stage1/stage2 boundary:
  - `HPlace`: `Local(name) | Field(base, field) | Index(base, idx) | Deref(base) | …`
- Extend borrow/deref lowering to cover:
  - `&s.field` / `&mut s.field`
  - `&arr[i]` / `&mut arr[i]`
  - nested projections (`&p.items[i].name`, `&(*p).field`, etc.)

Partial progress:
- Reborrow via deref place is supported end-to-end: `&*p` / `&mut *p`.
- Array element borrows are supported end-to-end: `&arr[i]` / `&mut arr[i]` (MVP: base must be a local/param array binding; bounds checked at borrow).
- Nested array borrows through a reference are supported: `&(*p)[i]` / `&mut (*p)[i]`.
- Struct field borrows are supported end-to-end for addressable struct places: `&p.x`, `&mut p.x`, `&(*p).x`, and combinations with indexing.

#### 2) Temporary materialization
- Shared borrow of rvalues is supported via materialization (`&(expr)` becomes `val tmp = expr; &tmp`).
- `&mut (rvalue)` remains rejected in MVP (no implicit temp materialization for mutable borrows).

#### 3) Stronger tests (non-blocking)
- Add targeted tests for:
  - borrow conflict detection (shared vs mut) within one statement
  - place borrowing once fields/indexes are supported

---

## Notes / Known Sharp Edges
- No lifetimes yet: reject reference escape patterns (returning refs, storing in long-lived objects, closure capture).
- No autoref/autoderef in MVP: keep call sites explicit until semantics are stable.
