# Cross-Package Defect Synthesis Report

**Date**: 2026-03-07
**Scope**: K10–K40 + ext-e2e-report (559 test cases through signed-package consumer path)
**Current state**: 545/558 pass (97.5%), remaining: 10 compile-check + 0 compile-codegen + 0 link + 4 runtime (2 newly exposed, 2 K42-@test_build_only)

---

## 1. Defect Map (K10–K21)

| Bug | Subsystem | Trigger shape | Root cause category | Package-only? |
|-----|-----------|---------------|---------------------|---------------|
| K10 | parser | Module-qualified struct ctor from package module | Parser-time nominal gate too early; no package symbol visibility | Yes |
| K11 | type-link | Variant tombstone metadata lost during package type-table linking | Missing field preservation in `type_table_link_v0.py` | Yes |
| K12 | type-link | Generic variant ctor with unresolved FORWARD_NOMINAL | Signature canonicalization incomplete post-link | Yes |
| K13 | checker | Boundary wrapper nothrow semantic analysis | Wrapper `declared_can_throw=True` but checker applying nothrow rules to wrapped callee | Yes |
| K15 | test infra | No existing e2e coverage for package consumer path | (test gap, not a code bug) | N/A |
| K16 | codegen | Package-consumer wrapper emission; entry main wiring | Wrapper MIR not emitted; entry `main::main` not wired through to codegen | Yes |
| K17 | codegen | Wrapper/entry completeness for all exported fns | Incomplete reachability in wrapper emission loop | Yes |
| K18 | codegen | Package preamble reachability | Preamble functions (ARC helpers, string ops) not emitted when only reachable through package code | Yes |
| K19 | MIR/ownership | `insert_string_arc` applied twice on package MIR | `compile_stubbed_funcs` already applies it internally; host re-applied it on deserialized MIR → double-drop | Yes |
| K20 | checker | `Array<Wrapper>::with_capacity` resolves to monomorphized `Array<CharRange>::with_capacity` | Package exports both generic + mono signatures; iteration order picks mono first; no `impl_subst` → wrong return type | Yes |
| K21 | codegen | `string_from_utf8_bytes` intrinsic in package path | Intrinsic handler hardcodes `%DriftString` return; package boundary wrappers set `can_throw=True` → FnResult expected | Yes |
| K22* | checker | Wrapper method duplication in callable_registry | Both original and `__wrap_method::` registered as candidates → "ambiguous method" | Yes |
| K23* | checker/codegen | Signature collision on re-instantiation | Package mono instances collide with host re-instantiation (same fn_id hash, different TypeIDs) | Yes |

*K22/K23 are newly identified and fixed in this session.

| K24 | checker/normalize | Wrapper method trait scope visibility | `HMethodCall` missing `origin` field — wrapper calls lose `"wrapper_call"` tag during `normalize_hir` rewriting; trait scope check fails for wrapper module | Yes |
| K28 | codegen/BFS | FnPtrConst/ConstructIface-referenced functions not in BFS reachable set | `_called_funcs_in_mir` only followed `Call` edges; missed `FnPtrConst` and `ConstructIface.fn_ref` → function bodies pruned → 21 link failures | Yes |
| K29 | codegen/BFS | ConstructIfaceValue impl methods not seeded into BFS | `ConstructIfaceValue` has no `fn_ref`; vtable thunks referencing impl methods are generated at codegen time; impl method bodies BFS-pruned → 4 interface link failures | Yes |
| K30 | checker/driver | Post-checker `can_throw` fixup + nothrow inference doesn't account for package ABI | (a) fixup OR logic can't correct True→False → ABI mismatch → SIGABRT in HashMap/HashSet/JSON/CLI tests (+54 tests); (b) `_call_may_throw` used `FnSignature.declared_can_throw` which is always True for non-nothrow ABI; fixed to use `inferred_may_throw` (+3 tests); (c) inline lambda HCall bodies walked instead of trusting ABI-level `can_throw` (+1 test); (d) TRAIT calls check trait method `declared_can_throw` (+1 test) | Yes |
| K31 | codegen/entry | Package-consumer `_emit_codegen` always uses `emit_entry_wrapper` (no-arg) | For `fn main(argv: Array<String>)`, needs `emit_argv_entry_wrapper` to build argv from C argc/argv; without it, `drift_main` receives garbage argv → SIGSEGV in all CLI tests | Yes |
| K25 | checker/driver | External module trait scope + module visibility | DMIR doesn't serialize `trait_scope` or module import graph; `compile_stubbed_funcs` builds empty `trait_scope_by_module` and `visible_module_names_by_name` for external modules → inherent methods like `HashMap::iter` invisible during generic re-instantiation | Yes |
| K26 | DMIR/codegen | Interface impl vtable not populated for external packages | Same-module trait impls (e.g., `Sink for StdErrSink` in std.log) lose trait identity during DMIR serialization because `impl_trait_key` is None when interface is already in type table; `_build_interface_impl_index` finds zero entries; vtable emission crashes with "interface impl not found" | Yes |
| K27 | codegen | Intrinsic `can_throw` not handled for VT/IO/net/runtime intrinsics | Boundary wrappers set `can_throw=True` on calls to nothrow intrinsics; 40+ intrinsic handlers in `_lower_call` only emitted raw results (e.g., `value_types[dest] = DRIFT_INT_TYPE`) without wrapping in FnResult; downstream `ResultOk`/`ResultIsErr` extraction failed with "unknown FnResult layout for drift.int" | Yes |
| K32 | type-link | Type alias ordering in `type_table_link_v0.py` | Type aliases (`pub type HashMap<K,V> = HashMapCore<K,V,DefaultBuildHasher>`) imported AFTER struct field finalization; `_eval_generic_type_expr` couldn't resolve alias-based fields → TypeId 1 (UNKNOWN) in codegen → 3 JSON tests crashing | Yes |
| K33 | checker/enforce | Trait requirement enforcement fails for package functions | Two bugs: (a) `enforce_fn_requires` passed `signatures_by_id` (local only) instead of `signatures_by_id_all` → package function signatures invisible → type param binding fails; (b) `subst` only included subject type params (e.g., `R`) but not trait argument type params (e.g., `T` in `require R is Permutable<T>`) → unresolved type params in proof → 4 sort_in_place + other trait requirement failures | Yes |
| K34 | parser/type-resolve | Package exception types resolved as FORWARD_NOMINAL in consumer function signatures | Package exception schemas loaded into `type_table.exception_schemas` at `import_type_tables_and_build_typeid_maps` (line 7031), but `parse_drift_workspace_to_hir` runs at line 6825 — before package type table linking. Consumer functions with exception params (e.g., `fn code_from_error(e: err.ResultError)`) resolve `ResultError` as FORWARD_NOMINAL instead of Error → `attrs access is only supported on Error values` + downstream cascade. Fix: extract exception schemas from package payloads pre-parse and pass via `external_exception_schemas` parameter | Yes |
| K35 | MIR/codegen | Package exception catch handlers use wrong event codes | `exception_catalog` only populated from locally-parsed source modules in `parse_drift_workspace_to_hir`. Package exceptions (e.g., `std.err:IteratorInvalidated`) get event code 0 instead of their xxHash64-based code. Throw site in package code uses correct code; catch handler in consumer uses 0 → typed catch never matches → falls through to wildcard. Fix: populate `exception_catalog` with `event_code(fqn)` for all package exception schemas after type table linking | Yes |

