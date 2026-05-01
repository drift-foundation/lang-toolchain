# Post-link ConstShare synthesis — mandatory design (2026-05-01)

**Status:** design only.  No code written.  Addresses the four
mandatory design items the user named as Phase 1 prerequisites:

  1. Snapshot-timing sub-plan
  2. `compile_stubbed_funcs` callsite enumeration
  3. Final helper signature after snapshot decision
  4. Fixed-point registration / ordering plan
  5. Precise visibility regression test design

Plus the audit-residue grep confirming no post-lower_program
consumer of `prog.implements`.

---

## 1. Snapshot-timing sub-plan

### The problem (recap)

In emit-package mode, `_pre_typecheck_hirs` is captured at
`driftc.py:9666` as a deepcopy of `normalized_hirs_by_id`.
Package emission (`L10777` → `per_module_hir`) reads from this
snapshot.  If synthesis adds HIR to `normalized_hirs_by_id`
AFTER L9666, the synthesized methods are excluded from package
serialization → consumers of the .dmp don't see them.

`_build_linked_world` (which materializes the LinkedWorld
synthesis depends on) is currently called at L10242 in main()
— AFTER the L9666 snapshot.  So synthesis cannot insert before
the snapshot under the current ordering.

### Proposed refactor (bounded)

**Move `_build_linked_world` from L10242 to BEFORE L9666.**

Verified this is safe by inspection:

  - `_build_linked_world(semantic_world.type_table)` reads
    only `type_table.trait_worlds` (per
    `_build_linked_world` at L848).
  - `semantic_world` is constructed at L8580 — well before
    L9666.
  - Between L9666 and L10242, ONLY one read of
    `trait_worlds` happens
    (`global_trait_index = GlobalTraitIndex.from_trait_worlds(...)`
    at L10120-area) — read-only, no mutation.
  - `_install_*_query` callbacks (L853-856) capture
    `linked_world.global_world` by reference, so they see any
    later mutations.

### Refactor steps

  1. Move `linked_world, require_env = _build_linked_world(semantic_world.type_table)`
     from L10242 to between L9650 (`normalize_hir`) and L9666
     (snapshot).
  2. Run synthesis between linked_world build and snapshot
     (synthesis updates `normalized_hirs_by_id` so the snapshot
     captures synthesized HIR).
  3. Delete the duplicate `_build_linked_world` call at L10494
     (the second one for trait require enforcement) — replace
     with the in-scope `linked_world` variable.

### Side concerns to verify (sub-deliverable)

  - **`_install_destructor_fns` (L10503):** runs after the
    L10494 `_build_linked_world` call.  Confirm it works the
    same when called against the earlier-built linked_world.
    Likely yes — it just reads from world.impls.
  - **`type_checker = TypeChecker(...)` (L9810):** constructs
    the TypeChecker after the snapshot but before the L10242
    linked_world.  Currently uses `semantic_world.type_table`.
    Move-friendly: TypeChecker doesn't take linked_world at
    construction; it receives it as a check_function argument.
  - **`global_impl_index = GlobalImplIndex.from_module_exports(...)`
    (L10120-area):** built BEFORE linked_world in current order.
    After move, will be built AFTER linked_world.  Already
    independent — `from_module_exports` doesn't take linked_world.
    Compatibility OK.

### Reuse pattern for `compile_stubbed_funcs`

`compile_stubbed_funcs` already accepts a prebuilt linked_world
via the `pass1_state` parameter (`Pass1State.linked_world` at
L3250).  Same mechanism we'll use for the moved-earlier build:
  - Build linked_world early in main().
  - Run synthesis.
  - Pack into `Pass1State`.
  - Pass to `compile_stubbed_funcs(pass1_state=...)` at L11349.
  - Inside `compile_stubbed_funcs`, the `if pass1_state is not
    None: linked_world = pass1_state.linked_world` branch
    (L3249-3250) skips the redundant rebuild.

