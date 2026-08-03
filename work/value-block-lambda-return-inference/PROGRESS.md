# PROGRESS: value-block lambda return-type inference (0.34.2)

Power-loss recovery point. Update after every meaningful step.

## Status legend
[x] done+verified  [~] done, needs test/verify  [ ] pending

## R2 REVIEW ROUND — all code findings CLOSED (2026-08-03)
- R2.1 no re-typing: statement-form/return tail now reads `expr_types[node_id]`
  (type recorded by `type_stmt`); never re-`type_expr`s a return value.  [x]
- R2.2 Void fallback: `_lambda_body_result` returns `self._void` for empty /
  value-less bodies (never None->Unknown).  Pins added; stored empty + return;
  lambdas compile+run.  [x]
- R2.3 shared coercion authority: raw-equality E-LAMBDA-BODY-TYPE DELETED; both
  HReturn and every lambda tail route through the new `_type_return_value`
  authority (auto-try + &T->T + iface/callback coercion + mismatch diagnosis).
  Negative (Int<-String) rejected identically to `return "x"`.  Owned
  concrete->interface/callback RETURN positive is blocked by the pre-existing
  E-AUTO owned-interface-return gap (a plain `fn ->Speaker{return dog;}` fails
  the same way) — my lambda is consistent with HReturn by construction; positive
  awaits [[project_ref_to_iface_coercion_gap]].  [x]
- R2.P2 for-desugar synthetic match now `statement_form=True`
  (ast_to_hir.py:1694).  [x]
- P1.3 both routes: HCall(fn=HLambda) (call_resolver 5100, live) and
  HInvoke(callee=HLambda) (type_checker 10004-10015) already CONSUME
  `type_expr(lam)`'s function type and extract param_types[-1]; the authority
  fix makes that inference correct.  6019 fallback hardened to consume the same
  function type instead of `... or unknown`.  No second body-inference path.  [x]

## R3 REVIEW ROUND — P1 return-authority completeness (2026-08-03)
Finding: `_type_return_value` assumed `type_expr(..., expected_type=...)`
diagnoses Int<-String, but ordinary variables/literals IGNORE the expectation →
non-interface mismatches fell through.  Repro'd on certified 0.33.90 (all
pre-existing): named-fn `return "x"` in `-> Int` = ConstructResultOk payload
ICE; stored `val f = || -> Int => { "x" }; f()` = SILENT compile+link
(miscompile); direct IIFE rejected only via checker raw-equality re-inference,
DOUBLED diagnostic.
- Fix A (type_checker.py `_type_return_value`): post-coercion-ladder diagnosis
  of ALL remaining incompatibilities, mirroring the HLet `val x: T = expr`
  initializer ladder — `_same_type` normalized-key equality (FORWARD_NOMINAL /
  alias), unknown suppression, Void skip (phase-2 checker owns
  void-return-with-value), Int/Uint/Uint64 literal allowances, else
  `return type 'X' does not match declared type 'Y'`.  Iface branch unchanged
  (implements-relation probe NOT added — stays with
  [[project_ref_to_iface_coercion_gap]]).  [x]
- Fix B (checker/__init__.py HCall(fn=HLambda)): consumes CallInfo when
  present (same shape as HQualifiedMember branch); body re-inference survives
  ONLY as the stub-pipeline fallback (no CallInfo there;
  test_checker_call_type_checks.py pins it).  Kills the second authority +
  the doubled diagnostic.  [x] (superseded by R4.2 full removal)
- Probes after fix: named literal/variable mismatch → 1 clean diag (no
  traceback); stored mismatch → REJECTED (was silent); direct IIFE → exactly
  1 diag.  Positive sweep (generic ident, &T->T return, Optional ctor,
  auto-try return, matching lambdas/IIFE) compiles AND runs exit 0 — no false
  positives.  [x]
- New pins in test_lambda_return_inference_boundary.py: named literal, named
  variable, stored-lambda mismatch, IIFE single-diagnostic count.  [x]
