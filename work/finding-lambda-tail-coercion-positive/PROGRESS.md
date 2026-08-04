# Progress: lambda-tail coercion positive

Last updated: 2026-08-04 (K picked up from review-2026-08-04T15-57-18Z;
the 2026-08-03 snapshot below is reconciled — its open items are now
tracked in the CURRENT PASS section)

## CURRENT PASS (2026-08-04, K)

- [x] Re-ran saved probes on current main: red probe module 2/2 FAILED
      (no ConstructIfaceValue in hidden MIR; SSA Dog-vs-Speaker full
      driver); stored value-match repro builds but exits 0 (needs 5);
      stored terminal-call repro dies with `NotImplementedError: LLVM
      codegen v1: FnResult ok type UNKNOWN`.  All four documented reds
      confirmed.
- [x] Installed in-tree red module BEFORE production changes:
      lang/tests/driver/test_hidden_lambda_return_boundary.py — 6 red
      (cb block-tail MIR pin, cb block-tail run, annotated IIFE, moved-
      local block tail [same SSA 15-vs-16 route, verified], value-match
      exit-5, terminal-tail traceback pin) / 3 green controls (explicit
      return, expression body, Cat negative single clean diagnostic).
- [x] refactor_triggers.md rescanned during pickup: no trigger fires
      (matches reviewer scan; root cause unchanged from evidence).
- [x] Implemented module-level `_hidden_lambda_body` + factory
      `_hidden_lambda_terminal_call_predicate` in driftc.py; BOTH
      worklists (HiddenLambdaSpec + captureless LambdaFnSpec) now call
      it before normalize_hir.  Wrap fires only for concrete non-Void
      spec returns; preserved: existing HReturn, non-expr statements,
      empty bodies, Void/Unknown specs, statement-form matches (the
      parser's authoritative HMatchExpr.statement_form flag — never
      arm-terminator inference), and terminal tails via a
      CallInfo/signature predicate through hir_flow.is_terminal_stmt
      (mirrors HIRToMIR._is_call_terminal_throws; no name spelling, no
      new walker, no call resolution duplication).
- [x] Spec-return parity: LambdaFnSpec ret type is now
      `spec.return_type` with fallback ONLY when None/Unknown (narrow
      explicit handling, no broad except); `_hidden_lambda_ret_type`
      (Unknown-only fallback) now prefers the hidden TypedFn's own
      iface_coercions mark over raw expr_types for both HReturn.value
      and HExprStmt tails.  No side-table copying across the deep-copy
      boundary.
- [x] All 9 boundary tests GREEN post-fix (were 6 red / 3 green), plus
      the structural helper pin (matrix item 9, includes the
      predicate-not-spelling terminal check): 10/10.
- [x] Version/history: DRIFTC_VERSION 0.34.2 → 0.35.0 (this slice owns
      the minor; no other queued slice had established it); ABI stays
      22; doc/history.md 0.35.0 entry added (three failure shapes,
      shared normalizer, spec-return authority, tests).
- [x] ADJACENT FIX (exposed by authoritative spec returns, root-caused
      before changing anything): three battery tests
      (dead-throw/break-after-continue, dead-catch-break divergent
      lambdas) hit "captureless lambda block must end with a value or
      return".  Root cause is NOT the normalizer: the checker's
      reachability-refined terminal-flow pass accepts these bodies as
      divergent (the only `break` is in a DEAD catch arm), but MIR
      finalize treated the structurally-emitted after-loop open block as
      a missing return.  The old code "passed" only because the raw-tail
      inference mislabeled the lambdas Void (wrong signature, benign by
      accident); the NAMED-fn twin fails on current main with an
      internal contract failure (probe kept in scratchpad), and
      certified 0.33.90 rejected it outright with the coarser checker.
      Fix: `HIRToMIR._body_is_divergent` (shared hir_flow.block_exits
      with the checker's own CallInfo predicates — terminal calls via
      declared_terminal_throws, can-throw conservative when unrecorded)
      consulted by ALL THREE finalize paths (lower_function_body,
      hidden, captureless): a divergent non-Void body's open block is
      sealed with M.Unreachable; a genuinely missing return still
      asserts.  Named twin now builds and runs (new pin
      test_named_divergent_dead_break_body_finalizes); the three lambda
      tests pass with CORRECT Int signatures.
