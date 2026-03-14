# Drift development history

## 2026-03-14
- **Fixed LANGUAGE_BUG: __wrap_method stubs not emitted for stdlib method calls
  in packaged code**:
  When a package body calls a method on a stdlib type (e.g., `String.byte_length()`),
  the consumer's IR references `__wrap_method` stubs that were never defined,
  causing a link-time failure.
  - Root cause (two gaps):
    1. `_inject_method_boundary_wrappers` only ran on source-file signatures
       (`base_signatures_by_id`), not external/stdlib signatures.  Wrapper
       declarations for stdlib methods were never created in the consumer.
    2. The package BFS expansion loop (`_build_package_consumer_unit`) only
       checked `pkg_mir_all` for callees, not `wrapper_target_by_id`.  Wrapper
       references in package MIR were never added to `wrappers_needed`.
    3. When wrapper targets were in source MIR (stdlib compiled from source),
       they were not seeded into `src_needed`, so BFS pruning removed them.
  - Fix:
    - Add second `_inject_method_boundary_wrappers` call after
      `external_signatures_by_id` is populated (CLI path, after line 7881).
    - In BFS expansion, detect wrapper references in package MIR and add them
      to `wrappers_needed`.
    - Seed wrapper targets from `_src_mir_full` into `src_needed` when the
      target is a source-compiled function (e.g., stdlib method).
  - Boundary sweep:
    - Source path: unaffected (all modules compiled together).
    - Package-consumer path: **fixed**.
    - Multi-package: covered by existing test.
    - Stdlib methods (`String.byte_length`): **fixed** — regression test.
    - Non-stdlib package methods: covered (wrapper synthesis already worked for
      methods in `base_signatures_by_id`).
  - Added regression test `test_driftc_package_stdlib_method_call_wrapper`.
- Bumped compiler version to `0.27.45-dev`; ABI remains `5`.

- **Fixed LANGUAGE_BUG: RawPtr<T> field TypeId remapping in package consumer**:
  Consuming a package containing a struct with a `RawPtr<T>` field and a
  `Destructible` impl crashed codegen with `unsupported param type id 1`
  (UNKNOWN sentinel).
  - Root cause: `_eval_generic_type_expr` in `types_core.py` handled `Ptr`
    (internal name, `module_id="std.mem"`) but not `RawPtr` (user-facing alias,
    no module_id).  When the package struct schema stores the field type as
    `GenericTypeExpr(name="RawPtr", ...)`, the evaluator fell through to
    nominal lookup, failed to find a struct named `RawPtr`, and returned
    `ensure_unknown()`.  This left the struct's field types as UNKNOWN
    placeholders, crashing codegen when the `Destructible::destroy` body
    referenced the struct.
  - Fix: add `RawPtr` handler in `_eval_generic_type_expr` that delegates to
    `new_ptr()`, matching the existing `resolve_opaque_type` behavior.
  - Boundary sweep:
    - Source path: unaffected (field types resolved via `resolve_opaque_type`).
    - Package-consumer path: fixed (`_eval_generic_type_expr` now handles RawPtr).
    - Multi-package: verified (existing test passes).
    - Adjacent constructed types: `Array<T>`, `&T`, `&mut T` already handled.
    - Trait/impl/destructor: Destructible is the trigger path; inherent impls
      and no-impl cases are unaffected (struct field resolution is the issue).
  - Added regression test `test_driftc_package_rawptr_field_with_destructible`.
- Bumped compiler version to `0.27.44-dev`; ABI remains `5`.

- **Fixed LANGUAGE_BUG: multi-package impl_id collision**:
  Consuming two or more packages that each contain `implement` blocks crashed
  with `ValueError: impl id already interned with a different key` in
  `id_registry.py`.  This affected any deployed binary where stdlib is loaded
  as a pre-compiled package (`std.dmp` via `--package-root`).
  - Root cause: package serialization assigns impl_ids as sequential counters
    starting from 0 within each package (`pkg_next_impl_id` in `driftc.py`).
    The consumer passed these package-local ids via `preferred=impl_id` to
    `id_registry.intern_impl()`, which expects globally unique ids.  When two
    packages both have impl_id=0 for different impl blocks, the registry
    raises a collision error.
  - Fix: drop `preferred=` from `intern_impl()` call at the package-consumer
    site — let the registry assign fresh global ids instead of forcing
    package-local values.
  - Added regression test `test_driftc_multi_package_impl_id_no_collision`
    (builds two packages with implement blocks, consumes both).
- Bumped compiler version to `0.27.43-dev`; ABI remains `5`.

## 2026-03-13
- **Fixed LANGUAGE_BUG: package-consumer extern C symbol mangling**:
  Extern C declarations consumed from a `.dmp` package emitted module-qualified
  LLVM symbols like `@lib::abs(i32)` instead of bare C symbols like `@abs(i32)`.
  This is the package-consumer analogue of the source-file extern-C mangling bug
  fixed in 0.27.36-dev.
  - Root cause: workspace-loader package signature reconstruction (`driftc.py`
    line ~7647) unconditionally module-qualifies bare names via
    `name = f"{module_name}::{name}"`. This is correct for Drift functions but
    wrong for extern C declarations, which must preserve their bare C symbol
    identity for correct LLVM `declare`/`call` emission.
  - Fix: skip module-qualification when `sd.get("is_extern_c")` is true.
  - Source-file path confirmed clean (13/13 FFI tests pass).
  - Extended regression test to assert `declare i32 @abs(i32)` in consumer IR
    and reject `lib::abs`.
- Bumped compiler version to `0.27.42-dev`; ABI remains `5`.

## 2026-03-13
- **Fixed LANGUAGE_BUG: extern C package codegen crash**:
  Extern C declarations consumed from a `.dmp` package crashed LLVM codegen
  with `NotImplementedError: unsupported terminator NoneType` because the
  `is_extern_c` flag was not round-tripped through the package pipeline.
  - Root cause: `encode_signatures()` in `provisional_dmir_v0.py` did not
    serialize `is_extern_c`; `FnSignature(...)` reconstruction in `driftc.py`
    (two sites) did not deserialize it. Without this flag, codegen tried to
    emit a body for what should be an extern `declare`.
  - Fix: serialize `is_extern_c` in `provisional_dmir_v0.py`; deserialize at
    both reconstruction sites in `driftc.py`.
  - Added regression test `test_driftc_can_consume_package_with_extern_c_declarations`.
- Bumped compiler version to `0.27.41-dev`; ABI remains `5`.

## 2026-03-13
- **Fixed LANGUAGE_BUG: Array<String>.push(name) MIR validation failure**:
  `arr.push(name)` for Copy non-bitcopy element types (e.g., String) failed
  MIR validation because the lowering did not emit `CopyValue` before the
  array store instruction. The MIR validator requires that array stores of
  Copy non-bitcopy values are explicitly wrapped in `CopyValue` to ensure
  correct retain/refcount semantics at runtime.
  - Root cause: `_lower_call_arg` correctly identifies String as "copy" but
    does not emit `CopyValue` — it was designed for function call args, not
    array storage which has its own MIR invariants.
  - Fix: added `_ensure_array_elem_copy` helper in `hir_to_mir.py` that wraps
    values in `CopyValue` when the element type is Copy but non-bitcopy.
    Applied to `push`, `insert`, `set`, and `extend` lowering paths.
  - Affects all Copy non-bitcopy element types in container methods, not just
    String. Bitcopy types (Int, Bool, etc.) and non-Copy types are unaffected.
  - Not limited to `--entry` mode; the bug occurs in any compilation path.
  - Added regression test `array_push_string_no_move`.
- Bumped compiler version to `0.27.39-dev`; ABI remains `5`.

## 2026-03-13
- **Fixed LANGUAGE_BUG: Int32/Uint32 package serialization failure**:
  Packages exporting functions with Int32/Uint32 parameter or return types
  failed at consumer import with "package SCALAR 'Int32' missing module_id".
  - Root cause: Int32/Uint32 were added as user-facing scalar types in
    0.27.32-dev, but the package serialization/deserialization path was not
    updated — `_builtin_type_id()` and `ensure_builtin()` in
    `type_table_link_v0.py` lacked entries for Int32/Uint32.
  - Also fixed: `_BUILTIN_TYPE_NAMES` in `provisional_dmir_v0.py` was missing
    Uint64, Byte, Int32, Uint32 (Uint64/Byte were latent; Int32/Uint32 were
    active failures).
  - Also fixed: `_host_type_key_for_tid()` builtin set and FnResult ok-type
    lowering in `llvm_codegen.py` for i32 returns through entrypoint wrappers.
  - Added regression test `test_driftc_can_consume_package_exporting_int32_uint32`.
- Bumped compiler version to `0.27.40-dev`; ABI remains `5`.

## 2026-03-13
- **stdlib: TcpStream.raw_fd()**: added `pub fn raw_fd(self: &TcpStream) nothrow -> Int`
  to `std.net.TcpStream`. Returns the underlying file descriptor without consuming
  ownership — borrowed fd observation only. The `fd` field remains private.
  - Added regression test `std_net_tcp_stream_raw_fd`.
- Bumped compiler version to `0.27.38-dev`; ABI remains `5`.

## 2026-03-13
- **Extern C cross-module visibility**: `pub extern "C" fn` and `pub extern "C" { ... }`
  are now valid declaration forms. Public extern C declarations can be exported and
  imported across modules like normal functions. Codegen preserves bare C symbol
  identity — no module-qualified names, no entrypoint wrappers.
  - Grammar: added `extern_fn TERMINATOR` and `extern_block` to `pub_item` rule.
  - Parser: propagates `is_pub` to extern C FunctionDefs.
  - Workspace loader: extern C signatures skip `is_exported_entrypoint` (no wrapper
    generation) and preserve bare C symbol name.
  - Added regression tests: `ffi_c_cross_module_import`, `ffi_c_cross_module_export_block`.
- Bumped compiler version to `0.27.37-dev`; ABI remains `5`.

## 2026-03-13
- Fixed two LANGUAGE_BUGs blocking TLS team FFI work:
  1. **Extern C symbol mangling in non-main modules**: `sig.name` was being
     module-qualified (e.g., `repro_mod::puts` instead of `puts`) by the
     workspace loader's `replace(sig, name=function_symbol(fn_id), ...)` call.
     Fix: skip module qualification for `is_extern_c` signatures.
     File: `lang/driftc/parser/__init__.py` line ~3206.
  2. **Inline `ptr_from_ref(&arr[0])` in multi-arg call**: `StoreLocal` for
     address-taken locals unconditionally applied `_bool_to_storage` (zext i1 → i8)
     when `store_llty == "i8"`, but Byte values are also i8. This produced
     invalid IR (`zext i1 %byte_val to i8`).
     Fix: guard with `_is_bool_storage_pair` to only convert when the value is
     actually i1 (Bool), not i8 (Byte).
     File: `lang/codegen/llvm/llvm_codegen.py` line ~2758.
- Added regression tests: `ffi_c_extern_nonmain_module`, `ffi_c_inline_ptrref_multiarg`.
- Bumped compiler version to `0.27.36-dev`; ABI remains `5`.

## 2026-03-12
- Fixed void-returning extern C calls with `RawPtr<T>` by-value args (proof cache fix):
  - Root cause: `_query_copy` in driftc.py lacked a `RAW_PTR` early-return, so trait
    resolution returned REFUTED → proof cache stored `False` → structural fallback
    in `copy_status()` was never reached.
  - Fix: added `if td.kind is TypeKind.RAW_PTR: return True` to `_query_copy`, matching
    the existing FUNCTION/SCALAR/REF early-returns.
  - Added regression test `ffi_c_void_rawptr_arg` (alloc_byte → free_byte round-trip).
- Bumped compiler version to `0.27.35-dev`; ABI remains `5`.

## 2026-03-12
- Fixed the hex-literal parser follow-up so integer parsing now selects base explicitly:
  - `0x` / `0X` literals parse as hexadecimal,
  - all other integer literals parse as decimal,
  - leading-zero decimals like `010` no longer crash the parser via Python `int(..., 0)`.
- Bumped compiler version to `0.27.33-dev`; ABI remains `5`.

## 2026-03-12
- FFI follow-up: hex integer literals, Int32/Uint32, out-parameter pattern:
  - Parser: extended INT/UINT_LIT/UINT64_LIT tokens to accept hexadecimal form (`0xFF`, `0x00200000u`, `0x10u64`).
  - Type system: `Int32` and `Uint32` are now first-class scalar types available in user code (removed from fixed-width reserved set).
  - FFI validator: `Int32` and `Uint32` accepted in `extern "C"` signatures; map to `i32` in LLVM IR.
  - Checker: `cast<Int32>(...)` / `cast<Uint32>(...)` supported; Int32/Uint32 recognized as Copy.
  - MIR: scalar cast lowering extended for Int32/Uint32 (trunc/sext between 32-bit and word-width).
  - LLVM codegen: Int32/Uint32 map to `i32` in type resolution, extern C declarations, and scalar cast info.
  - PEX e2e runner: added `c_sources` support for compiling/linking C helper files (mirrors runner.py).
  - Added e2e tests: `hex_integer_literals`, `ffi_c_int32_uint32`, `ffi_c_out_param_rawptr`.
  - Updated language spec: §3.1 literal forms, §3.1.1 fixed-width availability, §17.5 FFI type table, §21.6 scope.
