# Post-link ConstShare synthesis — investigations A–E results

**Status:** investigation complete.  No code written.  This file
answers the five open questions raised in
`post-link-synthesis-deliverables.md` and the four acceptance
criteria from the user's directive (table of structures,
single-helper API, phase-1 sketch, do-not-mutate notes).

---

## Investigation A — does HIR→MIR walk `func_hirs_by_id` directly?

**Answer:** No.  HIR→MIR walks `normalized_hirs_by_id.items()`
at `driftc.py:5576`.

**Trace:**
  - `func_hirs_by_id` is the per-fn-id HIR table merged from
    per-module lower_program at `driftc.py:2731`.
  - `normalized_hirs_by_id` is built from it at `driftc.py:2978`
    via `normalize_hir(hir_block) for fn_id, hir_block in func_hirs_by_id.items()`
    (a shallow-copied dict, then re-built per-entry).
  - Type-check (L3743) AND HIR→MIR (L5576) both consume
    `normalized_hirs_by_id`.
  - Generic instantiations also write into `normalized_hirs_by_id`
    directly (L4563), confirming it's the post-normalize
    authoritative table.

**Implication:** synthesized HIR must be added to
`normalized_hirs_by_id`.  Adding to `func_hirs_by_id` alone is
insufficient (the dict was shallow-copied; mutations don't
propagate).

---

## Investigation B — does type-check iterate `signatures_by_id` or `prog.functions`?

**Answer:** Type-check iterates `normalized_hirs_by_id.items()`
at `driftc.py:3743` (`for fn_id, hir_norm in normalized_hirs_by_id.items(): _typecheck_fn(fn_id, hir_norm)`).

`signatures_by_id` is consulted INSIDE `_typecheck_fn` for
parameter / return-type info but is NOT the iteration source.

**Implication:** to get a synthesized function type-checked, it
must appear in `normalized_hirs_by_id` BEFORE L3743.
`signatures_by_id[fn_id]` must also be populated — looked up at
L3592 of `_typecheck_fn`.

**Confirmed:** `normalized_hirs_by_id` is the SINGLE
authoritative HIR-table for both type-check and HIR→MIR.

---

## Investigation C — `GlobalImplIndex` lifecycle

**Answer:** Built ONCE at `driftc.py:3420` via
`GlobalImplIndex.from_module_exports(...)`.  External-package
impls register at L3434 via `impl_index.add_impl(...)`.  After
that, `impl_index` is treated as a complete authority by
type-check (passed in at L3632).

**Insertion point for synthesized impls:** after the external-
package registration block (L3434-3436) and before the
type-check loop (L3743).

**API to use:** `impl_index.add_impl(impl=meta, type_table=..., module_ids=...)`
— the SAME entry point external impls use.  Synthesized
`ImplMeta` objects must conform to the same shape (target_type_id,
trait_key, methods with fn_id/name/is_pub, etc.).

---

## Investigation D — query callback caching

**Answer (two parts):**

  1. **`_install_*_query` callbacks (driftc.py:503-700):** install
     closures on `type_table` that capture `linked_world.global_world`
     by reference.  Subsequent calls see ANY mutations to
     `world.impls` / `impls_by_*`.  No impl-list caching at install
     time.

  2. **`type_table.copy_status(ty)` (`types_core.py:2528`):** caches
     per-tid results in `_copy_cache_proof` and
     `_copy_cache_structural`.  Once a tid's status is cached,
     it persists.

  3. **`prove_is` per-call `_cache`:** fresh per top-level call;
     not shared across callers.  Safe.

**Implication for synthesis:** ConstShare additions do NOT
affect Copy/Frozen proof results, so the per-tid copy-status
cache won't pollute or be polluted.  No invalidation needed.

**The `_install_*_query` calls happen at driftc.py:853-856,
INSIDE `_build_linked_world`.  Synthesis runs AFTER this
returns, so the queries are ready and will see the synthesized
impls on next call.  No re-installation needed.

**Confirmed:** no cache invalidation surgery required.

---

## Investigation E — symbol mangling parity

