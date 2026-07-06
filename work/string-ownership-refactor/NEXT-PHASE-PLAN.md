# Next phase plan: String/non-bitcopy transfer policy, Scope A

Date: 2026-07-06

Status: planning only. Do not start implementation until the 0.33.70
projected-capture branch has cleared cert/merge, unless using `/tmp` scratch
files for isolated probes.

## Objective

Land the Scope A compiler refactor from
`research-string-semantics-audit.md`: make String/non-bitcopy Copy ownership
classification and transfer handling structurally consistent, then use that to
fix the next real gap exposed by 0.33.70 review:

- Copy-but-non-bitcopy projected captures are still rejected.
- A Copy struct containing a String (`Tag(label: String)`) previously produced a
  confirmed ASAN heap-use-after-free when the bitcopy gate was relaxed.
- 0.33.70 correctly avoided shipping that bug by accepting only
  `Copy && is_bitcopy` projected fields.

The next phase should make the non-bitcopy Copy path correct before widening
the accepted projected-capture surface.

## Scope

Target Scope A only:

1. Make String's retain-copy + needs-drop classification structural and
   mode-independent.
2. Centralize the MIR helper paths that turn borrowed/non-bitcopy aliases into
   owned values at transfer boundaries.
3. Audit the known parallel lowering paths against that helper contract.
4. Only after the above is proven, consider relaxing the projected-capture gate
   from `Copy && is_bitcopy` to the intended safe Copy surface.

Keep String Copy. This work is about unifying mechanism, not making String
move-only.

## Non-goals

- Do not reshape the runtime String representation toward ArcBox in this phase.
- Do not spend effort preserving ABI for a future Scope B representation
  redesign. Scope A should be ABI-neutral, but Scope B is a separate project and
  may bump ABI freely when it happens.
- Do not bundle the ref-typed callback-argument escape gap. That needs a clean
  repro or ICE root cause first.
- Do not lift the `--emit-package` projected-capture rejection in this phase
  unless typed capture decisions are serialized through the package boundary.

## Recommended branch

`refactor/string-transfer-policy-scope-a`

If the branch is framed as a bugfix rather than a refactor, use:

`fix/nonbitcopy-copy-projected-captures`

The first name is more honest if the deliverable includes the central transfer
policy cleanup. The second name is better only if the patch is narrowed to the
boxed-callback projected-capture bug surface.

## Regression-first entry criteria

Before changing compiler behavior, pin the current unsafe shape in a way that
can fail before the fix and pass after it:

1. Start from the 0.33.70 lock-in case:
   `test_copy_typed_non_bitcopy_struct_field_still_rejected`.
2. Create a scratch/probe variant that temporarily allows the capture so the
   current ASAN UAF is reproducible and the owner/retain loss can be traced.
3. Add the final regression only when the intended behavior is chosen:
   - If the goal is support: compile and run the `Tag(label: String)` projected
     boxed-callback case under ASAN, returning the captured value and comparing
     the String after both source struct and callback env are dropped.
   - If the goal is still reject: keep the rejection, but then this phase should
     not claim to fix non-bitcopy projected captures.

Do not update an existing rejection test to positive until the ownership fix is
implemented and demonstrated.

## Implementation spine

1. Define the central transfer-policy classification.
   - Suggested lanes: bitcopy, retain-copy, structural-copy, move-only.
   - String should classify as retain-copy + needs-drop without relying on
     `_copy_query`.
   - Preserve existing semantics where String remains Copy.

2. Wire classification into existing decision points.
   - `TypeTable.copy_status`
   - `DropPolicy.is_cheap_copy`
   - `_should_copy_value`
   - `_copy_if_ref_alias`
   - capture env construction paths in `hir_to_mir.py`

3. Centralize alias-to-owned transfer handling.
   - Replace ad hoc "remember to call `_ref_field_temps.add` here" patterns with
     a helper contract that every field/ref/capture load path uses.
   - Include the fourth known path from `doc/refactor_triggers.md`: whole-root
     HVar REF/REF_MUT capture reads that bypass `_load_capture_from_env`.

4. Revisit projected captures only after the transfer policy is stable.
   - Keep non-Copy projected MOVE captures rejected.
   - Keep `--emit-package` projected captures rejected.
   - Relax the Copy-projected gate only for cases covered by the new ownership
     tests.

## Test matrix

Focused tests first:

- Positive: bitcopy scalar projected capture (`Int`) still compiles/runs.
- Positive: bitcopy struct projected capture (`Point { x: Int, y: Int }`) still
  compiles/runs.
- Positive, if widening: Copy non-bitcopy struct projected capture
  (`Tag(label: String)`) compiles/runs under ASAN.
- Positive, if widening: plain `String` projected capture compiles/runs under
  ASAN, but do not rely on `string_arc.py` as the only safety net.
- Negative: non-Copy projected MOVE capture remains rejected.
- Negative: `--emit-package` projected captures remain rejected.
- Regression for whole-root HVar REF/REF_MUT capture alias marking if that path
  is touched.

Broader tests after focused green:

- Existing string ownership/leak tests.
- Projected capture driver tests.
- Package tests, because package rejection must remain stable.
- Valgrind/ASAN rows for any accepted non-bitcopy Copy path.

## Separate follow-up: ref-typed callback args

Keep this as its own branch unless it becomes trivially tied to the same helper
work. The research recommendation is to bound lambda escape level in
`borrow_checker_pass.py::_lambda_escape_level` when a MOVE/COPY capture root is
reference-typed, but the empirical repro currently hits an unrelated SSA
load-before-store ICE. First step there is to reduce/root-cause that ICE or find
a clean repro route.

Suggested branch when ready:

`fix/ref-valued-capture-escape-level`

## Completion criteria

- A failing regression exists for the non-bitcopy Copy ownership bug before the
  fix or via a documented mutation/probe.
- The root-cause transfer/classification fix is in compiler code, not a source
  workaround.
- The accepted behavior is covered by full compile/run tests, not just
  build-only checks.
- `DRIFTC_VERSION` is bumped for behavior change.
- `DRIFT_RT_ABI_VERSION` is unchanged for Scope A unless an actual boundary
  shape changes.
