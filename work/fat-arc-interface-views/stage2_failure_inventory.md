# Stage 2 Option B — Failure inventory and root-cause note

Snapshot: `feature/fat-arc-interface-views` head, after Option B bridge
landed.

**Full driver-suite counts (same on both pre-change stashed HEAD and post-change):**
- 78 FAILED, 950 PASSED.
- All 78 failures in 19 package-path test files.
- 0 regressions introduced by the Stage 2 Option B bridge.

Pre-change run (stashed to `bdb18a69`): 78 failed / 950 passed.
Post-change run (Option B bridge in place): 78 failed / 950 passed.
Same failing test names, same error shapes, identical root cause
across every failure. See `project_arc_header_drop_thunk_bug.md` and
the per-file classification below.

## Cluster-level root cause

All 78 failures trace to a single Stage 2 PREP artefact: the ArcHeader
struct field

```drift
drop_thunk: Fn(mem.Ptr<Byte>) nothrow -> Void
```

in `stdlib/std/concurrent/concurrent.drift` (added by task #21,
commit-range leading to bdb18a69 "Stage 2 prep landed"). The `Fn<...>`
struct-field type does not round-trip through package serialization.

The failures split into two symptomatic variants of the **same** root
cause, differing only in *where* the mismatch first surfaces:

### Variant A — same-process TypeId rebind (`ValueError` at struct field definition)

Shape:
```
ValueError: struct 'std.concurrent::ArcHeader' fields already defined:
    [5, 5, 3] vs [5, 5, 1218]
```
(the third field TypeId varies between ~1218 and ~1226 across runs; the
first two are stable because `strong` and `weak` are `AtomicInt`).

Trigger: test drives `driftc` to build the stdlib as a package
(`--emit-package`) and then a second driftc invocation (or the same
process after package rehydration) re-parses the stdlib's
`concurrent.drift`. On the first pass `drop_thunk` interns as Fn
(TypeId ~1218). On the re-parse inside the same process the type
table is seeded with the package's deserialized struct schema where
`drop_thunk` came back with the wrong TypeId (typically BYTE=3, i.e.
the raw `mem.Ptr<Byte>` got unwrapped because the Fn wrapper was not
preserved). When the source-parse pass then calls
`TypeTable.define_struct_fields(std.concurrent::ArcHeader, [5, 5, 1218])`,
the two-field-list comparison at
`lang/driftc/core/types_core.py:1888` rejects the rebind as a
schema collision.

Tests in this cluster (60 total):
- `test_driftc_package_v0.py` — **40 tests** (all package-roundtrip,
  dedup, version-pin, signature, variant-schema, and
  export-star shapes).
- `test_instantiation_odr.py` — **9 tests** (package + impl generic
  dedup across modules/packages).
- `test_pkg_transitive_dep_resolution.py` — **2 tests**
  (transitive-dep conflict, transitive-dep narrowing).
- `test_pkg_hidden_lambda_construct_iface.py` — **1 test**
  (hidden-lambda iface construction).
- `test_pkg_cross_package_method_param.py` — **3 tests** (the
  subprocess crashes before producing the JSON payload the driver
  harness expects; the underlying stderr carries the same ArcHeader
  field-rebind ValueError).
- `test_cross_source_module_overload.py` — **1 test** (cross-source
  module with package dep; explicit ValueError).
- `test_drift_multisig_policy.py` — **1 test** (sidecar signature
  accepted when signed; subprocess stderr has the ValueError).
- `test_external_consumer.py` — **1 test** (cross-package throw-impl;
  different TypeId offset [6,6,3] vs [6,6,1220] — same class).
- `test_drift_sign_cli.py` — **1 test** (sign-CLI uses env key cmd;
  subprocess stderr has the ValueError).
- `test_pkg_trait_impl_target_type.py` — **1 test** (array range len;
  Variant B consumer-side thunk=Unknown).

### Variant B — consumer-side `drop_thunk: Unknown`