- Stub-pipeline pin updates (test_checker_call_type_checks.py):
  (a) lambda mismatch test now pins the authority message ("return type
  'String' does not match declared type 'Int'") — old raw-equality message no
  longer fires there (CallInfo present);
  (b) `return Ok(1)` HResultOk stub: contract updated from "no diagnostics" to
  "clean FnResult-vs-Int diagnostic, no crash" — real-source `return Ok(5)`
  NEVER worked (certified 0.33.90 dies with the ConstructResultOk
  payload-mismatch ICE; probe ok_ret.drift), so the clean rejection is an
  improvement.  DECISION FOR REVIEW: explicit `Ok(...)` at return is now
  checker-rejected instead of codegen-ICE; if `Ok()` should remain a legal
  legacy surface shape, the alternative is an HResultOk ok-payload comparison
  exemption in the authority.  [x] (R4.4: reviewer approved the rejection contract)
- NOTE pre-existing (NOT this slice, identical on certified 0.33.90): stored
  UNANNOTATED CAPTURING lambda `val g = || => { t + x }; g()` rejects with
  E-COPY-UNKNOWN + "call target is not a function value".

## R4 REVIEW ROUND (2026-08-03) — ALL ITEMS CLOSED, targeted-verified
Targeted evidence: 47/47 (new+updated pins across boundary / stored-capturing /
stmt-IIFE / stub-checker / trailing-match / try-IIFE / stored-in-match-arm /
bareword-capture / callback-dispatch files) + 198/198 (type_checker suite incl.
copy-unknown pins, explicit-capture diagnostics, hidden-lambda capture
machinery, closure lowering).  Full suite + corpus gate: USER-DRIVEN, not run.
Ruling: capturing-lambda failure + empty-IIFE ICE are IN-SCOPE LANGUAGE_BUGs.
Trigger scan (doc/refactor_triggers.md): NO entry matches either lambda bug —
deliverables stay root-cause fixes, no refactor escalation.

- R4.1 iface returns verify implements relation before recording (mirror of
  HLet 0.33.77 block).  Probes: `return Dog(n=7)` (fresh ctor) AND `return
  move dog` → Speaker COMPILES AND RUNS exit 0 (earlier R2 claim that the
  positive "isn't demonstrable" was WRONG — I had conflated the ownership
  copy-rejection of `return dog;` with a coercion failure); `return Cat(...)`
  → clean "'Cat' does not implement interface 'Speaker'".  [x]
- R4.3 Unknown suppression moved AHEAD of the interface branch.  [x]
- R4.2 CallInfo-less body re-inference REMOVED from checker `_infer_expr_type`
  HCall(fn=HLambda) branch — absence of CallInfo now emits the standard
  "typecheck contract failure: missing CallInfo" diagnostics (HVar-branch
  parity).  The "lambda with explicit return type must return a value" guard
  MOVED to the authority (`_lambda_body_result`), now covering empty AND
  value-less bodies, with a FLAT-TRAILING-THROW exemption (certified parity:
  stored `|| -> Int => { throw e; }` runs on 0.33.90; probes confirm both
  stored and direct flat forms compile+run in-tree, direct form is an
  IMPROVEMENT over certified's doubled rejection).  Non-flat divergent bodies
  still reject cleanly (codegen mislowers them; lambda-lowering follow-up).  [x]
- R4.4 `return Ok(5)` stays checker-rejected (reviewer-approved contract; no
  HResultOk exemption).  real-source driver pin ADDED (boundary file).  [x]

### LANGUAGE_BUG A — stored capturing lambda Unknown cascade: FIXED
Root cause was THREE stacked defects:
1. HLambda was the one HExpr with no `loc` field — every lambda diagnostic
   (incl. the capture rejection that DID fire) rendered "<unknown location>"
   and was invisible.  Fix: `loc` field on HLambda + stamped at both
   ast_to_hir construction sites.
2. `_require_copy_value` complained "cannot copy: type 'Unknown'" over
   already-poisoned bindings.  Fix: Unknown + an existing error diagnostic →
   suppress (without a prior error the tripwire stays).
3. call_resolver binding-call "call target is not a function value" repeated
   over the same poisoned binding.  Fix: same suppress-when-prior-error gate.
Result: `val g = || => { t + x }; g()` now emits exactly ONE spanned
diagnostic ("capturing lambdas cannot be coerced to function pointers", with
a callbackN/IIFE guidance note).  Also heals the non-flat throw-only stored
lambda (was the same cascade on certified).  Language contract unchanged:
bare stored capturing lambdas remain rejected (callback iface is the
supported vehicle); this is a diagnosis fix, not a feature.  [x]

### LANGUAGE_BUG B — statement-position IIFE MIR ICE: FIXED (3 layers)
`(|| => {})();` as a STATEMENT routed HCall→`_lower_call`→INDIRECT→
`_lower_indirect_call`→`lower_expr(HLambda)` → "No MIR lowering for HLambda".
Expression position already routed via `_lower_lambda_immediate_call`.  Fixes:
1. `_visit_stmt_HExprStmt` HCall/HInvoke fast paths EXCLUDE lambda callees so
   statement IIFEs take the generic tail (immediate-call lowering + owned-
   result drop); `_lower_indirect_call` gained a labeled AssertionError
   backstop (delegating from there would double-wrap throw checking).
2. `_lower_lambda_immediate_call`'s re-derived return type now falls back to
   VOID for value-less/empty block bodies (was Unknown → tripped the hidden-
   lambda "must end with a value or return" assertion) — the MIR-side sibling
   of the checker's R2.2 Void fallback.
3. Its non-throw call now emits `Call(dest=None)` + void value for Void
   hidden lambdas (LLVM rejects capturing a void call's result).
Probe stmt_iife.drift: empty, value-less, discarded-owned-String, and
can-throw-in-try statement IIFEs all compile AND run exit 0.
New pins: test_stmt_position_iife.py (4 tests).  [x]

### Empty-IIFE ICE — investigated, PRE-EXISTING, out of scope
`(|| => {})()` ICEs in MIR (`No MIR lowering for HLambda` in
`_lower_indirect_call`).  Isolation proof: flipping the Void fallback back to
`return None` STILL ICEs → not caused by this fix.  It's an independent
lambda-inline gap for empty-body IIFEs.  Void pins therefore use the STORED form
(`val f = || => {}; f();`), which compiles+runs.

## Checklist

### P1.1 — one-pass authority (no double-typing)
- [x] Replace `type_block()` + separate re-typing with `_lambda_body_result`
      (one scope, prefix stmts normal, final value expr typed once in value ctx)
      — type_checker.py.  Verified: try/plain/trailing-match pass.
- [ ] **REVIEW R2.1**: statement-form / explicit-return tail STILL double-typed —
      after `type_stmt(_last)` types the returns, the authority calls
      `type_expr()` on the return value AGAIN (~7800).  Fix: read
      `expr_types[_ret_expr.node_id]` (recorded), or collect while typing; do NOT
      re-type.  Affects: final HReturn; statement-form match w/ returns; returns
      whose arm-local scope was popped.

### P1.2 — authoritative statement_form on HMatchExpr
- [x] Add `statement_form` field (hir_nodes.py); set in `_lower_match_expr`;
      preserve in alpha-renamer, borrow_materialize, place_canonicalize.
- [ ] **REVIEW R2.P2**: the for-desugar synthetic match (ast_to_hir.py:1694) is
      statement-form but inherits `statement_form=False` → set True.  (ConstShare
      synthetic match is correctly value-form; leave False.)

### P1.1 — Void fallback
- [ ] **REVIEW R2.2**: `_lambda_body_result` returns None for `|| => {}` and
      `|| => { return; }` → lambda_ret_type stays None → becomes Unknown, not
      Void.  Must return `self._void` for empty/value-less body.  Pins: empty +
      explicit-return-void unannotated lambdas.

### Declared/expected comparison (E-LAMBDA-BODY-TYPE)
- [~] Added raw normalized-type equality compare in the authority.
- [ ] **REVIEW R2.3**: raw equality is wrong — a concrete tail returned to an
      expected INTERFACE return needs the same recorded coercion HReturn does
      (type_checker.py:12749).  Otherwise `Callback0<Speaker>` ← `Dog` tail =
      false rejection.  Fix: share ONE return-compatibility/coercion authority
      between HReturn and lambda-tail.  Pins: Int ← String NEGATIVE + Dog →
      Speaker POSITIVE (proving the lowering-visible coercion mark is recorded).

### P1.3 — direct HCall(fn=HLambda) CallInfo  (OPEN)
- [ ] Route direct `HCall(fn=HLambda)` (call_resolver.py:6019) + `HInvoke`
      through the lambda-result authority; record exact inferred return in
      CallInfo.  Confirmed the direct-call path does NOT pre-type the lambda
      (typing it there is safe/first).
- [ ] Checker-boundary assertion for BOTH routes (not only arithmetic use).

### Version
- [x] lang/versions.py 0.34.1 → 0.34.2 (ABI 22 unchanged).

### Tests
- [x] Repurposed stale Void-inference negative → compile/run positive
      (test_lambda_trailing_match_value.py).
- [x] Plain-value-block IIFE pin (test_try_expr_immediate_lambda.py).
- [ ] Empty + explicit-return-void unannotated lambda pins (R2.2).
- [ ] E-LAMBDA-BODY-TYPE: Int←String negative + Dog→Speaker positive (R2.3).
- [ ] CallInfo boundary assertion, both routes (P1.3).
- [ ] Broader lambda/closure/try regression (only a targeted subset run so far).

## Changed files (uncommitted; +R3/R4 additions)
```
lang/driftc/type_checker.py            (authority + guard + copy-cascade gate + capture notes)
lang/driftc/checker/__init__.py        (R4.2 fallback removal → CallInfo contract)
lang/driftc/checker/call_resolver.py   (call-target cascade gate)
lang/driftc/stage1/hir_nodes.py        (HLambda loc field)
lang/driftc/stage1/ast_to_hir.py       (HLambda loc stamping ×2)
lang/driftc/stage1/borrow_materialize.py
lang/driftc/stage1/place_canonicalize.py
lang/driftc/stage2/hir_to_mir.py       (stmt-IIFE routing + Void ret fallback + Void call dest)
lang/versions.py
lang/tests/driver/test_try_expr_immediate_lambda.py
lang/tests/driver/test_lambda_trailing_match_value.py
lang/tests/driver/test_checker_call_type_checks.py
lang/tests/driver/test_lambda_return_inference_boundary.py   (NEW)
lang/tests/driver/test_stored_capturing_lambda_diagnostic.py (NEW)
lang/tests/driver/test_stmt_position_iife.py                 (NEW)
```

## Gate order (corpus is MANUAL; not in run-all-tests.sh anymore)
1. ownership-corpus-check --fresh
2. review + ownership-corpus-promote
3. commit golden baseline/fingerprint
4. full suite
(Do NOT verify corpus before re-promotion — stale 0.34.1 baseline.)

## Evidence log
- try file 5/5; callback0 (stmt-form match) pass; trailing-match 3/3;
  restructure batch 8 passed.
- new boundary pins 6/6; combined lambda/try/trailing-match 14/14.
- FOCUSED REGRESSION (2026-08-03): `pytest -n16 driver+checker+borrow_checker
  -k "lambda|closure|try|callback|invoke|return|capture|match|borrow|fnptr"` →
  **837 passed, 2 skipped, exit 0** (14m13s).  No regressions from the authority
  extraction (HReturn + all lambda tails share `_type_return_value`).
- FULL SUITE (run-all-tests.sh, perf + memcheck + ASAN, corpus excluded):
  **GREEN, exit 0** (2h48m).  Perf 2246 passed/51 skipped; memcheck 1387 →
  1381 ok / 6 skipped / **0 failed**, leaks=0; ASAN OK; deploy 272 passed.
  No FAILED / definitely-lost / ERROR SUMMARY / traceback.
- Corpus re-promote: NOT run (user drives; I cannot commit the baseline).
  Expect ownership-counter drift on value-block-lambda fixtures at promote.
