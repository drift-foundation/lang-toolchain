# Re: Per-request typed slot map (Ask B)

Most of what you need is already in stdlib — Ask B doesn't block on a compiler change. Pointers below; one named follow-up that we can ship if/when you hit it.

## What's already there (use today)

### Type identity

```drift
import std.core as core;

val tid: Uint64 = core.type_id<type Foo>();
```

- `core.type_id<type T>() nothrow -> Uint64` — `stdlib/std/core/core.drift:348`. Stable for a given concrete `T` for the process lifetime; trivially equatable and hashable as a `Uint64`.
- **Syntax note**: explicit type arguments at call sites use `<type T>`, not `<T>`. The `type` keyword disambiguates "type argument" from `<` as a less-than. Your earlier probe (`core.type_id<Foo>()`) hit the parser because of this. The `<type T>` form is what stdlib uses everywhere — `mem.alloc_uninit<type T>(1)`, `core.drop_value<type T>(...)`, `mem.ptr_at_ref<type T>(...)`, etc. Same syntax for any free or qualified call that needs a type arg.

### Type-erased value with drop glue

```drift
import std.runtime as runtime;

val box: runtime.TypeBox = runtime.type_box(my_value);    // consumes my_value
val tid: Uint64 = box.type_id();                          // tag for the stored type
match runtime.downcast<type Foo>(&box) {                  // borrowed view
    Optional::Some(foo_ref) => { /* &Foo */ },
    Optional::None => { /* tag mismatch */ }
}
// box drops here — runs the stored T's destructor through the typed `dropper` closure.
```

- `runtime.TypeBox` — `stdlib/std/runtime/runtime.drift:491-495`. Internally `{ tag: Uint64, buf: RawBuffer<Byte>, dropper: Callback1<Ptr<Byte>, Void> }`. The dropper runs when the box drops, so per-slot destruction works through the existing runtime drop machinery — no hand-rolled drop-fn array needed.
- `type_box<T>(value: T) -> TypeBox` — moves `value` in, captures `core.type_id<type T>()` as the tag and a typed-dropper closure.
- `downcast<T>(&TypeBox) -> Optional<&T>` — borrowed view if the tag matches.
- `expect_downcast<T>(&TypeBox, tag: String) -> &T` — strict form, throws `TypeBoxError` on mismatch.
- `TypeBox` implements `core.Destructible` — proper drop on Context drop.

### Existing precedent — registries

```drift
val reg: &runtime.ThreadRegistry = runtime.thread_registry();
reg.set<type Foo>(my_foo);
val opt_foo: Optional<&Foo> = runtime.get<type Foo>(reg);
```

- `runtime.GlobalRegistry` — process-wide type-keyed value map.
- `runtime.ThreadRegistry` — per-virtual-thread type-keyed value map.

These are exactly the shape of "typed slot map" you're asking for, just at process / VT scope rather than per-Context-instance scope. They demonstrate the pattern the web-rest implementation can mirror — and they prove the runtime drop machinery handles per-slot typed destruction correctly today.

## What you can build immediately (no compiler ask)

A `Context.slots: HashMap<Uint64, runtime.TypeBox>` field plus thin wrappers:

```drift
pub fn ctx_set<T>(ctx: &mut Context, value: T) nothrow -> Void {
    ctx.slots.set(core.type_id<type T>(), runtime.type_box(move value));
}

pub fn ctx_get<T>(ctx: &Context) nothrow -> Optional<&T> {
    match ctx.slots.get(&core.type_id<type T>()) {
        Optional::Some(box) => { return runtime.downcast<type T>(box); },
        Optional::None => { return Optional<&T>::None(); }
    }
}
```

`ctx_set<T>` overwriting a slot drops the previous occupant correctly because `HashMap.set` replaces and the replaced `TypeBox` runs through `Destructible`. When `Context` drops at request end, the `HashMap` drops, each `TypeBox` drops, and each stored `T`'s destructor fires through its dropper closure.

## Owning take — the one named follow-up

`ctx_take<T>(ctx: &mut Context) nothrow -> Optional<T>` is **not** expressible against today's `TypeBox` API. `downcast<T>` returns `Optional<&T>` (borrowed); there's no `into_inner<T>(self: TypeBox) -> Optional<T>` (consuming) function in stdlib.

You have two paths:

1. **(Recommended)** We add `runtime.into_inner<T>(box: TypeBox) -> Optional<T>` to stdlib in a follow-up. It's a ~15-line addition next to `downcast<T>` — moves the buf out, runs the typed reconstructor, leaves the dropper to no-op on the now-empty box. We can ship it whenever — it doesn't block your 0.4 cut.
2. **(If you want to ship 0.4 without us)** Implement `ctx_take` as a "downcast + clone" rather than "downcast + move out". Works for `Share`-able stored types (the `Arc<T>` case you'll have most often anyway) — `ctx_take<T>` returns `Some(t.clone())` and the original stays in the box until Context drops. Doesn't work for non-Copy non-Share types, which is a surface restriction you'd document.

If you want option 1, ping us and we'll land it; figure ~half a day end-to-end.

## Minor detail — `set_principal` / `get_principal` removal

You can drop those from `Context` and use `ctx_set<Principal>(ctx, p)` / `ctx_get<Principal>(&ctx)` directly once you've adopted the slot map. No compiler change needed, and the semantics are richer (any handler can stash any type, not just principal).

## Summary

| Ask B sub-piece | Status |
|---|---|
| `core.TypeId` value type (hashable, equatable, stable) | ✅ Use `Uint64` from `core.type_id<type T>()`. |
| `core.type_id<T>()` intrinsic | ✅ Already exists; syntax is `<type T>`. |
| Explicit-type-argument call syntax | ✅ `<type T>` everywhere. |
| `Any` / `Box<Any>` with per-slot drop glue | ✅ `runtime.TypeBox` + `type_box` + `downcast` + `Destructible` impl. |
| Owning take (`ctx_take<T>`) | ⚠️ Tiny stdlib follow-up needed (`runtime.into_inner<T>`); workaround via clone for Share types. |

Web-rest 0.4 can ship middleware + typed Context immediately on top of these primitives. Compiler ask narrows to Ask A only (Callback3-6 + Share impls).