- [x] Focused gates (PLAN §4 battery + hir_flow pins, re-run
      post-adjacent-fix): 139 passed (the 11 mandated files + the new
      boundary module's 11 tests + test_hir_flow.py).  Compiler-suite
      smoke (type_checker/checker/stage1/stage2/parser — the finalize
      change touches all function lowering): 1096 passed.  `git diff
      --check` clean.  Diff scope inspected: driftc.py (normalizer +
      predicate factory + spec-return parity + two lambda finalizes),
      stage2/hir_to_mir.py (_body_is_divergent + lower_function_body
      finalize), versions.py (0.35.0), doc/history.md; NO
      call-resolver change, NO side-table copying, NO interface special
      case in MIR lowering.

## REVISION 2 (review-2026-08-04T18-28-05Z)

1. (P1) Third finalize route pinned:
   test_iife_divergent_dead_break_body_finalizes — the reviewer's exact
   IIFE dead-catch/dead-break shape, compile + run exit 0.  PRE-FIX RED
   CONFIRMED at the HiddenLambdaSpec finalizer: ran the shape against a
   read-only `git archive HEAD` scratch tree (no git writes) — it fails
   with exactly "hidden lambda block must end with a value or return".
2. (P2) Boundary assertions match their stated contracts:
   - non-implementing negative now pins EXACTLY ONE occurrence of the
     "does not implement interface 'Speaker'" message (count == 1), with
     the traceback/SSA/contract exclusions retained;
   - the MIR pin now requires exactly one ConstructIfaceValue and pins
     the exact type pair: iface_ty name == "Speaker", value_ty name ==
     "Dog".
3. (P2) History heading reworded: "three shapes; coupled hidden-return
   authority leaks" — shapes 1-2 share the missing block-tail routing;
   shape 3 additionally required the independent spec-return overwrite
   fix; the divergent-finalization issue stays a separate paragraph.

Verification: boundary module (12 test functions — the prior 11 plus the
new IIFE pin; count corrected per review-2026-08-04T18-36-06Z) + full
mandatory battery + hir_flow pins re-run post-changes: 140 passed;
`git diff --check` clean.

## POST-APPROVAL VERIFICATION (review-2026-08-04T18-36-06Z: substantive
review closed, no production change requested)

- Broad gate: my standalone lang-driver-test lane was STOPPED per
  Slawomir's direction — the gate is folded into HIS run-all-tests.sh
  full-suite run (superset).  Its result will be recorded here.
  Failures in this finding's changed surface come back through a normal
  handoff; a pass completes the release-announcement duty.

STATUS: READY FOR REVIEW.  Still gated on Slawomir separately:
APPROVAL-PENDING-2026-08-04T16-13-18Z (the two stale-comment edits in
test_stored_capturing_lambda_diagnostic.py — proposal below; blocks
nothing else).  Release announcement deliberately deferred to
post-review per PLAN §5.  Quick corpus check NOT run this pass (compiler
source changed heavily mid-pass; identity evidence is already explicitly
deferred to the batched corpus gate per Slawomir's ruling — flagging
that this slice adds compiler-source fingerprint drift to that batch).
- [x] /tmp/drift-announce checked: no inbound announcements to consume.
      Outbound release note is deliberately POST-REVIEW per PLAN §5
      ("after the contract is implemented and reviewed") — will be
      published at sign-off.
- [x] APPROVAL-PENDING posted for the two stale comments in
      test_stored_capturing_lambda_diagnostic.py (exact proposal below;
      does not block review of everything else).

## Approval proposal (existing-test comment edits, Slawomir gate)

lang/tests/driver/test_stored_capturing_lambda_diagnostic.py — both
comments claim the stored forms are "blocked by the hidden-lambda
return-authority leak (lambda-tail-coercion finding)"; that is now false
and names an ephemeral finding.  Proposed comment-only replacements:

