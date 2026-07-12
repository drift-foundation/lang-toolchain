Summary: ICE (typecheck contract failure) — match arm naming `core.Result<R, …>` inside a boxed lambda within a generic function

Classification
- Typecheck/monomorphization: generic type-parameter substitution inside boxed lambda bodies
- Priority: HIGH — internal contract failure (ICE), clean diagnostic absent; one of two ICEs
  currently BLOCKING drift-query Slice 12;
  per its defect policy the slice's implementation is stopped until certified fixes land
  (no local shape-dodging retained). Sibling issue:
  issues/mir-missing-binding-id-conditional-move-ice/
- Affected certified version: `driftc 0.33.81 | abi 21` (git d49486e0, certified run
  20260712-143442-drift-lang-d49486e)
- Surfaced by: drift-query, Slice 12 implementation (blocking-executor storage drivers),
  2026-07-12

Symptom
- `repro_ssa_ice.drift` (35 lines, no concurrency, no FFI): a generic
  `fn f<R>(var body: core.Callback1<&T, core.Result<R, Int>>)` builds a boxed
  `core.callback0` whose body is a statement-form `match` where arm 0 returns
  `core.Result<R, Int>::Err(e)` and arm 1 returns a moved `body.call(&t)` result.
  Compiling aborts with an INTERNAL error, no source location:

  ```
  <unknown location>:?:?: error: typecheck contract failure: SSA return type does not match
  declared signature for __lambda_cb_f__inst__49d6fdc0d1a48b3a_0_0 in match_arm_0 (3126 vs 2493)
  [E-AUTO-90fc29aa]
  ```

  The two internal type ids suggest the lambda's declared return type and the arm's
  `core.Result<R, Int>` are instantiated inconsistently (R substituted in one, not the other).
- `repro_variant_ctor_diag.drift` — likely the same root cause surfacing as an ordinary (but
  wrong) diagnostic instead of an ICE: the same shape with `Callback0` and an
  `Ok(v) => core.Result<R, Int>::Ok(move v)` arm rejects with
  `variant 'Ok' payload type mismatch (have Int, expected R) [E_VARIANT_CTOR_ARG_TYPE]` at the
  call site's `core.Result<Int, Int>::Ok(41)` — i.e. the concrete instantiation's Int payload is
  checked against an UNSUBSTITUTED `R` from inside the generic lambda.
- Context notes: the generic-`R` match compiles fine when it lives in a plain generic FUNCTION
  (not a lambda) — moving the match into a helper `fn run_txn_over<R>(…)` called from a
  single-`return` lambda compiles and runs correctly (that is the structure drift-query shipped,
  chosen for dedup; it is not blocked). A val-assigned tail-expression variant of the same lambda
  match fails earlier with `E-MATCH-ARM-TYPE: … (have Result, expected Result)` — same
  two-different-instantiations flavor.

Expected
- Either the lambda's generic substitution treats `core.Result<R, …>` written inside the lambda
  identically to the enclosing instantiation (both repros compile and exit 0), or a real
  diagnostic with a source location — never a typecheck contract failure.

Reproducers (in this directory)
- `repro_ssa_ice.drift` — the ICE; should compile and exit 0 after the fix.
- `repro_variant_ctor_diag.drift` — the sibling wrong-diagnostic shape; should compile and
  exit 0 after the fix.

Third manifestation (same family, added 2026-07-12): `val _ = <generic driver call>` inside a
SPAWNED Callback0 lambda fails with the same SSA-contract class —
`typecheck contract failure: SSA return type does not match declared signature for
__lambda_cb_main_0_0 in entry (338 vs 2179) [E-AUTO-30f18b1b]` (and _0_1/_0_2 siblings per
lambda). 2-file repro in this directory: `context_spawn_lambda_manifestation.drift` compiled
with `context_storage_slice12_snapshot.drift` (needs `--allow-unsafe --link-search
/usr/lib/x86_64-linux-gnu --link-lib :liblmdb.so.0`). The minimal standalone analogue
(`nonrepro_lambda_discard_passes.drift`: `val _ = g(cb)` for a LOCAL generic g inside
conc.spawn) compiles and runs fine — like the main repro, real stdlib/multi-module generics
appear load-bearing for this manifestation, while `repro_ssa_ice.drift` stays fully standalone.

Failing pin in this repo
- `lang/tests/driver/test_drift_query_slice12_ices.py::test_generic_lambda_match_result_compiles`
  — compiles `repro_ssa_ice.drift`'s source and asserts success; FAILS today by design
  (regression-first); flips green with the fix.

Reported by: drift-query (Slice 12 — scheduler-safe blocking storage execution). STATUS UPDATE
(2026-07-12, supersedes the earlier "not blocking" note): per drift-query's defect policy the
slice is now BLOCKED on this fix and the sibling issue above; the helper-fn shape that avoided
this ICE has been withdrawn from drift-query's working tree rather than retained as a
workaround — see drift-query/work/write-activity-api/Progress.md.
