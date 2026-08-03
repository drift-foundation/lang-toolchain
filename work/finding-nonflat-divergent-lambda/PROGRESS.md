# PROGRESS: finding-nonflat-divergent-lambda

STATUS: SIGNED OFF (terminal) — review-2026-08-03T17-29-27Z, independently
verified by the reviewer (stage1 file 11/11, full stage1 89/89, both e2e
cases green, diff clean).  The full-suite reopening is closed; no review
work remains.  User-driven gates before merge: RESTART the full suite (the
prior run stopped at the now-fixed stage1 failure) + ownership-corpus
check/promote; folder deletion after merge+closure per the ephemeral rule.

Revision 8 (below) had reopened the finding via review-2026-08-03T17-18-59Z
(full-suite failure: stale stage1 capture diagnostic pin) and completed the
approved capture-kind contract migration through the remaining test surface.

## Revision 8 (test migration, Slawomir-approved edits 2026-08-03)

Slawomir explicitly approved: update the two existing implicit-borrow
expectations, add a stage1 explicit-copy companion, keep the compiler and
both .drift sources unchanged, assert the implicit-borrow case does NOT emit
the generic message.

1. lang/tests/stage1/test_function_references.py::
   test_capturing_lambda_rejected_for_fn_pointer — implicit read of outer
   `y` is a shared BORROW: now asserts the borrowed-capture diagnostic AND
   the absence of the generic value-capture message.  NEW companion
   test_value_capture_lambda_rejected_for_fn_pointer pins the generic
   message (and borrowed-message absence) for an explicit `copy` capture —
   borrowed and value captures separately pinned at stage1.
2. lang/tests/codegen/e2e/fnptr_lambda_capture_rejected/expected.json —
   implicit-borrow source (`|x| => x + k`, unchanged) now expects the
   borrowed-capture diagnostic.
3. callable_capturing_lambda_not_fn_ptr (explicit `captures(copy y)` —
   value capture) verified STILL VALID with the generic message: green.
4. Stage1 conflation scan: no other stage1 assertions mix the two classes;
   the driver-file mentions of the generic message are docstring prose or
   value-capture pins.

Verification: complete stage1 suite 89/89; both e2e cases ok
(fnptr_lambda_capture_rejected, callable_capturing_lambda_not_fn_ptr).
No compiler or .drift source changes in this revision.

## Revision 7 changes (contract migration)

1. e2e closures_explicit_captures_move_use_after_move_rejected migrated to
   the supported representation: `core.callback0(| | captures(move x)
   nothrow => ...)` followed by `return x;` — the borrow checker's
   "use after move of 'x'" contract is preserved (RUN: ok via the e2e
   runner), pinning that a move capture consumes its source at CALLBACK
   CONSTRUCTION even when the callback is never invoked.
2. Spec taxonomy §22.0.1 rewritten: a capturing literal may be immediately
   invoked or converted through a supported representation; no standalone
   anonymous closure-value type exists in v1.  The explicit-captures spec
   change request's escaping/raw-closure-value passages are marked
   "Superseded (current v1 contract)" with pointers to the spec sections —
   one current contract, history retained.
3. Bare-storage fixture inventory repeated post-migration: the migrated e2e
   was the only remaining bare stored-capture Drift fixture outside the
   intentional negatives.
4. Focused contract files re-run after migration: 66/66 (hir_flow 16,
   uninvoked-stored-lambda 17, stored-capturing/divergence 22,
   explicit-capture-diagnostics 11) + the migrated e2e case ok.

## The finding (recap)

Non-flat divergent lambda bodies were checker-rejected "must return a value"
to mask lowering defects; the pinned test enshrined the rejection.
LANGUAGE_BUG + no-semantic-masking violation.

## Current implementation (revision 6)

ONE shared flow/effect module: lang/driftc/stage1/hir_flow.py.

Terminal flow — `block_exits` classifies every CFG path (FALLTHROUGH / THROWS /
RETURNS_VALUE / RETURNS_BARE) with phase-2 semantics: literal-`if` folding,
HUnsafeBlock, statement-position match by arm blocks, terminal-`throws` calls
via injected resolver; break/continue are empty exit sets (dead code after
them stays dead); loops terminal iff no reachable loop-local break; a
catch-all swallows the body's handled THROWS; catch arms are DEAD when the
attempt cannot throw (shared effect decision via injected `call_can_throw`).
The phase-2 checker delegates `_is_terminal_block` here; the lambda
value-less-body guard accepts iff exits ⊆ {THROWS}.

Throw effect — `lambda_body_can_throw` with the SAME reachability rules:
sequential capping (`_block_can_throw`/`_stmt_can_throw`), literal-`if`
folding, try-handler gating, LAZY expression evaluation (short-circuit
`and`/`or` with literal-LHS folds, literal-cond ternary, failure-only assert
message).  Deliberate variant classification (special / reflective-recursive /
leaf; unknown variants conservatively can-throw); nested uninvoked HLambda is
a construction boundary; both consumers (type_checker authority, stage2
fallback) delegate with only the CallInfo decision injected.

Terminal-`throws` tail calls in lambda bodies are statements on both sides
(checker reclassification after one-pass typing; statement-route lowering).