| K36 | checker/visibility | Package module methods not visible to consumer code | `visible_modules_by_name` BFS only traverses source module deps and reexports. Package modules (e.g., `std.core.copy` defining `String::byte_length`) not included → `_candidate_visible` returns False → "method exists but is not visible here". Fix: add all `external_module_packages` keys to the `visible` set during BFS construction. Also fixes `HashMap::get` visibility in JSON tests | Yes |
| K37 | checker/call_resolver | FORWARD_NOMINAL canonicalization for generic package types | Four interlocking bugs: (a) `receiver_nominal` in call_resolver.py stayed FORWARD_NOMINAL after `_struct_base_and_args` → method candidates filtered out; (b) `ImplDef.type_params` empty at two creation sites → trait solver skipped generic impls (Copy, etc.); (c) `check_call_signature` failed FORWARD_NOMINAL vs canonical TypeId comparison for struct constructors; (d) `_canonicalize_struct_field_type_ids` iterated wrong data structures (dict vs list). Fix: canonicalize receiver at resolution, propagate type_params, add FORWARD_NOMINAL equivalence in check_call_signature, rewrite struct field canonicalization for `TypeDef.param_types` (list) and `StructInstance.field_types` (list) | Yes |
| K38 | core/types_core | Primitive types (RAW_PTR, SCALAR, etc.) unresolvable for Copy status | `copy_status` with `_copy_query` hook returned None for primitives when no Copy trait impl existed in trait world. `_eligible_structural_fallback` only allowed STRUCT/VARIANT. Primitive types have deterministic Copy status from their kind — RAW_PTR is always Copy, VOID is always Copy, etc. Fix: add structural fallback bypass for primitive type kinds before STRUCT/VARIANT check | Yes |

| K39 | codegen/BFS | Destructible::destroy not emitted for types nested in variant payloads or package-only functions | Three interlocking issues: (a) `external_impl_metas` not scanned for Destructible impls (only `module_exports`); (b) BFS Phase 2 type graph walk only followed struct fields via `get_struct_instance`, not variant arm payloads via `get_variant_instance` — `HashMapCore<String, JsonNode, DefaultBuildHasher>` inside `JsonNode::Object` was invisible; (c) Phase 2+3 not interleaved — types discovered in destroy function bodies (via DropValue) couldn't feed back into type graph walk; (d) package-side BFS had no type graph walk at all — types only in package function params/locals (e.g., `HashMapCore<String, Int, DefaultBuildHasher>` in `std.log::_emit`) missed. Fix: add variant traversal, merge Phase 2+3 into fixpoint loop, mirror type graph walk in package BFS. **+63 tests** (all JSON/HashMap link failures). **(e)** K39-e: type graph walk missed ARRAY/OPTIONAL/RESULT element types — `td.param_types` not traversed, so `FutureGroup<Int>` → `Array<Future<Int>>` → `Future<Int>` → `VirtualThread<Int>` chain was broken at the Array boundary. Also: generic destroy instantiations (from CSF) in `src_mir` not discoverable by package-side `_seed_destroy_type_graph` which used `pkg_mir_all` only. Fix: walk `td.param_types` in type graph; use combined `pkg_mir_all + src_mir_full` pool for package-side seeding. **+2 tests** (concurrent_future_group, concurrent_stress_join_all) | Yes |
| K40 | codegen/BFS | Preamble functions (`install_process_preamble`) not emitted in package-consumer path | Preamble functions are injected by codegen into entry wrappers at LLVM emission time, not called from MIR. BFS from user code never discovers them. Fix: explicitly seed `ENTRY_WRAPPER_IMPLICIT_DEPS` into `pkg_needed` with transitive closure walk. **+2 tests** (preamble runtime failures) | Yes |
| K41 | type_checker | Lambda nothrow analysis resolves boundary wrapper instead of original method | `_lambda_can_throw` / `_treat_can_throw` checks `declared_can_throw` on the resolved target. In package path, auto-borrow methods (e.g. `Arc::borrow_mut`) resolve to `__wrap_method::` wrapper which has `declared_can_throw=True`. Fix: when target is a wrapper (`wraps_target_fn_id` set), check the wrapped function's `declared_can_throw` instead. **+1 test** (`callback_move_capture_nested_callback`). Three other lambda tests now pass initial type-check but fail in `compile_stubbed_funcs` with a different bug (K42). | Yes |
| K42 | driftc/compile_stubbed_funcs | `conc.lock(arc)` trait auto-borrow fails in compile_stubbed_funcs | `compile_stubbed_funcs` builds its own callable_registry and trait world. `BorrowMut<Mutex<T>> for Arc<Mutex<T>>` auto-borrow doesn't resolve for free function arg coercion, causing "no matching overload for function 'lock'" in the MIR compilation pass. Initial type-check (line 8296) passes; only the second type-check inside compile_stubbed_funcs fails. Affects: callback_move_capture_{arc_lifetime,replace_state}, effective_drift_emitter_example, callback_arc_mutex_full_mutation (**4 tests**) | Yes |
| K27+ | codegen/intrinsic | array_byte intrinsic FnResult wrapping | `array_byte_alloc_uninit`, `array_byte_as_mut_ptr`, `array_byte_commit_init_len` intrinsics didn't wrap results in FnResult when `can_throw=True` from boundary wrappers. Fix: add `if instr.can_throw:` + `_wrap_ok_fnresult` to all 3 handlers. Also fixed LLVM type name sanitization (`*` → `Ptr` in ok_key). **+7 tests** (all array_byte codegen crashes resolved). Exposed 1 pre-existing issue: `array_byte_alloc_uninit_requires_unsafe` now fails as "expected compile failure but got success" because `compile_stubbed_funcs` defaults `allow_unsafe=True` — another K42-class symptom. | Yes |

**All bugs are package-only.** The local/source compilation path is unaffected.

---

## 2. Recurring Pattern Analysis

### 2.1 Common architectural weaknesses

**A. Duplicated transform paths.** The package-consumer path (`if loaded_pkgs:` in `driftc.py`) rebuilds checker/codegen state from deserialized package data but doesn't share the same transform pipeline as local compilation. K19 (double `insert_string_arc`) is the canonical example: the local path runs it inside `compile_stubbed_funcs`, but the package path ran it again externally.

**B. Signature identity confusion.** Package export serializes BOTH generic templates AND monomorphized instances. The host-side import creates two paths:
- Path 1 (`_load_external_signatures`, ~line 6510): reconstructs TypeParam objects, `impl_type_params`, type expressions
- Path 2 (`_remap_external_mir_signatures`, ~line 8229): raw TypeId remapping only

When the checker iterates `signatures_by_id`, it sees both the generic and monomorphized forms. Without filtering, this causes:
- K20: wrong return type (mono picked before generic, no substitution)
- K22: ambiguous method (wrapper + original both visible)
- K23: signature collision (host re-instantiates what package already exported)

**C. Boundary wrapper contract is implicit.** The wrapper system (`_inject_method_boundary_wrappers` at line 620) generates `__wrap_method::` stubs for every nothrow public method. But:
- The checker registers both wrapper and original in `callable_registry` (K22)
- Codegen intrinsic handlers don't account for `can_throw=True` from wrappers (K21)
- The MIR/SSA pipeline doesn't validate that boundary-wrapped calls produce FnResult types

**D. TypeId remapping incompleteness.** `_remap_mir_func_typeids` was missing instruction types (fixed earlier in K14-era). But the remap still doesn't handle all codegen paths — `CastScalar` on unremapped type params (TypeId 1) hits 34 cases.

### 2.2 Implicit invariants that should be explicit

| Invariant | Currently | Should be |
|-----------|-----------|-----------|
| `insert_string_arc` applied exactly once per MIR function | Implicit (caller discipline) | Assert: MIR function has `_string_arc_applied` flag |
| Package mono signatures not used for type resolution | Implicit (iteration order) | Filter: skip `__inst__` fns from candidate collection |
| Wrapper sigs not in callable_registry | Implicit (registration loop) | Assert: no `is_wrapper` sigs in registry after build |
| Codegen intrinsics handle `can_throw` | Implicit (per-handler) | Validator: all Call instrs with `can_throw=True` produce FnResult-typed dest |
| TypeId remap covers all MIR instruction fields | Implicit (manual instruction list) | Validator: no raw package TypeIds survive into host MIR |
| Every `Destructible::destroy` call target has emitted IR | Implicit (reachability) | Assert: all `@"...::destroy"` references have definitions before clang |
| External module trait scope matches original source | **TEMPORARY**: all traits (K25) | Serialize exact `trait_scope` in DMIR; reconstruct on load |
| External module visibility matches original imports | **TEMPORARY**: all modules (K25) | Serialize module import graph in DMIR; reconstruct on load |