- Bumped compiler version to `0.27.32-dev`; ABI remains `5`.

## 2026-03-12
- Fixed owned-struct field extraction aliasing/double-free follow-up in stage2 lowering:
  - non-bitcopy `StructGetField` results from owned struct locals/params are now tracked as aliased temps, not just `&T` field reads,
  - the lazy copy-at-consumption path now covers the TLS-style `val ap = builder.protocols; return Config(ap);` ownership transfer without double-free,
  - match scrutinee handling keeps the existing tombstone path for owned-local field reads while still deep-copying true alias cases.
- Added upstream e2e regression `owned_field_extract_double_free` for owned field extraction from an owned parameter, and kept the borrowed/recursive ownership regressions green under both normal runs and `DRIFT_MEMCHECK=1`.
- Bumped compiler version to `0.27.31-dev`; ABI remains `5`.

## 2026-03-12
- Fixed borrowed-struct owned-field aliasing/double-free in stage2/LLVM lowering:
  - reading owned fields from `&T` now marks aliased temps at field-read sites instead of eagerly cloning them,
  - deep copies are emitted only at real ownership-transfer boundaries (constructor args, call args, returns, local binding/reassignment, match scrutinee),
  - recursive owned copies are handled through generated LLVM clone helpers for `Struct`, `Array`, and `Variant`, including self-referential shapes.
- Added/updated regressions covering:
  - `Array<String>` moved through a chained builder into a struct field,
  - deep nested borrowed-copy through array/struct ownership,
  - recursive struct and recursive variant borrowed-copy cases.
- Verified green both normally and under `DRIFT_MEMCHECK=1`.
- Bumped compiler version to `0.27.30-dev`; ABI remains `5`.

## 2026-03-11
- Implemented C FFI MVP (`extern "C"`):
  - Parser: `extern "C" fn name(params) nothrow -> RetType;` and block form `extern "C" { ... }` syntax via Lark grammar extensions.
  - Type resolver: FFI-safe type validation rejects non-scalar types (String, Array, Fn, etc.) in extern C signatures; diagnostics returned as structured errors.
  - Checker: `unsafe` block required at call sites for extern C functions; `nothrow` required on declarations.
  - MIR: void-returning extern C calls in unsafe blocks now lower correctly (fixed assertion in expression context).
  - Type system: `RawPtr<T>` recognized as a user-facing alias for the internal `Ptr<T>` type from any module context.
  - LLVM codegen: extern C functions emit `declare` instead of `define`; calls use direct ABI without FnResult wrapping.
  - Linker CLI: `--link-lib`, `--link-search`, `--link-obj` flags for specifying libraries and object files.
  - Supported FFI-safe types: Int, UInt, Uint64, Byte, Bool, Float, RawPtr<T>, Void.
  - End-to-end test suite: 6 positive tests (libc abs, custom C lib, void return, block syntax, RawPtr, link-lib) and 5 negative tests (String rejected, throws rejected, missing nothrow, unsafe required, bad ABI string).
- Bumped compiler version to `0.27.29-dev`; ABI remains `5`.

## 2026-03-11
- Finished PEX CLI/e2e JSON parity cleanup for the supported codegen fleet:
  - added the missing `--emit-ir` diagnostic-path behavior in the e2e runners so entrypoint validation cases exercise the same validating pipeline stages under JSON mode,
  - corrected reserved-namespace rejection coverage by omitting `--dev` for cases that intentionally assert reserved-module diagnostics,
  - aligned `duplicate_main` and `entry_multiple_main_rejected` fixture expectations with the actual CLI parser-phase diagnostics,
  - removed obsolete `use_driftc_json=false` special-casing from the historical-artifact cases that are now covered correctly through the JSON/PEX path.
- Final PEX e2e snapshot: `1013` total, `1008` passed, `5` skipped, `0` failed; the remaining skips are the real `package_consumer_only` boundary, not parity debt.
- Bumped compiler version to `0.27.28-dev`; ABI remains `5`.

## 2026-03-11
- Completed PEX CLI parity follow-up for the codegen/e2e path:
  - removed the remaining `CLI_KNOWN_SKIP` cases so the PEX CLI runner now reaches full parity for the supported fleet,
  - corrected the `cycle_direct` and `cycle_indirect_3way` fixtures so they assert real `import cycle detected` diagnostics instead of accidentally passing on case-sensitive export-name errors,
  - confirmed the cycle cases pass in both the in-process runner and the PEX CLI runner with the intended parser diagnostics.
- Current parity snapshot: `999 passed`, `14 skipped`, `0 failed`; the remaining skips are non-parity paths (`use_driftc_json=false`, `package_consumer_only`), not CLI gaps.
- Bumped compiler version to `0.27.27-dev`; ABI remains `5`.

## 2026-03-10
- Switched deploy packaging to a `PEX --scie eager` compiler entrypoint:
  - `just deploy` now builds a self-contained `bin/driftc` executable instead of publishing the shell-wrapper plus `lib/python_vendor` layout,
  - deployed compiler use no longer requires a host Python interpreter or ambient Python packages,
  - staged deploy validation now covers the PEX/scie artifact directly, including self-sufficiency, signed stdlib usage, read-only install-tree behavior, runtime archive linking, and symlinked entry resolution.
- Recorded the new deployed artifact shape in the manifest/deploy metadata and updated deploy docs to reflect the embedded-interpreter model and larger artifact-size tradeoff.
- Bumped compiler version to `0.27.26-dev`; ABI remains `5`.

## 2026-03-10
- Fixed a deploy/toolchain read-only install bug where deployed `bin/driftc` in runtime-archive mode tried to create lock/build directories under the installed `lib/runtime` tree:
  - `build_runtime_archive()` now returns an existing up-to-date `libdrift_rt.a` before creating build directories or acquiring the archive lock,
  - deployed/runtime-archive use now consumes the prebuilt archive from the install tree as a read-only input instead of treating the deployed tree as a writable cache.
- Added a deploy regression that makes the staged deployed runtime tree read-only and verifies wrapper compilation succeeds without creating `.build.lock` or other mutable state under the install path.
- Bumped compiler version to `0.27.25-dev`; ABI remains `5`.

## 2026-03-10
- Fixed a deploy/toolchain self-sufficiency bug where the published `bin/driftc` wrapper still depended on ambient Python packages from the caller environment:
  - deploy now vendors the compiler's required Python distribution closure into `lib/python_vendor`,
  - the deployed wrapper now runs Python in `-S` mode and resolves both compiler modules and vendored dependencies from the deployed tree,
  - deploy smoke now validates the bundled Python-dependency path instead of prechecking for host-installed `lark`/`llvmlite`/`cryptography`.
- Added a deploy regression that exercises the bundled wrapper under a site-disabled Python interpreter and verifies compilation succeeds using only the deployed tree.
- Bumped compiler version to `0.27.24-dev`; ABI remains `5`.

## 2026-03-08 (Option B convergence finished)
- Finished the structural convergence work between driver Pass 1 and `compile_stubbed_funcs` for package-consumer compilation.
- Shared all duplicated material resolution/setup state through `Pass1State`, including:
  - typed functions and resolution infrastructure,
  - function/template keys,
  - callable registry / impl index / trait indexes,
  - trait scope / linked world / require environment,
  - module ids / visibility provenance,
  - wrapper specs / unsafe-trusted-module set.
- Removed or guarded duplicated second-pass work in `compile_stubbed_funcs` across Phases 1-6:
  - source typecheck loop,
  - callable-registry / index construction,
  - trait-world merge mutations,
  - source-HIR normalization,
  - wrapper injection scan,
  - generic function-key fingerprint loop,
  - destructor setup and K39 package destructible registration,
  - repeated allocations and visibility-provenance reconstruction.
- Preserved intentionally unique work under `pass1_state`:
  - orphan/interface/trait validation paths,
  - generic instantiation,
  - lambda/thunk/wrapper MIR synthesis,
  - MIR lowering, SSA, and throw checks,
  - cheap/debug bookkeeping.
- Cleaned up final convergence leftovers:
  - removed redundant duplicate `validate_interface_impls` call,
  - removed dead `Pass1State.visibility_provenance_by_name` after precomputing `visibility_provenance_by_id`,
  - documented invariants around fallback function-key ownership and one-time destructor registration ordering.
- Added post-convergence parity/test-target hardening:
  - introduced convergence parity assertions gated by `DRIFT_DEBUG=convergence_parity` to verify shared function-key identity, wrapper injection emptiness under `pass1_state`, signature `error_type_id` completeness, visibility-provenance coverage, and destructor registration coverage,
  - added `test_convergence_parity_pass1_state` in `lang/tests/driver/test_external_consumer.py`,
  - added `ext-e2e-boundary` as a package-consumer boundary regression slice,
  - folded `ext-e2e-smoke` and `ext-e2e-boundary` into `just test` for everyday CI/dev confidence,
  - removed the separate `ext-e2e-asan` target in favor of `DRIFT_ASAN=1` execution mode on existing targets.
- Post-convergence regression cleanup:
  - fixed missing driver-side K25 trait-scope fallback for external package modules so Pass 1 and `compile_stubbed_funcs` use the same temporary scope broadening during generic re-instantiation,
  - serialized per-module `trait_scope` into DMIR payload/interface and reconstructed it on package load, so newly-built packages no longer depend on the broad K25 fallback for trait scope,
  - restored `FnSignature.is_instantiation` during DMIR external-signature decode for concrete `__inst__*` package functions, so package-instantiated method bodies use the correct instantiation visibility mode during method resolution,
  - fixed package-consumer destroy reachability for nested generic/container element types by extending `_seed_destroy_type_graph` to walk `TypeDef.param_types`,
  - fixed package-side generic destructor discovery by using a combined source+package MIR pool for destroy seeding and dispatching discovered destroyers back into the correct needed set,
  - restored full function-key parity checking (fingerprint + package/module/name identity),
  - renamed the compact package-consumer regression slice from `ext-e2e-parity` to `ext-e2e-boundary` for accuracy and removed the non-parity `pkg_vis_source_private_method_rejected` fixture from that target,
  - fixed `Array.reserve(n)` lowering to use total-capacity semantics instead of incorrectly treating `n` as extra capacity, removing false iterator invalidation on no-op reserve calls,
  - corrected `deque_range_sort_binary_search_wrap` to search for an element that is actually present after the fixture’s `pop_front` operations,
  - generalized runtime-drop classification for `DiagnosticValue` containment so values are moved instead of copied when they transitively contain DV-owned runtime resources (direct DV, struct fields, variant payloads, and generic/container param types), fixing the remaining memcheck leak in `diagnostic_value_object_nested_get_no_leak`.
  - reconciled package wrapper-target `return_type_id` values with checker-produced signatures for package-consumer `__wrap_method` emission, fixing `FnResult` part mismatches on generic wrapper returns across the package boundary (for example `Optional<V>` / iterator `next()` wrappers in downstream rpc consumers).
  - resynced `FnInfo.return_type_id` / `FnInfo.error_type_id` with canonicalized signature TypeIds after `compile_stubbed_funcs` mutates signatures in place, fixing a second package-consumer `__wrap_method` `FnResult` mismatch where throw/type-aware checks were comparing stale pre-canonicalization FnInfo ids against canonical signature ids.
  - landed TypeId normalization Phase 1 hardening:
    - added a post-link `FORWARD_NOMINAL` sweep in the package linker so surviving cross-package forward nominals are rebound to already-allocated concrete host nominal ids when available,
    - made external signature construction prefer `tid_map`-derived numeric TypeIds as authoritative for concrete signatures, using `resolve_opaque_type` only as fallback when serialized ids are absent or still forward-nominal,
    - canonicalized external signatures after construction as a safety net,
    - added debug-gated divergence assertions for package-signature vs checker-signature return ids and for `FnInfo` vs signature return/error ids after resync,
    - added `test_ext_sig_preserves_linked_typeids` to pin external-signature TypeId convergence under the package-consumer path.
  - added read-only process environment access as `std.env`:
    - new public stdlib API `std.env.get(String) -> Optional<String>` and `std.env.has(String) -> Bool`,
    - new runtime helpers `drift_env_get` / `drift_env_has` with LLVM declarations and `lang.thread` intrinsics,
    - new e2e coverage for set/unset lookup, raw-helper validity on unset values, boolean presence checks, and signed package-consumer boundary usage.
  - fixed the initial raw env-helper contract leak by removing the invalid `{-1, NULL}` sentinel from `drift_env_get`:
    - unset now returns a valid empty `DriftString`,
    - `std.env.get` uses the two-call pattern (`env_has_raw` then `env_get_raw`) to preserve unset vs empty-string semantics without violating `String` invariants.
  - added narrow prelude visibility for builtin-type methods from core stdlib implementation modules:
    - methods like `String.byte_length()` and core `Array` methods now resolve without explicit import of their defining module,
    - visibility is gated by both builtin/prelude receiver type identity and known stdlib source modules, so user-defined `implement String { ... }` blocks do not gain implicit cross-module visibility.
  - added additive shared JSON support without changing existing `JsonNode` semantics:
    - introduced `std.json.JsonHandle` as a read-only shared root handle backed by `Arc<JsonNode>`,
    - added `std.json.share(JsonNode) -> JsonHandle` and `JsonHandle.clone()` for O(1) whole-tree sharing,
    - added read-only handle accessors and `JsonHandle.encode_compact()` for root-handle caching/use in framework layers,
    - added `JsonNode.clone_deep()` and `JsonHandle.clone_deep()` for explicit O(N) owned subtree/tree extraction without semantic masking,
    - added local and package-consumer regressions covering share/access, clone/read, encode parity, missing access, and deep-copy from both root and borrowed sub-node paths.
