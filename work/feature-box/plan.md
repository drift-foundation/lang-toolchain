# Box<T> — Initial Public Design — Implementation Plan

**Status:** DRAFT — for static review. No implementation, no git operations performed.
**Author context:** drafted against driftc 0.33.32 / runtime ABI 17.
**Motivation:** give users a first-class, value-semantic heap indirection that breaks
recursive value-type layout cycles (the `E_RECURSIVE_VALUE_TYPE` case) without the
`Array<T>`-of-one workaround, and without sharing semantics (unlike `Arc<T>`).

---

## 1. Target contract (as specified)

```drift
pub struct Box<T> { /* opaque: single heap-owned T */ }

pub fn box<T>(value: T) nothrow -> Box<T>;

implement<T> Box<T> {
    pub fn get(self: &Box<T>) nothrow -> &T;
    pub fn get_mut(self: &mut Box<T>) nothrow -> &mut T;
    pub fn take(self: move Box<T>) nothrow -> T;   // consume box, move T out, free the cell
}
```

Semantics: exactly one heap-owned `T`; **explicit access only** — no compiler
auto-deref, no implicit unboxing, no `Box<T> → T` coercion. `Box<T>` is **move-only**
(not `Copy`, not `Share`, not `ConstShare`). Dropping a `Box<T>` runs `T`'s destructor
(if any) exactly once, then frees the cell.

---

## 2. Where Box<T> lives — module placement

**Decision: define `Box<T>` in a new submodule `std.core.box`, parallel to
`std.core.arc`, and place the `Destructible` impl in `std.core` (core.drift).**

Rationale, from the existing dependency DAG:

- `std.mem` (`stdlib/std/mem/mem.drift:1`) is the **root** memory layer — it imports
  no other `std` module. It owns `RawBuffer<T>` (`mem.drift:15`) and the alloc/read/
  write/dealloc intrinsics.
- `std.core` (`stdlib/std/core/core.drift`) **depends on** `std.mem`
  (`core.drift:8`), never the reverse.
- `Arc<T>` already lives in `std.core.arc` (`stdlib/std/core/arc.drift`), which
  **imports only `std.mem` + `lang.atomic`** and deliberately does NOT import
  `std.core` to avoid a cycle (`arc.drift:23-35`). The `Destructible`/`Share` impls
  for `Arc<T>` live in `core.drift` (`core.drift:752-776`), which is free to import
  the `arc` submodule.

`Box<T>` follows the same split exactly:

- **`std.core.box`** — defines `pub struct Box<T>`, `pub fn box<T>`, the inherent
  `get`/`get_mut`/`take`, and a per-`T` drop helper. Imports **only `std.mem`**
  (no `lang.atomic` — Box has no refcount). Does NOT import `std.core`.
- **`core.drift`** — `implement<T> core.Destructible for box_mod.Box<T>` (mirrors
  the Arc pattern at `core.drift:752-757`), and re-export `box`/`Box` from the
  `std.core` export list (mirrors `arc` re-export at `core.drift:98`).

**Rejected: `std.mem`.** `Box<T>` needs the `Destructible` trait (in `std.core`); a
`std.mem`-resident `Box` would force `std.mem → std.core`, closing a cycle. The
`Destructible` impl must sit in `std.core` regardless, so the struct belongs in a
`std.core` submodule.

**Rejected: a brand-new `std.memory` top-level module.** No such module exists today;
introducing one only to host `Box` adds a module + dependency edges for no benefit
over the proven `std.core.arc` sibling pattern.

---

## 3. Implementation strategy — reuse, no new runtime boundary

**Box<T> is pure stdlib over existing `std.mem` intrinsics. It needs NO new
`drift_*` runtime symbol and therefore NO ABI bump.** This mirrors `Arc<T>`, which is
built entirely from `mem.alloc_uninit` / `mem.write` / `mem.read` / `mem.dealloc`
plus per-`T` monomorphized helpers — no Arc-specific C runtime function exists
(`arc.drift:139-275`).