### 2.3 Duplicated logic between local and package paths

| Area | Local path | Package path | Divergence risk |
|------|-----------|--------------|-----------------|
| Signature resolution | `resolve_program_signatures` | `_load_external_signatures` + remap | Two different ways to build FnSignature |
| Method candidate registration | `callable_registry.register_inherent_method` loop (line 7133) | Same loop for external sigs (line 7202) | Wrapper/mono filtering differs |
| MIR transform | `compile_stubbed_funcs` applies all transforms | Host manually applies transforms on deserialized MIR | Double-application risk (K19) |
| Intrinsic lowering | `can_throw=False` always | `can_throw=True` via wrapper | Intrinsic handlers not wrapper-aware (K21) |
| Type registration | `type_table.define_*` during parsing | `type_table_link_v0` remaps package types | Field/metadata loss (K11, K12) |

---

## 3. "Step Back and Redo" Proposal

### Option A: Minimal refactor (incremental, low risk)

**Scope**: Targeted hardening of the three weakest joints without restructuring.

**Changes**:

1. **`driftc.py` — Unified MIR transform gate** (~50 lines)
   - Add `_mir_transforms_applied: set[FunctionId]` tracking set
   - `insert_string_arc` checks and records in this set
   - Assert at SSA entry: all MIR funcs are in the set
   - Prevents K19-class bugs

2. **`call_resolver.py` — Candidate filtering for package sigs** (~30 lines)
   - In `resolve_nonvariant_qualified_static_call`: skip `__inst__` fns when generic exists (current K20 fix, generalized)
   - In method candidate collection: add `is_wrapper` exclusion (current K22 fix)
   - In `_register_derived_signature_precheck`: skip collisions against base (current K23 fix)

3. **`llvm_codegen.py` — Intrinsic `can_throw` audit** (~100 lines)
   - Audit all intrinsic handlers in `_lower_instr` for `can_throw` awareness
   - Add FnResult wrapping for: `string_from_utf8_bytes` (done), `caller`, `time_now_ms`, `time_now_utc_ms`, and any other nothrow intrinsics
   - Add validator: after lowering, check that all `can_throw=True` Call dests have FnResult-typed values

4. **`type_table_link_v0.py` — Remap completeness validator** (~40 lines)
   - After remap: scan all MIR instructions for TypeIds < package_base_offset
   - Assert none survive (catches K14-class remap holes)

**Files**: `driftc.py`, `call_resolver.py`, `llvm_codegen.py`, `type_table_link_v0.py`
**Risk**: Low. Each change is isolated and backwards-compatible.
**Expected defect reduction**: Eliminates K19/K20/K21/K22/K23 classes. Catches future instances via assertions.
**Rollout**: Ship all 4 in one commit. No phasing needed.
**Rollback**: Revert commit.

### Option B: Structural refactor (larger change, higher payoff)

**Scope**: Unify the local and package compilation paths into a single pipeline.

**Core idea**: Instead of two separate codepaths (`if loaded_pkgs:` branch vs normal), make package import produce the same intermediate representation as local compilation, then run the same pipeline.

**Changes**:

1. **`driftc.py` — Single `CompilationUnit` abstraction** (~500 lines)
   - New `CompilationUnit` dataclass: `{signatures_by_id, mir_by_id, type_table, module_exports}`
   - Local compilation produces a `CompilationUnit` directly
   - Package import produces a `CompilationUnit` via deserialization + remap
   - Both merge into `ProgramUnit` which runs the shared pipeline
   - Eliminates: K19 (single transform path), K22/K23 (single registration path)

2. **`packages/type_table_link_v0.py` → `packages/compilation_unit_link.py`** (~300 lines)
   - Remap operates on `CompilationUnit` not raw type tables
   - Remap validates completeness as part of the link step
   - Eliminates: K11/K12/K14-class type-link bugs

3. **`checker/call_resolver.py` — Package-aware candidate registry** (~100 lines)
   - `CallableRegistry` gains `package_id` per entry
   - Dedup: for same (base_type, method_name), prefer highest-package-version entry
   - Wrapper entries tagged as `boundary_only` — invisible to user code
   - Eliminates: K20/K22 (clean candidate model)

4. **`codegen/llvm/llvm_codegen.py` — Boundary-aware intrinsic dispatch** (~150 lines)
   - Intrinsic lowering table: `{fn_id: (handler, supports_can_throw: bool)}`
   - If `can_throw` and `not supports_can_throw`: auto-wrap result in FnResult
   - Eliminates: K21-class bugs for all intrinsics at once

**Files**: `driftc.py` (major), `type_table_link_v0.py` (rename+rewrite), `call_resolver.py`, `llvm_codegen.py`, `callable_registry.py`
**Risk**: Medium-high. Touches the core compilation driver. Needs full e2e validation.
**Expected defect reduction**: Eliminates all K10-K23 classes structurally. New bugs in these areas become much harder to introduce.
**Rollout**:
  - Phase 1 (1 week): `CompilationUnit` abstraction + local path migration. All existing tests must pass.
  - Phase 2 (1 week): Package import path migration. ext-e2e-report as validation.
  - Phase 3 (3 days): Intrinsic dispatch table + candidate registry cleanup.
**Rollback**: Phase-level revert. Each phase is independently revertible.

---

## 4. Contract Hardening Plan

### 4.1 TypeId remap completeness

**Contract**: After `type_table_link_v0` remap, no MIR instruction in any package function may contain a TypeId from the package's pre-remap namespace.

**Check location**: `_remap_mir_func_typeids` exit — add a scan of all instruction fields. Assert all TypeIds are in host namespace.

**File**: `lang/driftc/packages/type_table_link_v0.py`, end of `remap_mir_functions`

### 4.2 Function reachability/emission completeness

**Contract**: Every function symbol referenced in emitted IR must have a definition (either emitted locally or declared as external).

**Check location**: After `lower_module_to_llvm` returns, before clang invocation — scan IR text for `@"...::"` references and verify each has a matching `define` or `declare`.

**File**: `lang/codegen/llvm/llvm_codegen.py`, in `lower_module_to_llvm` or in `pkg_consumer_runner.py` as a post-compile check.

### 4.3 Entry-wrapper implicit deps

**Contract**: When `_inject_method_boundary_wrappers` creates a wrapper for fn_id F, the wrapper's MIR must be emitted AND F's MIR must be emitted (wrapper calls F).

**Check location**: After `_inject_method_boundary_wrappers` returns — assert all `MethodWrapperSpec.target_fn_id` values are in the emitted MIR set.

**File**: `lang/driftc/driftc.py`, after line 668

### 4.4 Ownership/drop invariants after move through variant/match/push

**Contract**: After `insert_string_arc` and scope-drop injection, every MoveOut must have exactly one corresponding tombstone. No local may be dropped after MoveOut without re-initialization.

**Check location**: MIR validator pass (exists as `validate_mir_array_copy_invariants`; extend to cover move/tombstone pairs).

**File**: `lang/driftc/stage2/hir_to_mir.py`, extend `validate_mir_array_copy_invariants` or add `validate_mir_move_tombstone_pairs`

### 4.5 FnResult type consistency for `can_throw` calls

**Contract**: Every Call instruction with `can_throw=True` must produce a value whose LLVM type is a `%FnResult_*` type.

**Check location**: After `_lower_instr` for Call instructions — assert `self.value_types[dest].startswith("%FnResult_")` when `instr.can_throw`.

