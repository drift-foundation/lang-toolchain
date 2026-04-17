# Stage 2 Option B — focused gate record

Feature branch: `feature/fat-arc-interface-views`
Post-change HEAD: Stage 2 Option B bridge landed.

## The clean gate

**Result: `871 passed, 0 failed` in 5m27s (post-change).**

```
PYTHONPATH=. pytest lang/tests/driver/ \
  --ignore=<19 ArcHeader-impacted package-path files> \
  -n16 --tb=no -q
```

The 19 ignored files are the **exact set** of driver tests that
transit through `stdlib/std/concurrent/ArcHeader.drop_thunk:
Fn(mem.Ptr<Byte>) nothrow -> Void` via package serialization —
i.e. every file with at least one test that triggers Variant A
(`ValueError: struct 'std.concurrent::ArcHeader' fields already
defined`) or Variant B (`cannot copy 'thunk': type 'Unknown'`).
Each file was individually verified to hit the ArcHeader cluster;
see `stage2_failure_inventory.md` for the per-test classification.

Ignore list (`/tmp/driver_ignore.txt`):
```
test_driftc_package_v0.py
test_instantiation_odr.py
test_mode_equivalence.py
test_pkg_consumer_e2e.py
test_pkg_cross_package_method_param.py
test_pkg_generic_wrapper_lambda.py
test_pkg_hidden_lambda_construct_iface.py
test_pkg_hir_scope_reconstruction.py
test_pkg_transitive_dep_resolution.py
test_stdlib_as_package.py
test_deploy_runtime_readonly.py
test_drift_trust_cli.py
test_cross_source_module_overload.py
test_drift_multisig_policy.py
test_external_consumer.py
test_drift_sign_cli.py
test_linker_typevar_dedup.py
test_package_root_stdlib_method_resolution.py
test_pkg_trait_impl_target_type.py
```

## What the gate covers

- **All driver tests** outside the 19 ignored files — 871 tests
  across 237 files.
- **`test_arc_intrinsic_bridge.py`** (5 tests, all green):
  - `test_arc_clone_get_chain_returns_correct_value` — lvalue form.
  - `test_arc_clone_get_chained_rvalue_receiver` — **rvalue
    receiver form** (`a.clone().get().n`) — pins the chain
    previously handled by the deleted method bodies.
  - `test_arc_drop_runs_helper_destroy` — drop path twice, no leak.
  - `test_no_bodyless_intrinsic_template_inst_symbols` — link
    contract: no `Arc<T>::clone__inst__…` / `get__inst__…` /
    `destroy__inst__…` in IR.
  - `test_no_generic_helper_call_survives` — link contract: no
    generic `_arc_*_impl` call reference in IR (every call site
    resolves to the monomorphized `_arc_*_impl__inst__<hash>`).
- **Arc behavioural tests** (all green):
  - `test_borrowed_interface_dispatch.py` (3): `arc.get().method()`
    hot-path no-retain/release contract.
  - `test_arc_as_interface_require.py` (1): Stage 3 gate (T does
    not implement I rejected at typecheck).
  - `test_field_projection_noncopy_arc_uaf.py` (1).
  - `test_copy_impl_noncopy_field_rejected.py` (1).
  - `test_require_interface_impl.py` (8).
  - `test_method_type_param_require.py` (6).
  - `test_std_log_resolver_scoped_stack.py`,
    `test_std_log_envelope_thread_id_snapshot.py` (std.log
    resolver hot-path contract — the downstream consumer that
    motivated this whole branch).

## What the gate does NOT cover

- The 78 package-path failures (documented in
  `stage2_failure_inventory.md` and
  `~/.claude/.../memory/project_arc_header_drop_thunk_bug.md`).
  All 78 reproduce identically on the stashed bdb18a69 pre-change
  state — zero Stage 2 Option B delta.
- Codegen e2e tests under `lang/tests/codegen/e2e/`. Those run via
  the standalone `pex_e2e_runner.py` harness. Spot-checked
  compile+run:
  - `string_arc_if_join`, `json_handle`, `json_handle_clone_read`,
    `json_clone_deep`, `std_log_resolver_active`,
    `std_concurrent_arc_mutex_shared_lock`,
    `std_concurrent_arc_mutex_full_mutation`,
    `callback_arc_mutex_full_mutation`,
    `std_runtime_global_registry_arc_payload`,
    `arc_struct_field_get_drop_leak` — **all compile and exit 0**.

## Verification summary (full driver suite, no ignores)

- Pre-change (stashed to `bdb18a69`): 78 FAILED, 950 PASSED.
- Post-change (Option B bridge): 78 FAILED, 950 PASSED.
- Identical failing test names, identical root cause (ArcHeader
  drop_thunk Fn-field serialization).
- Zero Stage 2 Option B regressions.

## Gate status

**GREEN on the Stage 2 Option B surface**: 871/871 non-package-path
driver tests pass, including the focused Arc bridge contract, the
chained-rvalue form, the drop path, the link contracts, and every
Arc-touching behavioural test outside the ArcHeader serialization
cluster.

**BLOCKED on merge**: the ArcHeader drop_thunk package-serialization
bug must be fixed before this branch merges to main — that bug is
inherited from Stage 2 PREP (task #21, pre-`bdb18a69`), belongs to
the package-serialization code path (same class as the RawPtr<T>
field TypeId remapping fix in 0.27.44), and is not caused by, nor
fixable within, the Stage 2 Option B bridge work.