- Validation snapshot after convergence:
  - external consumer driver `16/16`,
  - Stage2 `86/86`,
  - checker `33/33` (with known pre-existing exclusions tracked separately),
  - package-consumer e2e green at the convergence checkpoint.
- Compiler version bumped to `0.27.23-dev`; ABI is `5`.

## 2026-03-07 (Option B WIP: package-consumer unification and parity hardening)
- Continued Option B structural work to reduce local vs package-consumer divergence:
  - extracted shared `CompilationUnit`/`_emit_codegen` pipeline pieces in `lang/driftc/driftc.py`,
  - moved package-consumer unit assembly into `_build_package_consumer_unit`,
  - extended package BFS reachability to include `ConstructIface` call edges and seeded required destroy paths via shared resolver.
- Hardened package TypeId remap path:
  - completed `_remap_mir_func_typeids` coverage for previously missing instructions (`CastScalar`, `ConstructIface`, `ConstructIfaceValue`, `CallIndirect`, `CallIface`),
  - added `_validate_remap_completeness` with stale-key + missing-key detection and package type-universe checks (decoded from package type tables),
  - included `fn.local_types` in remap validation.
- Improved package signature/call registration behavior:
  - unified callable-registry registration helper,
  - preserved wrapper exclusion and improved monomorphized/generic dedup handling with normalized receiver matching.
- Added package-consumer e2e infrastructure and new pinned regressions:
  - new runner `lang/tests/codegen/e2e/pkg_consumer_runner.py`,
  - new `just` targets: `ext-e2e-report`, `ext-e2e-smoke`, `ext-e2e-boundary` (ASAN via `DRIFT_ASAN=1` env var),
  - new package-path visibility/trait-scope regressions under `lang/tests/codegen/e2e/`.
- K24/K25 follow-up hardening:
  - preserved `HMethodCall.origin` through stage1 rewriters,
  - restored trait candidate visibility gating while keeping explicit compiler-origin bypasses (`wrapper_call`, `for_iter`, `for_next`),
  - documented current external trait-scope/module-visibility fallback as temporary with DMIR-v1 removal target.
- Additional package/codegen fixes landed in this WIP window:
  - intrinsic `can_throw` FnResult wrapping expanded and centralized in LLVM codegen,
  - fixed package `FORWARD_NOMINAL` canonicalization/impl-type-parameter propagation issues affecting generic method/trait resolution,
  - added primitive `Copy` structural fallback for deterministic scalar/pointer/function/void/ref cases.
- Status snapshot from external-consumer reporting improved substantially during this window (from low-30% range to mid/high-60% raw, low/mid-70% adjusted), with `ext-e2e-smoke` remaining green at latest checkpoints.
- No compiler version bump in this WIP entry (versioning deferred until stabilization point).

## 2026-03-05 (K18 package-consumer preamble reachability correction)
- Fixed deploy/package-consumer codegen regression where forced BFS seeding of entry-wrapper preamble dependencies pulled in unsupported heavy transitive generic instantiations (e.g. `std.runtime::GlobalRegistry::set<...>`), causing LLVM lowering failures in deploy smoke.
- Removed forced preamble dependency seeding from package BFS in `lang/driftc/driftc.py`; entry-wrapper preamble emission now depends on actual lowered MIR availability.
- Added/updated external-consumer regression coverage to pin K18 behavior:
  - when consumer call graph does not naturally reach `std.io::install_process_preamble`, it is not leaked into emitted IR via force-seeding.
- Validated suites remain green:
  - `lang/tests/driver/test_external_consumer.py`,
  - `lang/tests/driver/test_deploy_compiler_hunk_regressions.py`,
  - `lang/tests/driver/test_deploy_stdlib_package.py`,
  - deploy smoke via `just deploy -- --dest ... --python ...`.
- Bumped compiler version to `0.27.11-dev`; ABI remains `4`.

## 2026-03-05 (K16/K17 package-consumer codegen completeness)
- Fixed package-consumer codegen omissions that surfaced as undefined symbols and missing entry wiring in downstream signed-package usage:
  - K16: package wrapper targets (`__wrap_method::*`) referenced by source MIR are now included/synthesized in the package-consumer path so IR does not reference undefined wrapper symbols.
  - K16: deployed/package entry handling now honors parsed `--entry <module>::<name>` values in codegen symbol mapping and emits linkable C `main` wrapper output.
  - K17: entry-wrapper implicit dependency handling now uses a codegen-declared dependency map (`ENTRY_WRAPPER_IMPLICIT_DEPS`) to seed package BFS and compute availability flags from actually-lowered MIR, preventing undefined `std.io::install_process_preamble__impl`.
- Added/extended external-consumer regressions in `lang/tests/driver/test_external_consumer.py` to pin:
  - wrapper symbol completeness in emitted IR,
  - entry wrapper/link completeness (`define i32 @main`, link/run),
  - preamble-symbol resolution in signed-package consume mode.
- Validated collateral remains green across external-consumer, deploy-hunk, and stdlib-package regression suites.
- Bumped compiler version to `0.27.10-dev`; ABI remains `4`.

## 2026-03-05 (K13 boundary nothrow fix)
- Fixed nothrow semantic analysis regression for cross-module/package calls in `lang/driftc/checker/__init__.py`:
  - boundary `HCall` no longer unconditionally poisons semantic throw analysis for explicitly `declared_can_throw=False` callees,
  - method-call wrapper paths now look through `wraps_target_fn_id` so ABI wrappers do not mask underlying nothrow declarations (both `fn_info` and signature fallback paths).
- Preserved ABI boundary behavior (`can_throw` calling convention remains enforced where required); this change is semantic checker behavior only.
- Validated with targeted regressions:
  - K13 direct boundary nothrow call isolation,
  - K13 wrapper look-through isolation,
  - plus no collateral across K7/K11/K12 suites.
- Bumped compiler version to `0.27.9-dev`; ABI remains `4`.

## 2026-03-05 (K11/K12 package variant fixes)
- Fixed two package-consumption regressions affecting downstream projects:
  - K11: tombstone metadata preservation across package type-table linking.
    - `lang/driftc/packages/type_table_link_v0.py` now preserves `tombstone_ctor` when declaring linked variant schemas.
    - This removes false `E-MATCH-NONEXHAUSTIVE ... missing: Tombstone` behavior caused by lost tombstone metadata on linked variants.
  - K12: generic variant constructor inference for package-loaded modules.
    - `lang/driftc/core/type_resolve_common.py` now preserves unresolved generic nominals with known module origin as parameterized `FORWARD_NOMINAL` instead of collapsing to `Unknown`.
    - `lang/driftc/driftc.py` post-link canonicalization now instantiates parameterized forward nominals when concrete bases are available, and canonicalizes signature `param`/`return`/`error` type ids.
- Added targeted regressions in `lang/tests/driver/test_deploy_compiler_hunk_regressions.py`:
  - K11 tombstone/exhaustiveness coverage,
  - K12 positive package variant ctor inference,
  - K12 negative unresolved generic nominal diagnostics.
- Bumped compiler version to `0.27.8-dev`; ABI remains `4`.

## 2026-03-05 (module-qualified ctor fix)
- LANGUAGE_BUG fix: module-qualified constructor calls for package-loaded modules could be incorrectly rejected at parser time with:
  - `module-qualified constructor call 'x.Type(...)' is only supported for structs in v1`
  even when the target type is a real exported struct.
- Root cause:
  - parser-side module-qualified ctor rewrite in `lang/driftc/parser/__init__.py` performed an early `shared_type_table.get_nominal(STRUCT, ...)` check.
  - For external/package-root modules, struct nominals are not guaranteed to be predeclared at that point, causing false negatives.
- Fix:
  - removed the premature parser-time struct-id gate in the module-qualified ctor rewrite path,
  - preserved deterministic rewrite to module-qualified call target and deferred nominal/type validation to later compilation phases where package symbols are fully loaded.
- Regression added:
  - `lang/tests/driver/test_deploy_compiler_hunk_regressions.py::test_k10_module_qualified_struct_ctor_from_package`
  - Builds an external package with `Duration` struct and verifies `conc.Duration(...)` compiles via `--package-root`.
- Bumped compiler version to `0.27.7-dev`; ABI remains `4`.

## 2026-03-05 (deploy prereq fix)
- Downstream rollout fix: deployed toolchain docs/checks now include Python `cryptography` as a required prerequisite for signed-package verification paths.
  - Updated deployed README generation in `tools/deploy/step_bundle.sh`:
    - prerequisites table now lists `cryptography`,
    - environment verify snippet now imports `lark`, `llvmlite`, and `cryptography`.
  - Updated deploy smoke prerequisite gate in `tools/deploy/step_smoke.sh` to require `cryptography` in the target interpreter.
  - Updated wrapper prerequisite note in `tools/deploy/driftc-wrapper.sh` to match runtime requirements.
- Bumped compiler version to `0.27.6-dev`; ABI remains `4`.

## 2026-03-05 (follow-up)
- Fixed a checker regression introduced during template-fingerprint stabilization:
  - restored alias-aware trait/type qualification in `lang/driftc/traits/world.py` (`module_id` or `module_alias`) so trait-guard conjunction scoping remains valid.
- Kept template fingerprint canonicalization strict and local to package fingerprinting:
  - `lang/driftc/packages/provisional_dmir_v0.py` now canonicalizes trait identity for fingerprinting via resolved module identity (`module_id`/default module), not import alias text.
  - This preserves the stdlib deploy fingerprint fix while avoiding global checker behavior drift.
- Deploy CLI ergonomics hardened:
  - `tools/deploy/deploy.sh` now supports `--dest/-d`, `--python/-p`, `--help`, plus `just deploy -- ...` passthrough compatibility and robust path normalization.
  - `justfile` deploy recipe is now a thin passthrough (`deploy *ARGS`) so script and `just` behavior stay aligned.
- Added/validated regressions for both sides:
  - trait guard scoping driver regression (`test_trait_guard_conjunction_adds_scope`) passes,
  - K4 stdlib fingerprint stability/consume regressions remain green.
- Bumped compiler version to `0.27.5-dev`; ABI remains `4`.

## 2026-03-05
- Tightened deploy to ship stdlib as a signed package (`std.dmp` + sidecar) through the staged deploy flow and wrapper package-root path:
  - deploy now builds/signs stdlib package and generates bundled core trust store,
  - deploy smoke test validates compile+run through package verification path,
  - legacy deploy-manifest signing/verify script flow removed.
- Fixed package linking/root-cause compiler issues uncovered by full-stdlib package consumption:
  - type-table linker now seeds generic struct schema fields before instantiation and unifies `FORWARD_NOMINAL` canonical keys with concrete nominal definitions,
  - MIR package TypeId remap now includes `fn.local_types` plus missing instruction families (`MoveOut`, variant addr/tag refs, unchecked/const array ops, raw-buffer ops, pointer ops).
- Hardened template import K4 behavior:
  - structural corruption paths fail with package diagnostics,
  - recoverable `ir_kind`/fingerprint mismatch paths are explicitly observable via notes (no silent skip for these classes).
- Narrowed checker external-signature bypass behavior for receiver validation:
  - bypass now requires both `loc is None` and module presence in `module_packages`,
  - cross-module throw analysis now honors explicit `declared_can_throw=False` on callees.
- Added targeted driver regressions in `lang/tests/driver/test_deploy_compiler_hunk_regressions.py` covering K4/K7/K9 behavior (including negative/positive bypass cases and observability checks).
- Bumped compiler version to `0.27.4-dev`; ABI remains `4`.

## 2026-03-04
- Added signed deployment tooling for isolated downstream compiler/runtime usage:
  - new `just deploy DEST=...`, `just deploy-print-env DEST=...`, and `just deploy-verify DEST=... PUBKEY=...` flows,
  - new deploy scripts under `tools/deploy/` that build a versioned distribution (`bin/lib/doc/examples`), run a smoke compile+run check, produce a hash manifest, sign `manifest.json`, self-verify, then publish with atomic `current` symlink switch.
- Deploy trust model hardening:
  - signing is mandatory (no unsigned deploy path),
  - external trust roots only (no key material shipped in deployed tree),
  - `deploy-verify` checks signature + per-file SHA-256 hashes + unsigned file detection,
  - Ed25519 key type enforced for both signing and verification paths,
  - self-verify is mandatory for both signer modes (custom signer now requires `DRIFT_DEPLOY_VERIFY_PUBKEY`).
- Improved first-time deploy docs for teams/users:
  - added explicit prerequisites and verification steps to deployed `doc/README.md`,
  - clarified no-repo-checkout usage with prerequisite caveats,
  - added hello-world example flow in `examples/`.
- Fixed archive-mode runtime linking robustness in `lang/driftc/driftc.py`:
  - `driftc` now calls `build_runtime_archive(...)` on demand in `DRIFT_RUNTIME_LINK_MODE=archive` instead of failing immediately when `libdrift_rt.a` is missing/stale,
  - this removes the manual prebuild dependency for driver flows and prevents false failures after runtime ABI/version bumps when cache state is out of date.