For emit-package mode at L10673, similar: pass the prebuilt
linked_world via Pass1State.

### Stop conditions for the refactor

  - Moving `_build_linked_world` breaks any callsite that
    relies on it being late.  Mitigation: trace every reader
    of `linked_world` between L9650 and L10242 and verify
    they're move-compatible.
  - The duplicate `_build_linked_world` at L10494 turns out to
    have a non-obvious reason for the rebuild (e.g., something
    between L10242 and L10494 mutates `trait_worlds`).
    Mitigation: grep for `trait_worlds` writes in that range.
    From earlier instrumentation: zero writes confirmed.
  - `pass1_state.linked_world` reuse misbehaves for the
    emit-package call.  Mitigation: end-to-end test:
    construct a minimal struct that auto-derives, emit-package
    with synthesis enabled, link-and-load the .dmp, verify the
    consumer sees the synthesized impl.

### Tests proving synthesis flows into .dmp and back

  - **Producer-side**: emit-package on a module containing
    `Holder { handle: ConstArc<String> }`.  Open the .dmp
    payload (or use a debug dump) and confirm
    `impl_headers` contains the synthesized impl AND
    `hir_funcs` contains `Holder::ConstShare::const_share`.
  - **Consumer-side**: a separate module imports the .dmp,
    instantiates `Holder`, calls `holder.const_share()`,
    drops both — compiles and runs cleanly.
  - **Memcheck**: same consumer-side scenario under valgrind.
    No leaks.

---

## 2. `compile_stubbed_funcs` callsite enumeration

| Site | Caller | Purpose | Compatibility |
|---|---|---|---|
| L7335 | `compile_to_llvm_ir_for_tests` (test helper) | Test path; minimal driver-state | Pass `pass1_state` if test wants synthesis; otherwise irrelevant |
| L10673 | main() emit-package path | Build .dmp via package signatures | Must pass prebuilt linked_world via `pass1_state` |
| L11326 | main() source-build path | Build executable from source | Already passes `pass1_state=_p1_state` |
| L11614 | main() instantiation-index re-run | Re-emit instantiation index | Optional — if synthesis is irrelevant for instantiation index, bypass |

**Conclusion:** L10673 and L11326 are the two call sites that
matter for synthesis.  Both can take the prebuilt linked_world
via `pass1_state`.  L11326 already does; L10673 needs to be
updated.

L7335 (test path) and L11614 (instantiation re-run) are
peripheral; they can stay as-is.

---

## 3. Final helper signature (post-snapshot decision)

Unchanged from the audit's recommendation, with the
linked_world ordering established:

```python
def register_synthesized_const_share_impl(
    *,
    target_struct_def: parser_ast.StructDef,
    target_type_id: TypeId,
    target_module_id: str,
    method_signature: FnSignature,
    method_hir: H.HBlock,
    fn_id: FunctionId,
    linked_world: LinkedWorld,                   # prebuilt at the new earlier point
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
    """
    SOLE entry point for multi-table synthesized-impl
    registration.  Atomic: either every table updates or none
    (raises before any partial mutation).

    Tables updated:
      1. signatures_by_id[fn_id] = method_signature
      2. normalized_hirs_by_id[fn_id] = method_hir
      3. func_hirs_by_id[fn_id] = method_hir            (defensive sync)
      4. fn_ids_by_name[function_symbol(fn_id)].append(fn_id)
      5. linked_world.global_world.impls += [ImplDef(...)] + 3 indices
      6. linked_world.trait_worlds[target_module_id].impls += same
      7. ImplMeta added to module_exports[target_module_id]["impls"]
      8. impl_index.add_impl(impl=meta, type_table=..., module_ids=...)

    NOT updated:
      - prog.implements per module                       (audit-confirmed unused)

    NOT mutated:
      - type_table._copy_cache_*
      - prove_is per-call _cache
      - impl_index._by_target_method (use add_impl)
      - non-def-module trait_worlds[mid]
    """
    ...
```

