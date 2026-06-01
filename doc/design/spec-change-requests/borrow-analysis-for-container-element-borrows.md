# Borrow Analysis for Container Element Borrows (Spec Change Request)

## Summary

Enable safe `&mut` element borrows (e.g., `HashMap.get_mut`, `TreeMap.get_mut`, `Array.get_mut`) without resorting to `with_mut`-style APIs, by adding a minimal borrow analysis that can prove short-lived borrows do not escape and are not invalidated by container mutations.

This is **not required for MVP**, but should be implemented before we expose `get_mut` on containers for general use.

---

## Motivation

Users expect `get_mut` APIs for maps and arrays. Today we avoid returning `&mut V` because:

- container storage can reallocate or move elements
- the compiler does not yet prove non-escape or no-invalidation

Result: `with_mut`/callback APIs feel less ergonomic.

Goal: allow the *common, simple* pattern:

```drift
var v = map.get_mut(&key);
if v.is_some() { v.unwrap().apply(); }
```

without making the compiler unsound.

---

## Safety Requirements

We need to ensure all of the following:

1. **No escape**: a `&mut V` derived from a container cannot be stored, returned, or captured beyond the borrow scope.
2. **No invalidation while live**: no operation that could reallocate/move the container’s elements can occur while the borrow is live.
3. **No aliasing**: a live `&mut V` blocks concurrent borrows that would alias the same container storage.

---

## Proposed Implementation Plan (Minimal)

### Phase 1 — Scoped borrows (NLL-lite)

Add flow-sensitive borrow tracking to determine the *actual* end of a borrow.

- Track borrows by **place** (`map`, `map.field`, etc.)
- Compute last use of a borrow (basic NLL)
- End borrow at last use, not end of block

This alone enables many “tight scope” cases.

### Phase 2 — Escape analysis

Reject `&mut` borrows that escape:

- returning the reference
- storing in a struct/array
- capturing in a closure
- assigning to a global/static

If escape is detected, emit a clear diagnostic:

```
map.get_mut() returns a reference that cannot escape the borrow scope
```

### Phase 3 — Invalidation model for containers

Define which methods can invalidate element borrows. For example:

- `HashMap.insert/remove/clear/rehash` invalidate
- `TreeMap.insert/remove/clear` invalidate (rotation moves nodes)
- `Array.push/pop/resize` invalidate

The borrow checker must reject any such call while a live element borrow exists.

### Phase 4 — Allow `get_mut` surfaces

Once phases 1–3 are implemented, allow:

- `HashMap.get_mut(&K) -> Optional<&mut V>`
- `TreeMap.get_mut(&K) -> Optional<&mut V>`
- `Array.get_mut(i) -> &mut T`

`Entry` APIs that return `&mut V` become viable in the same phase.

---

## Compiler Hooks

- **HIR borrow placement**: attach a “borrow id” to any `&mut` taken from container element access.
- **Borrow checker**:
  - track active borrows with the container root
  - forbid invalidating ops while borrow is active
  - forbid escape of `&mut` values
- **Error messages**: should point to the invalidating call or escape site.

---

## Testing Plan

### Positive (must compile)

- `get_mut` used in a small scope without mutation
- nested borrows that end before rehashing
- `get_mut` inside `if`/`match` branches with no escape

### Negative (must error)

- store `&mut V` into a struct field
- return `&mut V` from function
- call `insert/remove/clear` while `&mut V` is live
- capture `&mut V` in a closure

---

## Notes

This change does **not** require any Arc/Mutex magic and does not alter runtime layout. It is purely a compiler analysis change.

---

## Status

- Requested: 2026-01-24
- Priority: Post-MVP (unless we decide to expose `get_mut` early)
- Owner: TBD