- Bumped compiler version to `0.27.2-dev`; ABI remains `4`.
- Landed ET persistent registration MVP with bounded fairness/replay across compiler + runtime + stdlib:
  - runtime now uses one-time `EPOLL_CTL_ADD` with persistent `EPOLLET` registration and per-direction watch state (`read_vt`/`write_vt`, `pending_read`/`pending_write`) in `lang/language_runtime/posix/thread_runtime.c`,
  - added ET replay/fairness runtime helpers `drift_reactor_check_pending` and `drift_reactor_io_charge`,
  - wired new intrinsics in `stdlib/lang/thread.drift` and LLVM lowering/declarations in `lang/codegen/llvm/llvm_codegen.py`,
  - stdlib I/O paths in `stdlib/std/net/net.drift` and `stdlib/std/io/io.drift` now use pending-ready replay before park and charge per-VT I/O budget on successful reads/writes.
- Added ET-focused correctness regressions: `et_pending_replay_no_hang`, `et_per_direction_wake`, `et_budget_yield_forward_progress`, and `et_close_no_stale_replay`.
- ABI/versions:
  - bumped compiler version to `0.27.1-dev`,
  - bumped runtime ABI to `4` due to new runtime-exported helper signatures,
  - added driver ABI declaration regression `test_ir_declares_reactor_et_helpers` in `lang/tests/driver/test_abi_version_stamp.py`.
- Benchmarks (optimized, 5000 iters, team baseline run):
  - `Go raw TCP`: `113,636 req/s`, `Go net/http`: `45,871 req/s`,
  - `Drift baseline-vt`: `121,951 req/s`, `Drift baseline-health`: `79,365 req/s`,
  - result: Drift now leads on both measured paths in this benchmark setup.
- Added `docs/design/drift-runtime-targets.md`: documents the current VT runtime support boundary (x86_64 Linux only), how it is enforced (host-based gating in `__init__.py`), why host-based gating is insufficient for cross-compilation, and what target-triple-based selection will require.
- Updated `docs/design/drift-concurrency.md` with a cross-reference noting the concrete implementation boundary of the concurrency model described there.

## 2026-03-03
- Implemented Phase A worker-side polling in `lang/language_runtime/posix/thread_runtime.c` for the single-worker executor case:
  - when the executor run queue is empty, the worker can claim `poll_owner` and call `epoll_wait(...)` directly instead of waiting for the separate reactor->executor handoff,
  - ready I/O VTs can then be resumed directly by the worker on the hot path,
  - the reactor now falls back to timer expiry / shutdown / non-worker poll ownership paths instead of always owning `epoll_wait`.
- Added an explicit poll-owner protocol around `poll_owner` and `in_wait`:
  - worker-side polling publishes `in_wait` before entering `epoll_wait`,
  - `drift_thread_unpark(...)` now wakes a worker that is sleeping in poll mode,
  - reactor and worker poll paths avoid stomping each other's wake state.
- Replaced glibc `getcontext`/`makecontext`/`swapcontext` with a custom x86_64 Linux VT context switch:
  - added `lang/language_runtime/posix/drift_context.h`
  - added `lang/language_runtime/posix/drift_context.S`
  - removed per-switch signal-mask churn from the VT fast path
  - `drift_vt_fiber_entry(...)` now returns via the current worker TLS scheduler context instead of a scheduler context captured at VT initialization time.
- Added a Valgrind-only compatibility path for the VT runtime:
  - runtime detects `RUNNING_ON_VALGRIND` once at executor creation,
  - Valgrind mode falls back to glibc `getcontext` / `makecontext` / `swapcontext` because Valgrind cannot safely interpret the raw `%rsp` manipulation in the custom assembly path,
  - the Valgrind path still returns through the current TLS scheduler context rather than relying on `uc_link`.
- Fixed the Phase A correctness issues uncovered during implementation:
  - worker lost-wake window around queue re-check vs `epoll_wait`,
  - reactor `in_wait` stomping while worker owned poll,
  - reactor shutdown hang when `wake_fd` wake was guarded by `in_wait`,
  - aligned reactor-owned I/O completion (T4a) with worker-owned I/O completion (T4b): watch resolution, timer cancellation, and parked-VT state transition now happen under `r->mu` before enqueue/resume,
  - fixed the spawn-after-poll hang by making `drift_exec_submit(...)` wake a worker sleeping in poll mode and by re-signaling `wake_fd` if the reactor drains a wake intended for the worker during the brief overlap window. Regression: `concurrent_spawn_sequential_batches`.
- Added explicit host-based runtime target gating for the new VT backend in `lang/language_runtime/__init__.py`:
  - current supported host/runtime combination is `x86_64 Linux`,
  - unsupported hosts fail early with a clear runtime-build error,
  - no `ucontext` fallback remains.
- Validation:
  - Phase A stayed within the intended scope (single-worker only, level-triggered epoll, no `EPOLLONESHOT`, no `EPOLLET`, no broader reactor rewrite),
  - benchmark results showed substantial raw VT improvement over the pre-Phase-A baseline in both debug and optimized configurations,
  - targeted concurrent/perf/TCP e2e coverage remained green,
  - ASAN stayed clean on the exercised paths,
  - MEMCHECK validation passes through the Valgrind fallback path.
- Bumped compiler version to `0.27.0-dev`; ABI remains `3`.
- Measured a temporary raw-VT timer-path bypass experiment (`DRIFT_EXP_NO_IO_TIMER=1`) to test whether per-I/O timer-node allocation/cancellation was still a meaningful raw-TCP bottleneck. The experiment skipped `drift_reactor_register_timer(...)` inside `drift_reactor_register_io(...)`, intentionally removing timed-I/O timeout protection only for benchmark purposes. Result: raw loopback moved by only about `0.36 us/iter` (`~2.3%`, within noise), and syscall counts were effectively unchanged. Conclusion: timer-node churn is no longer a meaningful suspect for the remaining raw-TCP gap; the bigger remaining costs are in reactor→executor handoff and epoll control churn. This was an experiment only and is not intended to land as a runtime behavior change.
- Hardened the codegen e2e runner timeout path in `lang/tests/codegen/e2e/runner.py`:
  - added `_disarm_alarm(...)` so late `SIGALRM` delivery during cleanup cannot escape without cancelling the alarm and restoring the prior handler,
  - `_run_case_worker(...)` now always converts timeouts into named `(case, FAIL)` results even when the alarm fires during cleanup,
  - `_run_case_chunk(...)` now catches escaped worker exceptions and records them against the active case instead of crashing the whole future,
  - raised the default per-case timeout from `30s` to `40s` for less brittle parallel e2e runs under heavier build+run load.
- Added targeted driver coverage in `lang/tests/driver/test_codegen_e2e_runner_any.py` for:
  - named timeout reporting,
  - late-timeout cleanup/disarm behavior,
  - non-contamination of the next case in the same chunk,
  - chunk-level exception containment and continued execution.
- Reduced redundant reactor wake traffic in `lang/language_runtime/posix/thread_runtime.c`:
  - `drift_reactor_register_io()` no longer wakes the reactor directly after `epoll_ctl`; timed I/O relies on `drift_reactor_register_timer()` to trigger the wake path when deadline changes matter,
  - added `in_wait` tracking so `drift_reactor_wake()` only writes to the wake `eventfd` when the reactor is actually blocked in `epoll_wait`,
  - reactor loop now publishes/clears `in_wait` around `epoll_wait`, allowing multiple concurrent wakers to coalesce to a single wake write per wait cycle.
- Validation showed a large reduction in worker-side eventfd writes and reactor-side wake-fd drains with no throughput regression on the VT loopback benchmark; this was a focused wake-path optimization with no ABI change.
- Bumped compiler version to `0.25.0-dev`; ABI remains `3`.
- Refined `--optimized` build policy:
  - `--optimized` now produces a clean optimized build (`-O2`) without `-g` and without `--gdb-index`,
  - `--optimized --debug-info` remains the explicit opt-in for debuggable optimized output,
  - compiler provenance/build profile continues to report `optimized` for this mode, while the non-optimized fallback profile remains `default`.
- Updated driver coverage in `lang/tests/driver/test_driftc_wrapper_env_modes.py` to pin:
  - `--optimized` => `-O2` and no `-g`
  - `--optimized --debug-info` => `-O2` plus `-g`
- Bumped compiler version to `0.24.0-dev`; ABI remains `3`.
- Added a bounded per-executor `ExecNode` freelist in `lang/language_runtime/posix/thread_runtime.c` to reuse executor queue nodes across park/unpark cycles:
  - `DriftExec` now keeps a small LIFO freelist of `ExecNode` instances,
  - enqueue paths (`drift_exec_submit`, `drift_thread_unpark`) allocate from the freelist before falling back to `malloc`,
  - dequeue/removal paths recycle nodes back into the freelist under `exec->mu`,
  - executor teardown now drains both the queue and the freelist.
- This reduces hot-path allocator churn in VT scheduling without changing queue semantics or the compiler/runtime ABI.
- Validation showed allocator traffic dropped substantially in the loopback benchmark while throughput stayed effectively unchanged, confirming this is a low-risk cleanup rather than a major throughput win.
- Bumped compiler version to `0.23.0-dev`; ABI remains `3`.
- Added `TCP_NODELAY` control to `std.net.TcpStream`:
  - new stdlib APIs `set_nodelay(enabled: Bool) -> Result<Void, NetError>` and `nodelay() -> Result<Bool, NetError>` in `stdlib/std/net/net.drift`,
  - new POSIX runtime helpers `drift_net_set_nodelay` / `drift_net_get_nodelay` in `lang/language_runtime/posix/io_runtime.c`,
  - new `lang.thread` intrinsics `net_set_nodelay` / `net_get_nodelay`,
  - LLVM lowering/declarations added in `lang/codegen/llvm/llvm_codegen.py`.
- This expands the compiler/runtime boundary with new runtime-exported helper signatures, so the runtime ABI version was bumped to `3`.
- Added e2e coverage:
  - `std_net_tcp_nodelay_toggle`
  - `std_net_tcp_nodelay_roundtrip`
- Bumped compiler version to `0.22.0-dev`; ABI is now `3`.
- Fixed a loop-induced fiber stack overflow in LLVM codegen for interface/callback-heavy paths such as `.on_error()` dispatch:
  - `lang/codegen/llvm/llvm_codegen.py` previously emitted `%DriftIface` allocas in non-entry blocks, so loop iterations accumulated stack space until function return.
  - Added `_ensure_iface_tmp_alloca()` to create a single reusable entry-block `%DriftIface` slot and routed temporary interface construction through it (`_lower_construct_iface`, `_lower_construct_iface_value`, `_lower_iface_upcast`, registry-set call paths, and interface inline-data extraction).
  - This stops per-iteration stack growth for repeated callback/interface lowering inside loops.
- Hardened Linux fiber stacks in `lang/language_runtime/posix/thread_runtime.c`:
  - fiber stacks now prefer `mmap` with a `PROT_NONE` guard page at the low end, converting future stack overflows into clean faults instead of silent heap corruption,
  - added `stack_is_mmap` so stack teardown dispatches correctly to `munmap` vs `free`,
  - if `mprotect` fails, the mmap region is discarded and allocation falls back to `malloc`.
- Added/validated regression coverage through the keep-alive VT loopback benchmark and callback/closure/interface/result-on_error suites; high-iteration `.on_error()` loop paths no longer overflow the fiber stack.
- Bumped compiler version to `0.19.0-dev`; ABI remains `2`.
- Added Linux-backed secure random bytes support:
  - new stdlib API `std.random.random_secure_bytes(n: Int) -> Result<Array<Byte>, RandomError>`
  - new `RandomError { tag: String, errno: Int }` diagnostic type
  - wrapper validates negative length in stdlib, returns `Err("invalid-length", 0)`, returns `Ok([])` for zero length, and maps runtime failures to `Err("os-random-failed", errno)`
- Added byte-fill runtime helper `drift_random_fill` implemented via `getrandom(2)` with EINTR retry loop (`lang/language_runtime/random_runtime.c`, `random_runtime.h`), exposed to stdlib through interim `lang.thread` unsafe intrinsic `random_fill`.
- This changes the compiler/runtime boundary by adding a new runtime-exported helper signature, so the runtime ABI version was bumped to `2`.
- Added e2e coverage:
  - `std_random_secure_bytes_basic`
  - `std_random_secure_bytes_invalid_len`
  along with the previously reviewed byte-array-init regressions used by the implementation.
- Bumped compiler version to `0.15.0-dev`; ABI is now `2`.
- Added expression-form `unsafe { expr }` to the language:
  - grammar now supports `unsafe_expr: UNSAFE value_block` in expression position
  - parser/stage0/stage1 introduce `UnsafeExpr` / `HUnsafeExpr`
  - type checker propagates `unsafe_context=True` through the expression body/result and preserves existing `--allow-unsafe` gating
  - MIR lowering treats `HUnsafeExpr` as a transparent block-then-result expression