**Answer:** identical for synthesized vs hand-written impls.

  - `function_symbol(fn_id)` is purely
    `f"{module}::{name}"` (or `name` for `module=="main"`),
    with `#ordinal` suffix when `ordinal != 0`.  Defined at
    `core/function_id.py:29`.
  - For impl methods, `name` follows the
    `{target_str}::{trait_str}::{fn.name}` shape (built at
    `parser/__init__.py:4649`).  E.g.
    `Holder::ConstShare::const_share`.
  - Synthesized methods using the same `_impl_target_key` /
    `_type_expr_key_str` rendering produce the same symbol.

**Implication:** if the synthesizer constructs `fn_id`s with
the same `(module, name, ordinal)` shape user-written impls
produce, downstream symbol-mangled paths (LLVM symbols, package
serialization, debug info) work without special-case branches.

**Action item for synthesizer:** use existing
`_impl_target_key` / `_type_expr_key_str` helpers (or extract
their logic to a shared helper) to build the `name` field of
the synthesized fn_id.  Use a deterministic ordinal allocator
to avoid clashes.

---

## Authoritative-tables table (acceptance criterion #1)

| # | Structure | Type | Built where | Consumed by | Needs synth update? | How to update |
|---|---|---|---|---|---|---|
| 1 | `normalized_hirs_by_id` | `dict[FunctionId, H.HBlock]` | `driftc.py:2978` | type-check loop (L3743), HIR→MIR (L5576), generic instantiation (L4563) | **Yes** | Direct dict assignment: `normalized_hirs_by_id[fn_id] = synth_hir`.  Update via single helper (below) — never write at random call sites. |
| 2 | `signatures_by_id` | `dict[FunctionId, FnSignature]` | `_normalize_func_maps` (L2731) | `_typecheck_fn` (L3592), HIR→MIR (L5578), package emit | **Yes** | Direct dict assignment. |
| 3 | `func_hirs_by_id` | `dict[FunctionId, H.HBlock]` | `_normalize_func_maps` (L2731) | normalize → `normalized_hirs_by_id` (L2978), one keys-only read for `_required_modules` (L2757) | **Yes (defensive)** | Add too, for consistency.  Stale state otherwise. |
| 4 | `fn_ids_by_name` | `dict[str, list[FunctionId]]` | `_normalize_func_maps` (L2731) | resolution paths in checker | **Yes** | Append fn_id to `fn_ids_by_name[function_symbol(fn_id)]`. |
| 5 | `impl_metas` | `list[ImplMeta]` | per-module `lower_program` + external metas | package emit (`_encode_impl_headers_for_module`) | **Yes** | Append synthesized `ImplMeta`. |
| 6 | `linked_world.global_world.impls` | `list[ImplDef]` | `link_trait_worlds` (L851) | `prove_is` via global trait queries | **Yes** | `world.impls.append(impl_def); update three indices` (mirroring `world.py:904-919`). |
| 7 | `linked_world.global_world.impls_by_trait` | `dict[TraitKey, list[int]]` | linked merge | trait→impls dispatch | **Yes** | `setdefault(trait_key, []).append(impl_id)` |
| 8 | `linked_world.global_world.impls_by_target_head` | `dict[TypeHeadKey, list[int]]` | linked merge | type-head→impls dispatch | **Yes** | Same pattern. |
| 9 | `linked_world.global_world.impls_by_trait_target` | `dict[(TraitKey, TypeHeadKey), list[int]]` | linked merge | (trait,type)→impls dispatch | **Yes** | Same pattern. |
| 10 | `linked_world.trait_worlds[def_module]` | per-module `TraitWorld` | per-module `build_trait_world` | `LinkedWorld.visible_world(...)` re-merge (`linked_world.py:61`) | **Yes (load-bearing)** | Same four updates as #6-9 but on the per-module world. |
| 11 | `impl_index` (`GlobalImplIndex`) | private indices | `from_module_exports` (L3420), `add_impl` later | method dispatch via `make_call_ctx` → resolution | **Yes** | `impl_index.add_impl(impl=synth_meta, type_table=..., module_ids=...)`. |
| 12 | `prog.implements` (per-module) | `list[parser_ast.ImplementDef]` | parser → `lower_program` | downstream walks (audit needed for serializer) | **Maybe** | Append parser_ast.ImplementDef.  Investigate whether the synthesizer can skip this and rely solely on `impl_metas` for serialization (sub-deliverable below). |
| 13 | `type_table._copy_cache_*` | per-tid Copy result cache | first `copy_status` call | Copy lookup | **No** | Independent trait; ConstShare additions don't touch this.  **DO NOT mutate.** |
| 14 | `prove_is` per-call `_cache` | per-call result cache | `prove_is(_cache=...)` callers | within a single proof | **No** | Fresh per top-level call.  **DO NOT cache across calls.** |

