# Progress: P1.3 CallInfo inference boundary

Last updated: 2026-08-03

## Status

- [x] Checked `/tmp/drift-announce`; directory absent.
- [x] Scanned `doc/refactor_triggers.md`; no trigger fires.
- [x] Traced both `HCall(fn=HLambda)` resolver branches.
- [x] Proved the second branch body and K's addition are unreachable.
- [x] Traced pending stored-lambda resolution through `HCall(fn=HVar)`.
- [x] Traced the separate `HInvoke` path.
- [x] Audited the two existing P1.3 driver tests; both inject `Int` context.
- [x] Added executable no-context CallInfo and producer-shape probes under work/.
- [x] Verified probes: 4 passed in 0.54s.
- [x] Verified no-context surface repro: compile/link/run exit 0.
- [x] Verified line coverage: live HCall/HInvoke paths executed; dead branch body unexecuted.
- [x] Proposed the minimal source/test patch.
- [ ] Refresh against K's final #1 and #2 diff.
- [ ] Add the characterization probes to the in-tree suite.
- [ ] Delete the duplicate 6019 branch.
- [ ] Correct stale contextual/inference and stored/HInvoke comments.
- [ ] Add the no-context compile/run driver companion.
- [ ] Run combined focused gates.

## Commands and evidence

CallInfo and producer-shape probes:

```bash
./.venv/bin/python3 -m pytest -q work/finding-p13-callinfo-inference/probe_callinfo_inference.py
```

Result:

```text
4 passed in 0.54s
```

Surface compile/run:

```bash
./.venv/bin/python3 -m lang.driftc.driftc \
  work/finding-p13-callinfo-inference/repro_no_expected_result.drift \
  --entry repro::main --target-word-bits 64 --stdlib-root stdlib \
  -o /tmp/drift-p13-no-expected
/tmp/drift-p13-no-expected
```

Result: compiler exit 0, executable exit 0.

Line-trace command used during research:

```bash
./.venv/bin/python3 -m trace --count --missing --summary \
  --coverdir /tmp/drift-p13-trace \
  --ignore-dir /usr --ignore-dir .venv \
  --module pytest -q \
  work/finding-p13-callinfo-inference/probe_callinfo_inference.py
```

The generated `lang.driftc.checker.call_resolver.cover` marked every statement
inside the 6019 lambda branch `>>>>>>` (unexecuted), including the in-flight
`_lam_fn_ty = type_expr(lam)` addition.  It recorded the 5100 branch's
`callee_ty`, `fn_sig_ret`, and CallInfo path, as well as the separate HInvoke
path.

## Resume notes for K

1. Land/review #1 and #2 first, then rerun the four probes without editing them.
2. If they stay green, install them as characterization tests and delete the
   dead resolver branch; no new inference implementation is required for P1.3.
3. If a no-context CallInfo probe turns red after #2, stop and reclassify the
   newly exposed behavior as `LANGUAGE_BUG`; add the in-tree failing regression
   before changing the compiler.
4. Keep the source-shape distinction explicit: source `f()` is an `HCall` node
   whose CallInfo target is `INDIRECT`; that does not make the node an `HInvoke`.

Only files under `work/finding-p13-callinfo-inference/` were created by this
research.  No compiler, runtime, stdlib, or in-tree test file was edited.