- Updated all affected HIR walkers/rewriters/validators to descend into `HUnsafeExpr`, including borrow materialization, place canonicalization, normalization, capture discovery, lambda validation, non-retaining analysis, typed HIR validation, checker walks, and `driftc.py` helper traversals.
- Closed a pre-existing test harness blind spot in `rawbuffer_read_write`: `expected.json` used `"expect"` instead of `"exit_code"`, so the parse failure from missing expression-form `unsafe` was passing vacuously. The test now genuinely exercises value-position unsafe calls.
- Added e2e coverage:
  - `unsafe_expr_basic`
  - `unsafe_expr_requires_flag`
  - fixed `rawbuffer_read_write`
- Bumped compiler version to `0.14.0-dev`; ABI remains `1`.
- Fixed a type-system ownership bug in `lang/driftc/core/types_core.py`: structural `Copy` analysis could misclassify `Destructible` structs as `Copy` if their field layout looked scalar/copyable (for example `Arc<T>` wrapping `RawBuffer`). This caused MIR scope-exit drop glue to be skipped for enclosing structs and leaked destructor-managed resources.
- Hardened `TypeTable` copy/drop classification in three places:
  - `_is_copy_structural(...)` now rejects `Destructible` structs before field-recursive structural analysis.
  - `copy_status(...)` now rejects `Destructible` types before structural fallback when the copy query hook defers.
  - `define_struct_fields(...)` now invalidates `_needs_drop_cache` when placeholder field types are replaced by concrete types, preventing stale no-drop results.
- Root-caused and fixed the Arc-in-struct leak shape reported by the web framework team: a struct containing `Arc<T>` and later accessed through its field path could be misclassified as `Copy`, preventing destructor emission and leaking the Arc backing allocation. Regression: `arc_struct_field_get_drop_leak`.
- Added direct driver-level contract coverage in `test_destructible_not_copy_contract.py`:
  - `test_arc_copy_status_is_false`
  - `test_struct_containing_arc_copy_status_is_false`
  - `test_destructible_implies_not_copy`
  These pin the invariant `is_destructible(T) => copy_status(T) is not True`.
- Bumped compiler version to `0.13.0-dev`; ABI remains `1`.

## 2026-03-02
- Fixed can-throw interface call lowering for `Void` returns (`lang/driftc/stage2/hir_to_mir.py`): `_lower_iface_call` previously checked `is_void(user_ret_type)` before `can_throw`, returning `None` for can-throw `Void` interface calls instead of an `FnResult<Void, Error>` wrapper. Reordered the logic to handle `can_throw` first, matching the indirect-call path. Regression: `iface_canthrow_void_stmt`.
- Fixed SSA type-environment propagation for `CallIface` in `lang/driftc/checker/__init__.py`: interface-call destinations were previously left `Unknown` because `build_type_env_from_ssa` handled `Call` and `CallIndirect` but not `CallIface`. This broke downstream `ResultErr` / `ConstructResultErr` typing and throw-check validation. Regression: `iface_canthrow_err_propagation`.
- Fixed a pre-existing Python shadowing bug in `lang/driftc/driftc.py`: local `import sys` statements inside `compile_stubbed_funcs` could trigger `UnboundLocalError` on earlier `sys.stderr` debug prints. Debug-only imports are now aliased.
- The above fixes root-caused and cleared `std_net_tcp_stress_connections`.
- Bumped compiler version to `0.12.0-dev`; ABI remains `1`.

## 2026-03-01
- Added call-boundary shared reborrow ergonomics in the checker/type checker: immediate call arguments of type `&mut T` are now accepted where parameters require `&T`, including callback dispatch paths. The implementation is intentionally narrow and limited to call argument matching (`lang/driftc/checker/__init__.py`, `lang/driftc/type_checker.py`); no lowering or ABI changes were required.
- Added positive e2e coverage for shared reborrow at direct call sites and callback calls: `reborrow_mut_to_shared_call_site`, `reborrow_mut_to_shared_callback`.
- Added negative soundness regression `reborrow_mut_through_shared_ref_rejected`, pinning that mutable sub-borrows through shared references remain rejected (`cannot take &mut through *f unless f is a mutable reference`).
- During validation, return-position `&mut T -> &T` coercion was confirmed to be pre-existing behavior rather than introduced by this patch; this update only formalizes the immediate call-boundary case.
- Bumped compiler version to `0.11.0-dev`; ABI remains `1`.

## 2026-02-28
- Completed `std.regex` v1 Phase D/E closeout:
  - Added `replace_first` and `replace_all` (literal replacement, non-overlapping left-to-right).
  - Added `_find_from` (offset-based first-match search) and `_substr` (nothrow byte-slice helper for replacement assembly).
  - Enforced zero-length-match forward progress in `replace_all` by advancing one byte on empty matches to prevent infinite loops.
- Added replacement stress guards in `std_regex_replace`: long-input replace-all, greedy whole-string replacement, repeated pair replacements, empty-pattern insertion behavior, zero-length `a*` progression, and replace-first-on-long-input.
- Completed regex hardening passes with clean instrumentation runs (no ASAN errors and no memcheck leak/error exits) across regex + charclass + regression suites.
- Marked `work/regex-stdlib/plan.md` phases A–E and completion criteria as complete.
- Added gotcha-focused conformance/stress coverage for `std.regex`:
  - `std_regex_gotchas_greediness`
  - `std_regex_gotchas_class_edges`
  - `std_regex_gotchas_utf8_offsets`
  - `std_regex_stress_compile_growth`
  - `std_regex_stress_adversarial`
- Frozen contracts pinned by tests and plan updates:
  - match selection: leftmost-longest
  - quantifiers: greedy only (`*`, `+`, `?`), no lazy mode in v1
  - class edge rules for `-`, `]`, `^`
  - byte-offset semantics for matching/replacement behavior
- Bumped compiler version to `0.10.0-dev`; ABI remains `1`.

## 2026-02-26
- Added UTC build timestamp (`build_utc`) to compiler provenance string embedded in every compiled binary (`@__drift_compiler_build`).
- Added `std.meta::compiler_info()` compile-time intrinsic that returns the full provenance string (version, ABI, word size, git SHA, build profile, build timestamp) as a `String`.
- Moved `emit_compiler_provenance()` before function lowering so the intrinsic and the embedded constant carry identical content.
- Bumped `DRIFTC_VERSION` to `0.7.0-dev` (new intrinsic = API surface change).
- Added e2e coverage: `lang/tests/codegen/e2e/std_meta_compiler_info/` (substring checks for all provenance fields).

## 2026-02-27
- Fixed Array `push`/`insert` move semantics for non-Copy element types (`lang/driftc/stage2/hir_to_mir.py`): the intrinsic lowering used `lower_expr` (bare `LoadLocal` bitcopy) for the value argument instead of `_lower_call_arg` (which emits `MoveOut` to tombstone the source). This left both the source local and the array element owning the same heap data; scope-drop of the source then freed memory still referenced by the array, causing double-free crashes on any subsequent array drop or element access. Regression: `array_push_move_non_copy_implicit`.
- Fixed match-on-struct-field double-free for non-Copy scrutinees (`lang/driftc/stage2/hir_to_mir.py`): when the match scrutinee is a field access on a local (e.g. `match re.root`), `StructGetField` extracts the variant as an SSA copy without moving ownership from the struct. Each match arm copies the scrutinee into its own temp for binder extraction, but the original struct local still holds the same value. The field in the owning struct's storage is now tombstoned (zeroed via `AddrOfLocal` → `AddrOfField` → `ZeroValue` → `StoreRef`) after extraction, preventing double-free when the struct is later dropped. Covered by `std_regex_parser_corners` (match on `re.root` patterns).
- Added `std.regex` standard library module (`stdlib/std/regex/regex.drift`, 918 lines): recursive-descent parser, NFA compiler with pre-computed sizes and absolute jump targets, Thompson NFA executor with epsilon-closure simulation. Public API: `compile`, `is_match`, `find_first`. AST types: `RegexNode` variant (Literal, Dot, Anchor, Class, Group, Alternation, Repeat), `CharClass`, `CharRange`, `Quantifier`, `AnchorKind`. Supports `.`, `^`, `$`, `[...]`/`[^...]` character classes with ranges and escapes, `*`/`+`/`?` quantifiers, `(...)` grouping, `|` alternation, `\d`/`\D`/`\w`/`\W`/`\s`/`\S`/`\t`/`\n`/`\r` escapes, metachar literal escapes. E2e coverage: `std_regex_compile_valid`, `std_regex_compile_errors`, `std_regex_is_match_semantics`, `std_regex_quantifier_behavior`, `std_regex_anchor_behavior`, `std_regex_class_escape_behavior`, `std_regex_find_first_offsets`, `std_regex_zero_length_progress`, `std_regex_parser_corners`.
- Added `std.text` character classification helpers: `is_digit`, `is_alpha`, `is_alnum`, `is_space` — ASCII-only, `nothrow`, pure `Byte -> Bool`. E2e coverage: `std_text_charclass_helpers`.
- Bumped compiler version to `0.9.0-dev` for behavior-changing ownership fix (Array push/insert move semantics) without ABI boundary changes (`DRIFT_RT_ABI_VERSION` remains `1`).
- Fixed Stage 1 match-arm alpha-renaming (`lang/driftc/stage1/ast_to_hir.py`): `_rename_stmt` was missing the `HLoop` case and `_rename_expr` was missing `HCast`, `HResultOk`, `HDVInit`. Match binder references inside while-loops and cast expressions in arm bodies were not renamed to their mangled `__match_binder_N_xxx` forms, producing spurious `unknown name` errors. Regression: `match_qualified_binder_local`.
- Fixed LLVM codegen `ConstructVariant` Byte-field type mismatch (`lang/codegen/llvm/llvm_codegen.py`): MIR can pass a `VariantGetFieldAddr` result (`i8*` pointer) directly to `ConstructVariant` which expects an `i8` value. Added auto-load when `have == want*` to bridge the pointer-to-value gap. Regression: `variant_byte_payload_basic`.
- Added `std.text` character classification helpers: `is_digit`, `is_alpha`, `is_alnum`, `is_space` — ASCII-only, `nothrow`, pure `Byte -> Bool`. These replace three private `_is_digit` duplicates across `std.parse`, `std.time`, `std.json` and one private `_is_ws` in `std.json`; dedup will follow in a cleanup pass.
- Added e2e coverage: `std_text_charclass_helpers/` (positive/negative byte boundary tests for all four helpers).
- Added `std.text.TextError` struct (with `Diagnostic` impl) and `substring(s, start, len)` function for byte-level substring extraction with overflow-safe bounds checking.
- Added `std.meta.CompilerTag` struct (key/value accessors) and `compiler_info_pairs()` pure-Drift parser that splits the `compiler_info()` provenance string into structured key-value pairs.
- Froze the `compiler_info()` grammar as a formal contract: `item (" | " item)*` where each item is `<key> <value>` with `[a-z_]+` keys. Added `test_compiler_provenance_grammar` golden test validating the contract on every compiled binary.
- Added e2e coverage: `std_text_substring/` (happy path slices + error paths) and `std_meta_compiler_info_pairs/` (array length, deterministic key order, non-empty assertions).
- Bumped compiler version to `0.6.0-dev` for behavior-changing toolchain fixes without ABI boundary changes (`DRIFT_RT_ABI_VERSION` remains `1`).
- Fixed cross-module drop resolution for `FORWARD_NOMINAL` type references that could suppress nested payload destruction in variant/array paths.
  - `TypeTable.has_drop()` now resolves `FORWARD_NOMINAL` before drop classification.
  - LLVM drop paths now resolve `FORWARD_NOMINAL` consistently in `_type_needs_drop`, `_emit_drop_value`, `_ensure_array_drop_helper`, and helper-local `emit_drop`.
- Added regression coverage for cross-module owned payload lifecycle in `lang/tests/codegen/e2e/variant_match_loop_owned_payload_leak/` with alloc tracking and loop/skip/drain patterns to pin the leak class.

## 2026-02-25
- Language enhancement batch (branch `lang-enhancment-260224`): Items 1-4 from crypto implementation feedback.
- Item 1: Checker now infers `Uint` result types for binary operations (shift, bitwise, arithmetic) parallel to existing `Int` inference, eliminating forced `: Uint` annotations.
- Item 2: Match binder payload types now propagate correctly in checker context, enabling direct indexed access on variant/result payloads.
- Item 3: Added `u`-suffix for Uint literals (`42u`) and `const` array support (`const K: Array<Uint> = [...]`) backed by read-only LLVM globals.
  - Full pipeline: grammar (`UINT_LIT`) → AST (`UintLiteral`) → HIR (`HLiteralUint`) → MIR (`ConstUint`/`ConstArray`) → LLVM codegen.
  - `_UintConst` tagged wrapper preserves Uint origin through const evaluation (prevents type erasure to Int).
  - Strict literal range validation: Uint bounded by target word size (`TypeTable.uint_max`), Uint64 bounded by `[0, 2^64-1]`.
  - Grammar: replaced `SIGNED_INT` with unsigned `INT` terminal to fix no-space subtraction (`5-1`, `5u-1u`).
  - Shared `validate_const_value()` in `types_core.py` eliminates parser/checker const validation duplication.
  - 18 e2e tests: 7 positive (suffix, const arrays, nospace subtraction, max boundary, cast truncation pin) + 11 negative (overflow, type mismatch, empty array, non-literal, string elements).
