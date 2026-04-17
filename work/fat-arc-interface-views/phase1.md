# Fat Arc for interface views — Phase 1 design

**Branch:** `feature/fat-arc-interface-views`
**Decision date:** 2026-04-17
**Target version:** bump from 0.27.203 (ABI 9) to next (ABI 10).

## Scope

One concrete type `T` that implements interfaces `I1`, `I2`, …, `Ik` should be
`Arc`-shareable such that **a single allocation** holds the concrete value and
its refcount, and multiple `Arc<Ij>` interface-views share that same control
block. This is Rust's `Arc<T>` → `Arc<dyn Trait>` unsizing coercion, adapted to
Drift's explicit API style.

Out of Phase 1 scope (deferred to Phase 2):

- `arc_borrow_as<I>(&Arc<T>) -> &I` unless the borrow-checker story is trivial.
- Cross-package-boundary regression coverage.
- Method-form sugar `arc.as_interface<type I>()`.
- Weak-Arc plumbing (header slot reserved, feature deferred).
- Implicit `conc.arc<I>(concrete)` auto-coercion. Users write the explicit two
  lines in Phase 1.

## Representation

### Fixed ArcBox header layout

All `ArcBox<T>` allocations share a constant-size header regardless of `T`.
However, `ArcBox<T>.value` is **not** guaranteed to live at a constant
offset — Drift struct layout inserts per-`T` alignment padding between
`header` and `value`. The fat `Arc<Interface>` handle therefore carries
`data_ptr` explicitly (see below) rather than deriving it from
`ctrl_ptr + sizeof(ArcHeader)`.

```
pub struct ArcHeader {
    strong: atomic.AtomicInt,
    weak:   atomic.AtomicInt,        // reserved for Phase 3 weak Arcs; always 0 for now
    drop_thunk: mem.Ptr<Byte>,       // fn(Ptr<Byte>) -> Void, set at arc<T>(value) time
}

pub struct ArcBox<T> {
    header: ArcHeader,
    value: T,
}
```

The drop thunk is a concrete-typed destroy-and-dealloc function pointer, set
when `arc<T>(value)` allocates the box. It is called on last-drop regardless of
which view holds the final handle. **Do not reconstruct T from an interface
view at drop time** — the view doesn't carry that information.

### Two Arc shapes

Thin `Arc<T>` (concrete):
```
struct Arc<T> {
    ctrl: mem.Ptr<Byte>,     // points at ArcBox<T>.header
}
```

Fat `Arc<I>` (interface) — **Option 3 representation**:
```
struct Arc<I>  where I is interface {
    ctrl:   mem.Ptr<Byte>,   // points at ArcBox<T>.header (refcount ops)
    data:   mem.Ptr<Byte>,   // points at ArcBox<T>.value (receiver for dispatch)
    vtable: mem.Ptr<Byte>,   // T-as-I vtable symbol
}
```

`data_ptr` is computed once inside `as_interface<I>()` at the call site
where `T` is known at compile time — `offsetof(ArcBox<T>, value)` resolves
to a per-`T` constant the compiler already has. Carrying the pointer
explicitly in the handle avoids binding `Arc<Interface>` correctness to
any struct-padding invariant of `ArcBox<T>`. The extra 8-byte slot is
worth the robustness.

`.clone()` and destruction work on `ctrl.header.strong` (atomic ops at
offset 0 of the header — the header-first layout does guarantee that
much).  `.get()` returns `&I = {data, vtable}`.

### Why this is sound

1. **Same ctrl_ptr = same strong count.** Any view's clone/drop operates on
   `*(ctrl + offsetof(header, strong))`. One source of truth for refcount.
2. **Last drop runs the concrete destructor.** `ctrl.header.drop_thunk` was
   captured at `arc<T>(value)` time and holds a T-shaped destroy closure. The
   interface view's vtable is irrelevant at drop — drop always goes through
   the stored thunk.
3. **No second allocation.** `arc_as` only bumps the strong count and mints a
   new fat struct; no new ArcBox.

## API (Phase 1) — method form on `Arc<T>`