### 3.1 Layout

```drift
// std.core.box
import std.mem as mem;

pub struct Box<T> {
    buf: mem.RawBuffer<T>   // one-element cell; { ptr: Ptr<Byte>, cap: Int }
}
```

Two layout options — recommend **(A)** for infra reuse and parity with Arc:

| Option | Field | In-struct size (64-bit) | Notes |
|--------|-------|--------------------------|-------|
| **(A)** reuse `RawBuffer<T>` | `buf: RawBuffer<T>` | 16 B (`ptr` + `cap`) | reuses every `mem.*` op verbatim; `cap` is always 1 (vestigial) |
| (B) bare pointer | `ptr: mem.Ptr<Byte>` | 8 B | leaner, but needs `ptr_*` paths instead of `rawbuffer_*`, and a typed view reconstructed via `rawbuffer_from_parts` on every op |

Option (A) keeps Box byte-for-byte parallel to Arc-minus-header and reuses the exact
move-out/sentinel machinery already exercised by Arc/TypeBox/Deque. The 8 vestigial
bytes are acceptable for v1. **Plan assumes (A) and FREEZES it as the committed v1
layout.**

> **Finding 3 (corrected): the initial layout is frozen; a later (A)→(B) change is
> NOT simply "non-breaking."** Opaqueness protects only *source-level construction*
> (callers can't name the fields), not the **compiled package layout**: a package
> that embeds `Box<T>` by value bakes the 16-byte `RawBuffer` layout into its emitted
> metadata/ABI, so already-compiled consumers would mismatch if the size/shape
> changed. Therefore the 16-byte Option (A) is the committed v1 layout; any future
> move to an 8-byte pointer (or other shape) is a **package-format/ABI review item**
> (treat as a boundary change requiring the normal version bump + rebuild-through-cert
> process), not a silent internal tweak. The cost-comparison in §9 reflects the frozen
> 16-byte layout.

### 3.2 Constructor

```drift
pub fn box<T>(var value: T) nothrow -> Box<T> {
    unsafe {
        var buf = mem.alloc_uninit<type T>(1);
        mem.write<type T>(&mut buf, 0, move value);   // raw store, no drop of uninit slot
        return Box<type T>(buf = move buf);
    }
}
```

Identical shape to `arc()` (`arc.drift:139-151`) minus the `ArcHeader`/refcount.

### 3.3 Accessors

```drift
implement<T> Box<T> {
    pub fn get(self: &Box<T>) nothrow -> &T {
        unsafe { return mem.ptr_at_ref<type T>(&self.buf, 0); }
    }
    pub fn get_mut(self: &mut Box<T>) nothrow -> &mut T {
        unsafe { return mem.ptr_at_mut<type T>(&mut self.buf, 0); }
    }
    pub fn take(var self: Box<T>) nothrow -> T {
        unsafe {
            // Drain BEFORE the implicit destructor runs on `self`: swap the live
            // cell out for an empty sentinel so Destructible::destroy sees null and
            // no-ops (no double free / double drop of T).
            var old = mem.replace<type mem.RawBuffer<T>>(&mut self.buf, mem.rawbuffer_empty<type T>());
            var v = mem.read<type T>(&mut old, 0);   // move T out of the heap cell
            mem.dealloc<type T>(move old);            // free the cell (T already moved out)
            return move v;
        }
    }
}
```

`get`/`get_mut` are direct, bounds-check-free pointer projections (single cell,
index 0). `take` uses the **drained-sentinel** idiom already used by
`TypeBox::take` (`runtime.drift:608-627`) and `Deque::ensure_capacity`
(`array.drift:517`): replace `buf` with `rawbuffer_empty` first so the destructor
that fires on the consumed `self` is a no-op.

> Receiver form note: the spec writes `take(self: move Box<T>)`. The stdlib idiom for
> a consuming receiver is `var self: Box<T>` (as in `_arc_destroy_impl`,
> `arc.drift:254`). Confirm during implementation whether `self: move Box<T>` is
> accepted surface syntax or whether `var self` is the canonical spelling; the
> semantics (by-value consume) are identical. **Open question Q1.**

### 3.4 Destructor (drops nested-droppable T exactly once)

```drift
// in core.drift, mirroring core.drift:752-757
implement<T> core.Destructible for box_mod.Box<T> {
    pub fn destroy(var self: box_mod.Box<T>) nothrow -> Void {
        unsafe {
            val ptr = mem.rawbuffer_ptr<type T>(&self.buf);
            if mem.ptr_is_null<type Byte>(ptr) { return; }   // drained (post-take) sentinel
            // Drift forbids a partial move of a field out of `self`
            // (`mem.dealloc(move self.buf)` is illegal). Use the canonical
            // move-one-field-out primitive `mem.replace` first, then read/drop/
            // dealloc the displaced buffer (same shape as `take` and TypeBox).
            var old = mem.replace<type mem.RawBuffer<T>>(&mut self.buf, mem.rawbuffer_empty<type T>());
            var v = mem.read<type T>(&mut old, 0);
            core.drop_value<type T>(move v);                 // runs T's destructor if Destructible
            mem.dealloc<type T>(move old);
        }
        return;
    }
}
```

> **Finding 1 (corrected):** the destructor must NOT `mem.dealloc(move self.buf)` —
> that is an illegal partial move of `self.buf` out of `self`. It must `mem.replace`
> `self.buf` with `rawbuffer_empty` first, then read/drop/dealloc the displaced
> buffer. This is the same `mem.replace`-first discipline `take` (§3.3) and
> `TypeBox::take` (`runtime.drift:608-627`) already use, and it is the canonical
> "move one field out of an owned struct" primitive (`mem.drift:151-155`).

`drop_value<T>` (`arc.drift:65`, used by the Arc drop thunk) recursively drops a
nested-droppable `T` exactly once; the null-sentinel guard makes the destructor
idempotent and safe after `take`. Unlike Arc, there is **no refcount and no
`drop_thunk` indirection** — the destructor is the per-`T` monomorphized method
itself, so `T`'s destructor is reached directly.

**`destroy` is an ordinary generic `Destructible` method — no intrinsic, no
fallback.** Arc's `destroy` is `@intrinsic` (`core.drift:752-757`) ONLY because Arc
routes through the `ARC_DESTROY` IntrinsicKind for fat `Arc<Interface>`
special-casing (`hir_to_mir.py:9771-10301`) — **not** because generic destructors
require an intrinsic. Ordinary generic `Destructible` impls already work and are
exercised by other generic stdlib types (e.g. `Array<T>`, `ScopeGuard<T>`): the
existing destructor-insertion machinery calls the plain monomorphized method at
scope exit. Box has no fat/interface case, so Box's `destroy` is a **plain generic
method — no new IntrinsicKind, no `hir_to_mir` lowering branch, no `BOX_DESTROY`
fallback.** Per the review, the speculative `BOX_DESTROY` intrinsic is **removed**
from this plan; introduce one only if a regression actually demonstrates a compiler
defect in the generic-destructor path (treat that as a separate compiler bug, not
expected Box work).

---

## 4. Minimal public API

Beyond the three required accessors, the **only** additional public surface is:

- `pub fn box<T>(value: T) nothrow -> Box<T>` — construction (required).
- `implement<T> Destructible for Box<T>` — destruction (required; not "extra" API
  but a required impl).

Explicitly **NOT** added in v1 (keep the surface minimal; each is a future,
non-breaking addition): `clone`/`share` (would contradict unique ownership),
`map`/`replace`/`into_inner` aliases, `Box::from_raw`/`leak`, `Default`,
comparison/hash/format passthroughs, `Copy`/`ConstShare`. No auto-deref or `Deref`-style
trait is introduced — access is exclusively through `get`/`get_mut`/`take`.

---

## 5. Ownership, move, destruction, allocation-failure, nested-droppable T

- **Unique ownership / move:** `Box<T>` has a `Destructible` impl ⇒ the type system
  marks it **non-Copy** automatically (Copy is denied for any type with a destructor;
  `types_core.py:2687-2708`). Assignment/passing moves it; the borrow checker enforces
  no use-after-move. No `Share`/`ConstShare` impl ⇒ it cannot be duplicated.
- **Destruction:** scope-exit destructor (§3.4) drops `T` then frees; idempotent via
  the null sentinel. `take` transfers `T` out and leaves the box drained so the
  trailing destructor no-ops — exactly one free, exactly one (or zero, if taken) drop
  of `T`.
- **Nested-droppable T:** `drop_value<T>` dispatches to `T`'s destructor; a
  `Box<SomethingDestructible>` drops the inner value before freeing the cell. A
  `Box<Box<U>>` drops the inner box (which drops `U` and frees) before freeing the
  outer cell — recursion terminates because each layer is a distinct allocation.
- **Allocation failure:** `box<T>` returns `Box<T>` (not `Result`), so it must follow
  the same OOM policy as `arc<T>` / `mem.alloc_uninit`. **Open question Q3:** confirm
  `mem.alloc_uninit`'s failure mode (abort-on-OOM vs null return). If `alloc_uninit`
  aborts on OOM, `box` inherits abort (consistent with `arc`). If it can return a
  null buffer, either (a) `box` aborts explicitly on null for v1 (documented), or
  (b) a separate `try_box<T>(value) -> Result<Box<T>, AllocError>` is added later
  (non-breaking). Recommend (a) for v1 to match `arc` and keep the signature clean.

---

## 6. Recursive-value detector — structural recognition, no name allowlist

**Finding: `Box<T>` breaks recursive value-type cycles automatically and
structurally, with NO change to the detector's cycle logic and NO name allowlist.**

The detector `validate_no_recursive_value_types` (`type_checker.py:818-1115`) is
kind-based. It builds by-value edges via `_by_value_children`
(`type_checker.py:873-886`), which **stops at any field whose resolved `TypeKind` is
in** `_INDIRECTION_KINDS = {REF, RAW_PTR, ARRAY, FUNCTION, INTERFACE}`
(`type_checker.py:844-850`), and otherwise recurses into STRUCT/VARIANT field types.

Why `Box<T>` (Option A, `buf: RawBuffer<T>`) is non-recursive by construction:

- `RawBuffer<T>` is a **struct** `{ ptr: Ptr<Byte>, cap: Int }` (`mem.drift:15-18`).
  Its monomorphized `field_types` are `[Ptr<Byte>, Int]` — **`T` is a phantom type
  parameter that is never stored by value.** The heap pointer is type-erased to
  `Ptr<Byte>` (`RAW_PTR`).
- So the edge walk from a monomorphized `Box<IrType>` is:
  `Box<IrType> → RawBuffer<IrType> (struct) → { Ptr<Byte>:RAW_PTR (suppressed),
  Int:SCALAR (not struct/variant) }` → **dead end; `IrType` is never reached.**
- Therefore `variant IrType { TArray(elem: Box<IrType>) }` has **no** by-value cycle
  and is **accepted**, while `TArray(elem: IrType)` keeps its direct self-edge and is
  **rejected** — exactly the desired behavior, decided purely by kind/structure.

This is the same mechanism that makes `Arc<T>` (`buf: RawBuffer<ArcBox<T>>`) and a
1-element `Array<T>` cycle-breaking today. The detector already has **no name
allowlist** for indirection (the only name check is the diagnostic-suggestion helper
`_suggest_indirection`, `type_checker.py:994-997`, which is cosmetic and does not
affect detection). **No detector change is required for correctness.**

### 6.1 Recommended (small) detector-adjacent change — diagnostic suggestion

`_suggest_indirection` currently always proposes `Arc<Self>` (or
`Optional<Arc<Self>>`). For **value-semantic, unique-ownership** recursion, `Box<Self>`
is the more appropriate fix (Arc implies shared ownership + atomic refcount cost).
Recommend updating the suggestion to offer `Box<Self>` as the primary value-recursion
remedy (and `Optional<Box<Self>>` when the field is `Optional<...Self...>`), still a
principled, name-allowlist-free message. This is the **only** code change touching the
detector, and it is message-only (no logic change). The existing
`test_recursive_value_struct_diagnostic.py` asserts the suggestion text and must be
updated in lockstep. **Open question Q4:** keep `Arc` as the suggestion, switch to
`Box`, or list both? Recommend listing `Box<Self>` first (cheapest correct fix) and
mentioning `Arc<Self>` for the shared-ownership case.

### 6.2 Regression to pin the structural property

Add a detector regression that does NOT rely on the name "Box": construct a minimal
RawBuffer-backed wrapper and assert a variant routing its self-reference through it is
accepted, and that the same variant by value is rejected — pinning that recognition is
structural (phantom-T / RAW_PTR), so a future refactor of `Box`'s internals can't
silently make it stop breaking cycles. (If `Box`'s internal field ever changed to hold
`T` by value, this regression would catch the regression.)

---

## 7. ABI impact and versioning

**No ABI bump. ABI stays 17.**

- `Box<T>` introduces **no new runtime boundary symbol**: it composes existing
  `std.mem` intrinsics (`alloc_uninit`/`dealloc`/`read`/`write`/`ptr_at_ref`/
  `ptr_at_mut`/`replace`/`rawbuffer_empty`/`rawbuffer_ptr`/`ptr_is_null`), all already
  part of ABI 17 (`mem.drift:54-155`). Cross-checked against `versions.py:8-15`: ABI
  bumps are reserved for runtime-exported helper signatures, boundary data layouts,
  calling conventions, or ownership/drop-contract changes — none of which Box touches.
- `Box<T>` is a normal generic stdlib struct; its monomorphized instances and generic
  templates ride the **existing** package-metadata mechanism (§8), which is governed by
  the package format version, not the runtime ABI. No package-format change is needed
  (Box uses no new metadata kind beyond what `Arc<T>` already exercises).
- DRIFTC version: a patch/minor bump for the new stdlib surface
  (`versions.py:14`), per the project's normal stdlib-feature convention — **not** an
  ABI change. (Confirm with the ABI policy: a pure additive stdlib type over existing
  intrinsics is a DRIFTC version change only.)

---

## 8. Generics, packaging, Copy/ConstShare, borrowing, exception safety, codegen

- **Monomorphization:** `Box<Int>`/`Box<IrType>` instantiate through
  `ensure_struct_instantiated` (`types_core.py:1310-1385`); `box`/`get`/`get_mut`/
  `take`/`destroy` instantiate as generic-method templates
  (`__inst__<hash>`, `driftc.py:4566-4571`). Identical to the Arc path; no new
  monomorphization machinery. No fat/interface instantiation path (Box never erases to
  an interface), so it avoids the Stage-3 fat-`Arc<Interface>` complexity entirely
  (`types_core.py:293,1354-1372`).
- **Package metadata:** `Box<T>`'s templates serialize via the existing
  `generic_templates` channel (`provisional_dmir_v0.py:1590-1672`) with
  `decl_fingerprint` identity, and re-instantiate on the consumer through
  `type_table_link_v0`. A package that exports a type using `Box<T>` by value, or a
  recursive variant broken by `Box<Self>`, must round-trip emit→consume cleanly — this
  is exactly the path the recent recursive-value-validation fix hardened, so it gets a
  dedicated regression (§9).
- **Copy/ConstShare:** non-Copy is automatic from the `Destructible` impl
  (`types_core.py:2687-2708`). Box declares **no** `Share`/`ConstShare`/`Frozen`
  impl ⇒ it is move-only and cannot be const-shared. **Per the review, this must be
  asserted explicitly, not merely assumed:** a negative test asserts that `Box<T>`
  neither *proves* nor *auto-derives* `ConstShare` (e.g. using a `Box<T>` where a
  `ConstShare`-bound type parameter is required is rejected, and querying
  const-share status reports false), **alongside** the existing non-Copy and
  no-`Share`/copy-by-assignment rejections. The risk being guarded is a future
  blanket/auto-derive rule silently granting `ConstShare` to a `Destructible`
  wrapper.
- **Borrowing:** `get`/`get_mut` return `&T`/`&mut T` borrowed **from the box**; the
  borrow checker ties the returned reference's lifetime to the `&self`/`&mut self`
  borrow of the box (standard field-projection borrow, as for `Arc::borrow`,
  `core.drift:740-750`). `&mut` exclusivity is enforced normally. No auto-deref means
  no implicit borrow extension — every access is an explicit call site.
- **Exception safety:** all stdlib methods are `nothrow`. The one window to reason
  about is `box<T>`: after `alloc_uninit` succeeds and before `write` completes there
  is no throwing call (write is an intrinsic store), so there is no partial-init leak.
  `take`'s drain-then-read-then-dealloc sequence is panic-free (all intrinsics). If a
  user `T`'s own destructor can abort, that is `T`'s contract, not Box's.
- **Codegen layout:** `Box<T>` lowers to `{ ptr, i64 }` (the `RawBuffer<T>` LLVM
  struct); `get`/`get_mut` are a load + GEP(0); the destructor is a synthesized per-`T`
  function the scope-exit drop calls (same shape as Arc's destructor minus the atomic
  decrement and thunk indirection). No new codegen node is required.

---

## 9. Cost comparison vs the `Array<T>`-of-one workaround

Today users break value recursion with a 1-element `Array<T>` (or `Array<Self>`).
Concrete comparison (64-bit), per recursive cell:

| Dimension | `Array<T>` (len 1) workaround | `Box<T>` (Option A) |
|---|---|---|
| In-struct footprint | Array header: `ptr` + `len` + `cap` ≈ **24 B** | `RawBuffer`: `ptr` + `cap` = **16 B** (8 B if Option B) |
| Heap allocations to reach `T` | 1 (the element buffer) | 1 (the cell) |
| Indirections per access | element access **with bounds check** (`i < len`) | single load+GEP, **no bounds check** |
| Growth/cap bookkeeping | yes (growable semantics, reserve/shrink) | none (fixed single cell) |
| Semantic fit | "array that happens to hold one" — misleading | "exactly one heap T" — intends uniqueness |
| Drop | drops element(s) + frees buffer | drops `T` + frees cell |

Box wins on footprint (no `len`), on access cost (no per-access bounds check), and on
intent (a reader sees unique ownership, not a degenerate collection). Allocation count
is identical (one heap block either way). The improvement is modest in raw bytes but
meaningful in clarity and in removing the always-present bounds check on the hot
access path. (Against Option B, Box also drops the vestigial `cap` for an 8 B cell.)

---

## 10. Concrete files / functions likely to change

Stdlib (the bulk of the work):
- **NEW** `stdlib/std/core/box.drift` — `module std.core.box`; `pub struct Box<T>`,
  `pub fn box<T>`, `implement<T> Box<T> { get, get_mut, take }`, the per-`T` drain
  helper. Imports only `std.mem`. Model on `arc.drift:1-160`.
- `stdlib/std/core/core.drift` — import the `box` submodule; add
  `implement<T> Destructible for box_mod.Box<T>`; add `box`/`Box` to the `std.core`
  export list (mirror `arc` at `core.drift:8,98,752-757`).

Compiler (small / possibly zero logic change):
- `lang/driftc/type_checker.py:994-997` — `_suggest_indirection`: offer `Box<Self>`
  as the primary value-recursion suggestion (message-only; §6.1). **Only detector
  touch.**
- **No change expected:** destructor-insertion picks up Box's plain `Destructible`
  method — ordinary generic destructors already work (`Array<T>`, `ScopeGuard<T>`
  precedent). No `BOX_DESTROY` intrinsic, no `hir_to_mir` branch. (If a regression
  ever shows the generic-destructor path failing for Box, that is a *compiler bug* to
  file separately — not planned Box scope.)
- Confirm (likely **no change**): monomorphization (`types_core.py:1310-1385`),
  packaging (`provisional_dmir_v0.py`), and Copy determination
  (`types_core.py:2687-2708`) treat Box like any generic Destructible struct.

Docs:
- `doc/design/` (effective-drift / stdlib spec): one paragraph — `Box<T>` is the
  sanctioned value-indirection for breaking recursive value-type cycles when sharing
  is not wanted; `Arc<T>` for shared ownership; explicit `get`/`get_mut`/`take` only.
- `doc/history.md` — DRIFTC version entry (no ABI change).

---

## 11. Regressions (full set, TDD-first)

Compile/run + semantics:
1. **Construct/access/run:** `box(42).get() == 42`; `get_mut` mutates; round-trips a
   `String` payload (droppable). Run-clean.
2. **Move-only + not const-shareable:** using a `Box` after move is rejected by the
   borrow checker; copy-by-assignment rejected; `share(box)` rejected; and an
   explicit assertion that `Box<T>` neither **proves nor auto-derives `ConstShare`**
   (using a `Box<T>` where a `ConstShare` bound is required is rejected; const-share
   status query reports false) — guarding against a future auto-derive granting
   `ConstShare` to a `Destructible` wrapper (Finding 4).
3. **take:** `take` returns the inner `T` by value and the box does not double-free
   (run + memcheck); using the box after `take` is rejected.
4. **Nested-droppable T:** `Box<DestructibleThing>` runs the inner destructor exactly
   once at scope end (instrument a drop counter ⇒ exactly 1); `Box<Box<String>>`
   drops cleanly.
5. **Drop/leak (memcheck):** construct + drop, take + drop, and early-scope drop are
   all leak-clean and UAF-clean under valgrind (use `valgrind_cmd()` + `asan_active()`
   skip per the house rule).
6. **Double-drop / UAF:** explicit `take` then scope-end destructor must not double
   free (drained sentinel); memcheck-clean.

Recursive-type (the headline use case):
7. **Accept:** `variant IrType { TNull, TArray(elem: Box<IrType>), TOptional(inner:
   Box<IrType>) }` compiles, constructs, matches, runs.
8. **Still reject:** the same variant with a **direct** `IrType` arm still produces
   `E_RECURSIVE_VALUE_TYPE` (no over-broad acceptance).
9. **Structural, not by-name (§6.2):** a RawBuffer-backed wrapper that is NOT named
   `Box` also breaks the cycle (pins kind/phantom-T recognition, guards against a
   future name allowlist creeping in).
10. **Suggestion text:** the diagnostic for a direct-recursive value type suggests
    `Box<Self>` (and `Optional<Box<Self>>` for the Optional case) — update
    `test_recursive_value_struct_diagnostic.py` accordingly.

Package-consumer (cross-boundary, the path the recent validation fix hardened):
11. **Emit→consume by value:** a package exports a type with a `Box<T>` field; a
    consumer that loads it and uses it compiles + runs.
12. **Emit→consume recursive-broken:** a package exports `variant IrType { ...
    Box<IrType> ... }`; a consumer build (loaded-packages / two-pass path) accepts it
    and runs — and the **direct**-recursive variant is still rejected on that same
    consumer path with a clean diagnostic (never a Traceback). (Composes with
    `test_recursive_value_type_package_path.py`.)

Negative — explicit-access only:
13. **No auto-deref / no coercion:** `Box<Int>` is NOT usable where `Int` is expected
    (no implicit unbox); `box(x) + 1` is rejected; passing a `Box<T>` to a `T`
    parameter is rejected. Access must go through `get`/`get_mut`/`take`.

Allocation-failure (per Q3 resolution):
14. If `box` aborts on OOM, a documented note + (if feasible) a fault-injected
    OOM test asserting clean abort, not UB.

---

## 12. Open questions

- **Q1 — receiver syntax:** is `take(self: move Box<T>)` accepted surface syntax, or
  is `var self: Box<T>` the canonical consuming-receiver spelling? (Semantics
  identical; affects only the signature text.)
- **Q2 — RESOLVED (per review):** ordinary generic `Destructible` destructors
  already work for stdlib generic types (`Array<T>`, `ScopeGuard<T>`), so Box's
  plain `destroy` method is picked up by scope-exit destructor insertion with **no
  intrinsic**. The speculative `BOX_DESTROY` fallback is removed; the prior "primary
  scope risk" no longer stands. (Only revisit if a regression proves a generic-
  destructor compiler defect — a separate bug, not Box scope.)