Shape (from the consumer's `driftc --dep std@…` invocation):
```
<source>:525:4: error: cannot copy 'thunk': type 'Unknown'
  Copy is unknown (element type contains Unknown)
<source>:525:9: error: call target is not a function value
```
Line 525 of `stdlib/std/concurrent/concurrent.drift` inside
`_arc_destroy_impl<T>` reads `val thunk = slot.header.drop_thunk;`
followed by `thunk(ctrl_ptr);`. The consumer deserializes the stdlib
package, binds `ArcHeader` from the package's schema, and `drop_thunk`
comes back as `Unknown`. Every downstream use of the field — Copy
solving, taking its value, invoking it as a function — then fails.

Tests in this cluster (18 total):
- `test_pkg_hir_scope_reconstruction.py` — **4 tests** (private const
  lookup, private function lookup, package modules not visible,
  zero fallbacks).
- `test_pkg_consumer_e2e.py` — **4 tests** (iface impl vtable,
  wrap-method FnResult boundary, and two adjacent consumer
  roundtrips).
- `test_pkg_generic_wrapper_lambda.py` — **2 tests** (cell-get in
  lambda; cell-in-nested-lambda).
- `test_stdlib_as_package.py` — **1 test** (arc scope drop no leak).
- `test_mode_equivalence.py` — **1 test** (source-mode vs
  package-mode produce same exit code).
- `test_deploy_runtime_readonly.py` — **1 test** (deployed wrapper
  uses runtime archives; stderr shows thunk=Unknown).
- `test_drift_trust_cli.py` — **1 test** (trust revoke blocks
  package consumption; exits silently because consumer compile
  aborts on the consumer-side thunk=Unknown).
- `test_linker_typevar_dedup.py` — **1 test** (cross-package typevar
  dedup for Copy proof; mylib build stderr shows thunk=Unknown).
- `test_package_root_stdlib_method_resolution.py` — **1 test**
  (package-root does not duplicate std methods; consumer subprocess
  produces empty JSON because stdlib-consume fails on thunk=Unknown).
- `test_external_consumer.py` — **2 more tests**
  (`test_ext_cross_package_or_throw`,
  `test_ext_std_core_non_prelude_still_hidden`) — Variant B shape
  when consumer tries to use drop_thunk; other
  `test_ext_cross_package_throw_impl` falls in Variant A.

## Why this is unrelated to the Stage 2 Option B bridge

1. **Layout side:** the ArcHeader struct field `drop_thunk` was
   introduced by Stage 2 PREP (task #21, on HEAD `bdb18a69` before
   my Option B work began). Option B did not change `ArcHeader`, the
   drop-thunk capture path, or the package serializer — it only
   changed the typechecker's call-info rewrite for `@intrinsic` Arc
   methods, `_queue_instantiations`' handling of intrinsic templates,
   and `hir_to_mir`'s INTRINSIC-branch lowering.

2. **Test delta (full driver suite):** I stashed all Python/stdlib
   changes back to `bdb18a69` and ran `pytest lang/tests/driver/ -n16`
   both before and after the Option B bridge landed. Result:
   **identical 78 failed / 950 passed**, same test names, same error
   shapes. No test switched from pass→fail or fail→pass.
   (An earlier sampled run restricted to 10 of the 19 impacted files
   reported 67/52 — that was a sampled subset, not the full inventory;
   the full driver count is 78/950 and that is what reviewers should
   compare against.)

3. **Symptom alignment:** Variant A errors fire during
   `TypeTable.define_struct_fields` (Fn TypeId bound on a second
   parse after package roundtrip); Variant B errors fire at the
   consumer-side read of `slot.header.drop_thunk` (Fn-field came
   back as Unknown). Both surfaces touch `ArcHeader.drop_thunk`
   specifically — never an intrinsic Arc method and never the Arc
   call dispatch bridge. Option B's bridge runs end-to-end in
   source mode (proven by `test_arc_intrinsic_bridge.py` and the
   focused gate below).

## Fix ordering — must it land before Stage 3?

**Yes, before merging the branch.** Rationale:

- Stage 3 will bump the ABI (9→10) and add `Arc<Interface>` as a
  distinct layout shape. That work requires the stdlib to be
  serializable as a package and the `ArcHeader` schema to round-trip
  identically on both sides of the package boundary. If `drop_thunk`
  doesn't round-trip today, Stage 3's `ctrl_ptr` invariant (header-
  first layout with constant atomic offset at `strong`) will hit
  equivalent serialization mismatches the moment we put `Arc<I>` in
  a package-consumed API.
- The Fn-field serialization gap is a pre-existing language-level
  bug that Stage 2 PREP surfaced by being the first stdlib type
  with a non-trivial Fn struct field. It is LANGUAGE_BUG per
  AGENTS.md: regression-first, test → fail → fix → pass.
- The fix belongs to the package-serialization owner (not to this
  feature branch's Arc work). Ownership: whoever fixed
  `RawPtr<T>` field TypeId remapping (0.27.44) — the bug shape
  here is the same class.

**Not blocked on Option B's bridge, and the bridge is not blocked on it for gate-green status** — provided the gate is run over the non-package-path surface. See `stage2_focused_gate.md` / the clean gate result below.

## Verification summary

Full driver suite (`pytest lang/tests/driver/ -n16`, no ignores):
- Pre-change (stashed to bdb18a69): 78 failed / 950 passed.
- Post-change (Option B bridge in place): 78 failed / 950 passed.
- Identical failure set. Identical root cause. No Stage 2 Option B
  regression.
