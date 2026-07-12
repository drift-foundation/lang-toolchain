Summary: 0.33.80 blocking-FFI API is `pub` but absent from `std.concurrent`'s `export {}` list — `BlockingExecutor` cannot be named in user signatures/fields

Classification
- Stdlib export surface (std/concurrent/concurrent.drift)
- Priority: high for downstream adopters — it blocks the exact integration shape the facility's own boundary guidance prescribes (a subsystem storing its named executor)
- Surfaced by: drift-query, Slice 12 design grounding for the LMDB blocking-executor integration, 2026-07-12, on certified `driftc 0.33.80 | abi 21`

Symptom
- The six 0.33.80 blocking-facility declarations are all `pub` in
  `stdlib/std/concurrent/concurrent.drift` but none appears in the module's
  `export {}` list (which was not updated by the 0.33.80 change):
  `BlockingExecutor` (:1555), `blocking_executor_builder` (:1570),
  `build_blocking_executor` (:1594), `spawn_blocking_on` (:1613),
  `run_blocking_on` (:1633), `spawn_on_labeled` (:1864).
- Observed split behavior on `driftc 0.33.80 | abi 21`:
  - The FUNCTIONS resolve and run fine cross-module (`repro_functions_resolve.drift`
    compiles and exits 0 — the closure genuinely runs on the named executor).
  - The TYPE does not: any attempt to write `conc.BlockingExecutor` in a struct
    field, parameter, or return type fails
    (`repro_type_unnameable.drift:15:6: error: module 'std.concurrent' does not
    export type 'BlockingExecutor' [E-AUTO-0fd5b919]`).
- A generic-parameter dodge (`fn f<X>(ex: &X)` hoping X infers to the executor
  type) does not work either: `run_blocking_on` inside such a function fails
  with "cannot infer type arguments for 'run_blocking_on': conflicting
  constraints [E-AUTO-f402afdf]".

Why this blocks real adopters
- `examples/blocking_ffi/main.drift` never names the type (local `val ex` +
  `captures(copy exw)` only), so the example compiles — but any real subsystem
  integration must STORE its executor (e.g. drift-query's `LmdbStorage` needs an
  executor field so every storage transaction routes through the same named
  "storage-lmdb" executor, exactly per the drift-concurrency.md diagnosability
  pattern). Without the exported type name there is no way to declare that
  field, a parameter that passes the executor down, or an accessor that returns
  it.
- The inconsistency between function and type resolution for unexported `pub`
  symbols may itself be worth a look (functions being callable while the type
  is unnameable suggests the export gate is applied unevenly), but the minimal
  fix for adopters is the export-list addition.

Expected
- All six symbols in the `export {}` list, matching their documented status as
  the standard public facility ("Blocking FFI from virtual threads",
  drift-concurrency.md; effective-drift.md "Blocking FFI: isolate it, and make
  it diagnosable"; 0.33.80 history entry).

Reproducers (in this directory)
- `repro_functions_resolve.drift` — compiles and exits 0 today; pins that the
  functions are reachable (should keep passing after the fix).
- `repro_type_unnameable.drift` — fails today with E-AUTO-0fd5b919 at :15:6;
  should compile and exit 0 after the fix.

Reported by: drift-query (Slice 12 — scheduler-safe blocking storage execution).
Downstream status: drift-query treats this as a blocker for Slice 12
IMPLEMENTATION (design proceeds); per its defect policy it will wait for a
certified toolchain carrying the export fix rather than dodge locally.