### Sub-deliverable (open before coding)

**Audit:** does any code path read from `prog.implements` AFTER
the synthesizer runs?  Specifically check:
  - Package serialization — does it iterate `prog.implements`
    or `impl_metas`?  (Section §6 of deliverables suggests it
    reads `impl_metas`; verify by tracing
    `_encode_impl_headers_for_module` callers.)
  - Any post-typecheck pass that walks `prog.implements`?

If the answer is "only `impl_metas`," then `prog.implements`
update (row 12) is unnecessary and can be skipped.  If
serialization OR another pass DOES read `prog.implements`,
update is required.

---

## Single insertion helper API (acceptance criterion #2)

```python
# In a new module: lang/driftc/const_share_synth.py
# (replaces the previous parser-local draft; this lives at
# driver level, not parser level.)

def register_synthesized_const_share_impl(
    *,
    # Inputs from synthesis
    target_struct_def: parser_ast.StructDef,
    target_type_id: TypeId,
    target_module_id: str,
    method_hir: H.HBlock,            # synthesized const_share body
    method_signature: FnSignature,    # synthesized signature
    fn_id: FunctionId,                # allocated synthesized fn_id
    method_def_for_serialization: parser_ast.FunctionDef,  # for prog.implements / serializer
    method_def_loc: Span | None,
    # State to update (all driver-level objects)
    linked_world: LinkedWorld,
    impl_index: object,               # GlobalImplIndex
    signatures_by_id: dict[FunctionId, FnSignature],
    normalized_hirs_by_id: dict[FunctionId, H.HBlock],
    func_hirs_by_id: dict[FunctionId, H.HBlock],
    fn_ids_by_name: dict[str, list[FunctionId]],
    impl_metas: list[ImplMeta],
    type_table: TypeTable,
    module_ids: dict[str, int],
    prog_implements_by_module: dict[str, list[parser_ast.ImplementDef]],
    package_id: str | None,
    module_packages: dict[str, str],
) -> None:
    """Atomically register a synthesized ConstShare impl across
    every authoritative table identified in the post-link
    investigation.

    NEVER call this from random sites; it is the SOLE entry point
    for synthesized impl registration.  Adds:

      - signatures_by_id[fn_id] = method_signature
      - normalized_hirs_by_id[fn_id] = method_hir
      - func_hirs_by_id[fn_id] = method_hir       (defensive sync)
      - fn_ids_by_name[symbol] += [fn_id]
      - impl_metas += [ImplMeta(...)]
      - linked_world.global_world.impls += [ImplDef(...)] + 3 indices
      - linked_world.trait_worlds[target_module_id].impls += same
      - impl_index.add_impl(meta, type_table, module_ids)
      - prog_implements_by_module[target_module_id] += [ImplementDef(...)]   (if needed; see audit)

    Does NOT mutate:
      - type_table._copy_cache_*       (independent traits)
      - prove_is per-call caches       (per-caller scope)
      - impl_index private indices     (use add_impl, not direct)
    """
    ...  # implementation per the table above
```

### Discovery / driver flow