**File**: `lang/codegen/llvm/llvm_codegen.py`, after Call lowering

### 4.6 K25 broad fallback — TEMPORARY CONTRACT

**Status**: TEMPORARY. Must be replaced before DMIR v1 freeze.

**Code locations** (marked with `K25 TEMPORARY FALLBACK` comments):
- `driftc.py:3011` — trait_scope_by_module: all traits for external modules
- `driftc.py:3138` — visible_module_names_by_name: all modules for external modules

**What K25 does**: When `compile_stubbed_funcs` processes external (package) modules during consumer build, it gives them:
1. **All traits** in `trait_scope_by_module` (because DMIR doesn't serialize `trait_scope`)
2. **All modules** in `visible_module_names_by_name` (because DMIR doesn't serialize import graph)

**Why it's safe for now**: These broadened scopes only affect re-instantiation of generic templates inside package modules. User source code is unaffected — its visibility is still gated by its own imports and `use trait` declarations. Package module dependencies were validated at package build time, so re-granting them is semantically correct (just imprecise).

**Guard regressions** (in-tree):
- `pkg_vis_source_trait_scope_rejected` — source code without `use trait Iterable` still fails on `iter()`
- `pkg_vis_source_private_method_rejected` — source code still fails on `TreeMap.__test_validate()` (private)

**Concrete removal plan**:
1. Serialize exact `trait_scope` (list of TraitKey) per module in DMIR format (~30 lines in `provisional_dmir_v0.py`)
2. Serialize module dependency graph (dict of module→set[module]) in DMIR format (~15 lines in `provisional_dmir_v0.py`)
3. Deserialize and populate in `provider_v0.py` (~10 lines)
4. Use deserialized scopes in `compile_stubbed_funcs` instead of "all" fallback (~5 lines in `driftc.py`)
5. Remove `K25 TEMPORARY FALLBACK` code blocks
6. Verify guard regressions still pass (they should, since source visibility is unaffected)

---

## 5. Test Strategy Upgrades

### 5.1 Blocking tests (CI gate, day 1)

Current smoke set (7 cases) should expand to include:

| Case | Why blocking |
|------|-------------|
| Current 7 | K19/K20/K21 regressions |
| `destructible_array_element_drop` | Local struct destroy emission |
| `callable_callback_drop_in_array` | Lambda callback emission |
| `std_time_monotonic_smoke` | Runtime intrinsic declarations |
| `result_on_error_throw` | FnResult error path through callbacks |
| `std_crypto_sha1_stability_large_prealloc` | Signature collision regression |
| `treemap_entry_basic` | Container generic re-instantiation |

**Total blocking**: ~13 cases, ~30s runtime.

### 5.2 Nightly tests

- Full 556-case ext-e2e-report (report-only)
- ASAN variant of passing cases
- Cases using VT runtime (`concurrent_*`, `borrow_escape_thread_*`) — these need the VT executor which the package test harness doesn't set up

### 5.3 Keeping runtime manageable

- Fixture caching: build signed stdlib once per CI run, share across all test invocations
- Parallelism: 8-way ProcessPoolExecutor already configured
- Incremental: `--only-cases` for targeted regression checks on PRs
- Phase gating: if compile-check fails, skip link+run (already implemented)

---

## 6. Recommendation

### Immediate (this week)

1. **Ship current fixes** (K19/K20/K21/K22/K23) — the diff is 36 lines across 4 files
2. **Intrinsic `can_throw` audit** — fix `caller`, `time_now_ms`, `time_now_utc_ms` (3 more intrinsics, ~40 lines). This unblocks ~5 more cases.
3. **Local struct `Destructible::destroy` emission** — the link failure bucket (10 cases with `::destroy` undefined) is a reachability bug in the package preamble. Fix in codegen wrapper emission. Unblocks ~10 cases.
4. **Lambda callback emission** — `__lambda_cb_*` undefined (13 link failures). These are local lambdas whose bodies aren't emitted when compilation goes through package path. Fix in MIR collection. Unblocks ~13 cases.
5. **Expand blocking smoke set** to 13 cases.

### Medium term (next 1-2 weeks)

6. **Option A hardening** (MIR transform gate, remap validator, FnResult validator) — prevents regression in fixed areas
7. **`module declaration required` (28 cases)** — ✅ DONE. Added explicit `module m` to all 28 test files. **Decision (pinned)**: no runner-side injection of module declarations — package-consumer tests must reflect real source text exactly so line numbers, diagnostics, and compiler inputs stay aligned with what is on disk.
8. **`MemoryOrder` export (28 cases)** — `std.sync` re-exports `MemoryOrder` from `lang.atomic`. Package export doesn't preserve re-export chains. Fix in package serialization.
9. **HashMap/HashSet method resolution (31+5+4+3=43 cases)** — `insert`/`len`/`get` not found. Related to generic container methods not resolving through package path. Investigate whether this is a K20-like signature issue or a missing export.

### Defer

- Option B structural refactor — high value but too risky mid-sprint. Plan for after the ext-e2e pass rate exceeds 80%.
- VT-specific tests (`concurrent_*`) — need VT executor setup in test harness.
- `unsafe` block tests (9 cases) — need `--allow-unsafe` flag in runner for specific cases.
- `CastScalar` on generic types (8 cases) — deep codegen issue with unremapped type params.
- `unsupported param type id 1` (34 cases) — generic type param not remapped to concrete in package path. This is the single largest codegen bucket but requires Option B-level changes to fix properly.

### Expected trajectory

| Milestone | Pass rate | Cases passing |
|-----------|-----------|---------------|
| Current (with K19-K23 fixes) | 32% | 179/556 |
| After K24 | 66.8% | 372/557 |
| After K25 (ext module visibility) | 67.7% | 376/555 |
| After K26 (interface impl vtable) | 67.6% | 379/561 |
| After K27 (intrinsic can_throw audit, 40+ handlers) | 71.2% | 708/995 |
| After K30-K38 (checker/type-link/codegen fixes) | 76.9% | 430/559 |
| After K39+K40 (BFS destroy+preamble+variant walk) | 88.7% | 496/559 |
| After module-m + unsafe + dict-iter + K41 + K27+ | 96.4% | 538/558 |
| After K18 guard + bucket(a) test fixes + stdlib export | 97.5% | 545/558 |
| **Remaining 14 failures (=1 of which was in prior 19)** | | |
| **(a) Newly exposed runtime issues (2 — NOT package-specific):** | | |
| — `array_range_reserve_noop_invalidates`: reserve(n<=cap) still invalidates range | No | Stdlib behavior bug |
| — `deque_range_sort_binary_search_wrap`: binary_search returns None on sorted deque | Maybe | Investigate |
| **(b) K42 class — duplicate compile_stubbed_funcs state (10):** | | |
| — K42 lock/auto-borrow (4): trait auto-borrow diverges in second-pass callable_registry | Yes | Converge pipeline |
| — K42 unsafe default (1): compile_stubbed_funcs defaults allow_unsafe=True | Yes | Converge pipeline (see K43 below) |
| — MIR invariant (2): array copy invariant — Copy status diverges in second pass | Yes | Converge pipeline |
| — Package-path semantic (1): ambiguous trait req (hashmap_iter_empty) | Yes | Converge pipeline |
| — K42 @test_build_only visibility (2): __test_invalidate invisible in second pass | Yes | Converge pipeline |
| **(c) K42 class — generic variant inference (1):** | | |
| — result_generic_ok_copy_struct_string_match_return_no_leak | Yes | Converge pipeline |

---

## 7. Phase 1a Report (2026-03-07)

### Changes made
1. **`driftc.py` — `compile_stubbed_funcs` signature**: Added `skip_typecheck: bool = False` and `typed_fns_from_pass1: Mapping[FunctionId, object] | None = None` parameters (lines 2470-2472). Added conditional bypass logic at the type-check loop (lines 3329-3344): when both params provided, pre-populates `typed_fns_by_id` and `typecheck_ok_by_fn` from Pass 1 results.
2. **`driftc.py` — call site** (line 9086): Added `allow_unsafe=bool(getattr(args, "allow_unsafe", False))` to propagate CLI unsafe flag instead of defaulting to `True`.

### What was NOT activated
The `skip_typecheck=True` bypass is NOT passed at the call site. Attempting it caused 545→319 regression (226 test failures). Root cause: `type_checker.check_function()` accumulates critical side-effect state — `thunk_specs()`, `lambda_fn_specs()`, CallInfo — consumed downstream by generic instantiation (line 3993), lambda emission (line 5971), and thunk emission (line 5899). Skipping type-check deprives these downstream consumers of their input.

### K43: `declared_unsafe` missing from package function signature serialization
The K42 unsafe test (`array_byte_alloc_uninit_requires_unsafe`) cannot pass via `allow_unsafe` propagation alone. The unsafe marker IS serialized for interface method schemas (`provisional_dmir_v0.py:523`, `type_table_link_v0.py:357` — `is_unsafe` field). However, it is NOT serialized in `encode_signatures()`, which is the path that external function signatures rely on for package-consumer compilation. External function signatures loaded from packages never carry `declared_unsafe`, so neither Pass 1 nor Pass 2 can detect unsafe calls to package functions. This is a package signature serialization gap (K43), not a type-check pass issue.

### Score delta
- Before: 545/559 (97.5%)
- After: 545/559 (97.5%) — no change
- The `allow_unsafe` call-site fix is correct but has no observable effect because K43 blocks it

### Phase 1a acceptance vs plan
Plan acceptance was: "K42 unsafe test passes → total 539/558 (96.6%)". This cannot be met because:
1. K43 (declared_unsafe not in DMIR) blocks the unsafe test regardless of allow_unsafe propagation
2. skip_typecheck bypass breaks 226 tests due to type_checker side-effect dependency

### Revised Phase 1a/1b boundary
The original Phase 1a/1b split assumed typed_fns could be passed independently of callable_registry state. This is incorrect — the type_checker accumulates thunk/lambda specs during check_function that are consumed later. The correct split:

- **Phase 1a (done)**: Signature + bypass scaffolding in place; `allow_unsafe` propagation fix applied. No behavioral change.
- **Phase 1b (revised)**: Must pass the ENTIRE type_checker object (or its accumulated state: thunk_specs, lambda_fn_specs, CallInfo) from Pass 1 into `compile_stubbed_funcs`, not just typed_fns. Alternatively, share a single TypeChecker instance between Pass 1 and compile_stubbed_funcs.
- **K43 prerequisite**: Serialize `declared_unsafe` in DMIR before the unsafe test can pass.

---

## 8. Farm Regression Review (2026-03-07)

### Classification

| Test | Exit/Expected | Classification | Path | Root cause |
|------|--------------|---------------|------|------------|
| `array_range_reserve_noop_invalidates` | 10/0 | Pre-existing stdlib runtime bug | Both (local+pkg) | `reserve(n <= cap)` still invalidates iterator ranges even when capacity doesn't change. Local-path link+run confirms exit 10. Not related to package work. |
| `deque_range_sort_binary_search_wrap` | 2/0 | Pre-existing stdlib runtime bug | Both (local+pkg) | `binary_search` returns `None` on sorted deque with wraparound layout. Local-path link+run confirms exit 2. Not related to package work. |
| `pkg_vis_source_private_method_rejected` | missing diag | Package-runner-only test | Package-only | Test requires package-consumer context. On local path, parser fails on `c.TreeMap::new<Int, Int>()` (generic type param `<` ambiguity) before the `__test_validate` visibility check is reached. Passes on `pkg_consumer_runner.py`. Local runner cannot exercise the intended negative. Fix: mark package-runner-only. |
| `optional_on_none_try_block_no_catch_rejected` | 0/1 | **LANGUAGE_BUG** (nothrow analysis) | Both (local+pkg) | `try { ... }` without `catch` passes empty catches to `HTry`. MIR lowerer at `hir_to_mir.py:5704` inlines body without try wrapping. Throw analysis at `checker/__init__.py:1147` correctly sets `catch_all=False` for empty catches. However, the `throw` is inside a lambda arg to `on_none(| | => { throw ... })`, and the throw analysis doesn't walk lambda argument bodies — it relies on `on_none`'s `can_throw` from CallInfo. The nothrow inference fixpoint fails to propagate can-throw through `on_none` (which takes `CallbackThrow0<T>` and calls `f.call()` which is NOT nothrow). Verified on clean HEAD: same exit 0. |
| `result_on_error_try_block_no_catch_rejected` | 0/1 | **LANGUAGE_BUG** (nothrow analysis) | Both (local+pkg) | Same root cause as `optional_on_none_try_block_no_catch_rejected` — `Result::on_error` with throwing lambda in bare `try {}` block. Verified on clean HEAD: same exit 0. |

### Verdicts

1. **`array_range_reserve_noop_invalidates`** — Pre-existing stdlib runtime bug. Deferred until current structural phase checkpoint completes. Not blocking one-pipeline work.
2. **`deque_range_sort_binary_search_wrap`** — Pre-existing stdlib runtime bug. Deferred until current structural phase checkpoint completes. Not blocking one-pipeline work.
3. **`pkg_vis_source_private_method_rejected`** — Package-runner-only test. Passes on `pkg_consumer_runner.py`. Marked for local-runner exclusion. No compiler bug.
4. **`optional_on_none_try_block_no_catch_rejected`** — **LANGUAGE_BUG**. Subsystem: checker / nothrow analysis. Tests pinned as failing semantic regressions. Root cause investigation and fix required per LANGUAGE_BUG protocol.
5. **`result_on_error_try_block_no_catch_rejected`** — **LANGUAGE_BUG**. Same subsystem and root cause as #4.

### Actions

- **No compiler regression from recent package-path work.** All 5 failures reproduce on clean HEAD.
- Items 1-2: stdlib runtime bugs, deferred past structural phase checkpoint.
- Item 3: mark package-runner-only in expected.json.
- Items 4-5: LANGUAGE_BUG — **FIXED**.
  - **Root cause**: Nothrow fixpoint termination bug in `checker/__init__.py` (line 772). The fixpoint set `changed = True` only when `info.declared_can_throw` flipped from falsy to `True`. For functions like `on_none` whose `declared_can_throw` was already `True` (from the missing-annotation fallback at line 581), a change in `inferred_may_throw` from `False` to `True` did NOT trigger re-iteration. If `main` was processed before `on_none` in the first (and only) iteration, `on_none.inferred_may_throw` was still `False`, so `main` saw `call_can_throw = False` for the `on_none` call and concluded `may_throw = False`.
  - **Fix**: Track `inferred_may_throw` changes to drive the fixpoint: set `changed = True` whenever any function's `inferred_may_throw` flips from `False` to `True`, regardless of `declared_can_throw` status. This ensures callers get re-evaluated when a callee's inferred throw status changes.
  - **Verification**: Both `optional_on_none_try_block_no_catch_rejected` and `result_on_error_try_block_no_catch_rejected` now correctly reject with "declared nothrow but may throw". 10 additional nothrow-related e2e tests + 6 driver tests all pass.
  - **Consequence**: Nothrow fixpoint fix correctly exposed 3 tests calling `algo.binary_search`/`sort_in_place` from `nothrow main` without try/catch. These stdlib functions call trait methods (`compare_key`, `compare_at`, `swap`) that throw on bounds errors — they genuinely can throw. Fix: wrap algo calls in `try run() catch { 99 }` pattern. Tests fixed: `algo_binary_search_basic`, `algo_binary_search_duplicates`, `deque_range_sort_binary_search_wrap`.
  - **Post-fix score**: 990/995 e2e (3 skipped pkg-consumer-only, 2 pre-existing runtime bugs: `array_range_reserve_noop_invalidates` exit 10, `deque_range_sort_binary_search_wrap` exit 2). Driver: 716/716.

## 9. Converge-One-Pipeline Phase 1 Investigation (2026-03-07)

### Baseline frozen
- e2e: 990/995 (3 skipped pkg-consumer-only, 2 pre-existing runtime bugs)
- driver: 716/716
- stage1-4: 195/195 (excluding packages/traits tests with pre-existing failures)
- Deferred runtime bugs: `array_range_reserve_noop_invalidates`, `deque_range_sort_binary_search_wrap`

### Phase 1 blocking finding: CORRECTED — missing instantiation records, not type resolution

**Previous diagnosis was wrong.** The method resolution itself works correctly in both passes — `impl_subst` and `subst_for_receiver` properly substitute `T→Int`, producing concrete `&Wrapper<Int>` params in the CallInfo. Both passes resolve identically. Confirmed via runtime tracing: `has_tv=False`, `coerced_tv=False` in both Pass 1 and Pass 2.

**Actual root cause: missing `function_keys_by_fn_id` in Pass 1 type-check → no instantiation records.**

The flow:
1. When `resolve_method_call` upgrades to boundary wrapper (`__wrap_method::Wrapper<T>::get`), `record_instantiation` is called with `target_fn_id=wrapper_fn_id` and `impl_args=(Int,)`.
2. `record_instantiation` (`type_checker.py:4871`) needs `function_keys_by_fn_id.get(target_fn_id)` to get the FunctionKey. If the key is not found, the instantiation is **silently dropped** (line 4873-4874: `if key is None: return`).
3. **Pass 1** (`driftc.py:8387-8405`): does NOT pass `function_keys_by_fn_id` to `check_function` at all. `function_keys_by_fn_id` defaults to None → `record_instantiation` returns at line 4871 → **no instantiation records stored** in typed_fns.
4. **Pass 2** (`compile_stubbed_funcs`): builds its own `function_keys_by_fn_id` (line 2831) which includes wrapper functions (line 2998-3023 iterates ALL generic signatures including wrappers). Passes it to `check_function` (line 3356) → instantiation records ARE stored → concrete wrapper IS created.
5. **Phase 1 activated**: typed_fns from Pass 1 (no instantiation records) are reused. `_queue_instantiations` (line 3860) finds no entries → no concrete `__wrap_method::Wrapper<Int>::get` → codegen hits generic TYPEVAR → error.

**Why wrappers are particularly affected:** The DMIR template index explicitly skips wrappers (`provisional_dmir_v0.py:1013-1014: if getattr(sig, "is_wrapper", False): continue`). So `external_template_keys_by_fn_id` never contains wrapper function keys. Only `compile_stubbed_funcs`'s locally-built `function_keys_by_fn_id` (which iterates all signatures including wrappers) has them.

### Fix applied: build and pass `function_keys_by_fn_id` in Pass 1 + activate Phase 1

**Hunk 1** (`driftc.py`, after `linked_world, require_env = _build_linked_world(type_table)` in `main`):
- Build `pass1_function_keys: dict[FunctionId, FunctionKey]` starting from `external_template_keys_by_fn_id`
- Iterate only **wrapper** signatures (`is_wrapper=True`) in `signatures_by_id_all`, compute `FunctionKey` via `compute_template_decl_fingerprint` for any not already present
- Non-wrapper external templates already have correct keys in `external_template_keys_by_fn_id`; local consumer generics (if any) would need proper `require_expr` + `package_id` derivation
- Wrappers never have require clauses, so `require_expr=None` is correct; `package_id` matches `compile_stubbed_funcs`'s `local_package_id`

**Hunk 2** (`driftc.py`, `check_function` call in Pass 1 type-check loop):
- Added `function_keys_by_fn_id=pass1_function_keys` kwarg

**Hunk 3** (`driftc.py`, `compile_stubbed_funcs` call at package consumer codegen):
- Construct `Pass1State(typed_fns=typed_fns, callable_registry=callable_registry, ...)` from all driver Pass 1 state
- Pass `pass1_state=_p1_state` to `compile_stubbed_funcs`

### Verification
- `test_ext_nonlib_method_visibility`: PASS (the K42 regression — generic wrapper method on concrete receiver)
- All 16 `test_external_consumer.py` tests: PASS
- `pkg_iter_next_visibility` (e2e): PASS
- `pkg_iface_impl_vtable`, `pkg_vis_source_private_method_rejected`, `pkg_vis_source_trait_scope_rejected` (e2e): PASS
- `pkg_ext_module_trait_scope`: FAIL (pre-existing, unrelated — iterator trait scope visibility issue, not caused by this change)

### Before/after resolved types
Both passes always resolved identically — `has_tv=False`, substitution correct. The issue was never resolution divergence; it was **missing instantiation records**:
- **Before fix**: Pass 1 typed_fns have empty `instantiations_by_callsite_id` → `_queue_instantiations` finds nothing → no concrete `__wrap_method::Wrapper<Int>::get` created → codegen hits TYPEVAR
- **After fix**: Pass 1 typed_fns have proper `CallInstantiation(target_key=FunctionKey(...), type_args=(Int,))` → `_queue_instantiations` queues concrete wrapper → codegen succeeds

### Phase 1 status: ACTIVATED
- `Pass1State` dataclass at `driftc.py:2443` — bundles typed_fns + callable_registry + all resolution state
- `pass1_state` parameter on `compile_stubbed_funcs` — now passed from driver
- Override logic at `driftc.py:3162` — replaces locally-built state with pass1_state
- Typecheck skip at `driftc.py:3377` — uses pass1_state.typed_fns instead of re-running type-check
- Pinned regression: `test_ext_nonlib_method_visibility` (existing test covers the exact K42 scenario)

### Phase 2: skip most duplicate second-pass construction

**Changes in `compile_stubbed_funcs`:**

1. **`_build_linked_world` guard (line 2952)**: When pass1_state provides linked_world/require_env, skips the expensive `_build_linked_world(shared_type_table)` call (trait world linking + copy/destructible queries). `_install_destructor_fns` still runs, using the pass1_state.linked_world.

2. **Callable registry + trait/impl index construction (lines 3028+) guarded**: The entire callable_registry population loop (iterating all signatures, registering methods/free functions) and all three trait/impl index constructions (`GlobalImplIndex`, `GlobalTraitIndex`, `GlobalTraitImplIndex`) + trait_scope_by_module are now inside `else` — only built when pass1_state is absent.

3. **Module IDs + visibility provenance (lines 2806-2830) guarded**: Initial `module_ids` dict construction and `visibility_provenance_by_id` population skipped when pass1_state provides them.

4. **`visible_module_names_by_name` + prelude construction (lines 3238-3321) guarded**: Module visibility BFS, prelude injection, and K25 fallback all skipped when pass1_state provides the map.

5. **Source function type-check loop (line 3386-3402) skipped**: Already Phase 1 — typed_fns reused from pass1_state.

**What is now skipped (with stdlib ~1018 fn defs, ~30 modules):**
- CallableRegistry population (~1018 registrations)
- 3 trait/impl indices from module_exports + external metadata
- LinkedWorld construction (full trait world linking)
- visible_module_names_by_name BFS per module
- module_ids + visibility provenance construction
- Source function type-check (N consumer functions)

**What still runs under pass1_state (classified):**

*Intentionally kept (needed for correctness):*
- TypeChecker construction (line 2804): needed for lambda/thunk/instantiation type-checking that happens later
- `requires_by_fn_id` population from trait_worlds (lines 2948-2951): needed for `function_keys_by_fn_id` fingerprint computation
- `_install_destructor_fns` + K39 destructor processing: modifies shared_type_table.destructor_fns for MIR scope drops
- `function_keys_by_fn_id` extension loop: needed for template instantiation
- Generic instantiation / `_drain_instantiations`
- Lambda/thunk/wrapper spec lowering
- MIR lowering, validation, string ARC insertion, SSA construction

*Phase 3 — now resolved:*
- **External trait/impl merge mutations into shared_type_table.trait_worlds**: Guarded behind `_skip_trait_merge = pass1_state is not None`. When pass1_state is provided, external_trait_defs merge (2865-2871) and impl_def insertion (2929-2943) are skipped. Orphan-impl diagnostic check (2900-2914) still runs — preserves diagnostics parity. `requires_by_fn_id` population (2948-2951) remains unconditional, reading from shared_type_table.trait_worlds which Pass 1 already populated.
- **`validate_interface_schemas` (line 3201)**: Guarded behind `pass1_state is None`. Pass 1 already calls this at main:8063.
- **`validate_interface_impls` and `validate_trait_impls`**: NOT guarded — these are unique to compile_stubbed_funcs (Pass 1 does NOT run them). They remain needed for interface/trait impl contract checking.
- **Note**: `validate_interface_impls` appears to be called twice (lines 3202-3214 and 3215-3227) — pre-existing duplicate, not introduced or changed here.

**Verification:** 16/16 driver tests pass. 4/4 e2e pkg consumer tests pass.

### Phase 4: skip remaining early-stage duplication in compile_stubbed_funcs

**Changes in `compile_stubbed_funcs`:**

1. **Signature resolution loop (lines 2628-2675) guarded**: When pass1_state provides pre-resolved signatures from the driver, skip the O(n) `resolve_opaque_type` loop over all signatures. A lightweight fixup pass still runs to fill missing `error_type_id` on can-throw signatures (package serialization doesn't persist error_type_id).

2. **`_inject_method_boundary_wrappers` (line 2743) guarded**: Under pass1_state, wrapper signatures are already in `base_signatures_by_id` (flattened from the driver's ChainMap). Injection would return empty specs since all wrapper_ids are in `existing_ids`. Instead, reuse `pass1_state.method_wrapper_specs` for downstream wrapper MIR synthesis (lines 3668, 6123).

3. **HIR normalization (lines 2752-2758) guarded**: The driver already normalizes HIR at Pass 1 (main:8012). The call site now passes `normalized_hirs_by_id` instead of raw `func_hirs_by_id`. compile_stubbed_funcs skips the redundant `normalize_hir` traversal and uses the input directly.

4. **`unsafe_trusted_modules` construction (lines 2806-2822) guarded**: Reuses `pass1_state.unsafe_trusted_modules` from the driver, which is built from the same type_table and module_exports.

**Pass1State extensions:**
- Added `method_wrapper_specs: list` — list[MethodWrapperSpec] from driver's wrapper injection
- Added `unsafe_trusted_modules: set` — set[str] from driver's unsafe module computation

**Driver call site change (line 9203):**
- `func_hirs=normalized_hirs_by_id` instead of `func_hirs=func_hirs_by_id` — passes pre-normalized HIR when pass1_state is active

**What is now skipped (cumulative Phases 1-4, with stdlib ~1018 fn defs):**
- Signature resolution: O(n) `resolve_opaque_type` calls per param + return + error per sig
- Wrapper injection: O(n) scan over all signatures + FnSignature creation
- HIR normalization: O(n) `normalize_hir` traversal over all function bodies
- unsafe_trusted_modules: O(n) scan over module_packages + module_exports + signatures
- (Phase 1) Source function type-check: N consumer functions
- (Phase 2) CallableRegistry population, trait/impl indices, LinkedWorld, module IDs/visibility BFS
- (Phase 3) Trait world merge mutations, validate_interface_schemas

**What still runs under pass1_state (remaining):**
- TypeChecker construction (line 2828): needed for lambda/thunk/instantiation type-checking
- Signature provenance recording (lines 2683-2713): debug instrumentation, only active with `type_prov` debug flag
- `_ensure_module_packages` (lines 2714-2720): ensures module→package mappings, cheap
- `_register_derived_signature_precheck` definition + ChainMap assembly: O(1) setup
- error_type_id fixup on can-throw sigs (Phase 4 lightweight pass): O(n) but only `replace()` on sigs with missing error_type_id
- Orphan-impl diagnostic check: preserves diagnostics parity
- `requires_by_fn_id` population from trait_worlds
- `function_keys_by_fn_id` extension loop
- Generic instantiation / `_drain_instantiations`
- Lambda/thunk/wrapper spec lowering
- MIR lowering, validation, string ARC insertion, SSA construction

**Verification:** 16/16 driver tests pass. 86/86 stage2 tests pass. pkg_iter_next_visibility e2e: PASS.

### Phase 5: share function_keys_by_fn_id from driver

**Problem:** compile_stubbed_funcs builds `function_keys_by_fn_id` (lines 3047-3072) by iterating ALL generic signatures and calling `compute_template_decl_fingerprint` for each. The driver already does this at Pass 1 (lines 8402-8450) but previously only for wrapper signatures.

**Changes:**

1. **Driver pass1_function_keys extended to ALL generics (lines 8412-8456)**: Removed the `is_wrapper` gate. Now iterates all generic signatures in `signatures_by_id_all`, building `requires_by_fn_id` from `type_table.trait_worlds` for fingerprint computation. Previously `require_expr=None` was only correct for wrappers; now uses `_p1_requires_by_fn_id.get(fn_id)` for correct require expressions on non-wrapper generics.

**Invariant (package_id in synthesized keys):** The fallback loop uses `package_id=package_id` (the local consumer package). This is correct because `external_template_keys_by_fn_id` already covers all external non-wrapper generics (from DMIR TemplateHIR-v1 entries). The `if _p1_fn_id in pass1_function_keys: continue` guard ensures the fallback only fires for fn_ids NOT already keyed — i.e. local consumer generics and wrapper methods, which are by definition in the local package. If external non-wrapper generics ever become missing from DMIR, the fallback must derive per-signature package_id from module_packages.

2. **Pass1State extended**: Added `function_keys_by_fn_id: dict` field.

3. **`function_keys_by_fn_id` extension loop guarded (compile_stubbed_funcs line 3047)**: When pass1_state provides keys, `function_keys_by_fn_id.update(pass1_state.function_keys_by_fn_id)` replaces the O(n) fingerprint loop.

4. **`requires_by_fn_id` population guarded (line 2993)**: Only consumed by the function_keys extension loop. When that loop is skipped, the trait_worlds iteration is also skipped.

**What is now additionally skipped under pass1_state:**
- `compute_template_decl_fingerprint` per generic signature (expensive: walks type expressions, computes hashes)
- `requires_by_fn_id` population from trait_worlds
- `_declared_name_from_fn_id` + `FunctionKey` construction per generic

**Remaining non-guarded work in compile_stubbed_funcs under pass1_state:**
- TypeChecker construction: needed for lambda/thunk/instantiation type-checking
- Signature provenance recording: debug-only, gated on `type_prov` flag
- `_ensure_module_packages`: cheap module→package mapping
- error_type_id fixup: lightweight O(n) on can-throw sigs only
- Orphan-impl diagnostic check: preserves diagnostics parity
- `_install_destructor_fns` + K39 destructor processing: correctness-critical
- `validate_interface_impls` / `validate_trait_impls`: unique to compile_stubbed_funcs (not duplicated with driver)
- Generic instantiation, lambda/thunk lowering, MIR lowering, SSA construction: core pipeline work

**Assessment:** The remaining items are either correctness-critical (unique to compile_stubbed_funcs, not duplicated with the driver), cheap/debug-only, or core pipeline work that must run regardless. There is no further duplicated-state reduction to achieve — all duplicated resolution/construction from the driver's Pass 1 is now shared via Pass1State.

**Verification:** 16/16 driver tests pass. 86/86 stage2 tests pass. pkg_iter_next_visibility e2e: PASS.

### Phase 6: structural convergence — eliminate wasted allocations, precompute visibility provenance, move destructor registration to driver

**Changes:**

1. **Eliminate wasted allocations (CSF lines 2842-2844)**: Under pass1_state, `CallableRegistry()`, `{None: 0}`, and `{}` were allocated then immediately overwritten from pass1_state at lines 3095-3112. Moved the pass1_state assignment of `callable_registry`, `module_ids`, `visibility_provenance_by_id` to the top of the initialization block (line 2845), before any code that uses them.

2. **Precompute `visibility_provenance_by_id` in driver**: Previously, CSF reconstructed this int-keyed map from `visibility_provenance_by_name` + `module_ids` (O(m²) nested iteration). Now precomputed in the driver before Pass1State construction and passed directly via `Pass1State.visibility_provenance_by_id`. The 10-line reconstruction loop in CSF is eliminated.

3. **Move `_install_destructor_fns` + K39 to driver**: Both now execute in the driver before Pass1State construction. CSF guards both under `pass1_state is None`.

   **INVARIANT (destructor_fns ordering):** `_install_destructor_fns` REPLACES `type_table.destructor_fns` entirely (line 499: `type_table.destructor_fns = destructor_fns`). K39 then EXTENDS the dict with external package Destructible impls. If CSF called `_install_destructor_fns` after the driver already ran both, the replace would clobber K39 entries. The guard ensures both run exactly once — either in the driver (pass1_state path) or in CSF (standalone path).

**What is now additionally skipped under pass1_state:**
- `CallableRegistry()` allocation (immediately overwritten)
- `module_ids = {None: 0}` allocation (immediately overwritten)
- `visibility_provenance_by_id` reconstruction from name→chain data (O(m²))
- `_install_destructor_fns` call (already on shared type_table)
- K39 external Destructible impl registration (already on shared type_table)

**Remaining non-guarded work in compile_stubbed_funcs under pass1_state:**
- TypeChecker construction: needed for lambda/thunk/instantiation type-checking during MIR lowering
- Signature provenance recording: debug-only, gated on `type_prov` flag
- `_ensure_module_packages`: cheap module→package mapping
- error_type_id fixup: lightweight, only on can-throw sigs with missing error_type_id
- Orphan-impl diagnostic check: preserves diagnostics parity (unique to CSF)
- `validate_interface_impls` / `validate_trait_impls`: unique to CSF, not duplicated with driver
- Generic instantiation, lambda/thunk lowering, MIR lowering, SSA construction: core pipeline

**Convergence assessment:** All duplicated state between the driver's Pass 1 and compile_stubbed_funcs is now shared via Pass1State. The remaining work is either unique to CSF (validations, destructor registration now moved), debug-only, or core pipeline. No further convergence is achievable without fundamentally restructuring compile_stubbed_funcs (e.g., splitting it into separate phases or inlining it into the driver).

**Verification:** 16/16 driver tests pass. 86/86 stage2 tests pass. pkg_iter_next_visibility e2e: PASS.

### Convergence freeze (2026-03-08)

**Cleanup:** Removed `visibility_provenance_by_name` from Pass1State. It was only consumed by the visibility_provenance_by_id reconstruction loop (Phase 6 replaced with precomputed field). Confirmed zero remaining references to `pass1_state.visibility_provenance_by_name` in CSF.

**Final Pass1State shape (14 fields):**
```
typed_fns                      # Phase 1: pre-typed function results
callable_registry              # Phase 2: method/fn resolution registry
impl_index                     # Phase 2: GlobalImplIndex
trait_index                    # Phase 2: GlobalTraitIndex
trait_impl_index               # Phase 2: GlobalTraitImplIndex
trait_scope_by_module          # Phase 2: per-module trait scope
linked_world                   # Phase 2: LinkedWorld (trait world linking)
require_env                    # Phase 2: RequireEnv
visible_module_names_by_name   # Phase 2: module visibility graph
module_ids                     # Phase 2: module name → int mapping
method_wrapper_specs           # Phase 4: wrapper MIR synthesis specs
unsafe_trusted_modules         # Phase 4: set of std/unsafe-allowed modules
function_keys_by_fn_id         # Phase 5: FunctionId → FunctionKey (all generics)
visibility_provenance_by_id    # Phase 6: int → provenance chain
```

**Guard map (16 guards in compile_stubbed_funcs):**

| Line | Phase | Skips |
|------|-------|-------|
| 2625 | 4 | Signature resolution loop (fills error_type_id only) |
| 2755 | 4 | Wrapper injection scan |
| 2771 | 4 | HIR normalization |
| 2818 | 4 | unsafe_trusted_modules build |
| 2845 | 6 | Wasted allocations (callable_registry, module_ids, vis_prov) |
| 2853 | 2 | module_ids from module_deps |
| 2887 | 3 | Trait-world merge mutations |
| 3004 | 5 | requires_by_fn_id population |
| 3008 | 2 | _build_linked_world |
| 3017 | 6 | _install_destructor_fns + K39 |
| 3061 | 5 | function_keys extension loop |
| 3101 | 2 | impl/trait indices, trait_scope, visible_modules |
| 3250 | 3 | validate_interface_schemas |
| 3283 | 2 | visible_module_names_by_name construction |
| 3295 | 2 | module visibility BFS |
| 3428 | 1 | Source function type-check loop |

**Remaining CSF work under pass1_state (intentional, not duplicated):**

*Unique validation (CSF-only, not in driver):*
- TypeChecker construction for lambda/thunk/instantiation checking
- orphan-impl diagnostic check (E-IMPL-ORPHAN)
- validate_interface_impls (interface impl contract adherence)
- validate_trait_impls (trait impl contract adherence)

*Core lowering pipeline:*
- generic instantiation / _drain_instantiations
- lambda/thunk/wrapper MIR synthesis
- HIR→MIR lowering
- SSA construction
- throw checks

*Cheap/debug:*
- signature provenance recording (debug flag only)
- _ensure_module_packages (O(m) module→package mapping)
- error_type_id fixup (O(n) on can-throw sigs with missing error_type_id)

**Verification (convergence freeze):**
- External consumer driver suite: 17/17 PASS (16 original + 1 convergence_parity)
- Stage2 tests: 86/86 PASS
- Checker tests: 33/33 PASS (5 pre-existing excluded)
- Package consumer e2e boundary: 3/3 PASS (pkg_iter_next_visibility, pkg_vis_source_trait_scope_rejected, pkg_iface_impl_vtable)
- Package consumer e2e smoke: 10/10 PASS
- No new local/package divergence detected

**Convergence parity assertions (5 checks, all PASS across 11 pkg-consumer compilations):**
1. Function keys: recomputed fingerprints match shared function_keys_by_fn_id
2. Wrapper injection: re-running _inject_method_boundary_wrappers produces empty specs
3. Signature resolution: all can-throw sigs have error_type_id populated
4. Visibility provenance: module_ids fully covered by visibility_provenance_by_id
5. Destructor registration: destructor_fns populated for all Destructible impl targets

**Excluded from boundary CI (pre-existing LANGUAGE_BUG):**
- pkg_ext_module_trait_scope (K25): external module trait-scope re-instantiation not fully working yet
- pkg_vis_source_private_method_rejected: local runner hits parser error on fixture syntax, not a real boundary case

**Test target cleanup (post-convergence):**
- Removed: `ext-e2e-asan` justfile target (ASAN now via env var: `DRIFT_ASAN=1 just ext-e2e-smoke`)
- Added: `ext-e2e-boundary` justfile target (3 blocking package-boundary regression cases)
- Updated: `just test` now includes `ext-e2e-smoke` + `ext-e2e-boundary` for everyday CI
- Note: `ext-e2e-boundary` is a package-consumer regression slice, not a cross-lane parity target