Scheduler — the captureless-lambda drain reaches a joint fixed point over
EVERY producer: hidden-lambda specs (re-enterable drain; error returns
propagate), thunk specs (callable, idempotent — thunk completion is part of
the quiescence predicate, closing the instantiation-registers-thunk edge),
generic instantiations (late NON-wrapper instantiated fns get real HIR→MIR
lowering + can-throw classification), late wrappers/fn_infos.  Each drained
spec registers its typed fn and queues instantiations; each lowering harvests
hidden/synth producers.
REBUTTAL (rev-6, thunk-first-produced-by-concrete-instantiation regression):
no source spelling can FIRST-produce a thunk inside `_drain_instantiations` —
fn-reference coercion requires a concrete callee signature (probe:
`Fn(T) -> T = genericfn` rejects "no overload ... matches" at template check),
and a concrete thunk key inside a generic template registers at template
check, deduplicated thereafter.  The quiescence predicate nevertheless
includes thunk completion, so the edge is closed even if a future spelling
reaches it.

v1 RULING (2026-08-03, user): a bare stored capturing lambda is INVALID even
when never invoked — v1 has no anonymous closure-value type.  "Value captures
may escape" means through a SUPPORTED representation (core.callbackN / an
accepted Fn-bounded conversion).  Implementation: the end-of-check flush
rejects every capturing pending lambda (borrow captures → the borrowed-
capture diagnostic; value captures → "bare capturing lambdas cannot be stored
in v1; wrap with core.callbackN(...) or invoke immediately"); the
construction-only MIR path is DELETED; a raw HLambda reaching HLet lowering
is a labeled contract assertion; the spec's escape wording is reconciled
(doc/design/drift-lang-spec.md §22.2.3).  This closes rev-6 review finding 1
entirely: no unchecked body can slip through (the binding rejects first), no
uninitialized binding exists to move/borrow.
Flush totality for CAPTURELESS lambdas: concrete → LambdaFnSpec + fnptr
const; unannotated params → "cannot infer" rejection; residual-Unknown fn
type → rejection.

## Child finding findings/finding-uninvoked-stored-lambda-lowering: CLOSED
(under the ruling: captureless forms lower; capturing forms reject at the
binding; supported escape via core.callbackN compiles AND runs with capture
effects at construction).

## Pins (current, accurate counts)

- lang/tests/checker/test_hir_flow.py — 16 unit tests: boundary pair +
  lambda-as-argument; wrapper descent (map/f-string/kwarg); unknown-variant
  conservatism; catch-all swallow (effect + exit-set); block_exits kind
  matrix; break/continue incl. dead-break/dead-return loops; CFG-reachable
  effect (dead literal arms, dead tails, dead catches, live counterparts);
  LAZY forms (and/or/ternary/assert dead + live + non-literal); terminal-call
  predicate.
- lang/tests/driver/test_uninvoked_stored_lambda.py — 17 tests: captureless
  4-way matrix + store-then-call control; bare-storage rejections (implicit
  shared borrow single spanned diagnostic, implicit mut borrow, explicit &x,
  value captures, move-of-binding, unchecked-body); "cannot infer" rejection;
  callback escape positive (copy+move captures, never invoked, runs);
  nested-generic positive; during-drain producers (mid-drain generic spec /
  late thunk / late hidden lambda — all compile AND run).
- lang/tests/driver/test_stored_capturing_lambda_diagnostic.py — 22 tests:
  original capturing-diagnostic pins; divergence positives (if/else both
  branches, capturing IIFE, statement-form match both arms, nested block,
  try/catch all-terminal, unsafe terminal, non-breaking while-true,
  terminal-`throws` tail call IIFE+named, dead-break-after-continue,
  dead-catch-break, dead-throw-after-continue); dead/live lazy-operand
  nothrow driver pins; negatives (fallthrough, bare return, nothrow-IIFE,
  lazy-live).
- Updated per the ruling: test_explicit_capture_diagnostics.py — the stale
  silent-acceptance pin REPLACED by bare-storage rejection; the non-Copy
  copy-capture Copy-authority pin moved to the SUPPORTED callback form, with
  a bare-form binding-rejection companion.

## Verification state

- FULL TARGETED BATTERY (revision 6): 162/162 across 18 files (the three
  above + stmt-IIFE, boundary, stub-checker, trailing-match, try-IIFE,
  closure-void-tail, hidden-lambda-captures, callback-dispatch,
  terminal-`throws` phases 1/2/3.5, void-callback throw-check,
  stored-in-match-arm, explicit-capture-diagnostics, bareword-capture).
- Probe evidence in this folder; certified-0.33.90 baselines recorded for
  every intentional divergence.
- Full suite + corpus gate: user-driven, not run.

## Blocked/routed shapes (explicitly NOT pinned here)

STORED throwing lambdas' hidden return derivation (`_hidden_lambda_ret_type`
authority leak) — QUEUED work/finding-lambda-tail-coercion-positive (evidence
+ repros appended there).

## Checklist (rev-6 closure bar)

- [x] Finding 1: value-capture storage semantics reconciled per user ruling
      (reject at binding; construction path deleted; spec + pinned tests
      updated under that authority; unchecked-body and move-of-binding holes
      closed and pinned)
- [x] Finding 2: thunk completion in the quiescence predicate + documented
      rebuttal of the instantiation-first-thunk source spelling
- [x] Finding 3: lazy expression effect traversal (unit + driver dead/live
      pins)
- [x] Finding 4: green explicit-capture test reconciled (the Copy authority
      is the borrow checker's capture validation; its pin now exercises the
      supported callback form)
- [x] Finding 5: this file rewritten with accurate counts; child refreshed
- [x] Full targeted battery green — 162/162 (18 files)
- [x] Reviewer sign-off (review-2026-08-03T16-55-27Z)