Explicit at the coercion point. The user names only the target face;
the source `T` is inferred from the `Arc<T>` receiver. No compiler
partial-type-arg-inference work needed; a plain method with its own
type parameter achieves exactly the target shape.

```drift
// In std.concurrent:
pub fn arc<T>(var value: T) nothrow -> Arc<T>;   // rejects T = interface

implement<T> Arc<T> {
    pub fn as_interface<I>(self: &Arc<T>) nothrow -> Arc<I> require T is I;
}
```

Usage:
```drift
val svc: conc.Arc<AppService>           = conc.arc(AppService(...));
val logs: conc.Arc<log.ContextResolver> = svc.as_interface<type log.ContextResolver>();
val metrics: conc.Arc<metrics.Emitter>  = svc.as_interface<type metrics.Emitter>();
```

`require T is I` is the compile-time soundness gate. For Phase 1 this
requires extending the trait solver to recognize interface impls (see
"Solver extension" below) — `require T is InterfaceName` currently fails
with "unknown trait" because `world.traits` tracks only `pub trait`
declarations. This extension is a general language improvement and is
done regression-first before any Arc-view work.

Rejection of `conc.arc(iface_value)`: when `T` in `arc<T>(value)`
resolves to an interface type (rather than a concrete struct/variant),
the checker emits a diagnostic directing the caller to
`arc(concrete).as_interface<type I>()`. No implicit coercion is added
in Phase 1.

An internal two-type-arg free function `conc.arc_as<T, I>(&arc)` may
remain as a compiler-facing intrinsic for the method to lower through,
but it is not documented as a user API and test fixtures go through the
method form.

### std.log migration — explicit, no sugar

The builder takes `conc.Arc<ContextResolver>` directly. The caller is
responsible for constructing the concrete Arc and coercing it. No
implicit wrapping, no generic concrete-typed parameter hiding the
coercion inside logger-specific plumbing.

```drift
pub fn context_resolver(
    self: &mut LoggerConfigBuilder,
    var r: conc.Arc<ContextResolver>
) nothrow -> &mut LoggerConfigBuilder {
    self.resolver = move r;
    return self;
}
```

Caller writes both the ownership transfer and the coercion explicitly:

```drift
val resolver = conc.arc(AppResolver(...));
val resolver_view = resolver.as_interface<type log.ContextResolver>();

var b = log.config_builder();
b.context_resolver(resolver_view);
```

This makes the "one `Arc<Concrete>`, many `Arc<Interface>` views sharing
the same control block" model visible at every use site, and keeps
Phase 1 mechanically easy to audit. Ergonomic sugar can come later once
the primitive is stable.

## Solver extension — `require T is I` for interface `I`

Drift's trait solver today only recognizes `require T is X` when `X` is
a `pub trait`. Interfaces live in a separate registry that the solver
does not consult; a `require T is SomeInterface` call fails with
`reasons=["unknown trait"]` at `lang/driftc/traits/solver.py:378` even
when `implement SomeInterface for T` exists.

Phase 1 extends the solver so `require T is I` proves when `I` is a
`pub interface` and `implement I for T` exists in the interface-impl
index. This is a general language improvement — useful beyond Arc, and
removes an asymmetry where traits participate in generic constraints
but interfaces need one-off intrinsic checks. The diagnostic path is
unchanged: `E_REQUIREMENT_NOT_SATISFIED` with the same message shape
the trait case emits.

**Regression-first**: a dedicated test (`test_require_interface_impl.py`
or similar) pins the desired behavior before the solver change. Shape:

- `pub interface Speaker { fn speak(...) }`
- `struct Dog` with `implement Speaker for Dog`
- `struct Cat` with no Speaker impl
- `fn f<T>(x: T) require T is Speaker` (or similar)
- `f(Dog(...))` accepted
- `f(Cat(...))` rejected with `E_REQUIREMENT_NOT_SATISFIED`

Once that test turns green, the `require T is I` clause on
`Arc<T>.as_interface<I>()` picks up the same machinery. No Arc-specific
soundness-gate code is needed.

## Compiler touchpoints

