# Post-link ConstShare synthesis — final audit + corrections (2026-05-01)

**Status:** audit complete.  No code written.  Addresses both
user corrections (visible-world rule + package serialization
audit) and delivers the final blockers from §7 of the
deliverables doc.

---

## Correction 1 — visibility-aware proof world

### Rule (replaces the earlier `linked_world.global_world` proposal)

For a struct/variant defined in module `M`, **field qualification
queries use the same trait world the type-checker uses for
functions in `M`**:

```python
visible = visible_module_names_by_name.get(M, {M})
proof_world = linked_world.visible_world(visible)
result = prove_is(proof_world, env, {}, field_ty_key, ...)
```

### Why

The user's correction:
> If synthesis proves a field using a globally present but
> non-visible impl, we create an impl that user code could
> not have written under normal import rules.

The visibility view used by type-check is
`linked_world.visible_world(visible_module_names_by_name[M])`.
Type-check passes this as `linked_world.visible_world(visible_modules)`
to `enforce_struct_requires` /
`enforce_fn_requires` (`traits/enforce.py:220`, `:294`).
Synthesis must use the same view per defining module — otherwise
auto-derive could "see through" import boundaries and produce
impls the user couldn't have hand-written.

### Concrete proof-world helper

```python
def _proof_world_for_module(
    *,
    linked_world: LinkedWorld,
    visible_module_names_by_name: dict[str, set[str]],
    def_module: str,
) -> TraitWorld:
    """Returns the same trait-world view type-check uses for
    functions defined in `def_module`."""
    visible = visible_module_names_by_name.get(def_module, {def_module})
    return linked_world.visible_world(visible)
```

This is the SOLE proof-world entry point synthesis discovery
uses.  No direct `linked_world.global_world` reads inside the
qualification check.

### `linked_world.global_world` is still updated as a registry

Synthesis registers the synthesized `ImplDef` into BOTH:
  - `linked_world.global_world.impls` (and 3 indices) — so any
    consumer that holds `global_world` directly sees the new
    impl;
  - `linked_world.trait_worlds[def_module].impls` (and 3
    indices) — so subsequent `visible_world(...)` re-merges
    pick it up.

This separation keeps:
  - `visible_world(M)` as the **proof authority** for
    qualification of types defined in M;
  - `global_world` as the **registry/index** updated in lockstep.

Type-check itself uses `visible_world(...)` already
(`traits/enforce.py:220, 294`), so the registry behavior is
consistent with existing patterns.

---

## Correction 2 — Package serialization audit

### What package emission actually reads

Traced from `lang/driftc/driftc.py:9656` (snapshot) through
`L10619` (emit-package main) to `L11255` (`write_dmir_pkg_v0`):

| Source | Read at line | Used for |
|---|---|---|
| `module_exports[mid]["impls"]` | L10931 → `_encode_impl_headers_for_module` | `impl_headers` in each module's payload |
| `_pre_typecheck_hirs` (deepcopy of `normalized_hirs_by_id` at L9666) | L10777 → `per_module_hir` → `encode_hir_funcs` | `hir_funcs` in each module's payload |
| `pkg_signatures_by_symbol` (derived from `signatures_by_id`) | L10728 → `per_module_sigs` → `encode_module_payload_v0` | `signatures` in each module's payload |
| `mir_funcs` (from `compile_stubbed_funcs`) | L10755 → `per_module_mir` | MIR output of stubbed funcs |
| **NOT READ:** `prog.implements` | — | — |

### Conclusions

1. **`prog.implements` is NOT read by package emission.**  Row
   12 of the deliverables-doc table can be skipped — the
   synthesizer does not need to append to per-module
   `prog.implements` for serialization purposes.

   Caveat: a separate audit is needed to confirm no
   non-serialization pass reads `prog.implements` between
   synthesis insertion and package emission.  Sub-bullet
   below.

2. **`module_exports[mid]["impls"]` is the impl source.**  This
   is per-module impl_metas inside the `module_exports` dict.
   Synthesis must add to this list in the per-module sub-dict.

3. **`_pre_typecheck_hirs` (the deepcopy of
   `normalized_hirs_by_id` at L9666 in emit-package mode) is
   the HIR source.**  Synthesis must update
   `normalized_hirs_by_id` BEFORE this snapshot is taken,
   otherwise the synthesized HIR is excluded from package
   emission.

4. **`signatures_by_id` flows in via
   `pkg_signatures_by_symbol`.**  Synthesized signatures need
   to be in `signatures_by_id` (and consequently
   `pkg_signatures_by_symbol`).

### `prog.implements` audit residue

Outside package emission, `prog.implements` is consumed during
per-module `lower_program` (parser stage, before our synthesis
runs).  After lower_program, the per-module impls have already
been turned into `impl_metas`.  We can confirm:

