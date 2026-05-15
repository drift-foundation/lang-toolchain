# PR2 handoff — `std.concurrent.Condvar`

PR1 (`vt_is_cancelled` + `MutexGuard` unlock/relock state) landed
cleanly.  PR2 (the Condvar itself) hit a Drift-idiom blocker
during implementation that needs someone with more hands-on
Drift expertise to resolve.

## What landed in the WIP commit

The `Condvar` API skeleton in `stdlib/std/concurrent/concurrent.drift`:

- `pub struct Condvar { state: core.Arc<CondvarState> }`
- `pub fn condvar() -> Condvar`
- `Condvar.signal_one()` / `signal_all()` / `close()`
- `Condvar.wait(guard)` / `wait_timeout(guard, d)` / `wait_until(guard, deadline)`
- `CONCURRENCY_KIND_REQUIRES_VTHREAD` constant
- Internal helpers `_claim_one`, `_drain_active`, `_unpark_all`,
  `_wait_inner`

The shape and discipline match the plan exactly: self-CAS on every
wake path, CAS-before-unpark in signal/close, Arc-shared `Waiter`
records, close-race re-check after enqueue.

## Blocker

`Array<core.Arc<PoolWaiter>>.len()` does not resolve when accessed
through the Mutex guard's `get_mut() -> &mut Array<...>` return,
nor through `&Array<...>` directly:

```
error: no matching method 'len' for receiver RefMut<Array<std::std.core.arc.Arc<std::std.concurrent.PoolWaiter>>>
error: no matching method 'len' for receiver Ref<Array<std::std.core.arc.Arc<std::std.concurrent.PoolWaiter>>>
```

Same shape for `.push()`, `.remove(0)`.

Existing tests like `std_json_wrapper_build_encode` use
`arr.len()` on a local `var arr: Array<...>` (owned), which works.
The autoborrow from `&Array<T>` / `&mut Array<T>` to the method's
expected receiver type doesn't seem to happen in this context —
possibly because of the nested `Mutex<Array<Arc<T>>>` type or the
T being `core.Arc<PoolWaiter>` (a generic type with a destructor).

`cv.state.closed.load(...)` also fails — `cv.state` is
`core.Arc<CondvarState>` and accessing `.closed` on the Arc
doesn't auto-`.get()` to the inner CondvarState.  This is a
related but distinct issue: field access through Arc doesn't
auto-deref to the inner T's fields.

## What needs to be done

A Drift-fluent maintainer needs to:

1. **Find the right shape for accessing fields through `core.Arc<T>`.**
   Current attempt: `cv.state.closed` — fails ("unknown field
   'closed' on struct 'Arc'").  Likely fix: `cv.state.get().closed`.
   Verify and apply throughout `_wait_inner`, `signal_one`, etc.
2. **Find the right shape for `Array.len/push/remove` through a
   `MutexGuard<Array<T>>` lock.**  Either:
   - explicit reborrow: `(&*lg.get_mut()).len()`, or
   - intermediate value binding, or
   - a different data structure (e.g., a thin `WaiterList` struct
     wrapping the Array with explicit methods that take
     `&mut WaiterList`).
   Apply throughout `_claim_one`, `_drain_active`, `_unpark_all`,
   `_wait_inner`.
3. **Test compilation end-to-end** — the existing WIP code is
   structurally complete but doesn't compile.  Once these two
   Drift-idiom issues are fixed, the rest of the slice (tests +
   integration) should follow the plan.
4. **Add the 15 e2e tests** from the plan's test plan section
   (we dropped `condvar_cancel_during_wait` per the PR1 finding
   on the scheduler killing cancelled VTs).

## Why I'm stopping here

The runtime / API / discipline design is right and reviewed.  The
implementation blocker is shape-level Drift idiom (autoborrow
through Arc and MutexGuard for method resolution on generic
container types), not a design flaw.  A 10-line change in the
right place should unblock everything; trying to discover that
change blind has been more expensive than handing off.

## Files touched in PR2 WIP

```
stdlib/std/concurrent/concurrent.drift   — Condvar API + impl (does not compile)
```

PR1 (committed, clean):
```
lang/codegen/llvm/llvm_codegen.py
lang/language_runtime/posix/thread_runtime.c
lang/tests/codegen/e2e/mutex_guard_condvar_unlock_relock/
lang/tests/codegen/e2e/vt_is_cancelled_basic/
stdlib/lang/thread.drift
stdlib/std/concurrent/concurrent.drift  (MutexGuard.locked + unlock/relock)
work/stdlib-condvar/plan.md
```

## Suggested next-session entry point

Start at `_wait_inner` in `std.concurrent`: replace every
`cv.state.<field>` with `cv.state.get().<field>`, every
`list.len()` / `list.push(...)` / `list.remove(...)` through the
guard with the Drift-idiomatic shape (TBD), then compile + iterate
on the resulting diagnostics.  After the smoke compiles, run the
plan's test plan top-to-bottom.

— K, session ending 2026-05-16