| File | Change |
|------|--------|
| `stdlib/std/concurrent/concurrent.drift` | (done in Phase 1a — ArcHeader + ArcBox + drop_thunk). Phase 1b: add `implement<T> Arc<T> { pub fn as_interface<I>(self: &Arc<T>) nothrow -> Arc<I> require T is I }` (marked intrinsic). Add reject diagnostic for `arc<T>(value)` when T is interface. Internal `conc.arc_as<T, I>` free-fn as lowering target. |
| `stdlib/std/log/log.drift` | `context_resolver` takes `var r: conc.Arc<ContextResolver>`. Update internal construction path in `config_builder()` if needed. |
| `lang/driftc/traits/solver.py` | **Extend `prove_is_trait` to recognize interface requirements**. When `trait_key` resolves to an interface (via the interface-impl index rather than `world.traits`), prove iff `implement I for T` exists. Reuse `E_REQUIREMENT_NOT_SATISFIED` diagnostic. |
| `lang/driftc/traits/world.py` | May need companion work to surface the interface-impl index to the solver's lookup. Exact shape TBD during the solver-extension step. |
| `lang/driftc/core/types_core.py` or struct-layout owner | Specialize `Arc<T>` struct layout when T is interface — emit `{ctrl, data, vtable}` fat shape. Otherwise emit current `{buf}` thin layout. |
| `lang/codegen/llvm/llvm_codegen.py` | Vtable symbol lookup reused via `_ensure_interface_vtable(I, T)`; new lowering for the intrinsic (atomic fetch_add + compute data_ptr via `offsetof` + construct fat handle); rework `Arc.get()` lowering for the fat-handle case to produce `&I = {data, vtable}` (no refcount touch). |
| `lang/driftc/stage2/hir_to_mir.py` | New MIR instruction or intrinsic call for the coercion (parallel to existing `IfaceUpcast`). |
| `lang/driftc/checker/call_resolver.py` | Resolve method call into the intrinsic; thread T (from receiver) and I (from method type arg) TypeIds into the generated MIR. Reject `arc<T>(value)` when T is interface. |
| `lang/versions.py` | `DRIFT_RT_ABI_VERSION` 9→10; `DRIFTC_VERSION` bump. |

## Regression list (Phase 1, deduped to 6)

Regression file: `lang/tests/driver/test_fat_arc_interface_views.py`.

1. **Happy path, two interfaces.** One concrete `AppService` impls `I1` + `I2`.
   Build `Arc<AppService>` via `conc.arc`, derive `Arc<I1>` and `Arc<I2>` via
   `arc_as`. Dispatch one method through each view; assert both return correct
   values. Verifies same-object identity.

2. **Shared-state mutation observed across views.** `AppService` field accessed
   via `conc.Mutex` (Arc gives sharing; Mutex gives interior mutability).
   Mutate through `Arc<I1>`; read through `Arc<I2>`; observe the mutation.
   Verifies single underlying payload.

3. **Drop-order permutations, single destructor.** Instrument `AppService` with
   a process-local atomic counter in `Destructible::destroy`. Construct `arc`,
   `v1 = arc_as<_, I1>(&arc)`, `v2 = arc_as<_, I2>(&arc)`. Drop in all six
   permutations; assert counter is exactly 1 each time. Verifies shared
   refcount + drop_thunk correctness.

4. **Negative: require-failure at compile time.** Concrete `Foo` that does not
   implement `Unrelated`. `conc.arc_as<type Foo, type Unrelated>(&arc)` must
   be rejected with the `require T is Unrelated` unsatisfied diagnostic. No
   runtime artifact produced.

5. **Clone through interface view.** `Arc<I1>.clone()` produces a second
   `Arc<I1>` sharing the same ctrl. Both views dispatch correctly; drops
   balance (total destructor count = 1 across all views + clones).

6. **std.log integration.** The existing `test_std_log_resolver_scoped_stack.py`
   and `std_log_resolver_active` e2e fixture continue to pass against the new
   `context_resolver` generic signature. No changes to caller-facing shape.

Deferred to Phase 2 (documented, not implemented):
- Cross-package: producer exports concrete + interfaces, consumer does the
  coercion.