```python
# In driftc.py, between L3434 (external impl registration) and
# L3743 (type-check loop):

const_share_synthesizer = ConstShareSynthesizer(
    linked_world=linked_world,
    type_table=shared_type_table,
    package_id=...,
    module_packages=...,
)

candidates = const_share_synthesizer.discover(
    all_struct_defs=...,  # collected from all per-module progs
)

for candidate in candidates:
    synth = const_share_synthesizer.synthesize(candidate)
    register_synthesized_const_share_impl(
        target_struct_def=candidate.struct_def,
        target_type_id=candidate.target_type_id,
        target_module_id=candidate.module_id,
        method_hir=synth.hir,
        method_signature=synth.signature,
        fn_id=synth.fn_id,
        method_def_for_serialization=synth.parser_ast_method,
        method_def_loc=...,
        linked_world=linked_world,
        impl_index=impl_index,
        signatures_by_id=signatures_by_id,
        normalized_hirs_by_id=normalized_hirs_by_id,
        func_hirs_by_id=func_hirs_by_id,
        fn_ids_by_name=fn_ids_by_name,
        impl_metas=impl_metas,
        type_table=shared_type_table,
        module_ids=module_ids,
        prog_implements_by_module=prog_implements_by_module,
        package_id=...,
        module_packages=...,
    )
```

The `ConstShareSynthesizer` class encapsulates discovery + body
generation; `register_synthesized_const_share_impl` encapsulates
the multi-table update.  Two narrow APIs, no scattered writes.

---

## Phase-1 implementation sketch (acceptance criterion #3)

**Phase 1 scope** (per user direction):
  - Structs only.
  - Concrete fields only.
  - Same-module composition.
  - No variants / no generics / no inter-module fixed-point.

**Implementation sketch:**

```python
# 1. Discovery
def discover_phase1(all_progs):
    """Returns list of (StructDef, target_type_id, module_id,
    field_paths) where field_paths[i] is "const_share" or
    "copy_frozen" per field."""
    derived = {}  # type_id -> field_paths
    candidates = [(s, mid) for mid, prog in all_progs.items()
                          for s in prog.structs
                          if not s.type_params  # skip generics in phase 1
                          and s.fields]
    changed = True
    while changed:
        changed = False
        for struct_def, mid in candidates:
            target_type_id = type_table.get_struct_base(module_id=mid, name=struct_def.name)
            if target_type_id in derived:
                continue
            # Skip if user wrote an explicit ConstShare impl
            if has_user_const_share_impl(struct_def, prog):
                continue
            field_paths = []
            ok = True
            for field in struct_def.fields:
                field_ty_id = resolve_field_type_id(field.type_expr, ...)
                if field_ty_id is None or has_typevar(field_ty_id):
                    ok = False
                    break  # skip generics in phase 1
                field_ty_key = type_key_from_typeid(type_table, field_ty_id)
                # Try ConstShare directly via prove_is on linked_world.
                cs = prove_is(linked_world.global_world, env, {}, field_ty_key, const_share_key)
                if cs.status is ProofStatus.PROVED:
                    field_paths.append("const_share")
                    continue
                # Fall back to Copy + Frozen.
                cp = prove_is(linked_world.global_world, env, {}, field_ty_key, copy_key)
                fz = prove_is(linked_world.global_world, env, {}, field_ty_key, frozen_key)
                if cp.status is ProofStatus.PROVED and fz.status is ProofStatus.PROVED:
                    field_paths.append("copy_frozen")
                    continue
                ok = False
                break
            if ok:
                derived[target_type_id] = (struct_def, mid, field_paths)
                changed = True  # may unlock other candidates
    return derived

# 2. Synthesis (per derived candidate)
def synthesize_phase1(struct_def, mid, field_paths):
    """Builds the synthesized HIR body, signature, fn_id, and
    parser_ast.FunctionDef for the const_share method."""
    fn_id = allocate_fn_id(mid, f"{struct_def.name}::ConstShare::const_share")
    signature = FnSignature(
        param_names=["self"],
        param_type_ids=[type_table.ensure_ref(target_type_id)],
        param_mutable=[False],
        return_type_id=target_type_id,
        ...
    )
    # Build HIR for the body.  Body shape:
    #   return Self(f1=self.f1.const_share(), f2=self.f2, ...)
    # Construct H.HReturn(value=H.HCall(constructor=struct_name,
    #                                   kwargs=[...])).
    hir_body = build_const_share_hir_body(struct_def, field_paths, target_type_id)
    method_def = build_parser_ast_function_def(struct_def, field_paths)
    return synth_result(fn_id, signature, hir_body, method_def)

# 3. Registration via the single helper API
for tid, (struct_def, mid, field_paths) in derived.items():
    synth = synthesize_phase1(struct_def, mid, field_paths)
    register_synthesized_const_share_impl(
        target_struct_def=struct_def,
        target_type_id=tid,
        target_module_id=mid,
        method_hir=synth.hir,
        method_signature=synth.signature,
        fn_id=synth.fn_id,
        method_def_for_serialization=synth.method_def,
        ...,  # rest of state
    )
```