---

## 4. Fixed-point registration / order plan

### Per-iteration registration via the single helper

**Replaces the deliverables-doc sketch's "register all at end"
approach.**

```python
def discover_and_register_phase1(
    *,
    all_struct_defs,                         # iterable of (StructDef, def_module)
    register_helper,                         # the single helper above
    synthesizer,                             # ConstShareSynthesizer
):
    """Per-iteration registration: as soon as a struct's
    qualification is proved, synthesize its impl and register
    it via the single helper.  Subsequent iterations
    re-query visible_world for OTHER candidates and pick up
    the just-registered impl naturally.

    This is the SAME real-impl path method resolution will
    use.  No virtual / phantom proof model.
    """
    derived: set[TypeId] = set()
    candidates = list(all_struct_defs)
    changed = True
    iteration = 0
    while changed:
        iteration += 1
        if iteration > MAX_FIXEDPOINT_ITERS:  # e.g. 100; fail-fast guard
            raise AssertionError(
                "ConstShare structural synthesis exceeded fixed-point bound; "
                "cycle in candidate dependencies?"
            )
        changed = False
        for struct_def, def_module in candidates:
            target_type_id = synthesizer.target_type_id(struct_def, def_module)
            if target_type_id in derived:
                continue
            qual_result = synthesizer.qualify(struct_def, def_module)
            if qual_result is None:
                continue  # blocked or unknown — try later if changed
            field_paths = qual_result
            synth = synthesizer.synthesize(struct_def, def_module, field_paths)
            register_helper(
                target_struct_def=struct_def,
                target_type_id=target_type_id,
                target_module_id=def_module,
                method_signature=synth.signature,
                method_hir=synth.hir,
                fn_id=synth.fn_id,
                # ... pass driver state through
            )
            derived.add(target_type_id)
            changed = True
    return derived
```

### Why per-iteration registration is correct

  - **`synthesizer.qualify`** uses `linked_world.visible_world(M)`
    (the visibility-aware proof world) for the type-defining
    module.  Each call sees the CURRENT state of LinkedWorld.
  - When `register_helper` registers a new impl for `Inner`,
    the registration mutates BOTH `linked_world.global_world`
    AND `linked_world.trait_worlds[Inner_module]`.
  - On the NEXT iteration, when we try to qualify `Outer`
    (which has an `Inner` field), `linked_world.visible_world(Outer_module)`
    re-merges trait_worlds — including `Inner_module`'s — and
    the new Inner impl is in the result.  `prove_is(Inner, ConstShare)`
    returns PROVED.
  - Termination: each iteration either adds at least one new
    derived type or makes no progress (changed=False).  Finite
    candidates → bounded iterations.

### Cycle handling

A cyclic candidate (`Holder { next: ConstArc<Holder> }`) blocks
its own qualification via the prover's `_in_progress` set —
returns UNKNOWN on recursion, which `qualify` treats as
non-qualifying.  The candidate stays out of `derived`, the
fixed-point eventually stops with no progress.

### Termination guard

Hard cap on iteration count (e.g. 100).  In practice each
iteration adds at least one impl or none, so MAX_FIXEDPOINT_ITERS
== count(candidates) is sufficient.  Assertion-on-overrun is
defensive against bugs in the qualifier (e.g., a qualifier
that returns None even after registration would loop forever
without the guard).

---

## 5. Precise visibility regression test design

### The problem with the original sketch

The deliverables doc test #11 — "module A doesn't import
module B; B has an impl A's struct field needs" — is hard to
construct because the field type usually has to be NAMED in A,
which forces an import chain that gives A access to the impl
through transitive re-exports.

### Refined test scenarios

**Scenario A — visibility unit test (recommended primary):**

