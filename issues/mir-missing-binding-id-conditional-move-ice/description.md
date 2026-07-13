Summary: ICE — "MIR lowering contract failure (typed_mode strict: missing binding_id for local read (checker bug))" [E-AUTO-91e8ffe5] in an interface-impl method that conditionally early-returns around moves of non-Copy values

Classification
- MIR lowering / typed-mode checker: internal contract failure, no user diagnostic
- Priority: HIGH for drift-query — this is one of two ICEs currently BLOCKING Slice 12
  (scheduler-safe blocking storage execution); per drift-query's defect policy the slice's
  implementation is stopped until certified fixes land (no local shape-dodging retained)
- Affected certified version: `driftc 0.33.81 | abi 21` (git d49486e0, certified run
  20260712-143442-drift-lang-d49486e); also reproduces on the 0.33.80 certified snapshot
- Surfaced by: drift-query, Slice 12 test implementation (a fault-injecting `Storage`
  wrapper for its T6/T8 acceptance tests), 2026-07-12

Symptom
- `repro_single_file.drift` (standalone, no FFI/LMDB, ~175 lines): a 12-method interface
  mirroring drift-query's `dqc.storage.Storage` (transaction lifecycle ops taking the txn BY
  VALUE, scan methods taking `var visitor: core.Callback1<KeyValue, Bool>`, an
  `exec_handle() -> Optional<ExecHandle>` method whose payload struct contains a
  `conc.BlockingExecutor`), plus a mode-switched delegating wrapper whose `scan_prefix`
  reads a flag through an `Arc<Mutex<...>>` guard and then CONDITIONALLY early-returns an
  injected error (dropping the `var visitor` param) or delegates it by move:

  ```
  repro_single_file.drift:154:9: error: internal: MIR lowering contract failure
  (typed_mode strict: missing binding_id for local read (checker bug)) [E-AUTO-91e8ffe5]
  ```

  154:9 is the `scan_prefix` impl-method HEADER — no expression-level location is given.
- The failure is shape-sensitive in ways the reporter could not fully reduce:
  - `nonrepro_small_interface_passes.drift` — the SAME guard + conditional-early-return +
    move structure against a SMALL (1-method) interface compiles and runs fine, as do
    variants with a plain (non-Arc) `Mutex` field, an unconditional move into an
    `Optional`, and a conditionally-moved non-Copy `Result` local bound from the delegate
    call. The 12-method interface shape (or something it drags in — vtable width? the
    by-value txn lifecycle methods? the `BlockingExecutor`-bearing Optional payload?)
    appears load-bearing.
  - Rewriting the wrapper as two single-purpose TYPES (no mode conditional anywhere near
    the moves) compiles — that is how the shape was discovered to be the trigger.
- Original context (included): `context_original_wrapper_main.drift` compiled together with
  `context_storage_slice12_snapshot.drift` (drift-query's `dqc.storage` as of Slice 12)
  fails identically at the same method.

Expected
- The repro compiles and runs (exit 0) — or, if some shape here is genuinely illegal, a
  real diagnostic with an expression-level location, never a typed-mode contract failure.

Reproducers (in this directory)
- `repro_single_file.drift` — standalone; currently ICEs; should compile + exit 0 after the fix.
- `nonrepro_small_interface_passes.drift` — the passing near-miss (keep passing).
- `context_storage_slice12_snapshot.drift` + `context_original_wrapper_main.drift` — the
  original 2-file context (needs `--allow-unsafe --link-search /usr/lib/x86_64-linux-gnu
  --link-lib :liblmdb.so.0`).

Failing pin in this repo
- `lang/tests/driver/test_drift_query_slice12_ices.py::test_impl_method_conditional_move_compiles`
  — compiles `repro_single_file.drift`'s source and asserts success; FAILS today by design
  (regression-first); flips green with the fix.

Reported by: drift-query (Slice 12 — scheduler-safe blocking storage execution). Slice 12 is
BLOCKED on this fix plus issues/generic-lambda-match-result-ssa-ice/ — see
drift-query/work/write-activity-api/Progress.md.
