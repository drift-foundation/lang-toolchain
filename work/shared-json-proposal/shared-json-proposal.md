# Proposal: Shared JSON Representation for `std.json`

Status: upstream proposal (follow-up to Arc-backed JsonNode review)
From: web.rest (downstream consumer)
Date: 2026-03-09

## Context

We proposed replacing `std.json.JsonNode` with a per-node Arc-backed
representation. The compiler/stdlib team reviewed and recommended against
replacing the default type directly, citing:

- `into_array`/`into_object` semantic break (ownership transfer becomes
  conditional on refcount)
- Source-breaking loss of direct variant pattern matching
- Per-node Arc overhead penalizes the common single-use parse path
- "Implementable" does not justify changing default semantics for all users

We accept this recommendation. `JsonNode` should remain the zero-overhead
owned tree it is today.

## Downstream Need

`web.rest` dispatches a parsed JSON body through multiple layers (guards,
filters, handlers) that each receive `&Request`. The framework needs to:

1. Parse the request body JSON once per request.
2. Cache the parsed result inside `Request`.
3. Return an owned, shareable handle from `body_json(req: &Request)` on each
   call — no re-parse, no deep copy.
4. Keep `&Request` (not `&mut Request`) as the public callback contract.
5. Avoid returning `&JsonNode` from `body_json` — borrowed return leaks
   lifetime constraints into every handler signature and prevents the framework
   from controlling cache lifecycle.

Today's `JsonNode` cannot satisfy (3): it is move-only with no clone, so a
cached `JsonNode` cannot produce additional owned handles without re-parsing
or deep-copying the entire tree.

## Proposal: Separate Shared JSON Type

Add a shared JSON representation to `std.json` alongside the existing
`JsonNode`. Two naming options (stdlib team's call):

- `std.json.SharedJsonNode`
- `std.json.JsonHandle`

The shared type should provide cheap clone/share semantics suitable for
request-scoped caching and multi-reader reuse. The exact internal
representation is the stdlib team's call.

### Minimal API Required

The shared type needs the same read-only accessor surface as `JsonNode`:

```
clone(self: &T) -> T                              // O(1) Arc clone
get(self: &T, key: &String) -> Optional<&T>       // object field lookup
as_string(self: &T) -> Optional<String>            // string extraction
as_int(self: &T) -> Optional<Int>                  // integer extraction
as_bool(self: &T) -> Optional<Bool>                // boolean extraction
as_array(self: &T) -> Optional<&Array<T>>          // array access
as_object(self: &T) -> Optional<&HashMap<String, T>>  // object access
```

No `into_*` methods. The shared type is read-only by design — ownership
transfer semantics do not apply.

No pattern matching on variant cases. Access is method-only, which is the
natural API for shared immutable data.

### Construction Path

We can adopt either path, as long as one is available:

**Option A — owned-to-shared conversion:**
```
json.parse(text) -> Result<JsonNode, ...>      // existing, unchanged
json.share(node: JsonNode) -> SharedJsonNode   // new: wraps each node in Arc
```

Parse to the current zero-overhead tree, then convert when sharing is needed.
Conversion is O(N) but paid only at the caching boundary.

**Option B — direct parse-to-shared:**
```
json.parse_shared(text) -> Result<SharedJsonNode, ...>   // new
```

Parse directly into the Arc-backed tree. Avoids building an intermediate owned
tree. Same O(N) allocation cost as conversion, but in one pass.

Either option works for `web.rest`. Option A is simpler to implement (reuses
existing parser). Option B avoids a throwaway intermediate tree. We have no
strong preference — stdlib team should decide based on implementation
complexity and whether other consumers would benefit from direct shared parse.

## What This Unblocks

With a shared JSON type available, `web.rest` can:

1. Parse/convert the body at dispatch time (where `&mut Request` is available).
2. Store the shared handle in Request's internal cache.
3. Return `clone()` from `body_json(req: &Request)` — O(1), owned value.
4. Keep `&Request` in all public callback signatures.
5. Keep `body_json` returning an owned value (no `&JsonNode` in the API).
6. Expose the shared std.json type directly from `body_json(req)` in web.rest,
   rather than adding a separate `body_json_handle(...)` API.

## What This Does Not Require

- No changes to `std.json.JsonNode` (variant stays public, `into_*` preserved,
  pattern matching preserved, zero-overhead construction preserved).
- No compiler/runtime primitives beyond existing `Arc<T>`.
- No ABI break on existing types.
- No source break for any existing `JsonNode` consumer.

## Summary

We are requesting one new stdlib type with a read-only accessor surface and
O(1) clone, plus one construction path (conversion or direct parse). This is
an additive API extension with no impact on the existing `JsonNode` type or
its consumers.