```bash
$ grep -rn "prog.implements\|\.implements\b" lang/driftc/ | grep -v __pycache | grep -v "tests"
```

shows references in `parser/__init__.py` (parser stage) and
`packages/provider_v0.py` (consumer-side load).  No driver-level
post-link consumer reads `prog.implements`.

**Therefore: row 12 of the table is officially SKIPPED.**

### Snapshot-timing constraint

The snapshot at L9666 is taken ONLY in emit-package mode.  In
non-emit-package mode, package serialization doesn't run.  But
in both modes, **synthesis must run BEFORE the snapshot AND
BEFORE type-check** — both consume `normalized_hirs_by_id`.

The earliest valid synthesis insertion point in the
emit-package flow is between L9650 (`normalize_hir`) and L9666
(snapshot).  But the linked world must also exist by then —
which requires `_build_linked_world` to have run.

Looking at the actual main() flow (not the
`compile_stubbed_funcs` helper): `_build_linked_world` is
called at L3253 INSIDE `compile_stubbed_funcs`.  In the
emit-package main flow, `compile_stubbed_funcs` is called at
L10673 — AFTER the snapshot at L9666.

This is a problem: in emit-package mode, the snapshot happens
BEFORE compile_stubbed_funcs (which is where LinkedWorld
materializes).  Synthesis can't run between snapshot and
compile because LinkedWorld doesn't exist yet at snapshot
time.

### Resolution: synthesis runs INSIDE `compile_stubbed_funcs`

The synthesis insertion point must be INSIDE
`compile_stubbed_funcs`, between `_build_linked_world` (L3253)
and the type-check loop (L3743).  At that point:
  - LinkedWorld exists (post-L3253);
  - impl_index exists (post-L3420);
  - signatures_by_id exists (from the helper's normalize step
    at L2731 / dict `signatures_by_id`);
  - normalized_hirs_by_id exists (post-L2978).

But the snapshot at L9666 (in emit-package main flow) happens
BEFORE `compile_stubbed_funcs` is called.  So the snapshot
captures pre-synthesis HIR.

