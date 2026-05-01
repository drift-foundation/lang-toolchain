# Post-link ConstShare synthesis — §7 design deliverables

**Status:** design only.  No implementation.  Companion to
`post-link-synthesis-plan.md`; addresses the user's two
corrections (artifact identity + variant care) and answers
items 1–10 from §7 of the plan.

---

## Deliverable 1 — Pipeline insertion point (exact line)

### Phase order in `lang/driftc/driftc.py` (relevant subset)

```
per-module:
  L4220-ish: parser → lower_program(prog) → builds, PER MODULE:
              - prog.implements (parser_ast.ImplementDef[])
              - func_hirs        (FunctionId → H.HBlock)
              - signatures       (FunctionId → FnSignature)
              - impl_metas       (impl_index.ImplMeta[])
              - trait_worlds[mid] (per-module TraitWorld)

cross-module:
  L2731:   _normalize_func_maps(func_hirs, signatures)
            └── merges per-module maps into global dicts:
                signatures_by_id, func_hirs_by_id, fn_ids_by_name

  L3253:   linked_world, require_env = _build_linked_world(shared_type_table)
            └── L851: linked_world = link_trait_worlds(trait_worlds)
                Installs query callbacks on type_table.

  L3253-3622:   pre-typecheck setup (impl_index, callable_registry,
                visibility, etc.)

  L3622:   type_checker.check_function(...) — per-function

  L5572:   hir_to_mir start

  L5738+:  mir → llvm → emit
  …
  end:     emit_package (.dmp serialization)
```

### Insertion point: between `_build_linked_world` (L3253) and the pre-typecheck setup that builds `impl_index` / `callable_registry`

This is the unique window in which:
  - `LinkedWorld` exists with all impls visible (cross-module + stdlib).
  - `signatures_by_id` and `func_hirs_by_id` are merged-global.
  - The type-check loop has NOT yet run (so synthesized impls
    can be added before any consumer sees them).
  - `impl_index` (`GlobalImplIndex`) has NOT yet been built —
    this is critical because synthesized `ImplMeta`s must be
    registered with `impl_index` before method resolution
    consults it.
  - `callable_registry` has NOT yet been finalized — synthesized
    function ids need to register here too.

**Concrete callsite to add:** a new helper
`_synthesize_const_share_impls(linked_world, ...)` invoked
immediately after `_build_linked_world` returns and before the
`impl_index` build block.  Exact line numbers will shift; the
anchor is "after L3253 `linked_world, require_env = …`, before
the impl_index construction."

**Verification probes** (must be in the milestone tests):
  - dump `linked_world.global_world.impls` length before and after
    synthesis — confirm it grew by exactly the number of
    synthesized impls;
  - dump `signatures_by_id` length before and after — confirm
    it grew by N (one per synthesized method);
  - dump `func_hirs_by_id` length before and after — confirm
    same.

---

## Deliverable 2 — LinkedWorld mutability rule

### Mutation surface

`LinkedWorld` is a `dataclass(frozen=True)` (lang/driftc/traits/linked_world.py:55).  Cannot reassign its fields directly.  But its INNER objects (the `TraitWorld` instances inside `trait_worlds` and `global_world`) are mutable `dataclass`es (`world.py:139`).  So mutation of `linked_world.global_world.impls` IS sound:

```python
# OK — mutating mutable nested object
linked_world.global_world.impls.append(new_impl_def)
linked_world.global_world.impls_by_trait.setdefault(trait_key, []).append(impl_id)
linked_world.global_world.impls_by_target_head.setdefault(head_key, []).append(impl_id)
linked_world.global_world.impls_by_trait_target.setdefault((trait_key, head_key), []).append(impl_id)
```

### Required investigation before coding

Confirm no other code path between `link_trait_worlds` (L851)
and the type-check loop (L3622) caches `linked_world.global_world.impls`
length / contents.  Specifically check:
  - `_install_copy_query`, `_install_diagnostic_query`,
    `_install_destructible_query`, `_install_share_query` (L853-856)
    — do any cache the impl list?
  - `build_require_env` (L859) — does it precompute over impls?

If any caches exist, either invalidate them post-synthesis or
move synthesis BEFORE they run.

### Mutation also required on per-module `trait_worlds[mid]`?

