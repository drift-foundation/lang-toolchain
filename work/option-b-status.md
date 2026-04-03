# Option B Status: Packages as Distribution Containers

## Date: 2026-04-03

---

## What's done

### Consumer path (complete)
- Package payloads carry HIR, not MIR (payload_version=2)
- Consumer compiles all package functions through the standard pipeline
- Zero MIR fallback — all functions compile from HIR
- `_build_package_consumer_unit` deleted (~687 lines)
- `_remap_mir_func_typeids` deleted (~139 lines)
- `_validate_remap_completeness` deleted (~74 lines)
- `decode_mir_funcs` deleted (~33 lines)
- External wrapper injection deleted
- External `boundary_ret_type_id` setting deleted
- **Total deleted: ~950+ lines of package-MIR consumer machinery**

### Bugs fixed (all mainline)
- **Bookkeeper leak**: `drift_dv_as_string` double-retain (platform bug)
- **Generic wrapper lambda**: Missing drain after hidden lambda processing +
  wrapper `wraps_target_fn_id` identity mismatch (compile_stubbed_funcs bug)
- **ArrayRange/DequeRange method resolution**: `impl_target_type_id` using
  generic base instead of concrete instantiation (type_resolver bug)

### Regression coverage
- `test_pkg_map_literal_string_leak` — bookkeeper leak (3 tests)
- `test_hir_funcs_round_trip` — HIR serialization (4 tests)
- `test_pkg_hir_scope_reconstruction` — scope reconstruction (4 tests)
- `test_pkg_array_string_scope_drop` — cross-package drop
- `test_pkg_generic_wrapper_lambda` — generic wrapper in lambda (2 tests)
- `test_pkg_trait_impl_target_type` — trait impl target resolution
- `test_mode_equivalence` — source vs package behavioral equivalence
- **Total: 16 regression tests**

### Package consumer runner (605 tests)
- 0 new regressions vs mainline
- 20 former mainline-baseline failures fixed (now passing)
- All CI-gated tests pass

---

## What remains

### Producer-path cleanup (next workstream)
- `boundary_ret_type_id` field on FnSignature — still used when building
  packages from source (producer-side wrapper emission)
- `_inject_method_boundary_wrappers` — still called for source-mode
  compilation producing packages
- Call resolver / type checker `boundary_ret_type_id` checks — still fire
  for source-mode producer builds
- **Fix direction**: Stop emitting FnResult wrappers in packages. The
  consumer compiles from HIR and handles can-throw ABI itself. The
  producer doesn't need to pre-wrap nothrow methods.

### Type table linking simplification
- `type_table_link_v0.py` canonical key machinery — still used for
  declaration linking but could be simplified now that only type
  declarations (not MIR TypeIds) need linking
- `pkg_typeid_maps` — still needed for type table schema linking
  (struct field types, const imports, etc.)

### Test debt
- `test_package_modules_not_visible_to_consumer` — still indirect/smoke
- `test_mode_equivalence` — exit code only, should add observable behavior
- Some boundary tests are documented as transition-quality

---

## Architecture achieved

- One semantic pipeline after package ingress
- Packages are distribution/provenance containers with HIR + type declarations
- No semantically active package-MIR reconstruction
- No permanent dual pipeline
- No FnResult boundary ABI for consumer-compiled functions
