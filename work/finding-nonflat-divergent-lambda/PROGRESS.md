# PROGRESS: finding-nonflat-divergent-lambda

Protocol echo: work-mailbox-v1 2026-08-04T13-32-37Z

## Corpus-verify drift — independent implementer confirmation (review-2026-08-04T14-09-36Z)

All four reviewer claims INDEPENDENTLY CONFIRMED against the committed
baseline (lang/tests/ownership_corpus/reviewed-baseline/) and the retained
fresh actual (build/tmp/ownership-corpus-actual/), by direct JSON
comparison — not by re-reading the reviewer's evidence:

1. Universe identity: 1338/1338 fixtures, name sets equal, inclusion rule
   equal, ordered compiled_ok 969/969 equal, ordered failed 369/369 equal,
   exclusions 49/49 equal.  aggregate.json byte-identical
   (63c42910…) and projections.json byte-identical (1ad6cb45…) — my
   independently computed sha256 digests match the reviewer's exactly.
   Zero projection/aggregate/bucket/hard-gate drift.
2. Exactly three fixture sha256 diffs: bitwise_uint_ops,
   closures_explicit_captures_move_use_after_move_rejected,
   fnptr_lambda_capture_rejected — all six hash values match the
   reviewer's evidence byte-for-byte.  Attribution: `git diff
   e211863c..HEAD -- lang/tests/codegen/e2e` touches EXACTLY those three
   files; contents are the Slawomir-approved cast<Int> entrypoint edit,
   the approved callback0 migration, and the split capture diagnostic
   (`closures with borrowed captures are non-escaping in v0`).
3. Environments: baseline {abi 22, driftc 0.34.1, tool 1.7.1} → actual
   {abi 22, driftc 0.34.2, tool 1.7.1}.  ABI unchanged as expected.
4. build/tmp/ownership-corpus-projection.json mtime 2026-08-02
   23:31:53 -0600 — STALE (predates the fresh verification); must not be
   used for promotion.

CHECK-LANE RESULT: `just ownership-corpus-check` completed (exit 0,
2026-08-04 08:12 local) — reused 1335 projections, recompiled the 3
changed fixtures, exported a CURRENT candidate to
build/tmp/ownership-corpus-projection.json (stale 2026-08-02 handoff
overwritten).  Its UNIVERSE MISMATCH warning names exactly the 3 expected
fixtures.  Candidate vs retained full-run actual, compared directly:
- universe name→sha256 maps IDENTICAL (1338/1338); compiled_ok, failed,
  excluded, inclusion rule all equal;
- all 969 per-fixture projections EQUAL;
- all 14 aggregate counters EQUAL;
- candidate `observed` set = exactly the 3 drifted fixtures.

CONCLUSION (implementer, concurring with reviewer): identity/fingerprint
refresh only — zero ownership-semantic drift.  The corpus is ready for
re-baselining, gated on Slawomir's authorization.
ownership-corpus-promote NOT run; reviewed baseline NOT edited.

TERMINAL SIGN-OFF (review-2026-08-04T15-16-39Z): promotion accepted, no
findings; reviewer independently corroborated the diff scope, byte-identity
of aggregate/projections, composites (8b100d17… / 59a61c8e…), and the
exhaustive disjoint observed(3)/projected(1335) partition.  IMPL token
consumed; no review token per terminal rule.  REMAINING (user): commit the
4-file baseline diff.

PROMOTION EXECUTED (review-2026-08-04T14-20-55Z request): `just
ownership-corpus-promote` run by K in the SAME unchanged shell that
produced the accepted candidate (PATH normal, candidate composite
59a61c8e… confirmed present before running).  First attempt was killed at
10 min by a runner timeout (SIGTERM) — verified fail-closed: `git status`
on the corpus tree was clean, no partial mutation.  Rerun without
timeout: exit 0; fresh full compile of 1338/1338 fixtures (2430 s)
matched the handoff expectation exactly and replaced the reviewed
baseline.  No PATH override, no manual baseline edit, projection not
regenerated.

Tracked diff independently inspected (exact scope):
- aggregate.json and projections.json: ZERO diff lines — byte-identical,
  as required;
- manifest.json: driftc 0.34.1→0.34.2 plus EXACTLY the three accepted
  fixture sha256 updates (values match the accepted evidence);
- fingerprint.json: run-snapshot composite 082fdd75…→8b100d17…,
  static_universe_digest, audit_tool, compile_source, toolchain composite
  d5303418…→59a61c8e… (the accepted check-lane composite);
- metadata.json: duration 1913.7→2429.9 s + start timestamp;
- BASELINE.md: provenance table (0.34.2 / ABI 22 / new composites).
Commit is Slawomir's (git writes reserved); diff left in working tree.

ACCEPTED (review-2026-08-04T14-15-40Z): reviewer independently verified
the candidate comparison and consumed IMPL-PENDING-2026-08-04T14-13-19Z.
Both roles conclude identity/fingerprint refresh, zero ownership-semantic
drift; Slawomir's promotion condition satisfied.  Promotion execution and
the post-promotion tracked-baseline diff are handled OUTSIDE the
implementer role and will be verified separately.

CHECKPOINT (review-2026-08-04T13-23-04Z, informational, recorded
2026-08-04): full gates through ASAN are GREEN — `just perf-protocols` ok;
complete `DRIFT_MEMCHECK=1 just test` ok (lang tests Success; ownership
package-boundary gate 4/4 incl. pkgb_result_ok_string_heap and
pkgb_throws_auto_try_result); standalone `DRIFT_ASAN=1 just test` clean
per Slawomir.  The first run-all-tests.sh abort was runner-process
interference (reviewer edited the executing script; bash resumed at a
stale offset), NOT a test failure.  REMAINING: `just
ownership-corpus-verify` in flight — if it reports drift, preserve output
and route back through this finding; NO promote/rewrite of the baseline.
No implementation action requested; token consumed on recording.

STATUS: REVISION 12 SIGNED OFF (review-2026-08-04T03-50-42Z, terminal:
IMPL token consumed, no new REVIEW-PENDING).  Both children closed.
Post-sign-off hygiene: the ephemeral work/-path reference the review noted
in test_out_of_scope_name_resolution.py was removed (standing repo rule —
work/ is wiped at branch-close); file re-verified 4/4, and a
`work/finding` sweep over lang/, tools/, stdlib/ is clean.  The remaining
gap-observations were explicitly skipped per Slawomir's direction.
REMAINING GATE: user-driven full run-all-tests.sh.

Previous status: REVISION 12 READY FOR REVIEW (FULL SCOPE) — responds to
review-2026-08-04T03-04-52Z (two P1 children) AND the approval relay
review-2026-08-04T03-38-51Z.  BOTH children are resolved: child A
(finding-return-poisoned-catch-binder) fixed in tree; child B
(finding-return-uint-main-compatibility) closed with the
Slawomir-approved one-line fixture edit.  Combined focused gates green:
bitwise_uint_ops ok + catch_binder_scope_leak ok (e2e), 19/19
return-boundary + out-of-scope-resolution driver pins, plus the child-A
batteries recorded below.  The interim partial-scope IMPL token
(03-41-08Z) was retracted unconsumed when the approval relay landed;
this handoff covers both children.

## REVISION 12 — full-suite reopen: two return-authority children (review-2026-08-04T03-04-52Z)

Child summaries (details in each child's PROGRESS.md):

1. findings/finding-return-poisoned-catch-binder — FIX IN TREE.  Deleted
   the function-wide `binding_names` fallback in phase-1 HVar resolution
   (unauthored belt-and-braces from mixed commit 6fda4df3); names resolve
   only via binding identity or active lexical scope.  Phase-1's
   unknown-name diagnostic now carries the stable `E-UNKNOWN-NAME` code
   (same meaning/code as the phase-2 checker's).  e2e
   catch_binder_scope_leak ok; new 4-pin driver file
   test_out_of_scope_name_resolution.py; sibling-name-reuse suite green;
   34-file binder/scope/lambda battery 265+7 passed; compiler suites
   re-run in flight after the code stamp.
2. findings/finding-return-uint-main-compatibility — BLOCKED ON APPROVAL.
   Agreed with reviewer: stale fixture, not a coercion gap; NO compiler
   change.  Proposed one-line edit `return cast<Int>(x);` probed out of
   tree: compiles, exit 254.  APPROVAL-PENDING-2026-08-04T03-35-21Z
   raised; edit will be applied only after explicit approval.

Protocol cleanup per this review: stale REVIEW-PENDING-2026-08-03T23-05-59Z
consumed by K; REVIEW-PENDING-2026-08-04T03-04-52Z consumed on pickup after
this status update.  (Earlier context: revision 11's interim review found
no static issues; the run-all-tests.sh gate that reopened this finding is
the one that surfaced these two e2e failures.)

## REVISION 11 — registry probe no longer masks failures (review-2026-08-03T22-53-57Z)

1. (P1) Removed the `try/except Exception: _free_cands = []` guard around
   the ordinary-owner probe's `callable_registry.get_free_candidates(...)`
   call in checker/call_resolver.py.  The probe now calls the registry
   directly — the same authority the real free-call resolver consults — so
   a registry/visibility defect propagates through normal ICE containment
   instead of being converted into a false user-facing
   E-CTOR-EXPECTED-TYPE.  A comment pins the no-guard contract at the site.
   No other change; no fallback value exists anymore.

Verification: reviewer-required focused set rerun POST-patch — 31/31
(test_unqualified_ctor_name_precedence.py: 3 collision positives + both
no-context negatives; test_const_share_phase5_implicit_duplication.py:
structural + runtime pair; test_lambda_return_inference_boundary.py: all
Result-boundary pins).  The pre-patch full driver run was STOPPED per the
review (not reported as evidence).

Interim review review-2026-08-03T23-05-59Z: NO remaining static/code
finding; terminal sign-off pending only the post-patch driver gate.  Per
Slawomir's direction the standalone lang-driver-test rerun was STOPPED and
the final gate is folded into a user-run `run-all-tests.sh` (superset:
driver lane + all other suites); its result will be recorded here.

Revision 8 (below) had reopened the finding via review-2026-08-03T17-18-59Z
(full-suite failure: stale stage1 capture diagnostic pin) and completed the
approved capture-kind contract migration through the remaining test surface.

## REVISION 10 — fallback precedence + runtime ownership pin (review-2026-08-03T22-33-42Z)

1. (P1) Constructor-context fallback now fires ONLY when no ordinary
   candidate owns the call: it checks for a visible struct of that name and
   for free-function candidates (callable_registry) BEFORE treating an
   arm-name spelling as a constructor context.  All three reviewer collision
   repros compile AND run (user `fn Some`, `struct Some`, `fn Ok`); both
   no-context negatives keep the single clean E-CTOR-EXPECTED-TYPE.  New
   pins: lang/tests/driver/test_unqualified_ctor_name_precedence.py (3
   compile/run positives + the no-context negative).
2. (P1) The Phase-5 runtime companion now DEREFERENCES both owners:
   `use_arc` calls `a.get()` after `Ok(a)`, the `Ok(v)` arm calls `v.get()`,
   and the exit code derives from both byte lengths (7 + 7 - 14 = 0); the
   binary run has a timeout and captured stderr.
3. (P2) Evidence hygiene: the `rg -n --glob '*.py' '\bHResultOk\b'
   lang/driftc lang/tests` gate is now truthfully ZERO (the three prose
   references reworded to point at the doc/history.md note); the superseding
   doc/history.md entry is added (public-Ok contract + precedence rule, old
   entries preserved); the EOF blank line and the over-indented comment are
   fixed; `git diff --check` clean; this status header no longer claims a
   terminal sign-off.

Verification: focused battery 285/285 (precedence pins, Phase-5 file incl.
the strengthened runtime pair, return-boundary file, parser-adapter file,
type_checker + checker suites).  FULL lang-driver-test: IN FLIGHT at handoff
(posted early per the parallel-review workflow; result will be appended —
if this review finds issues first, the run is stopped and restarted after
rework).

## REVISION 9 — HResultOk source seam removed (review-2026-08-03T21-17-18Z ruling)

STATUS: REVISION 9 READY FOR REVIEW.  Implements the approved direction and
all four approved test migrations; the blocked state below is resolved (the
APPROVAL-PENDING token was consumed on the ruling).

### Implementation

1. Deleted the unconditional unqualified `Ok(expr) -> HResultOk` rewrite in
   stage1/ast_to_hir.py (and its alpha-renamer arm).  Unqualified `Ok(...)`
   is an ordinary HCall through the contextual variant-constructor resolver
   (spec §10.3; no spec edit).
2. Deleted `HResultOk` exhaustively: class + exports, and every traversal /
   typing / effect / borrow / place / Phase-5-slot / phase-2-inference /
   HIR→MIR arm.  `rg -n --glob '*.py' '\bHResultOk\b' lang/driftc lang/tests`
   returns NOTHING.  Can-throw success wrapping (MIR ConstructResultOk at
   return) is independent and untouched.
3. Fixed the constructor-context source diagnostic per the ruling: the
   E-CTOR-EXPECTED-TYPE fallback now ALSO fires when the expected type is
   non-variant (a `throws -> Int` return supplies Int, not an implicit
   Result), and recognizes std.core variant arms (Ok/Err/Some/...) alongside
   current-module ones — previously these fell through to an unhelpful
   "no matching overload for function 'Ok'".

### Probe evidence (all shapes)

- `val r: core.Result<Int, Int> = Ok(1)` + match: compiles AND RUNS.
- `return Ok(5)` in `throws -> core.Result<Int, String>` with caller
  try+match: compiles AND RUNS — public inner variant + exactly one outer
  throwing-ABI wrap; no double-wrap mismatch.
- `return Ok(5)` in `throws -> Int`: ONE clean E-CTOR-EXPECTED-TYPE.
- `val r = Ok(1)` (unannotated local — the child's ICE route): ONE clean
  E-CTOR-EXPECTED-TYPE, no traceback.

### Approved test migrations (as ruled)

1. Phase-5: test_phase5_public_result_ctor_payload_duplicates (structural —
   captured post-typecheck HIR asserts the Ok HCall payload is
   HMethodCall(const_share, origin=implicit_const_share) on the
   auto-borrowed place of `a`, and no bare HVar(a) ctor arg remains) +
   test_phase5_public_result_ctor_payload_runs (full compile/run: annotated
   Result built, `a` read afterwards, Ok payload matched, semantic exit).
   Prose no longer claims an HResultOk.value slot.
2. Return-boundary file: test_named_fn_return_ok_wrapped_rejected re-pinned
   to ONE E-CTOR-EXPECTED-TYPE (single error line, no traceback);
   NEW test_local_unannotated_ok_rejected_cleanly (child ICE route);
   NEW test_return_ok_into_public_result_runs (contrasting positive).
3. Deleted with the node: test_result_ok_without_signature_type_ids_does_
   not_blow_up; test_result_ok_uses_fnresult_type.
4. Parser adapter: NEW test_parse_unqualified_ok_lowers_to_plain_hcall;
   neighboring ns.Ok comment updated.  Stale HResultOk prose fixed in
   test_borrow_in_cast_no_double_free.py.

### Verification (required set)

- Phase-5 ConstShare driver file: green (in the 46/46 three-file run).
- Parser-adapter file, return-boundary file: green (same run).
- Affected checker/type-checker + stage1 + parser suites: 501/501.
- FULL lang-driver-test: 2340 passed, 10 skipped, exit 0 (48m10s).
- Child acceptance criteria met: no traceback on any Ok shape; public
  construction end-to-end positive; upstream rejection for non-variant
  contexts; single-wrap return contract proven by the running contrast
  positive; ConstShare payload pinned structurally AND by compile/run.

Child findings/finding-result-ok-source-boundary: RESOLVED (its PROGRESS.md
records the outcome).  Trigger re-scan at child start: no matching entry
(consistent with the reviewer's initial scan).  Repository-wide `just test`
restart remains user-driven after review convergence (prior run stopped
before ASAN).

## REOPENED by review-2026-08-03T21-05-43Z — BLOCKED ON TEST-EDIT APPROVAL

STATUS: investigation COMPLETE; implementation NOT STARTED — blocked on
Slawomir's explicit approval of three test edits (repository rule).  The
empty token APPROVAL-PENDING-<ts> in this folder signals the blocked
handoff; this section is the content.

### Investigation evidence (all probe-confirmed on the current tree)

1. Phase 5 red: test_phase5_result_ok_payload_duplicates fails with
   "return type 'FnResult' does not match declared type 'ConstArc'" (the
   R4.4 return-authority rejection, as the reopening review predicted).
2. Child finding findings/finding-result-ok-source-boundary VALIDATED:
   local `val r = Ok(a)` passes checking and ICEs in LLVM
   ("ok payload type mismatch for ConstructResultOk ... have ConstArc,
   expected drift.int").
3. NEW evidence beyond the child's notes: the SPEC-BLESSED contextual form
   `val r: core.Result<Int, Int> = Ok(1)` ALSO fails today
   ("initializer type 'FnResult' does not match declared type 'Result'") —
   the unconditional ast_to_hir rewrite hijacks the public constructor the
   spec §10.3 promises.
4. Producer inventory: the rewrite (ast_to_hir.py:429) + its alpha-renamer
   copy are HResultOk's ONLY producers besides two synthetic-HIR tests;
   production code uses qualified core.Result::Ok(...).

### Proposed direction (child hypothesis 1 — spec-aligned separation)

Delete the unconditional `Ok(...)` -> HResultOk source rewrite; unqualified
`Ok(...)` resolves through the ordinary contextual variant-constructor path
(implementing what the spec already says — no spec edit).  HResultOk then
has no producer: delete the node + its handling (checker typing, phase-2
inference arm, MIR lowering arm, effect-walker entry) per the pre-1.0
one-contract rule.  The internal success-wrapping of can-throw returns is a
separate mechanism and stays.

### The three test edits awaiting approval

1. test_phase5_result_ok_payload_duplicates → migrate to the spec spelling
   `val r: core.Result<core.ConstArc<String>, Int> = Ok(a);` + reuse of
   `a` afterwards — preserves the REAL contract (implicit ConstShare
   duplication in an owned ctor-arg slot; source binding usable after)
   end-to-end; the HResultOk.value slot is REMOVED with the node (code
   evidence, documented in the test), not left untested.
2. test_named_fn_return_ok_wrapped_rejected (R4.4 negative pinning
   "return type 'FnResult'...") → re-pin to the new contract's actual clean
   outcome, determined empirically post-fix: either a clean
   constructor-resolution diagnostic, or `return Ok(5)` compiling and
   returning 5 (Result constructed, auto-try unwraps).  Either way no ICE,
   one contract.
3. The two synthetic HResultOk pins (test_checker_call_type_checks::
   test_result_ok_without_signature_type_ids_does_not_blow_up;
   test_type_checker_expressions.py:659) → deleted with the node.

### On approval

Implement; verify the child's acceptance criteria (annotated positive runs
end-to-end, local form gets a clean upstream diagnostic, no double-wrap);
run the whole Phase 5 ConstShare driver file, the return-boundary file, the
stub checker file, and lang-driver-test; hand back via IMPL-PENDING token.

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