- Item 4: Stdlib cleanup — removed `_ror32` forced type annotations, replaced 64-branch `_sha256_k()` if-chain with `const SHA256_K: Array<Uint>` table lookup, eliminated `U32_MOD` constant (replaced with bitwise `& U32_MASK` / `>> 32`). All 10 crypto/codec e2e vectors pass byte-identical.
- Spec updates: added Numeric Conversion Policy (`docs/design/spec-change-requests/drift-numeric-conversion-policy.md`) and applied to `drift-lang-spec.md` — §2.y (cast semantics), §3.1.1 (conversion policy), §3.9 (const rules, u-suffix, const arrays). Uint64 exempted from fixed-width reservation for user code.
- ABI version stamping: link-time guard between compiler and runtime. Single source of truth in `lang/driftc/driftc_versions.py` (`DRIFT_RT_ABI_VERSION = 1`). Runtime exports `__drift_rt_abi_version_N` symbol (all archive variants). Codegen entry wrapper emits required call to same symbol. On mismatch, linker fails with unresolved symbol instead of runtime crash. Driver appends compatibility hint when detecting ABI mismatch in linker output. Documented in `drift-lang-abi.md` §7.1. Regression tests: IR presence, mismatch link failure, hint detection (`test_abi_version_stamp.py`).
- Added `driftc --version` (`-V`): outputs compiler version, ABI version, git SHA, license, and supervising body. Version constants (`DRIFTC_VERSION`, `DRIFT_RT_ABI_VERSION`) live in `lang/driftc/driftc_versions.py`.
- ABI stamping plan closeout (`work/compiler-ver-stamping/plan.md`): Phases A-D completed plus `§11` (`--version`). Kept Phase C hint regression at predicate-level (real mismatch stderr contains `__drift_rt_abi_version_`) by design; full driver interception test was intentionally deferred due to disproportionate harness complexity.
- Finalized stamp scope for test ergonomics: production/runtime-linked entrypoint paths are stamped; helper-only bare-clang IR paths remain unstamped to avoid forcing runtime archive linkage in low-level LLVM tests.
- Added `std.time` epoch accessors for JWT NumericDate support:
  - `utc_unix_seconds(&UtcTimestamp) -> Int`
  - `utc_unix_seconds_now() -> Int`
  - `utc_unix_millis(&UtcTimestamp) -> Int`
  with signed UTC semantics and deterministic truncation-toward-zero behavior for seconds conversion.
- Added e2e coverage: `lang/tests/codegen/e2e/std_time_epoch_accessors/` (fixed timestamp checks, `now()` plausibility, pre-1970 negative epoch conversion, epoch boundary).

## 2026-02-18
- Added initial `std.cli` argument parser API (`ArgParser`, `ParsedArgs`, `CliError`) with flags, string/int options, required-option enforcement, positional arguments (including trailing multiple), `--` terminator handling, and deterministic error tags for invalid/duplicate/missing inputs.
- Added CLI e2e coverage for both success and rejection paths: basic parse flow, help request, unknown option, missing required option/positional, invalid int option, duplicate option, unsupported short clusters, and double-dash positional mode.
- Fixed method-signature metadata classification so only receiver-bearing impl functions are tagged `is_method`, while impl-associated static functions still preserve `impl_target_type_id` for qualified-static resolution.
- Updated type/call resolution to match the metadata contract (`impl_target_type_id`-based qualified static lookup), and validated with driver/e2e regressions for positive and negative qualified-static behaviors.
- Added runnable CLI example `examples/cli/main.drift` and documented `std.cli` usage in `docs/effective-drift.md` with API-accurate parse/access patterns.

## 2026-01-06
- Optional consolidation: inventoried Optional-specific logic, locked arm order `None=0`/`Some=1`, and removed MIR OptionalIsSome/OptionalValue ops across stage2/ARC/codegen.
- DiagnosticValue Optional ABI pivoted to out-params + bool return; removed DriftOptional* runtime structs; updated DVAs* lowering/tests; aligned DV ctor ABI (isize for int, i8 for bool) and eliminated uninitialized out-param loads.
- Bool storage/value split hardened across structs/arrays/refs/FnResult/ABI boundaries, including FnResult ok `Bool` stored as i8 with encode/decode at ResultOk/ConstructResultOk.
- Struct sizing/align now uses StructInstance field types; Byte seeded in builtin priming to stabilize TypeIds; StringCmp cast on 32-bit fixed (no invalid bitcast).
- FnPtrConst now requires signature metadata and avoids unsafe fallback casts; ZeroValue pointer SSA handling restored after removing redundant bitcasts.
- Array lowering: ArrayLit insertvalue fix, CopyValue insertion for Copy-but-not-bitcopy elements (String), Array<String> literal retain test added, and ZST arrays are asserted unsupported in codegen.
- Optional base seeding now on-demand in stage2; Optional caches in checker/link/driver switched to canonical variant instantiation (no new_optional cache).
- Iterator intrinsics: iterator struct layout is `&Array<T>, idx`, iter() auto-borrows Array places, next() accepts place receivers, inserts CopyValue for Copy elements, and guards negative idx before Uint conversion.
- TypeTable declare_struct/declare_variant + define_struct_fields made idempotent with schema/field mismatch errors for determinism.
- Stage2 iter/next misuse now recovers with Unknown in non-strict mode (asserts in strict), matching checker diagnostics.

## 2025-12-28
- Added explicit prelude controls: `--no-prelude` disables implicit `lang.core` import, while explicit `import lang.core` still works via injected prelude exports; tests cover default/prelude-disabled behavior.
- Updated prelude injection to be conditional on explicit imports when disabled, and wired prelude exports into the external export surface so module-qualified calls resolve deterministically.
- Clarified exception matching: unqualified `catch EventName` resolves to the current module’s event, while `catch mod:EventName` targets other modules; spec updated.
- Driver ABI boundary test now passes module exports/deps into the LLVM test helper to preserve cross-module visibility.
- Removed `source_location()` and `SourceLocation` from the `lang.core` prelude spec until an intrinsic implementation exists.

## 2025-12-07
- Cleaned up the lang front-end slice: parser copy lives in `lang/parser/` with a table-driven adapter (`parse_drift_to_hir`); RaiseStmt for `throw` maps to ThrowStmt, unsupported stmts fail loudly; AstToHIR ThrowStmt uses the canonical `value` field. Adapter tests cover Ok sugar, attr-call non-rewrite, raise→throw, and unsupported constructs.
- LLVM backend tests relocated to `lang/codegen/llvm/tests/`; `just lang-codegen-test` now runs them there, still cleaning `build/tests/lang` and running IR/e2e runners.
- Drift-source e2e runner documents current simple_return case; codegen path remains SSA-first with wrapper helpers (`compile_to_llvm_ir_for_tests`) reusing the same SSA as throw checks.
- Work-progress notes updated accordingly; refactor considered complete, ready to tackle FnSignature enrichment and additional e2e coverage next.
- FnSignature/FnInfo enrichment: TypeId fields are now primary (param_type_ids/return_type_id/declared_can_throw/flags/error_type_id); FnInfo owns the signature and inferred_may_throw. The checker prefers pre-resolved TypeIds in signatures, falls back to legacy raw resolution only when missing, defaults declared_can_throw from throws_events, and uses TypeKind.FNRESULT for try-sugar checks. A shallow HIR walk marks inferred_may_throw and diagnoses missing throws. All tests remain green.
- Added a minimal type resolver (`lang/type_resolver.py`) that builds a shared TypeTable and FnSignatures from declared types; `compile_stubbed_funcs` resolves signatures from HIR (via a fake decl shim) when none are provided, keeping the pipeline TypeId-first even before a real checker lands.
- Threaded the resolver’s TypeTable everywhere and guarded against TypeId-carrying signatures without a shared table; string TypeId is shared across HIR/SSA. The parser adapter returns the shared table for Drift-source paths.
- Call checking now uses shallow arg type inference (literals, simple calls, Result.Ok, basic ops) to enforce arity and simple param-type equality; added driver tests for mismatched Bool→Int (diagnostic), matching Int call (no diagnostic), and synthesized FnResult<Unknown, Error> via Result.Ok with no signature TypeIds (no crash/diagnostic).

## 2025-12-01
- Hardened exception event codes: `Error.code` is `I64` with a 4+60 bit layout (kinds + payload), user exceptions hash their FQN via xxHash64, per-module collisions are rejected, and metadata now records FQN/kind/payload/event_code for future export.
- Updated runtime dummy helper to carry code + first payload; added C runtime tests to validate kind/payload packing across multiple inputs. `test-runtime-c` runs via `CLANG_BIN` (default clang-15).
- Try/catch expression dispatch fixed to use `I64` constants; added SSA programs for expr event dispatch (single and multi catch) to pin the lowering.
- Extended `drift-abi-exceptions.md` with the explicit kind/payload bit layout and reserved kinds, aligning checker/runtime/backend to the ABI.

## 2025-11-30
- Added deterministic event codes to exception definitions and wired SSA try/catch dispatch (stmt + expr) to project `ErrorEvent` and branch to matching catches with catch-all fallback/rethrow semantics; both stmt and expr event-catch tests now run end-to-end.
- Introduced a first-class `ErrorEvent` MIR instruction, SSA helper, and LLVM lowering that calls `drift_error_get_code`; the dummy runtime and a new SSA test (`mir_ssa_error_event_test`) prove the projection end-to-end.
- SSA backend now asserts MIR-provided can-error markings and uses `{T, Error*}` / `Error*` ABI for throws and call edges; SSA/e2e runners link against `error_dummy` by default for error-edge coverage. SSA e2e subset now covers hello/throw + try/catch + event dispatch via `test-e2e-ssa-subset`.
- try/catch work-progress document refreshed to mark Phase 1 (event dispatch) complete and capture current SSA e2e subset coverage.

## 2025-11-29
- SSA-first pipeline is now the only maintained path: `driftc` lowers every function to SSA, runs the simplifier and strict verifier, and the e2e runner uses the new SSA→LLVM backend to compile, link, and run real programs (hello, pure calls, console writes, structs by ref, arrays/for loops). Legacy lowering/codegen is deprecated.
- SSA→LLVM backend now handles multi-function modules, control flow with PHIs from block params, pure calls, string literals, console runtime calls, struct init/field get/set using checker-provided layouts, array len/get/set and stack array literals, and word-sized `Int` mapping (Int/Int64 → i64, Int32 → i32, Bool → i1, String → `DriftString`).
- Array lowering generalized: `{len: Size, data: T*}` works for any element type the backend can map (not just ints). Added a run-mode e2e `array_string` to prove `Array<String>` works; array literals now build a stack buffer of the element LLVM type and assemble the header. Bounds/len access reuse the same layout; unsupported element types hard-error.
- Checker alignment: array indices are `Int` (word-sized) for both reads and writes; SSA smoke/programs updated to use `Int` instead of `Int64`. Negative index/field tests updated to the new messages.
- Struct support tightened: `StructLayout` is threaded from the checker into SSA codegen; field get/set pick the correct layout via per-function SSA type maps (no cross-struct field-name guessing). Ref-struct mutation runs in e2e via SSA→LLVM with stack slots for struct locals.
- Tests/e2e: SSA-only smoke/program suites are green; run-mode e2e covers hello, pure calls, console writes, control flow, arrays/for, struct mutation, and array-of-strings. Added a compile-fail `bad_index` with the updated `Int` expectation. Simplifier runs in the SSA path by default.