1. Lines 213-214:
   `# The STORED form of the same body is blocked by the hidden-lambda`
   `# return-authority leak tracked in the lambda-tail-coercion finding.`
   →
   `# The STORED form of the same body is pinned green in`
   `# test_hidden_lambda_return_boundary.py (0.35.0 hidden-return fix).`
2. Lines 388-389:
   `# discard).  IIFE form: the stored form is blocked by the hidden-lambda`
   `# return-authority leak (lambda-tail-coercion finding).`
   →
   `# discard).  IIFE form; the stored form is pinned green in`
   `# test_hidden_lambda_return_boundary.py (0.35.0 hidden-return fix).`

No assertion, source-program, expectation, or collection change.

## 2026-08-03 snapshot (superseded status list)

## Status

- [x] Classified the observed internal SSA diagnostic as `LANGUAGE_BUG`.
- [x] Scanned `doc/refactor_triggers.md`; no trigger fires.
- [x] Checked cross-team announcements; `/tmp/drift-announce` absent.
- [x] Audited the existing positive and confirmed it covers named `HReturn`, not
  a lambda trailing value.
- [x] Saved the requested `Callback0<Speaker>`/`Dog` minimal repro.
- [x] Confirmed the block-tail callback fails with `Dog` versus `Speaker` at SSA.
- [x] Confirmed hidden MIR lacks `M.ConstructIfaceValue`.
- [x] Confirmed expression-body and explicit-return callback siblings compile
  and run with exit 0.
- [x] Confirmed a direct block-tail IIFE has the same failure.
- [x] Confirmed the non-implementing negative gets one clean checker diagnostic.
- [x] Added explicitly-run red MIR and full compile/run probes under this folder.
- [x] Proposed one shared hidden-function body normalizer and authoritative
  hidden return handling.
- [ ] Move/adapt the red probes into the in-tree test suite.
- [ ] Confirm both are red immediately before the compiler change.
- [ ] Implement the shared normalizer after K's #1 changes settle.
- [ ] Add the complete positive/negative matrix.
- [ ] Run focused and combined lambda gates.

## Evidence

Red MIR boundary:

```text
hidden instructions:
  ConstString(...)
  ConstInt(... value=7)
  ConstructStruct(... struct_ty=Dog ...)

expected but absent:
  ConstructIfaceValue(... iface_ty=Speaker, value_ty=Dog)
```

Red full driver boundary:

```text
error: typecheck contract failure: SSA return type does not match declared
signature for repro::__lambda_cb_main_0_0 in entry (15 vs 16)
exit 1
```

Green isolators before the fix:

```text
repro_callback0_speaker_explicit_return.drift  compile 0, run 0
repro_callback0_speaker_expr_body.drift        compile 0, run 0
repro_iife_speaker_expr_body.drift             compile 0
```

Clean negative before the fix:

```text
repro_callback0_nonimplementing_tail.drift:15:12:
error: 'Cat' does not implement interface 'Speaker'
exit 1
```

Run the red probes by explicit path:

```bash
./.venv/bin/python3 -m pytest -q work/finding-lambda-tail-coercion-positive/red_hidden_lambda_coercion_positive.py
```

The filename intentionally does not match pytest's default `test_*.py`
discovery pattern, so a repository-root pytest run will not accidentally absorb
the expected-red handoff tests.

## Resume notes for K

1. Refresh `git diff -- lang/driftc/type_checker.py lang/driftc/driftc.py` first;
   #1 is actively changing the return authority.
2. Preserve the final shared `_type_return_value`; this finding is downstream of
   it and should route regenerated block tails through it via `HReturn`.
3. Add one helper and use it in both hidden-lambda reconstruction loops.
4. Do not copy the enclosing `TypedFn` coercion tables across the deep-copy and
   normalization boundary.
5. Keep `spec.return_type_id` / `spec.return_type` authoritative; raw expression
   types are only an `Unknown` fallback and must consult coercion marks.
6. Verify the MIR pin first.  A merely diagnostic-free `compile_stubbed_funcs`
   call is insufficient: it is already diagnostic-free today while returning
   raw `Dog` MIR.

Only files under `work/finding-lambda-tail-coercion-positive/` were created by
this research.  No compiler, runtime, stdlib, or in-tree test file was edited.