Open question.  `link_trait_worlds` produces `LinkedWorld` from
the per-module `trait_worlds` dict.  After linking, downstream
code uses `linked_world.global_world` for lookups — but the
per-module `trait_worlds` are still attached to `type_table.trait_worlds`.

The `LinkedWorld.visible_world(module_names)` method
(`linked_world.py:61`) MERGES per-module trait_worlds on demand:

```python
def visible_world(self, module_names):
    names = set(module_names)
    return merge_trait_worlds(world for name, world in self.trait_worlds.items() if name in names)
```

So a `visible_world(...)` call AFTER synthesis will RE-MERGE the
per-module worlds — losing any synthesized impls that we only
added to `global_world`.  **This means we must ALSO add
synthesized impls to the appropriate per-module `trait_worlds[mid]`.**

This is the load-bearing detail the user asked about.

---

## Deliverable 3 — The artifact-identity question (user correction #1)

### What gets mutated for each synthesized impl

For ONE synthesized `implement ConstShare for Holder { fn const_share(...) ... }`, every one of the following structures must be updated:

| # | Structure | Owner | Purpose | Mutation needed |
|---|---|---|---|---|
| 1 | `prog.implements` (per-module) | `parser_ast.Program` | Parser AST that downstream walks consult | Append `ImplementDef` for the synthesized impl |
| 2 | `signatures_by_id` | `dict[FunctionId, FnSignature]` (driver-level) | Function signature lookup | Add `signatures_by_id[fn_id] = synth_sig` |
| 3 | `func_hirs_by_id` | `dict[FunctionId, H.HBlock]` (driver-level) | HIR body lookup → MIR lowering | Add `func_hirs_by_id[fn_id] = synth_body` |
| 4 | `fn_ids_by_name` | `dict[str, list[FunctionId]]` | Method name → FunctionIds for resolution | Add `fn_id` to the list |
| 5 | `impl_metas` | `list[ImplMeta]` (driver-level) | Package serialization + global impl index | Append synthesized `ImplMeta` |
| 6 | `linked_world.global_world.impls` | `list[ImplDef]` | Trait dispatch in type-check | Append `ImplDef` |
| 6.a | `linked_world.global_world.impls_by_trait` | `dict[TraitKey, list[int]]` | Trait → impls index | Append `impl_id` |
| 6.b | `linked_world.global_world.impls_by_target_head` | `dict[TypeHeadKey, list[int]]` | Type-head → impls index | Append `impl_id` |
| 6.c | `linked_world.global_world.impls_by_trait_target` | `dict[(TraitKey, TypeHeadKey), list[int]]` | (trait, type) → impls index | Append `impl_id` |
| 7 | `linked_world.trait_worlds[def_mid]` | per-module `TraitWorld` | Source for `LinkedWorld.visible_world()` re-merge | Same updates as #6 |
| 8 | `GlobalImplIndex` (`impl_index`) | per-target method index | Method resolution at call sites | `impl_index.add_impl(synth_meta)` |
| 9 | `callable_registry` | (TBD — check exact shape) | Function call dispatch | Register the synth function |

### Critical investigations before coding

**Open question A:** Does HIR→MIR walk `func_hirs_by_id`
directly, or via a different reachability set?  If different,
the synthesized fn_id may not get lowered.  Need to trace
`hir_to_mir` entry points.

**Open question B:** Does the type-check loop walk
`signatures_by_id`, or does it iterate per-module `prog.functions`?
If the latter, synthesized impls in `prog.implements` may be
ignored by type-check unless we ALSO register their methods
in the iteration source.  Need to trace `check_function` callsite.

**Open question C:** Does `GlobalImplIndex` get rebuilt or
mutated after synthesis?  If rebuilt, our additions get
discarded.  Need to trace `impl_index` lifecycle.

These three questions MUST be answered in the sub-plan before
any code lands.  All three are "verify-by-grep + read", not
"new mechanism design", so they are bounded investigation
tasks, not refactor risks.

### Recommendation

**Synthesize at the HIR layer, NOT at the parser AST layer.**

Building parser AST + relying on it to flow through
`lower_program`'s hooks would either require (a) re-running
`lower_program` (invasive), (b) hand-replicating its behavior
(fragile), or (c) finding a public lower-single-impl entry
point (may not exist).