**Phase-1 limitations enforced:**
  - Discovery skips `s.type_params` non-empty (generics).
  - Discovery skips empty-fields structs (open question for v1).
  - `has_typevar(field_ty_id)` short-circuits — concrete only.
  - Fixed-point on same-module candidates only (no
    inter-module iteration).

**Phase-1 tests** (per `post-link-synthesis-deliverables.md` §7):
  - Positive: ConstArc-only, mixed Copy+Frozen+ConstArc, nested
    same-module, generic dup<T:ConstShare>.
  - Negative: Mutex / Arc / Array / HashMap / refs block.
  - Direct user impl rejected.
  - Memcheck: derived struct lifecycle.

---

## Do-not-mutate notes (acceptance criterion #4)

**Caches and derived structures the synthesizer MUST NOT touch:**

  1. **`type_table._copy_cache_proof` / `_copy_cache_structural`**
     (`types_core.py:2530-2534`): per-tid Copy result cache.
     Independent of ConstShare; touching it would corrupt
     unrelated proofs.  Synthesis adds ConstShare impls; Copy
     impls are not affected.
  2. **`prove_is` `_cache` argument**: per-top-level-call.  Each
     synthesizer call creates a fresh _cache.  NEVER share across
     callers.
  3. **`impl_index._by_target_method` / `_seen_impl_methods`**
     (`impl_index.py:62-63`): private maps.  Use
     `impl_index.add_impl(...)` only.
  4. **`linked_world.global_world.impls` direct slice indexing**:
     impl_id is the position in the list; appending is fine,
     but never insert/replace.  The three index dicts use
     `len(impls)` BEFORE append as the new impl_id.
  5. **per-module `trait_worlds[mid]` impl lists when `mid` is
     NOT the target's `def_module`**: only the def_module's
     trait_world records the impl.  The user's import resolution
     through `LinkedWorld.visible_world(...)` re-merges, so
     downstream consumers get visibility.  Writing the impl into
     EVERY trait_world would create duplicate-impl errors at
     re-merge time.

---

## Summary of investigation outcomes

| Open question | Answer | Implication |
|---|---|---|
| A: HIR→MIR walks `func_hirs_by_id`? | No, walks `normalized_hirs_by_id`. | Synthesis must update `normalized_hirs_by_id`. |
| B: Type-check iterates `signatures_by_id` or `prog.functions`? | Walks `normalized_hirs_by_id`; `signatures_by_id` is consulted inside per-fn typecheck. | Same as A. Both tables must be updated. |
| C: `GlobalImplIndex` lifecycle? | Built once at L3420; `add_impl` is the registration entry point. | Synthesis registers via `add_impl` between L3434 and L3743. |
| D: Query callbacks cache impls? | No. `_install_*_query` capture world by reference. `copy_status` caches per-tid but is independent of ConstShare. `prove_is` _cache is per-call. | No invalidation needed. |
| E: Symbol mangling parity? | Identical. `function_symbol` is purely (module, name, ordinal). Impl method names follow `{target}::{trait}::{fn.name}`. | Synthesizer uses existing helpers; downstream paths work without special cases. |

---

## Decision points awaiting user (sub-plan only — no code)

  1. **Approval on the authoritative-table list** (12 tables to
     update; #13–14 explicit do-not-touch).  Anything missed?
  2. **Approval on the single-helper API shape**
     (`register_synthesized_const_share_impl`).  Acceptable to
     proceed with this signature, or refactor first?
  3. **Approval on `prog.implements` row 12** — should the
     synthesizer also append to per-module `prog.implements` for
     defensive completeness, or rely on `impl_metas` exclusively
     pending the package-serialization audit (sub-deliverable
     above)?
  4. **Approval to start Phase 1 implementation** (structs-only,
     same-module) following the sketch in §3 of this doc.

Holding at clean Path A.  Phase-1 implementation does NOT start
without explicit go-ahead.