Direct unit test of `ConstShareSynthesizer._proof_world_for(M)`
with a constructed `visible_module_names_by_name` that
explicitly excludes a module containing the impl.

  - Build a minimal `LinkedWorld` with two trait_worlds:
    - `mod_a`: contains a struct `S` with a single field of
      type `T`.  T is concrete and external.
    - `mod_b`: contains `implement ConstShare for T { ... }`.
  - Construct `visible_module_names_by_name = {"mod_a": {"mod_a"}}`
    (deliberately excluding `mod_b`).
  - Call `synthesizer.qualify(S, "mod_a")`.
  - Assert: returns None (does NOT auto-derive) — the impl in
    `mod_b` is invisible from `mod_a`'s view.
  - Compare control: with `visible_module_names_by_name = {"mod_a": {"mod_a", "mod_b"}}`,
    the same call should return a valid field-paths list (the
    impl IS visible).

**Scenario B — end-to-end test (secondary, for confidence):**

A real Drift compile scenario that exercises the visibility
rule.  Concrete construction:

  - `mod_x` (stdlib-shaped): defines `pub struct Carrier { ... }`
    (concrete fields, would auto-derive ConstShare).
  - `mod_x` ALSO contains a `mod_x_constshare_impl_for_carrier`
    block — for the test, INTERNAL to mod_x (so visible from
    mod_x's perspective).
  - `mod_y`: imports `mod_x` (via re-export of Carrier);
    declares `pub struct Holder { c: Carrier }`.
  - Visibility check: in `mod_y`'s visible world, does the
    Carrier impl flow through?  YES (because mod_y imports
    mod_x).  So Holder auto-derives.

That's the positive case.  For the negative case, I'd need
mod_y NOT to import mod_x — but then Carrier can't be named
in mod_y, the struct doesn't compile.  This proves the
scenario's edge case is genuinely unconstructable as
end-to-end Drift code.

**Conclusion:** the visibility rule's load-bearing nature is
correctly tested by **Scenario A (unit test)**, not by an
end-to-end driver test.  Driver tests for Phase 1 cover the
positive path (mod_y imports mod_x, Holder derives) but
cannot construct the negative path without contortions.

### Revised test #11

Replace the original "test #11 visibility regression" with:

  - **Test #11a (unit):** `ConstShareSynthesizer._proof_world_for`
    + qualifier returns None when the impl-providing module
    is excluded from `visible_module_names_by_name`.
  - **Test #11b (driver):** positive path — `mod_y` imports
    `mod_x` and inherits Carrier's auto-derive transitively.
    This proves the visibility rule allows reachable impls.

Drop the original "doesn't auto-derive when impl invisible"
driver test — confirmed unconstructable in pure Drift source.

---

## Audit residue — `prog.implements` post-lower_program consumers

Grep results:

```
$ grep -rn "prog.implements\|\.implements\b" lang/driftc/ \
    | grep -v __pycache | grep -v "tests/" | grep -v "\.pyc"
lang/driftc/parser/__init__.py:...           # parser stage; before synthesis
lang/driftc/packages/provider_v0.py:...     # consumer-side .dmp load
lang/driftc/packages/dmir_pkg_v0.py:...     # provider/consumer interfaces
```

**Conclusion:** no driver-level post-lower_program code reads
`prog.implements`.  All references are either:
  - parser-stage (BEFORE synthesis runs);
  - .dmp consumer-side (operates on `payload["impl_headers"]`,
    not parser AST `prog.implements`).

**Confirmed:** synthesis can SKIP the `prog.implements` update.
Row 12 of the deliverables-doc table stays officially
**skipped**.

---

## Summary of decisions awaiting user

  1. **Approve the snapshot-timing refactor** (move
     `_build_linked_world` to before L9666, remove duplicate
     L10494 call, reuse via Pass1State)?
  2. **Approve the per-iteration registration via the single
     helper** for fixed-point composition?
  3. **Approve the test #11 split** (unit Scenario A primary
     + driver positive Scenario B)?
  4. **Approve final helper signature** as written in §3?
  5. **Greenlight Phase 1 implementation** after these are
     approved?

Holding at clean Path A.
