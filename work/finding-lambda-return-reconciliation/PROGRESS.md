# Progress: inferred lambda return reconciliation

Last updated: 2026-08-03

## Status

- [x] Classified as `LANGUAGE_BUG`.
- [x] Scanned `doc/refactor_triggers.md`; no trigger fires.
- [x] Checked cross-team announcements; `/tmp/drift-announce` absent.
- [x] Reduced surface repro saved.
- [x] Traced first-pass and later hidden-lambda checks.
- [x] Confirmed the primary first pass accepts the conflict and records `Int`.
- [x] Confirmed the current full driver rejects only in the later re-check.
- [x] Added executable red boundary tests under this work folder.
- [x] Proposed nested-safe, no-retyping collector patch and test matrix.
- [ ] Move the red tests into the in-tree type-checker suite.
- [ ] Confirm those tests fail before the fix.
- [ ] Implement collector/reconciliation after K's #1 diff settles.
- [ ] Add driver negatives and compile/run positives.
- [ ] Run focused gates, then broader gates after #1 and #2 converge.

## Evidence

First-pass manual HIR probe on the current tree:

```text
diagnostics: []
direct call expr type: Int
```

Full driver compile:

```text
work/finding-lambda-return-reconciliation/repro_mixed_prefix_return_tail.drift:5:10:
error: return type 'String' does not match declared type 'Int'
exit 1
```

The apparent contradiction is expected: the first result is the defective
authority; the second is K's newly strengthened hidden-lambda re-check catching
the bad signature later.

Run the boundary probe with:

```bash
./.venv/bin/python3 -m pytest -q work/finding-lambda-return-reconciliation/red_first_pass_reconciliation.py
```

Verified before the fix: `2 failed in 0.50s`; both calls were typed as `Int`,
and both expected mismatch lists were empty.  Expected after the fix: two
passes, each with exactly one primary diagnostic.

The probe filename deliberately does not match pytest's default `test_*.py`
discovery pattern, so an accidental repository-root pytest invocation will not
turn this handoff artifact into an unrelated gate failure.  Pass the path
explicitly when running it.

## Resume notes for K

1. Refresh `git diff -- lang/driftc/type_checker.py` first; this plan was written
   while #1 was actively changing the same function.
2. Keep the final shared `_type_return_value` implementation from #1.
3. Add the observation stack beside per-function checker state, record from
   `type_stmt(HReturn)`, and delete `_find_return_expr`.
4. Reconcile only when the lambda entered with no known return type.
5. Verify the driver diagnostic now originates during the enclosing function's
   first check; do not accept green that comes only from `driftc.py`'s standalone
   hidden-lambda check.

Only files under `work/finding-lambda-return-reconciliation/` were created by
this research.  No compiler, runtime, stdlib, or in-tree test file was edited.