Instead, build directly:
  - `FnSignature` (the signature object)
  - `H.HBlock` body (HIR construction) — same shape `lower_program` produces for hand-written methods
  - `ImplMeta` + `ImplMethodMeta`
  - `ImplDef` for `linked_world.global_world` and `trait_worlds[mid]`
  - `parser_ast.ImplementDef` ALSO appended to `prog.implements`
    SOLELY for the package serializer, which reads
    `_encode_impl_headers_for_module` from impls passed in.
    (Actually — `_encode_impl_headers_for_module` reads
    `ImplMeta` attributes.  Verify whether `ImplMeta` alone
    suffices for serialization or whether we also need the
    parser AST.)

This is "synthesize at HIR layer, register ImplMeta for
serialization."  Cleaner than parser-AST injection because
HIR-level construction has well-defined shapes and doesn't
depend on a re-running of stage1.

---

## Deliverable 4 — Generic require-clause propagation rule

### v1 rule (approved by user)

A generic struct/variant auto-derives `ConstShare` iff its
declared `require` clause already provides enough to make every
field qualify in scope.  No implicit constraint-strengthening.

### Concrete shapes

  - `pub struct Box<T> require T is ConstShare { value: T }`
    → auto-derives.  Synthesized impl:
    `implement<T> ConstShare for Box<T> require T is ConstShare { ... }`.

  - `pub struct Box<T> require T is Copy, T is Frozen { value: T }`
    → auto-derives.  Synthesized impl propagates the same require.

  - `pub struct Box<T> { value: T }` (no require)
    → does NOT auto-derive.  Field `T` of unknown
    ConstShare-ness; UNKNOWN blocks.

  - `pub struct Pair<T, U> require T is ConstShare { first: T, second: Int }`
    → auto-derives.  `T`'s qualification comes from
    `T is ConstShare` (in scope as an assumption); `Int`
    qualifies via Copy+Frozen.

