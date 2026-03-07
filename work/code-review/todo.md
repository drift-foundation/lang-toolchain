# Cross-Package Defect Synthesis Report

**Date**: 2026-03-07
**Scope**: K10–K40 + ext-e2e-report (559 test cases through signed-package consumer path)
**Current state**: 532/558 pass (95.3%), remaining: 19 compile-check + 7 compile-codegen + 0 link + 0 runtime

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

| K39 | codegen/BFS | Destructible::destroy not emitted for types nested in variant payloads or package-only functions | Three interlocking issues: (a) `external_impl_metas` not scanned for Destructible impls (only `module_exports`); (b) BFS Phase 2 type graph walk only followed struct fields via `get_struct_instance`, not variant arm payloads via `get_variant_instance` — `HashMapCore<String, JsonNode, DefaultBuildHasher>` inside `JsonNode::Object` was invisible; (c) Phase 2+3 not interleaved — types discovered in destroy function bodies (via DropValue) couldn't feed back into type graph walk; (d) package-side BFS had no type graph walk at all — types only in package function params/locals (e.g., `HashMapCore<String, Int, DefaultBuildHasher>` in `std.log::_emit`) missed. Fix: add variant traversal, merge Phase 2+3 into fixpoint loop, mirror type graph walk in package BFS. **+63 tests** (all JSON/HashMap link failures) | Yes |
| K40 | codegen/BFS | Preamble functions (`install_process_preamble`) not emitted in package-consumer path | Preamble functions are injected by codegen into entry wrappers at LLVM emission time, not called from MIR. BFS from user code never discovers them. Fix: explicitly seed `ENTRY_WRAPPER_IMPLICIT_DEPS` into `pkg_needed` with transitive closure walk. **+2 tests** (preamble runtime failures) | Yes |
| K41 | type_checker | Lambda nothrow analysis resolves boundary wrapper instead of original method | `_lambda_can_throw` / `_treat_can_throw` checks `declared_can_throw` on the resolved target. In package path, auto-borrow methods (e.g. `Arc::borrow_mut`) resolve to `__wrap_method::` wrapper which has `declared_can_throw=True`. Fix: when target is a wrapper (`wraps_target_fn_id` set), check the wrapped function's `declared_can_throw` instead. **+1 test** (`callback_move_capture_nested_callback`). Three other lambda tests now pass initial type-check but fail in `compile_stubbed_funcs` with a different bug (K42). | Yes |
| K42 | driftc/compile_stubbed_funcs | `conc.lock(arc)` trait auto-borrow fails in compile_stubbed_funcs | `compile_stubbed_funcs` builds its own callable_registry and trait world. `BorrowMut<Mutex<T>> for Arc<Mutex<T>>` auto-borrow doesn't resolve for free function arg coercion, causing "no matching overload for function 'lock'" in the MIR compilation pass. Initial type-check (line 8296) passes; only the second type-check inside compile_stubbed_funcs fails. Affects: callback_move_capture_{arc_lifetime,replace_state}, effective_drift_emitter_example, callback_arc_mutex_full_mutation (**4 tests**) | Yes |

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
| After module-m + unsafe + dict-iter + K41 lambda nothrow | 95.3% | 532/558 |
| **Remaining 26 failures** | | |
| — Parser unsupported syntax (7): bare match arms, qualified trait calls, while-in-try | NOT package-specific | Exclude or fix parser |
| — Expression block return (2): `return` in expr blocks | NOT package-specific | Exclude or fix |
| — Test code bug (2): `core.Error` (primitive), `buffer_commit_read` (not exported) | NOT package-specific | Fix test/export |
| — array_byte codegen crash (7): K27-class intrinsic FnResult wrapping | Package-only | Codegen fix needed |
| — K42 lock/auto-borrow (4): trait auto-borrow in compile_stubbed_funcs | Package-only | Deep trait resolution fix |
| — MIR invariant (2): array copy invariant in diagnostic tests | Package-only | Investigate |
| — Package-path semantic (2): ambiguous trait req (hashmap_iter_empty), Result type inference | Package-only | Investigate |
| Estimated ceiling (excl parser/expr-block/test-bug) | ~97.9% | ~536/547 |