- **Q3 — OOM policy:** `mem.alloc_uninit` failure mode (abort vs null). Drives whether
  `box` aborts (recommended v1, matches `arc`) or a `try_box -> Result` is offered.
- **Q4 — suggestion wording:** `Box<Self>` vs `Arc<Self>` vs both in
  `_suggest_indirection`. Recommend `Box<Self>` primary.
- **Q5 — `take` ergonomics:** is `take` sufficient, or is an in-place
  `replace(&mut Box<T>, T) -> T` also wanted in v1? Recommend deferring (non-breaking
  later add).
- **Q6 — `Optional<Box<T>>` / null-niche:** no niche optimization in v1 (an
  `Optional<Box<T>>` is a normal tagged Optional). Note as a future optimization;
  out of scope.

---

## 13. Estimated effort

- **Stdlib (`box.drift` + `core.drift` wiring):** ~0.5–1 day. Direct port of the
  Arc pattern minus refcount/thunk/Share; the trickiest part is the `take` drain
  sequence and the destructor sentinel, both of which have working precedents
  (TypeBox/Deque).
- **Compiler:** ~0.25 day — only the `_suggest_indirection` message tweak (the
  generic destructor needs no compiler change; Q2 resolved). No `BOX_DESTROY`.
- **Detector:** ~0 logic; message + the structural regression (~0.25 day).
- **Regressions (§11, incl. memcheck + package round-trip + ConstShare-negative):**
  ~1–1.5 days.