## 2025-11-26
- Retired the legacy interpreter path: test runner no longer executes `drift.py` runtime programs; focus is MIR+codegen only.
- Revised borrowing syntax to `&T` / `&mut T` with global lvalue auto-borrowing and no borrowing from rvalues; receivers now use `self: T` / `self: &T` / `self: &mut T` throughout the spec and examples. Grammar updated to accept the new reference types and borrow expressions; lambda params now use explicit `copy` capture instead of reusing general params. Removed legacy `ref` spellings (now invalid), refreshed README examples, and aligned the iterator doc to the new syntax.
- Archived former interpreter tests under `tests/legacy/` and duplicated them as `tests/mir_codegen/runtime_*` stubs so they can be turned back on once I/O and strings are supported in codegen.
- Updated numeric primitives in the spec: natural-width `Int`/`Uint`/`Size`/`Float` as defaults, fixed-width `Int8`…`Int64`/`Uint8`…`Uint64`/`F32`/`F64` for explicit widths; added overflow/convert rules, mandated `Size` for lengths/indices, and added FFI numeric mapping guidance (C widths → Drift types) plus an FFI wrapper pattern. Array helpers now use `Size` for indices.
- Added string runtime stubs (`DriftString` layout + constructors/concat/free/to_cstr`) in the codegen runtime, and ported legacy runtime samples into `tests/mir_codegen/runtime_*` (currently skipped until string/console codegen lands).
- Hardened string runtime: lengths use `uintptr_t`, helpers always include trailing NUL, added an empty-string helper, C++ guards, and crash-fast on malloc failures to keep ABI consistent with the numeric/size rules.
- Lowered `throw` of an exception constructor into a real `Error*`: pick `msg` kwarg/first positional or fall back to the exception name, call builtin `error_new`, and raise that pointer. Added an `error_new` builtin signature/stub for interpreter parity.
- MIR→LLVM now seeds successor environments correctly and treats helper calls (`error_new`/`error`) as returning bare `Error*` while other calls with error edges expect `{T, Error*}`. This fixed undefined-SSA issues in the emitter.
- Switched codegen to PIC/PIE: LLVM target machine uses `reloc="pic"`, C stubs/harness are built with `-fPIC`, and we link with `-pie` so we no longer need `-no-pie` or see text-relocation warnings.
- Unskipped the error-path codegen test and added a success-path sibling (`tests/mir_codegen/error_path_ok`) so both error and non-error return flows are exercised end-to-end.
- Defined a stable Error C ABI in the spec (UTF-8 strings, attrs/frames layout, ownership rules) and wired runtime stubs to match (`drift_error_new`, owned diagnostics, no static buffers). `throw` lowering now targets `drift_error_new`, and MIR→LLVM treats it as returning `Error*`. Added try/else and try/catch codegen cases and updated MIR goldens accordingly; all codegen tests pass.
- Added frame-array plumbing: lowering captures throw-site frames (file basename, func, line) and passes them to `drift_error_new`; MIR→LLVM supports string/int64 array init; runtime stubs store frames and free them; added `error_push_frame` hook for future deeper stacks. Added attr-array codegen tests (including large sets) to validate deterministic attrs and frame handling. Spec now notes the hidden ctx must never affect the public C ABI.
- Extended MIR lowering to handle `try/catch` statements: errors in the try body branch to a catch block (binder typed as `Error`), with a new MIR golden `tests/mir_lowering/try_catch.mir` covering the shape.
- Calls now always branch on `{T, Error*}` with explicit normal/error continuations: lowering wraps calls with normal/error blocks and a join, and the error path forwards to enclosing handlers so outer `try/catch` can intercept. Caller frames use call-site source lines, and throw-site frames use source basename/function/line. Added deep-frame codegen cases (`frames_chain`, `frames_one/two/three`) and domain default/override tests to exercise propagation.
- Added module declarations and threaded module IDs through checking/lowering/MIR→LLVM so frame metadata now includes modules (plus file/func/line); frame files were normalized to basenames. Updated the C error ABI (`struct Error`, `drift_error_new`, `error_push_frame`) and frame codegen tests to capture/print module-aware stacks.
- Error diagnostics now emit valid JSON with event/domain/attrs and a `frames` array of `{module,file,func,line}` objects; the runtime builds the string lazily in `error_to_cstr`. Exception `domain` parsing now unquotes string literals so domains print without double quotes. Updated the codegen expectations for attr arrays, error paths, and frame tests to the new JSON shape.
- Added per-frame captured locals to the error payload: runtime ABI now carries `cap_keys/values` and per-frame counts, lowering threads `^` bindings into throw/call error paths, and `error_to_cstr` emits `captured:{...}` per frame. Added a multi-level codegen test (`tests/mir_codegen/frames_captures`) to assert captured locals across the stack; updated frame expectations accordingly.
- Enforced canonical module IDs at compile time (lowercase alnum with dots/underscores, no leading/trailing/consecutive separators, ≤254 UTF-8 bytes, reserved prefixes blocked); frames record declared module IDs only. Added negative tests for invalid/reserved module IDs and documented import aliasing (aliases do not affect frame metadata). Fixed linter spacing in the new module tests.
- Added `while` loops end-to-end (grammar, parser, checker, interpreter, MIR lowering), plus runtime tests for simple, nested, and nested-with-try/catch loops. Import alias support was added to the grammar/spec.
- Added `break`/`continue` tokens to the grammar and loop-scoping checks in the checker; added a nested while+try/catch runtime test (`while_try_catch.drift`). Documented control-flow (if/else, ternary, while, try/else, try/catch) in the language spec.
- Expanded reserved keywords: checker now rejects a broad set (language/FFI/legacy keywords, lowercase primitive aliases); added a negative test for using a reserved word as a function name; spec reserved-keyword list updated.
- Clarified that `Bool` lowers to LLVM `i1`; kept `true`/`false` as lowercase literals, so constructs like `while true { ... }` parse and type-check as expected. History/spec updated to reflect the current control-flow and reserved keyword rules.
- Reordered the language spec chapters for better definition-before-use flow (traits/interfaces early, variants before exceptions/null safety, arrays/collections grouped, standard I/O moved later) and renumbered chapters sequentially without duplicates.
- Error edges fully integrated end-to-end: `throw` now lowers through MIR→SSA→LLVM using the `{T, Error*}` / `Error*` ABI, and new e2e tests (`throw_try`, `void_throw`) cover both value+error and void can-error paths. SSA call-edge tests now include throwing and void callees.
- Generic SSA lowering now handles can-error functions without hand-crafted MIR: special cases for `may_fail_error`, `may_throw`, and `maybe_fail` were removed, relying on normal lowering plus can-error tagging.
- MIR `Function` now carries a `can_error` flag propagated from throws and call-with-edges; SSA codegen enforces can-error invariants (returns carry error operands, calls-with-edges only target can-error functions, plain calls to can-error functions are rejected).
- SSA lowering handles `RaiseStmt` and prunes unused join blocks in `if` lowering; throw lowering uses safe placeholder values in pairs.
- Added FFI-based plugin stance to the spec, clarified static modules/exports as can-throw entry points, and tightened DMIR export semantics to cover static modules only. Drift-native plugin ABI was removed in favor of FFI guidance.

## 2025-11-24
- Fixed the parser’s `if` builder to grab the nested `else_clause` block, so conditional statements with an else arm are preserved through parsing and lowering.
- Extended straight-line MIR lowering to handle `if/else` control flow (joins only when needed) and to reject functions that fall off without a return. Added a MIR golden for `if_else` in `tests/mir_lowering/` to cover the path.
- Aligned ternary lowering with a typed phi param at the join and updated the expected MIR formatting to match the printer/block ordering.
- Documented the FFI callback rules in the spec: only non-capturing functions cross the C ABI as callbacks; captured closures are not auto-boxed and require an explicit, manual state+trampoline if ever needed. Added a note for callbacks returned from C: treat function pointers (and optional ctx) as borrowed, enforce cdecl, block unwinding into C, and don’t assume ownership of ctx unless the API says so.
- Clarified destructor semantics in the spec: deterministic RAII at end-of-liveness (scope exit, early return, or consumption), move-only by default to avoid double drops, and copies only for `Copy` types with a defined copy+drop story.
- Clarified interface ownership: owned interface types should require `Destructible` so vtables always expose a drop slot; borrowed interface views omit destruction.
- Added a DMIR vtable section: interface values are fat pointers `{data, vtable}`; owned views require `Destructible` and dispatch `drop` via the vtable, borrowed views omit the drop slot, and vtable ordering is stable across inheritance (base entries first).
- Clarified multi-interface vtables: each interface gets its own per-type vtable; no merging across interfaces. Inheritance keeps base entries (including drop) at fixed offsets.
- Noted that a concrete type has a single destructor; every owned interface vtable for that type points its drop slot to the same concrete drop, so dropping via any interface dispatches identically.
- Stated explicitly in the spec: no class/struct inheritance; composition + traits + interfaces replace it to keep layout/ABI stable and avoid fragile-base/diamond issues.
- Added a closure preview to the spec: `|params| => expr` syntax with implicit return for expressions and explicit return for block form; explicit capture modes (default move consumes binding; `copy x` keeps a `Copy` value usable; borrow captures planned later alongside borrow/lifetime checking) to keep ownership clear; capturing closures lower to `{env_ptr, call_ptr}` with a single env destructor; non-capturing are thin function pointers; callable interfaces (`Fn`/`FnMut`/`FnOnce` style) can be auto-implemented based on capture mutability.
- Added an explicit `copy <expr>` expression to force duplication of `Copy` values (errors on non-`Copy`), usable in call args, closure captures, or bindings.
- Added a DMIR note for closures: capturing closures are fat `{env_ptr, call_ptr}` with a single env drop; non-capturing are thin pointers; callable interfaces can target the closure thunk/env.
- Added callable-usage examples: a single `CallbackN<...>` interface with usage determined by how it’s passed—`ref` for pure reuse, `ref mut` for stateful reuse, by value to consume (single-use for move-only callables, duplicating `Copy` ones).
- Added a TODO track for closure implementation: lower closure literals to `{env_ptr, call_ptr}`, generate thunks, represent thin/fat closures in MIR/LLVM with env drops, wire callable invocation/desugaring, and add borrow captures once borrow checking is available.
- Clarified DMP threat model and verification: signatures are checked only at import/compile time (not at runtime), and DMP guards against supply-chain tampering, not against attackers who already control the compiler/linker/runtime.
- Extended the MIR verifier’s dataflow: propagate defs/types across blocks and use propagated types for edge arg checking; CFG validation now uses out-state from the dataflow pass.
- Wired the MIR verifier into MIR golden tests; fixed edge checking to use propagated out-state so branch/phi args and returns validate across blocks.
- Integrated MIR verification into `driftc` so MIR is checked before LLVM codegen in the `mir-codegen` path.
- Added negative verifier tests (use-before-def, edge arity mismatch) to the test runner to ensure the verifier rejects bad MIR.
- Added a dominance-violation negative test (missing join arg) to the verifier suite to ensure defs must reach all predecessors.
- Added an edge type-mismatch negative test to cover edge param type validation.
- Added an edge undefined-arg negative test to ensure edges only reference values defined in the source block.
- Added ownership negative tests (use-after-move, double-drop) to exercise the verifier’s ownership rules.
- Added a return-type mismatch negative test to ensure returns match the function’s declared type.
- Added an error-edge type negative test to ensure `raise` carries `Error` and error edges have correct types.
- Added a missing-terminator negative test to enforce that every block ends in a terminator.
- Added an unknown-block negative test to ensure edges cannot target nonexistent blocks.
- Added an end-to-end MIR→LLVM→clang codegen test harness (`tests/mir_codegen/`), with a sample add case; harness is skipped when llvmlite/clang-15 are unavailable.
- Restored call normal/error edges and treated call-with-edges as terminators in the verifier/CFG/dataflow; MIR→LLVM now branches to call successors (placeholder success check; error payload TBD).
- Documented the Error ABI: errors are heap-allocated `Error*` owned by the caller; calls return `{T, Error*}` (or `Error*` for Error returns), branch on `err == null`, and propagate the pointer along error edges; handlers/freeing happen at catch/top-level.
- Defined the Error object layout for the ABI: `Error*` heap object with event id/name, preformatted args, ctx frames, backtrace handle; opaque to user code; caller frees via `error_free` unless propagating.
- Lowered `raise` in MIR→LLVM: returns an `{T, Error*}` pair with the error pointer (or `Error*` directly for Error-returning functions) along the error path; still a placeholder until full error ABI is wired through calls.
- Added a codegen skip for the planned error-path test until runtime error helpers and real error ABI wiring are in place.

## 2025-11-20
- Captured the `lang.core.source_location` helper in the spec as a zero-cost intrinsic that lowers to the current file/line. Kept the data shape explicit (`SourceLocation` struct) so callsites can choose when to capture site metadata, thread it through `^` context bindings, or pass it into exceptions; avoided auto-injecting locations in the runtime to keep logging/telemetry opt-in. (Prototype interpreter still needs the intrinsic wired in.)
- Hardened comment and error conventions: grammar now allows both `//` line comments and `/* ... */` block comments (non-nesting) so we can annotate examples without fighting terminator insertion. Documented a standard `IndexError(container, index)` event for out-of-bounds accesses to make future bounds checks report consistent payloads instead of ad-hoc errors.
- Elevated error declarations to first-class language items with an `exception` keyword, aligning them with structs so constructors are typed and usable from the interpreter. Fixed the parser to ignore non-Tree nodes when assembling parameter lists, preventing stray tokens/comments from polluting function signatures.
- Tightened tooling guardrails: the draft linter now enforces tabs (default) vs spaces and checks snake_case/PascalCase across functions, parameters, bindings, structs, and exceptions to keep examples consistent with the style guide. The `just` recipes parse examples to catch grammar regressions immediately; we deliberately stayed with a lightweight custom linter instead of a full formatter while the syntax is still in flux.
- Worked through module signing requirements and concluded the pipeline should canonically sign an ANF-like DMIR and lower to SSA MIR for optimization/codegen; added an overview of that split to `docs/design-first-afm-then-ssa.md`.
- Adopted a policy of fully monomorphizing generics (no shared reified bodies) so DMIR/SSA always see concrete types; watch for code-size blowups in heavily polymorphic code, but favor optimizer simplicity and performance first.
## 2025-11-23
- Added a DMIR draft spec and cleaned up primitive notes (ConsoleOut treated as runtime-provided only). Expanded control surface with ternary `?:`, plus try/catch and inline try/else support wired through grammar, parser, checker, interpreter, linter, and new runtime tests (including a ternary test case in `tests/`).
- Runtime now enforces array bounds with `IndexError(container, index)` and prints errors in the spec’s structured format with a simple call-stack capture; added runtime tests for out-of-bounds and error reporting.
- Documented DMIR canonicalization rules (naming, ordering, kwarg normalization) and added surface→DMIR examples for ternary, try/else, and constructors to stabilize the signing format. Approved SSA MIR control-flow model and value/ownership rules (monomorphized, move-only by default, explicit error edges, drops in MIR).
- Documented the SSA MIR instruction palette (const/move/copy/call with normal+error edges, struct/array ops, unary/binary, drop) in `docs/dmir-spec.md`; TODO updated accordingly.
- Added end-to-end surface→DMIR→SSA MIR examples (ternary, inline try/else with fallback) to ground the IR design.
- Added SSA MIR terminology/conventions (block labels, params-as-φ, SSA defs, explicit call successors, ownership rules).
- Added a CFG block notation alongside the ternary SSA example to visualize control flow and φ-like params.
- Added CFG notation to the try/else SSA example for readability.
- Added verifier expectations to the SSA MIR terminology section (SSA dominance, types, ownership, drops, terminators).
- Added a MIR verifier checklist to the DMIR spec so readers know the invariants to enforce before optimizations/codegen.
- Added initial MIR data structures (`lang/mir.py`) to model SSA blocks, instructions, edges, and programs; tests still pass.
- Tightened the String ABI plan: compiler never calls `drift_string_literal`; literals stay as static `%drift.String` constants. Renamed the runtime constructor to `drift_string_from_utf8_bytes` to make encoding explicit and aligned LLVM decls to the new symbol.
- Added console runtime stubs (`drift_console_write/writeln` taking `DriftString` by value), wired MIR with `ConsoleWrite/ConsoleWriteln` instructions, and lowered them in MIR→LLVM via the matching runtime decls.
- Updated the `test` just target to drop linting of the legacy interpreter programs to avoid keeping that folder in sync.
- Added a skeleton MIR verifier (`lang/mir_verifier.py`) covering SSA def/use, ownership moves/drops, edge/param arity, and basic terminator checks.
- Clarified dominance in the SSA terminology (defs must appear on every path to their uses).
- Documented the verifier implementation sketch (input, steps, output) in the DMIR spec.
- Enriched MIR nodes with source locations and wired the verifier to report locations on errors.
- Extended the MIR verifier with partial type tracking (propagating known types, checking calls against known function signatures, return/raise types) while still passing existing tests.
- Added CFG reachability and edge/arg/param/type checks in the MIR verifier (ensuring edge args are defined in source blocks and match dest param types where known).
- Added incoming edge arg/param validation to the MIR verifier to align predecessor args with block params across the CFG.
- Relaxed the MIR call shape to allow optional normal/error edges; updated printer/verifier accordingly to ease initial lowering.
- Added a MIR printer (`lang/mir_printer.py`) and a minimal straight-line lowering path (`lang/lower_to_mir.py`) with a MIR golden test wired into `tests/run_tests.py`.
- Added a minimal MIR→LLVM emitter (`lang/mir_to_llvm.py`) for straight-line functions and a `mir-codegen` just target that lowers `tests/mir_lowering/add.drift` to an object and links/runs it via clang-15/llvmlite.
- Introduced `lang/driftc.py` as a minimal Drift→MIR→LLVM driver (straight-line subset) and moved the MIR codegen harness out of `tools/test-llvm/` into `tests/mir_lowering/`.
- Fixed import shadowing (lang/types vs stdlib types) by adjusting `lang/driftc.py` sys.path handling and invoking it as a module; `just mir-codegen` now runs end-to-end producing and running a native binary.
- Added initial MIR data structures (`lang/mir.py`) to model SSA blocks, instructions, edges, and programs; tests still pass.
- Aligned the String ABI end-to-end: removed the obsolete `lang/_string_runtime_decls.py`, mapped Drift `String` in MIR→LLVM to the runtime struct `{drift_size_t, i8*}`, built string literals as static globals (no heap), and wired string `+` to `drift_string_concat`.
- Made SIZE_T derive from the target data layout (fallback 64-bit) instead of hardcoding i64, and treated an empty String literal as `{0, null}` for clarity; kept forward-declared string runtime helpers ready for future FFI lowering.
- Added console runtime stubs (`drift_console_write/writeln` taking `DriftString` by value), wired MIR with `ConsoleWrite/ConsoleWriteln` instructions, and lowered them in MIR→LLVM via the matching runtime decls.
- Updated the `test` just target to drop linting of the legacy interpreter programs to avoid keeping that folder in sync.
- Temporarily skipped the `runtime_*` codegen cases in `tests/run_tests.py` (they rely on mutation, arrays, full control flow, and module checks not yet supported by the minimal lowering). Also skipped the error/attr/frames cases until the new `String`/`Error` ABI is wired end-to-end. Will re-enable incrementally as features land.
- Reworked the C error runtime to use `DriftString` structs for all text fields/arrays (event, domain, attrs, frames, captures), deep-cloning inputs and freeing via `drift_string_free`. Added LLVM decls in `mir_to_llvm.py` for `drift_error_new`/`error_push_frame` that match the struct-based ABI; codegen still needs to route calls through these.
- Updated MIR→LLVM to resolve `drift_error_new`/`error_push_frame` to the struct-based declarations so the backend uses `%drift.String` everywhere the C runtime expects it.
- Zero-length array lowering now passes `null` pointers to the runtime for `String`/`Int64` arrays instead of GEPs on `[0 x ...]`; fixes GEP-related codegen errors when invoking error helpers.
- Fixed the error ABI mismatch: `drift_error_new`/`error_push_frame` now take pointers to `DriftString` for event/domain/module/file/func, and MIR→LLVM wraps the struct args in allocas before the call. This prevents the garbage `len`/`data` that was crashing `error_path`; `error_path` now passes end-to-end.
- Added implicit return insertion for `Void` functions and renamed generated `main` to `main_drift` to avoid C harness symbol clashes. Unskipped all error/attr/frame codegen cases plus `runtime_basics`; all now pass. Runtime-related codegen tests remain skipped until more language features are lowered.
- Added `ret_type` to MIR `Call` and use the callee’s type when building pair returns; short-circuit `and`/`or` now produce `Bool` phis. Unskipped `runtime_logic` (passes) and kept remaining `runtime_*` tests skipped until the necessary language features are lowered.
- Ensured error helper calls always use the struct-pointer ABI even when the callee was already declared, adjusting arguments accordingly. This fixed the remaining `runtime_functions` codegen failure; the test now passes.
- Implemented array literals and indexed loads for `Array[Int64]` in MIR lowering and MIR→LLVM, with runtime support (`drift_alloc_array`, `drift_bounds_check_fail`) that raises a structured `IndexError`. Unskipped and passed the `runtime_index_bounds` codegen test with the JSON error output.
- Hardened the array runtime: introduced a `drift_size` alias for array metadata, added an overflow/oom guard in `drift_alloc_array`, marked `drift_bounds_check_fail` as `noreturn`, and kept bounds failures returning a structured `IndexError` with exit code 1.
- Added string equality support in the runtime (`drift_string_eq`) and wired MIR→LLVM to lower `String ==/!=` via that helper. This fixes the invalid `icmp` on structs and enables the `runtime_try_catch` codegen test; it now passes and remains unskipped.
- Threaded loop-carried variables through while-block params/edges and relaxed the verifier to allow mutation, eliminating SSA redefinition errors. Unskipped and passed the `runtime_while_basic` codegen test.
- Removed the legacy interpreter and playground artifacts; SSA+LLVM is now the only supported backend. `drift.py` was replaced with a stub error, and runtime files moved under `lang/runtime/`.
- Completed try/catch rework: expression and statement forms support multi-catch and event-based dispatch; binders are scoped correctly; SSA lowering uses a dispatch block with `ErrorEvent` projection. Added e2e and SSA tests for stmt/expr, multi-catch, and event dispatch.
- Implemented reference semantics in SSA: `ReferenceType` mapped to pointers, FieldGet/Set unwrap references, and e2e `ref_struct_mutation` proves mutation through `&mut` across calls.
- Added event-code hashing for exceptions: FQN-based xxHash64 payload, kind/payload layout (4+60 bits), per-module collision checks, and metadata capture. `Error.code` is `I64`; runtime `drift_error_new_dummy` threads code+payload. New SSA/e2e tests cover exception constructors and expr dispatch.
- Introduced runtime C tests for error packing; `just test-runtime-c` builds/runs `runtime_error_dummy_raw` (via `CLANG_BIN`, default clang-15) to assert kind/payload masking over multiple inputs.
- Removed legacy exception args/payload path entirely; runtime `Error` now only carries typed attrs/frames, compiler/tests consume `attrs` + `DiagnosticValue`, and arg-view helpers/`drift_error_add_arg`/`__exc_args_get*` were deleted.

## 2025-12-02
- Clarified the exception model in the language spec: all exception arguments and `^`-captured locals are recorded as diagnostic strings in `Error.args` / `ctx_frames`; removed the old “first payload string” wording.
- Introduced a formal `Diagnostic` + `DiagnosticCtx` definition in the traits chapter and marked `Debuggable` as legacy for diagnostics; Chapter 14 now explicitly requires `Diagnostic` for exception fields and captures.
- Updated the exceptions/diagnostics work tracker with a spec-only Step 2 plan; implementation/runtime changes remain out of scope for this pass.
- Hardened dot-shortcut args access: parser now captures `.field` correctly, checker handles `e.args[.foo]` sugar and rejects unknown keys, and the SSA backend fixes the `__exc_args_get` ABI (sret) to stop crashes; added an e2e `exception_args_dot` covering the feature. Added a checker guard that functions may not `return Error`, with a negative SSA test.

## 2025-12-03
- Spec clarifications for struct `val` fields: they are type-level constants with compile-time initializers, excluded from layout/`size_of`, and disallow `Destructible`/non-const types; structs with only `val` fields are ZSTs. Added notes on required vs optional exception-args lookups and renamed `Option` to `Optional` (with a minimal `is_some`/`is_none`/`unwrap_or` API) in the spec.
- Fixed the `__exc_args_get` ABI to use an explicit sret out-param (matching the C runtime) and adjusted SSA codegen to allocate/load the Optional result; this stopped the Optional-path segfaults in `exception_args_optional`.
- SSA simplifier now counts uses across blocks so it no longer drops defs that are only threaded via edges/joins; try/catch lowered programs verify and run again.
- Updated the `captures` e2e expected compile error to the current can-error invariant (“call to can-error function … without error edges”); all e2e/SSA suites are green again.
- Optional as a first-class generic is usable outside exceptions: added `DriftOptionalInt` helpers (`drift_optional_int_some/none`), SSA lowering guards to avoid name clashes in `unwrap_or`, a negative SSA test for bad defaults, and an e2e `optional_basic` that exercises `is_some/is_none/unwrap_or` through SSA→LLVM.
- Added SSA Optional coverage: `optional_phi` exercises Optional<Int> flowing through a branch/join; `optional_phi_type_mismatch` asserts a clean checker error when mixing Optional and non-Optional types across branches.
- Implemented typed diagnostics plumbing: runtime `DriftError` now owns typed attrs and context frames of `DiagnosticValue` locals; added `drift_error_add_local_dv` and frame/local structs. SSA lowering now tracks `^` captures, wraps captured primitives/strings into `DiagnosticValue`, and emits calls to the typed local helper during exception construction. SSA codegen can call the new helper, and both SSA and e2e suites remain green.
- Removed legacy args/payload: `DriftError` no longer stores them, arg-view helpers and `__exc_args_get*` are gone, compiler/tests consume typed `attrs` + `DiagnosticValue` exclusively.
- Restored the receiver placeholder (“dot-shortcut”) feature: grammar/AST include `.` placeholders, SSA lowering evaluates the method receiver once and threads that SSA through argument/sub-expression lowering (including `.field`, `.method(...)`, and `.[idx]` forms). Added placeholder-aware lowering while keeping SSA/e2e suites green.

## 2025-12-08
- String/Array surface and backend aligned: `byte_length` documented as byte-count returning `Uint`, `String.EMPTY`/`is_empty` captured in the spec, and program entry clarified to a single `main` returning `Int` (either zero-arg or `Array<String>` argv) with no `drift_main` indirection.
- Checker hardening: string binops only allow `String +`/`==`; added diagnostics/tests for string misuses (String+Int, String in `if`). Boolean conditions are now validated when types are known. Array checks emit errors for non-Array indexing and mismatched index stores; array element inference for locals was strengthened to keep Array<String> element types flowing.
- Array<String> runtime/codegen: `_llvm_array_elem_type` maps String elements to `%DriftString` with correct size/align; IR tests cover Array<String> literals, loads, and stores. New e2e cases write/read Array<String> and sum byte lengths.
- argv entry implemented: C runtime builds `Array<String>` from `argc/argv`, LLVM emits an sret wrapper when `main(argv: Array<String>) returns Int`; runner enforces a single main and requires explicit argv in `expected.json`. Added e2e `main_argv_len`/`content`.
## 2025-12-04
- E2E runner builds into `build/tests/e2e/<case>/` instead of test dirs; `just test-e2e`/subset wipes that build dir first to avoid stale artifacts. All e2e tests green.
- Error-edge hardening: can-error inference is locked to MIR; negative tests cover plain call dropping error and edges to non-can-error; Throw returns now use deterministic zero/null placeholders for non-void `{T, Error*}` pairs. Try/catch generalization remains open.
- DiagnosticValue ABI fixes: modeled DV-returning helpers with explicit `sret` (DV ctor/get/index, diag-from primitives/optionals) and made `drift_dv_as_string` return via sret because `Optional<String>` is >16B. The C runtime layout now matches LLVM (24B, align 8) and attrs retrieval uses the correct out-param ABI, eliminating the intermittent `<missing>`/garbage output in `exception_args_dot`/`exception_args_optional`.
