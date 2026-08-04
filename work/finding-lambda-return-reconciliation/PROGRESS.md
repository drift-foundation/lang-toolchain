# Progress: inferred-lambda return reconciliation

Last updated: 2026-08-04 (K, from review-2026-08-04T20-27-32Z; the P1.3
sibling was folded in per review-2026-08-04T20-32-09Z).  Supersedes the
2026-08-03 research checklist: its five open items are all completed
below; its research evidence remains valid history.

STATUS: SIGNED OFF FOR BROADER GATES (review-2026-08-04T21-10-15Z: all
revision-1 findings closed; no further implementation change; child
finding separately signed off; validator strict; version 0.35.0 pending,
ABI 22).  Combined focused/smoke gates landed green below.  REMAINING:
the repository's broader gate sequence (user-run), then the 0.35.0
release announcement.

Previously: REVISION 2 READY FOR REVIEW — responds to
review-2026-08-04T20-55-56Z (required child fix + two P2s).

## REVISION 2 (review-2026-08-04T20-55-56Z)

1. (P1) Child findings/finding-nested-lambda-intrinsic-callinfo: FIX IN
   TREE — see the child's PROGRESS.md.  Summary: TypedFn construction now
   partitions callsite-indexed tables (`call_info_by_callsite_id`,
   `instantiations_by_callsite_id`) by finalized-body ownership via a
   full post-rewrite walk that descends into still-present lambdas;
   extracted stored-lambda entries no longer leak to the parent;
   validator untouched (strict); live maps unpruned for the LambdaFnSpec
   snapshot.  Red-first module
   lang/tests/driver/test_nested_lambda_callinfo_ownership.py (3 red →
   4/4 green incl. parent+extracted coexistence, immediate-IIFE
   counter-boundary, structural ownership pin with an extraction guard).
2. (P2) Stable-contract pin added:
   test_mismatch_diagnostic_code_and_span_are_stable — exact code
   E-LAMBDA-INFERRED-RETURN-MISMATCH, exact message, and the diagnostic
   span pinned to the offending HReturn's own non-default Span.
3. (P2) Contextual/downstream claim corrected in BOTH the module
   docstring and doc/history.md: contextual annotated results were
   already rejected by the declared-return authority; only the
   uncontextual shape depended on the hidden re-check.

Gates LANDED, all green: combined battery (both new type_checker files +
the ownership module + child PLAN §5 list + parent battery, 14 files) —
154 passed; 5-suite compiler smoke — 1115 passed; `git diff --check`
clean.

## Implementation (all PLAN invariants held)

1. RED FIRST: work probes rerun on the pre-fix tree (2 failed — the
   primary diagnostic absent), then adapted into
   lang/tests/type_checker/test_inferred_lambda_return_reconciliation.py
   BEFORE the compiler change.
2. Collector: `lambda_return_observation_stack` (per-function ephemeral
   state) of `(has_value, effective_type, span)` tuples; pushed/popped
   around EACH lambda's body typing (both body_expr and body_block,
   try/finally with identity assert).  Nested lambdas push their own
   list; named functions and the hidden re-check see an empty stack.
3. `type_stmt(HReturn)` now PRESERVES `_type_return_value`'s returned
   effective type and records it on the innermost collector (bare
   `return;` records Void).  No re-typing anywhere; the stored type
   survives arm-scope pops and reflects expected-return coercions.
4. `_find_return_expr` DELETED.  Statement-body/terminal-tail candidate
   selection reads `_first_valued_observation(observations)` (the list is
   passed into `_lambda_body_result` explicitly).  Deterministic rule
   unchanged: value tail, else first valued return, else Void.
5. Reconciliation runs ONLY when `infer_return_from_body` (no annotation,
   no concrete contextual return) and the candidate is concrete:
   each observation compared via the extracted `_same_normalized_type`
   helper (now shared with `_type_return_value`'s mismatch ladder —
   the ~12309 HLet copy left alone per plan); Unknown on either side
   suppressed; candidate stays installed after diagnosing.  Stable
   diagnostic: E-LAMBDA-INFERRED-RETURN-MISMATCH
   "return type 'X' does not match inferred lambda return type 'Y'"
   at the offending return's span.  No late coercion/LUB; no
   call-resolver inference route; no side-table copying.

## Evidence findings recorded (plan told me to verify; both confirmed)

- The minimal-repro driver source with `val result: Int = f(false)` is
  CONTEXTUAL, not inferred: the pending-lambda call path builds
  `fn_ret=expected_type` from the annotated binding, so the primary
  authority correctly diagnoses "does not match declared type 'Int'" at
  the return's original visit — the driver diagnostic did NOT come only
  from the hidden re-check once inspected on the fixed tree.  The driver
  negative matrix therefore has BOTH: an UNANNOTATED variant pinning
  exactly one E-LAMBDA-INFERRED-RETURN-MISMATCH (primary
  reconciliation authority), and the annotated variant pinning exactly
  one declared-type diagnostic with NO inferred-mismatch duplicate.
- callback0 INSIDE a stored lambda fails with
  E_INTRINSIC_CALLINFO_MISSING_NODE — PRE-EXISTING (identical failure on
  the pre-fix `git archive HEAD` scratch tree); not this slice's
  regression.  The nested-isolation positive uses an unannotated inner
  IIFE instead.  Candidate follow-up finding if worth tracking.

## Test matrix (lang/tests/type_checker/test_inferred_lambda_return_reconciliation.py — 13 tests, all green)

Direct primary-boundary: prefix-return-vs-tail (call stays Int + exactly
one stable mismatch), statement-only branches, bare-return-vs-valued-tail
(Void message), upstream-Unknown suppression (unknown-name preserved, no
cascade), nested-lambda isolation (inner String never enters outer
collector).  Driver: inferred single-primary-diagnostic negative,
contextual single-declared-diagnostic companion, statement-form match
mismatch through an ARM-LOCAL binding (captured before its arm scope
popped), and compile/run positives (prefix agrees, statement-only agrees,
statement match agrees, nested isolation, all-bare Void).

## Version/docs

DRIFTC_VERSION stays at pending 0.35.0 (no second bump); ABI 22.  History
folded into the pending 0.35.0 entry (reconciliation + P1.3 paragraphs).

## Gates

LANDED, all green: focused battery (both new files +
boundary/inference/trailing-match/try-IIFE/hidden-boundary/slice12/
stored-diagnostic + stage1 callinfo) — 91 passed; 5-suite compiler smoke
(type_checker/checker/stage1/stage2/parser, incl. the call_resolver
deletion surface) — 1114 passed; `git diff --check` clean.