- Borrowed-view: `arc_borrow_as<I>(&Arc<T>) -> &I`.

## ABI boundary audit

`Arc<Interface>` layout changes from `{buf}` (thin, `RawBuffer<ArcBox<FatPtr>>`)
to `{ctrl, vtable}` (fat, `{Ptr<Byte>, Ptr<Byte>}`). This is an ABI-visible
change for any published package that exposes an `Arc<SomeInterface>` at its
boundary.

Current inventory of boundary-exposed `Arc<Interface>`:

- `stdlib/std/log/log.drift`: `LoggerConfig.resolver: conc.Arc<ContextResolver>`.
  This is the only committed site on HEAD. No downstream packages have been
  published against 0.27.203 (the current tip).

Action: `DRIFT_RT_ABI_VERSION` 9 → 10 in `lang/versions.py`. No downstream
rebuild coordination needed because there are no downstream consumers.

## Work sequence

Strictly ordered because later steps depend on earlier:

1. Design doc (this file). **Done.**
2. ArcHeader + `ArcBox<T>` reshape in `stdlib/std/concurrent/concurrent.drift`.
   Verify existing `Arc<Concrete>` clone/drop still works. **Done** (Phase 1a).
3. **Solver extension**: `require T is I` for interface `I`.
   Regression-first (separate driver test pinning Dog-accepted /
   Cat-rejected). Solver change. Extension landed before any Arc-view
   work depends on it.
4. Type system specialization: `Arc<T>` struct layout emits fat shape
   `{ctrl, data, vtable}` when T is interface.
5. LLVM codegen for the fat-Arc case: clone, get, destroy.
6. `Arc<T>.as_interface<I>()` method + intrinsic: checker + HIR→MIR +
   LLVM lowering. Reject `arc<T>` when T is interface.
7. Update std.log `context_resolver` to take `conc.Arc<ContextResolver>`.
8. ABI bump 9→10 + version bump.
9. Regressions (test_fat_arc_interface_views.py — multi-face dispatch,
   mutation visibility, drop-order, destructor-once, refcount, and
   structural-IR pin against alloc-in-arc_as).
10. Confirm existing std.log regressions pass against the new shape.
11. Update docs (lang-spec 6.16 + effective-drift chapter) from the
    current branch-local two-type-arg draft to the method-form API and
    the interface-aware `require` semantics.
12. history.md entry.

## Non-obvious risks noted up front

- **`Arc.get()` on Arc<I>** must produce a fat `&I` whose `data_ptr` is rooted
  at `ctrl + header_offset`. If the existing `Arc.get()` lowering assumes a
  thin Arc layout, it needs to branch on T's kind. Keep the two paths clean
  rather than unifying them.
- **drop_thunk capture**. `arc<T>(value)` must install a compiler/runtime-
  emitted typed drop/dealloc thunk that:
  (a) runs T's `Destructible::destroy` on the value at `ctrl + header_offset`,
  (b) deallocates the `ArcBox<T>` allocation.
  Emitted as a per-T compiler symbol (helper name e.g. `_ensure_arc_drop_thunk(T)`,
  parallel to the existing `_ensure_interface_vtable(T, I)`). **Do not route
  Arc destruction through `core.Callback1` or any user-level callback
  machinery** — Arc destruction is ownership infrastructure and needs a
  direct erased thunk with a precise contract (signature, calling
  convention, no allocation, nothrow). `Callback1` is acceptable only as
  a discovery-spike tool, not as shipped code.
- **Weak count is reserved, not used**. Initialize to 0; never touch. Leaving
  the slot empty now avoids a future ABI break when Phase 3 weak support
  lands.
- **`Arc<I>.clone()` vs. `arc_as<T, I>(&arc<T>)`** must produce structurally
  identical fat handles. Both are refcount-bump + copy; validate via the
  drop-count test.
- **Atomic ordering**. `fetch_add` for clone uses Relaxed (matches current
  Arc.clone). `fetch_sub` for destroy uses AcqRel then Acquire on the last
  decrement (matches current pattern in `Arc::Destructible`).