### Implementation hooks

  - During discovery, build a `subst` / `assumed_true` env from
    the struct's `require` clause.  Pass to `prove_is` as the
    proof environment.
  - Synthesized impl gets the SAME require clause (verbatim
    copy from struct's `require`).  Do NOT widen, narrow, or
    re-derive.

### Edge cases

  - Multiple require atoms on a single typevar (`T is A, T is B`):
    propagate verbatim.
  - Conditional impls — N/A in v1; the synthesized impl is
    unconditional given the struct's require clause holds.

---

## Deliverable 5 — Method-body construction

### Struct case

For `pub struct Holder<T> require T is ConstShare { handle: ConstArc<T>, name: String, tag: Int }`:

```drift
implement<T> ConstShare for Holder<T> require T is ConstShare {
    fn const_share(self: &Holder<T>) nothrow -> Holder<T> {
        return Holder<type T>(
            handle = self.handle.const_share(),  // ConstShare path
            name = self.name,                    // Copy+Frozen path
            tag = self.tag                       // Copy+Frozen path
        );
    }
}
```

### Variant case (Phase 4 — defer if awkward)

The user's correction #2 is critical here.  `match self` on
`&Self` gives borrowed bindings per arm.  Each binding `field`
has type `&FieldT` (not owned `FieldT`).

For `pub variant Tag { Empty, Wrapped(handle: ConstArc<String>), Plain(value: ConstArc<Int>, count: Int) }`:

```drift
implement ConstShare for Tag {
    fn const_share(self: &Tag) nothrow -> Tag {
        match self {
            Tag::Empty => { return Tag::Empty; },
            Tag::Wrapped(handle) => {
                // `handle` is `&ConstArc<String>` per arm-binder semantics
                // for borrowed match.
                return Tag::Wrapped(handle = handle.const_share());
            },
            Tag::Plain(value, count) => {
                // `value` is `&ConstArc<Int>`, `count` is `&Int`.
                // ConstShare path: call `.const_share()` on the borrow.
                // Copy+Frozen path: `count` reads through &Int → Int via
                //   the existing borrowed-Copy auto-copy machinery
                //   (same path user-written code uses).
                return Tag::Plain(value = value.const_share(), count = count);
            }
        }
    }
}
```

### Stop conditions for variant body

  - **Borrowed-match grammar mismatch:** if Drift's match
    parser doesn't accept the arm-binder shape we need to
    construct, flag and defer to Phase 4 — phase 1 stays
    structs-only.
  - **`Copy+Frozen` field through &T → T:** must use the
    EXISTING borrowed-Copy auto-copy path; if synthesized AST
    forces a special checker rule, that's a "checker-only
    move/read illusion" the user explicitly forbade.

### Empty struct / payload-less variant

`pub struct Empty { }` → trivially ConstShare; synthesized body
returns `Empty()` (no fields to construct).
`Tag::Empty` arm — just `return Tag::Empty;`.

### Cyclic struct types

`pub struct Node { next: ConstArc<Node> }`:
  - Discovery's prove_is call against `ConstArc<Node>` recurses
    into Node's ConstShare proof.  The prover's existing
    `_in_progress` set catches the cycle and returns UNKNOWN,
    blocking the field's structural promotion.  Result: Node
    does NOT auto-derive on its first iteration.
  - On the next discovery iteration (after some other type
    enables Node via fixed-point), reconsider.  In the absence
    of a non-cyclic path, Node remains non-derived — which is
    the correct behavior (auto-derive on a cyclic type would
    require manual reasoning).
  - Document this limitation in the milestone test.

---

## Deliverable 6 — Package serialization audit

### Existing serializer behavior

`_encode_impl_headers_for_module` (`driftc.py:2010`) reads from
`ImplMeta` objects.  It iterates `impl.methods` and for each
records `name`, `fn_id`, `fn_symbol`, `is_pub`, `span`.  It
does NOT read parser AST, only `ImplMeta` attributes.

So **option (a) "synthesized impls serialize like hand-written
ones"** works iff:

  1. Synthesized impls produce real `ImplMeta` objects (with
     `target_expr`, `trait_key`, `trait_expr`, `methods`,
     `require_expr`, etc.).
  2. The `methods` list contains real `ImplMethodMeta` objects
     with valid `fn_id`s.
  3. Those `fn_id`s appear in `signatures_by_id` (so the
     package signature serializer picks them up).
  4. The HIR/MIR for those fn_ids gets serialized through the
     existing function-body emission path.

### Outstanding investigation (sub-plan deliverable)

Trace `lang/driftc/packages/dmir_pkg_v0.py` and friends:
  - Where do function bodies get serialized to .dmp?  Confirm
    the path keys on `signatures_by_id` / `mir_pool` / `func_hirs_by_id`
    — ALL of which we mutated, so the path picks up synthesized
    methods naturally.
  - Where does method symbol mangling happen for impl methods?
    Confirm synthesized methods get mangled the same way as
    user-written ones.

If the serializer filters by ANY criterion that excludes
synthesized fn_ids (e.g., a `def_module` check that requires a
"real" parsed source location), patch sites will be enumerated
in the sub-plan before coding.

---

## Deliverable 7 — Cross-module / package-mode test plan

### Phase 1 (structs, same-module)

  - Positive: `Holder { handle: ConstArc<String> }` derives;
    `holder.const_share()` works.
  - Positive: `Mixed { handle: ConstArc<Int>, tag: Int, name: String }`
    derives.
  - Positive: nested same-module — `Outer { inner: Inner }` where
    Inner also derives; both work.
  - Positive: generic `dup<T>(x: &T) -> T require T is ConstShare`
    accepts a derived struct type.
  - Negative: `Bad { handle: ConstArc<Int>, lock: Mutex<Int> }`
    blocks.
  - Negative: refs (`&T`, `&mut T`) block.
  - Negative: direct user `implement ConstShare for X` rejected.
  - Memcheck: derived struct lifecycle (heap-bearing payloads).

### Phase 2 (cross-module + cross-package)

  - Positive: struct in module A with field `b.Inner` where
    Inner is in module B (same build) and B's Inner derives —
    A's struct auto-derives via composition.
  - Positive: same as above, but Inner is in a published .dmp
    package — consumer's auto-derive sees the impl through
    LinkedWorld.
  - Positive: package round-trip — publish a derived struct,
    consume it, call `const_share()` → uses the serialized impl.
  - Negative: B's Inner blocks ConstShare → A's struct also
    blocks.
  - Memcheck: cross-module derived struct.

### Phase 3 (generics with explicit require)

  - Positive: `Box<T> require T is ConstShare { value: T }`
    derives; `Box<ConstArc<Int>>` and `Box<UserDerived>` both
    get the impl.
  - Positive: `Pair<T, U> require T is ConstShare { first: T, second: Int }`
    derives.
  - Negative: `Box<T> { value: T }` (no require) does NOT
    derive — explicit user opt-in.
  - Negative: `Pair<T, U> require T is ConstShare { first: T, second: U }`
    where U is unconstrained — does NOT derive.

### Phase 4 (variants — defer if awkward)

  - Positive: variant with payload arm derives.
  - Positive: variant with mix of payload and payload-less
    arms.
  - Negative: variant arm with non-qualifying field blocks.
  - Memcheck: variant lifecycle.

---

## Deliverable 8 — Phasing

(Approved by user.)

  1. Structs only, concrete fields, same-module.
  2. Structs cross-module + package-mode.
  3. Generic structs with explicit requires.
  4. Variants.

Each phase commit-able and revertable.  Each gates on:
  - phase-specific driver tests + memcheck;
  - the substrate suite (49 existing tests);
  - cross-module / package tests for Phase 2+;
  - full driver+stage+checker+packages suite;
  - full memcheck suite.

---

## Deliverable 9 — Stop conditions per phase

### Phase 1 stop conditions

  - HIR construction shape mismatch — synthesized HBlock fails
    type-check or HIR→MIR with cryptic errors.  Stop and
    investigate the exact shape mismatch.
  - The "open questions A/B/C" from §3 turn out to require
    pipeline restructure.  Stop and report.
  - Synthesized `ImplDef` mutation of `LinkedWorld` causes
    cache invalidation issues elsewhere.  Stop and report.

### Phase 2 stop conditions

  - Package serialization filters out synthesized fn_ids.
    Stop and report exact patch sites.
  - Cross-package consumer's `LinkedWorld` doesn't see the
    producer's synthesized impl.  Stop — investigate whether
    it's a serialization issue or a consumer-side load issue.

### Phase 3 stop conditions

  - Generic require-clause propagation needs deeper
    type-checker surgery beyond verbatim copy.  Stop.

### Phase 4 stop conditions (variants)

  - Match-on-borrowed grammar doesn't accept the synthesized
    arm shape.  Stop, structs-only stays as the final state
    until the grammar question is resolved.

---

## Deliverable 10 — Verification gates per phase

Same gate template per phase:

  1. Run new phase-specific driver tests.
  2. Run substrate suite (`test_const_share_substrate.py`,
     `test_const_arc_substrate.py`, `test_frozen_substrate.py`,
     `test_arc_relocation.py`).
  3. Run new phase-specific memcheck.
  4. Run existing memcheck suite
     (`lang/tests/memcheck/test_const_share_memcheck.py`,
     `test_const_arc_memcheck.py`).
  5. For Phase 2+: cross-module test
     (`test_const_share_substrate_package.py`-style — one
     producer module, one consumer module).
  6. Full driver+stage+checker+packages suite — `-n16`, no
     filters.
  7. Full memcheck suite.
  8. ABI bump regression (`test_abi_version_stamp.py`) — no
     ABI bump expected for any of these phases (the wire
     contract for `.dmp` doesn't change, only the count of
     ImplMeta entries).

---

## Open investigations — must complete before Phase 1 implementation

  1. **Question A:** Does HIR→MIR walk `func_hirs_by_id`
     directly, or via a reachability set?  Find the entry
     point in `lang/driftc/stage2/` and confirm.
  2. **Question B:** Does type-check iterate
     `signatures_by_id` or `prog.functions`?  Find the
     `check_function` callsite loop and confirm.
  3. **Question C:** `GlobalImplIndex` lifecycle — built once
     after synthesis, or rebuilt later?  Find construction
     site and confirm.
  4. **Question D:** Does `_install_*_query` (lines 853-856)
     cache impl lists?  If yes, must invalidate or move
     synthesis BEFORE these.
  5. **Question E:** Method symbol mangling — does the symbol
     for `Holder::const_share` get mangled the same way as a
     hand-written `Holder::method_name`?  Confirm by reading
     `function_symbol(fn_id)` for both shapes.

These five investigations are all "grep + read", bounded.  All
five must complete before Phase 1 starts.

---

## Decision points awaiting user

  - **Approval on the artifact-identity recommendation** (HIR-layer
    synthesis, not parser-AST injection — see §3 recommendation).
  - **Approval on the LinkedWorld mutation requirement** (must
    update both `global_world` AND `trait_worlds[def_mid]` per
    §2; must investigate query-cache invalidation).
  - **Approval to start the five open investigations** (A–E
    above) as the next concrete deliverable.

Holding at clean Path A.