- **Docs + version bump:** ~0.25 day.
- **Total:** ~2–3 days (gated mainly on Q3, the OOM policy). No ABI bump, no
  runtime-archive rebuild, no new C boundary code, no new compiler intrinsic.

---

## 14. Summary of decisions (for the static review)

- Home: **`std.core.box`** submodule + `Destructible` in `core.drift` (dependency-safe,
  mirrors `std.core.arc`).
- Backing: **reuse `std.mem` `RawBuffer<T>` + existing intrinsics** → **no new runtime
  boundary, no ABI bump (stays 17)**.
- Recursive cycles: **broken structurally and automatically** (phantom-`T` /
  type-erased `Ptr<Byte>` in `RawBuffer`), **no name allowlist**, **no detector logic
  change** — only an optional, message-only suggestion refinement.
- Ownership: **move-only** (non-Copy via the `Destructible` impl, no Share/ConstShare);
  ConstShare non-derivation is **explicitly tested** (Finding 4).
- Destructor + `take`: both use the **`mem.replace`-first** discipline — NO
  `mem.dealloc(move self.buf)` partial move (Finding 1). `destroy` is a **plain
  generic `Destructible` method, no `BOX_DESTROY` intrinsic** (Finding 2).
- Layout: 16-byte `RawBuffer` Option (A) is **frozen as committed v1 layout**; a
  future shape change is a package/ABI review item, not a silent internal tweak
  (Finding 3).
- API: **exactly** `box`, `get`, `get_mut`, `take`, `destroy` — no auto-deref, no
  coercion, no extra surface in v1.
- Cost: smaller footprint (no `len`) and no per-access bounds check vs the `Array<T>`-
  of-one workaround; identical allocation count.
- Primary remaining open item: **Q3** (OOM policy of `mem.alloc_uninit` → whether
  `box` aborts or a `try_box` is offered). No remaining compiler-scope risk.