**This is a problem for emit-package.**  Options:

  (a) Move the snapshot AFTER `compile_stubbed_funcs` returns
      (which means the snapshot captures post-typecheck HIR, but
      the comment at L9660-9664 explicitly says "before
      type-checking" because type-check mutates HIR in place).
      Possibly contradicts existing requirements.

  (b) Run synthesis BEFORE the snapshot, in emit-package mode
      specifically.  Requires a SECOND `_build_linked_world`
      call (or refactoring `compile_stubbed_funcs` to expose
      its setup separately).

  (c) Skip synthesis in emit-package mode for Phase 1.  The
      synthesized impls would be lost on serialization, so a
      consumer of the produced .dmp wouldn't see them.  Not
      acceptable for cross-package tests in Phase 2.

  (d) Recognize that the snapshot at L9666 might already
      include the synthesis IF we move synthesis to be a
      pre-compile step (before compile_stubbed_funcs).
      Requires linked_world to be built earlier.

**Recommended:** option (b) with a refactor.  Lift
`_build_linked_world` to the main() flow at a point BEFORE the
snapshot at L9666; run synthesis between linked-world build
and snapshot; let `compile_stubbed_funcs` reuse the prebuilt
linked_world rather than rebuilding it.

This refactor is bounded but is the kind of change the user
flagged as potentially "broad surgery."  **STOP point:**
before implementation begins, sub-plan must concretely show:
  - Where the lifted `_build_linked_world` call goes;
  - How `compile_stubbed_funcs` accepts a prebuilt LinkedWorld
    (or skips re-building);
  - Whether any existing callers of `compile_stubbed_funcs`
    break.

If this refactor is too invasive, fall back to a clear stop
condition for Phase 1: **emit-package mode synthesis is
deferred to Phase 2** with the snapshot-timing fix as part of
that phase.

---

## Final insertion helper signature

After the audit, the helper does NOT update `prog.implements`:

```python
def register_synthesized_const_share_impl(
    *,
    # Inputs
    target_struct_def: parser_ast.StructDef,
    target_type_id: TypeId,
    target_module_id: str,
    method_signature: FnSignature,
    method_hir: H.HBlock,
    fn_id: FunctionId,
    # State (driver-level)
    linked_world: LinkedWorld,
    impl_index: object,                          # GlobalImplIndex
    signatures_by_id: dict[FunctionId, FnSignature],
    normalized_hirs_by_id: dict[FunctionId, H.HBlock],
    func_hirs_by_id: dict[FunctionId, H.HBlock],
    fn_ids_by_name: dict[str, list[FunctionId]],
    module_exports: dict[str, dict[str, list]],  # for per-module impls
    type_table: TypeTable,
    module_ids: dict[str, int],
    package_id: str | None,
    module_packages: dict[str, str],
) -> None:
    """SOLE entry point for multi-table synthesized-impl
    registration.  Atomic: either every table updates or none
    (raises before any partial mutation).

    Tables updated (per the post-link investigation):
      1. signatures_by_id[fn_id] = method_signature
      2. normalized_hirs_by_id[fn_id] = method_hir
      3. func_hirs_by_id[fn_id] = method_hir          (defensive sync)
      4. fn_ids_by_name[function_symbol(fn_id)].append(fn_id)
      5. linked_world.global_world.impls += [ImplDef(...)] + 3 indices
      6. linked_world.trait_worlds[target_module_id].impls += same
      7. ImplMeta + ImplMethodMeta added to
         module_exports[target_module_id]["impls"]
      8. impl_index.add_impl(impl=meta, type_table=..., module_ids=...)

    Authority per consumer:
      - typecheck for fn_id: signatures_by_id + normalized_hirs_by_id
      - HIR→MIR for fn_id: same
      - package emission impl_headers: module_exports[mid]["impls"]
        (NOT prog.implements — confirmed by audit)
      - package emission hir_funcs: normalized_hirs_by_id (via
        L9666 snapshot)
      - package emission signatures: signatures_by_id
      - method dispatch at call sites: impl_index
      - prove_is(visible_world(M), ...): linked_world.trait_worlds[M]

    NOT updated (audit-confirmed unnecessary):
      - prog.implements per module — package emission and downstream
        passes do not read it post-lower_program.

    NOT mutated (explicit do-not-touch):
      - type_table._copy_cache_proof / _copy_cache_structural
      - prove_is per-call _cache (caller-scoped)
      - impl_index._by_target_method (use add_impl)
      - non-def-module trait_worlds[mid] (would create dup-impl errors)
    """
    # Implementation per the table above.
    # All updates done in a single try/except; on any error,
    # the function raises and the caller fails the whole
    # compile.  Atomicity is by abort-on-failure.
    ...
```

### Discovery helper (visibility-aware)

```python
class ConstShareSynthesizer:
    def __init__(self, *, linked_world, type_table,
                 visible_module_names_by_name, package_id,
                 module_packages):
        self.linked_world = linked_world
        self.type_table = type_table
        self.visible_module_names_by_name = visible_module_names_by_name
        self.package_id = package_id
        self.module_packages = module_packages
        # Resolve trait keys (ConstShare, Copy, Frozen) once
        self._trait_keys = self._resolve_trait_keys()

    def _proof_world_for(self, def_module: str) -> TraitWorld:
        """Visibility-aware proof world for a type defined in
        `def_module`.  THIS is the only proof-world function
        synthesis uses for qualification."""
        visible = self.visible_module_names_by_name.get(
            def_module, {def_module}
        )
        return self.linked_world.visible_world(visible)

    def discover(self, all_struct_defs):
        """Returns list[(StructDef, target_type_id, def_module,
        field_paths)] for structs that auto-derive ConstShare
        under the v1 composition rule.  Fixed-point iteration."""
        derived = {}
        candidates = self._candidate_structs(all_struct_defs)
        changed = True
        while changed:
            changed = False
            for cand in candidates:
                if cand.target_type_id in derived:
                    continue
                proof_world = self._proof_world_for(cand.def_module)
                field_paths = self._qualify_all_fields(
                    cand, proof_world,
                )
                if field_paths is not None:
                    derived[cand.target_type_id] = (
                        cand, field_paths,
                    )
                    # NOTE: changed=True here would only matter if
                    # we register the impl into the world DURING
                    # discovery so subsequent iterations see it.
                    # Phase-1 deferral: register all at end; if
                    # same-module composition matters in Phase 1,
                    # promote registration to per-iteration.
                    changed = True
        return list(derived.values())

    def _qualify_all_fields(self, cand, proof_world):
        """Returns list[str] of paths ('const_share' or
        'copy_frozen') per field, or None if any field blocks."""
        ...

    def synthesize(self, cand, field_paths):
        """Builds method_signature, method_hir, fn_id, and the
        ImplMeta inputs needed for register_synthesized_const_share_impl.
        Pure construction — no global state mutation."""
        ...
```

The synthesizer's `discover()` and `synthesize()` produce values
only.  Mutation of all 8 driver-level tables happens
EXCLUSIVELY through `register_synthesized_const_share_impl`.

---

## Updated Phase 1 test list

### Phase 1 scope (unchanged)

  - Structs only.
  - Concrete fields only (no typevars).
  - Same-module composition.
  - No variants.

### Tests

**Positive (driver):**
  1. `Holder { handle: ConstArc<String> }` — `holder.const_share()`
     compiles AND returns a fresh handle.
  2. `Mixed { handle: ConstArc<Int>, tag: Int, name: String }` —
     mixed Copy+Frozen + ConstShare fields; auto-derives.
  3. Nested same-module: `Outer { inner: Inner }` where Inner
     also auto-derives.  Both work; `outer.const_share()` produces
     a fresh Outer with a freshly-shared Inner.
  4. Generic `dup<T>(x: &T) nothrow -> T require T is shareable.ConstShare`
     accepts an auto-derived struct.

**Negative (driver):**
  5. `Bad { handle: ConstArc<Int>, lock: Mutex<Int> }` —
     Mutex blocks.
  6. `Bad { handle: ConstArc<Int>, items: Array<Int> }` —
     Array blocks.
  7. `Bad { handle: ConstArc<Int>, m: HashMap<String, Int> }` —
     HashMap blocks.
  8. `Bad { handle: ConstArc<Int>, r: &Int }` — & blocks.
  9. `Bad { handle: ConstArc<Int>, r: &mut Int }` — &mut blocks.
  10. Direct user `implement ConstShare for Holder { ... }` —
      rejected with `E_CONST_SHARE_USER_IMPL_REJECTED`
      (existing gate; verify synthesis doesn't produce a
      conflict).
  11. **Visibility regression pin:** a struct in module A whose
      field type's `ConstShare` impl is declared ONLY in a
      module B that is NOT imported by A — auto-derive must
      NOT fire (the visible-world rule blocks it).  This is the
      load-bearing test for Correction 1; deserves explicit
      coverage from day 1.
  12. **Forward pin:** the Phase-1-deferred `test_user_struct_does_not_prove_const_share_yet`
      from `test_const_share_substrate.py` — flip the assertion
      from `rc != 0` to `rc == 0` once Phase 1 lands; verify
      the test now compiles and the synthesized impl resolves.

**Memcheck:**
  13. Auto-derived struct with `ConstArc<String>` field —
      construct + clone via `const_share()` + drop both —
      no leaks.
  14. Auto-derived struct with mixed `ConstArc<String>` + `String`
      + `Int` fields — same lifecycle.  Tests that the
      Copy+Frozen path for `String` and `Int` doesn't
      leak/imbalance under refcount when combined with the
      ConstShare path for `ConstArc<String>`.
  15. Nested auto-derive — `Outer.const_share()` lifecycle.

### Tests OUT OF SCOPE for Phase 1 (explicit, kept as forward pins)

  - Cross-module composition (Phase 2).  Pin: `Holder` in
    module A with field of type `b.Inner` from module B —
    must NOT auto-derive in Phase 1 (since same-module-only).
    Test should FAIL in Phase 1, PASS in Phase 2.
  - Generic structs (Phase 3).  Pin: `Box<T> require T is
    shareable.ConstShare { value: T }` — must NOT auto-derive
    in Phase 1.
  - Variants (Phase 4).  Pin: `Tag::Wrapped(handle: ConstArc<String>)`
    — must NOT auto-derive in Phase 1.
  - Implicit `var b = a` duplication on auto-derived structs
    (separate later milestone — see `post-link-synthesis-plan.md` §8).

---

## Outstanding sub-deliverables before Phase 1 coding

  1. **Snapshot-timing resolution** (§"Resolution: synthesis
     runs INSIDE `compile_stubbed_funcs`" above) — concrete
     refactor proposal for how `_build_linked_world` is lifted
     ahead of the L9666 snapshot, and how
     `compile_stubbed_funcs` accepts a prebuilt linked_world.
     If this is too invasive, accept Phase 1 limitation:
     emit-package mode synthesis deferred.
  2. **Audit residue verification** — a `grep` confirming no
     post-lower_program code reads `prog.implements` outside
     the parser-stage.
  3. **`compile_stubbed_funcs` callsite enumeration** — the
     helper is called from multiple sites (main path, package
     path, possibly test paths).  Confirm all callsites are
     compatible with the synthesis insertion point.
  4. **Phase 1 test #11 (visibility regression)** —  exact
     scenario design.  This is the load-bearing test for
     Correction 1; if visible_world() doesn't actually block
     non-imported impls, the test fails and we discover the
     visibility rule needs more work.

---

## Decision points awaiting user

  1. **Approve visible-world correction** as embodied in
     `_proof_world_for_module(def_module)` and
     `ConstShareSynthesizer._proof_world_for(def_module)`?
  2. **Approve dropping `prog.implements` (row 12) update**
     from the synthesis helper, given the audit conclusion?
  3. **Approve deferring the snapshot-timing refactor to a
     dedicated sub-plan**, OR proceed to that sub-plan now as
     part of Phase 1 prep?
  4. **Approve Phase 1 test list** (15 tests above) as the
     gate for Phase 1 landing?
  5. **Approve the four outstanding sub-deliverables above**
     as the next concrete deliverables before Phase 1 coding?

Holding at clean Path A.  No code changes.
