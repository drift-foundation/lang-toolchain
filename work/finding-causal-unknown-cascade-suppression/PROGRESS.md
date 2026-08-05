# Progress: causal Unknown cascade suppression

Last updated: 2026-08-04 (K)

STATUS: PREFLIGHT COMPLETE (static + EMPIRICAL) — probes executed under
the review-2026-08-04T21-40-37Z authorization (work-folder only, no
shared-file edits, no suite/corpus gates).  Implementation still awaits
suite completion + Slawomir's start clearance.

## Empirical probe results (probe_preflight_hypotheses.py, 5 passed;
original red probe re-run: 2 failed exactly as baselined)

1. PENDING VALUE READ — HYPOTHESIS CONFIRMED (driver compile of the
   implicit-capture alias shape): BOTH diagnostics appear —
   `7:14 E-COPY-UNKNOWN` at `val alias = f` AND the primary
   `6:10 closures with borrowed captures are non-escaping in v0` from the
   flush — with the CASCADE FIRST in presentation order.  A
   binding-keyed "already diagnosed" table cannot fix this alone (no
   diagnostic exists at alias-read time).  The contract question is
   therefore REAL and needs Slawomir's ruling: (a) resolve/reject the
   pending lambda at first value use, (b) a "primary guaranteed at
   flush" pending-cause state, or (c) accept both diagnostics as
   independently meaningful.
2. ALIAS HOP — current global suppression silences the aliased call too
   (only the unrelated copy error surfaces).  Today: quiet for the wrong
   reason.  A naive exact-binding patch would REGRESS this to a fresh
   cascade at the alias (g carries no cause) — explicit
   HLet-from-diagnosed-HVar propagation is REQUIRED in the design, or
   the case must be split into a pre-declared child finding.
3. HINVOKE PARITY — GAP CONFIRMED EMPIRICALLY: the identical
   diagnosed-binding shape yields primary + "call target is not a
   function value" through HInvoke (double diagnostic) but primary-only
   through HCall(fn=HVar).  Opposite policies proven live.
4. HCALL SAME-BINDING control: suppressed (single primary) — the
   case-1 behavior the patch must preserve.
5. CONCRETE RECOVERY: pending captureless lambda resolved on first call;
   second call clean, zero diagnostics — no stale state today; pin
   stays as a regression guard.

## Preflight verification of the review's five points (all STATIC reads)

1. CONFIRMED — both consumers still use the function-global predicate on
   the current tree (line numbers shifted by this session's edits, code
   identical to the evidence):
   - type_checker.py:4030 `_require_copy_value`:
     `if ty_id == self._unknown and any(... "error" ... in diagnostics)`;
   - checker/call_resolver.py:6713 (binding-call route; was ~6623-6719
     pre-shift): same predicate guarding the call-target message, with
     the exact `binding_id` still in scope at the suppression point.
2. CONFIRMED — HInvoke parity gap: type_checker.py:10110's non-function
   fallback appends "call target is not a function value"
   UNCONDITIONALLY (no suppression predicate at all).  The two
   semantically-equivalent function-value call consumers therefore have
   OPPOSITE cascade policies today (HCall: global suppression; HInvoke:
   never suppresses) — parity must be a deliberate decision either way.
3. CONFIRMED (empirically, see the empirical section above — this item's
   earlier "red probe still required" qualifier is superseded per the
   review-2026-08-04T21-46-28Z bookkeeping note): bare value read of a
   pending stored lambda emits E-COPY-UNKNOWN at the alias BEFORE the
   final flush's primary rejection; static flow analysis matched the
   observed behavior exactly.
4. STATICALLY SUPPORTED — one-hop alias: an HLet initialized from a
   diagnosed-Unknown HVar stores Unknown on the NEW binding id with no
   new diagnostic; any exact-binding cause keyed to the original id will
   not cover the alias, so alias uses would cascade unless an explicit
   HLet-from-HVar propagation rule is added (or the case is split out).
   Exact-binding marking alone is NOT total for the promised surface.
5. CONFIRMED — FnCheckState.OWNED_TABLES (type_checker.py:462-474)
   enumerates the transaction-owned _TxnDict/_TxnList tables and drives
   state_fingerprint(); any mutable cause table written during expression
   typing (pending-lambda resolution runs inside HCall typing, which the
   deferred resolver can transact) MUST be added there — a closure dict
   would be a rollback leak.

## Additional preflight observations

- Exactly THREE `make_call_ctx(...)` construction sites (8518, 10003,
  10310) — matches the evidence; a new required context predicate must
  thread all three (or be defaulted FAIL-TOWARD-TRIPWIRE, never toward
  the global heuristic).
- The two suppression comments (4031-4035, 6714-6717) claim the causal
  relation their predicates do not establish — in-scope for correction
  when implementation lands (source comments; no approval needed).
- This session's landed slices did not touch either suppression site;
  the finding's premise is intact on the pending-0.35.0 tree.
- refactor_triggers.md: to be rescanned at actual LANGUAGE_BUG start per
  plan Phase 0 (reviewer's 2026-08-04 scan found no match).

## Agreements / positions for the follow-up

- Agree with the narrow binding-cause `_TxnDict` as the leading design,
  with cause data (category + producer identity), marked only from
  producer-local watermarks, cleared on concrete resolution — BUT the
  point-4 alias gap means the design must either add the explicit
  HLet-from-diagnosed-HVar propagation rule or pre-declare the alias
  case a child finding; silent narrowness would fail the acceptance
  criterion "no function-global predicate remains" while still
  cascading on aliases.
- Point-3 contract question (is the early E-COPY-UNKNOWN at
  `val alias = f` a cascade or an independent error?) is a LANGUAGE
  contract call: under the v1 bare-closure rules a stored capturing
  lambda is invalid at the BINDING, so an early diagnostic at the alias
  read arguably surfaces before the flush's primary with a worse span.
  Options (resolve-at-first-value-use / guaranteed-at-flush state /
  accept both diagnostics) need Slawomir only if the red probe confirms
  the double-diagnostic; flagging now so it is not decided silently.

## Planned red regressions (Phase 2, unchanged from PLAN + point-4 emphasis)

New file lang/tests/type_checker/test_causal_unknown_cascade_suppression.py:
independent-Unknown copy + call tripwires (the two existing work-probe
shapes), same-binding primary-only (HCall + HInvoke parity pin),
shadowing isolation, concrete recovery, pending-value-read order/count,
one-hop alias, transaction rollback/fingerprint teeth.

## IMPLEMENTATION IN PROGRESS (started 2026-08-05 per review-2026-08-05T02-44-49Z authorization)

Gates recorded: human-cleared suite start (intentional kill after clean
observation; final full-suite deferred to end of queue), accepted plan,
approved two-file comment ledger.  refactor_triggers.md rescanned: no
match (walker/provenance entries do not cover checker cause state).

### Child C (typed-let/return Callback wrap restoration) — CORE LANDED

ROOT CAUSE (instrumented, then instrumentation removed): the callback
intrinsic arg-marking stamps `allow_capture_invoke` on the SHARED
HLambda node; across re-check passes the stamped node short-circuits at
the lambda visit's interface-label return (type_checker.py ~8013), the
label makes HLet's `inferred == declared` equality skip the post-typing
Site-5 wrap, and MIR receives a raw HLambda under an interface-typed
binding (the observed ICE).  The checker's splice from the first pass
does not survive to the lowered body (distinct stmt identities traced).

FIX (constraint 3, wrap-BEFORE-typing): slot-site PRE-WRAP at BOTH the
typed-let initializer (before `type_expr(stmt.value,
expected_type=declared)`) and the return position (in
`_type_return_value` before the value typing) — a bare HLambda in a
declared Callback slot is spliced into the canonical
`core.callbackN(...)` construction FIRST and only ever typed inside it;
rejection binds Unknown without typing the poisoned lambda against the
iface.  Fn-typed values keep the post-typing wrap branch.  Deterministic
per pass regardless of node-attribute or splice persistence.

VERIFIED: typed-let repro compiles AND RUNS exit 0; return-position
repro compiles AND RUNS exit 0; existing
test_implicit_callback_wrap.py suite 30/30 green.

### Child B (pending-lambda value finalization) — CORE LANDED

- `_classify_and_type_pending(bid, lam, contextual_fn)`: ONE total
  outcome shared by the end-of-function drain (rewired, behavior
  preserved) and the new value-use finalizer — capturing → the approved
  v1 primary (borrow-capture or bare-storage message) WITHOUT entering
  callback construction (no capture effects on bare reference);
  unconstrained-with-no-context → the clean cannot-infer primary
  WITHOUT typing (no Unknown-ABI LambdaFnSpec can publish); otherwise
  typed once (with the contextual function shape when supplied) with
  nothrow tightening and the residual-Unknown ABI guard.
- `_finalize_pending_value_use(bid, expected_type)`: fires at the
  binding-linked HVar read (covers alias initializers, returns, call
  arguments, move/borrow subjects, discarded reads) through
  `PendingLambdaOwner.begin_resolution` — the probe barrier applies, so
  speculative probes defer instead of leaking.  HCall/HInvoke callee
  consumers keep their own pinned pre-resolution (plan Phase 6 caveat).
- Static-callback splice: `finalized_fnptr_by_binding` records the
  (fn_ref, call_sig) of finalized captureless lambdas; the SOLE implicit
  wrap constructor (`_implicit_callback_wrap`) splices the STATIC
  HFnPtrConst when wrapping an fn-typed HVar with a known const (MIR
  callback construction is static-only in v1).  Threaded via the
  `fnptr_const_for_binding` context field (CallResolverContext + all
  three make_call_ctx sites + the iface-slot adapter).

VERIFIED end-to-end (compile AND run exit 0): captureless alias,
resolve-after-alias (order independence), contextual Callback alias,
bare-pending Callback ARGUMENT (`take_cb(f)` — the round-2 MoveOut ICE);
single clean primaries for unconstrained + explicit-capture aliases (no
E-COPY-UNKNOWN cascades); non-lambda producer single primary preserved.
Family battery 135 + compiler smoke 1123 green after the finalizer/wrap
core (post-splice rerun in flight).

### Parent A (causal Unknown provenance) — CORE LANDED

- STATE: `unknown_cause_by_binding` + `unknown_cause_by_node` as
  FnCheckState-owned `_TxnDict`s (OWNED_TABLES + fingerprint covered;
  probe rollback exact by construction).  Immutable
  (category, producer_node_id) values; helpers mark/clear/query by
  EXACT identity; `_expr_unknown_is_caused(expr)` = node-cause OR
  direct HVar read of a caused binding; everything else fails toward
  the tripwire.
- PRODUCERS: unknown-name HVar (marks its node); HLet attachment at the
  main binding write — Unknown initializer marked ONLY via propagation
  (initializer node/HVar caused) or the producer-LOCAL diagnostic
  watermark (`_let_diag_watermark` set unconditionally at HLet entry);
  concrete types CLEAR (recovery); `_classify_and_type_pending`
  rejection paths mark, success clears.
- PROPAGATION (the four proven transparent shapes): caused-binding HVar
  read → node; `move <caused>` → node; reachability-aware ternary join
  (literal-cond folding mirrors hir_flow; ALL reachable
  Unknown-producing arms must be caused, else tripwire); causally
  suppressed call results → call node (marked by the resolver via ctx).
- CONSUMERS: `_require_copy_value` global scan REPLACED with
  `_expr_unknown_is_caused` (comment corrected — approved source edit);
  the resolver binding-call suppression REPLACED with the exact-binding
  ctx predicate (`binding_unknown_cause`) + result-node cause mark,
  fail-toward-tripwire on absent predicates (comment corrected);
  HInvoke's fallback gains the SAME causal suppression (parity closed).
  Both new ctx fields threaded at all three make_call_ctx sites.

VERIFIED (work probes, 11 passed): original two red tripwire probes now
GREEN (independent Unknowns diagnose again); alias-hop, poisoned move,
poisoned literal ternary, HInvoke-parity, and HCall same-binding chains
all show a SINGLE primary; concrete recovery clean; pending value-read,
callback-slot, and borrow-finalized shapes remain green.  Family battery
+ 5-suite smoke rerun IN FLIGHT at this handoff (posted early per the
parallel-review workflow; will be appended).

### Child D (named-function borrow materialization) — LANDED + PINNED

- RED (structural, per constraint 4): the HPlaceExpr/HFnPtrConst
  transition traced — the checker's fnptr rewrite replaces the name
  INSIDE the canonical borrow place, and `_lower_addr_of_place` read
  `expr.base.name` unconditionally → AttributeError ICE on
  `val r = &seven;` (traceback captured pre-fix at hir_to_mir.py:12643).
- FIX (`stage2/hir_to_mir.py`, new branch at the top of the canonical
  place block): `isinstance(expr.base, H.HFnPtrConst)` → materialize via
  the canonical `_materialize_owned_temp_for_borrow` helper (lazy value
  callable; `__borrow_tmp` prefix keeps the owned-temp audit contract),
  return (addr, fn type from call_sig) — same semantics as
  `val f = seven; &f`.  `is_mut=True` fails closed (AssertionError):
  the checker already rejects `&mut seven` as a non-addressable borrow
  operand (verified: `E-AUTO-73784bc4` diagnostic, no ICE).
- PINS: `lang/tests/driver/test_fnptr_borrow_materialization.py`
  (5 passed): two structural (`_lower_addr_of_place` direct on the
  rewritten place: FnPtrConst→StoreLocal(__borrow_tmp)→AddrOfLocal
  shared, exact fn type; mut fail-closed) + three e2e (borrow named fn
  compile/RUN; finalize-and-accept `val f = seven; &f` compile/RUN;
  `&mut seven` checker rejection, no Traceback).

### Battery results (appended per handoff promise)

Full family battery + 5-suite smoke rerun (post call-consumer causal
marks): 148 passed + 1123 passed, smoke diff-check clean.  The earlier
lone red (`test_stored_capturing_lambda_single_spanned_diagnostic`) is
green after the inline HCall/HInvoke pending consumers gained
mark-on-failed-resolution/clear-on-concrete.

### P1 round (review-2026-08-05T03-42-22Z) — ALL FOUR ADDRESSED

- P1-1 (classifier totality + Unknown-ABI retraction):
  `_classify_and_type_pending` is now the ONE consumer outcome — direct
  HCall and HInvoke callee pre-resolution route through it (old inline
  typing paths deleted); contextual can_throw=True shape preserved.
  Refinements: annotations are the authority when ALL params annotated
  (context dropped, self-typed); an Unknown context slot for an
  UNANNOTATED param poisons WITHOUT typing (no Unknown-ABI publication,
  no body-check cascade); residual Unknown component → poisoned Unknown
  binding + RETRACTION of the published `LambdaFnSpec` (reconstructed
  `__lambda_fn_{enclosing}_{node_id}` id) and the node-keyed fnptr
  const; residual primary emitted only when not already causally
  explained (typing watermark / context_caused from caused args).
- P1-2 (watermark vs compound join): `_COMPOUND_JOIN_SHAPES`
  (HTernary/HMatchExpr/HTryExpr) — the HLet watermark path no longer
  marks compound initializers; only propagation counts there, so the
  mixed-arm join's uncaused decision stands and the downstream tripwire
  fires (pinned).
- P1-3 (alias-hop provenance): the HLet main write propagates
  `finalized_fnptr_by_binding` across `val g = f` hops — IMMUTABLE hops
  only (either side `var` → no propagation; conservative fallback is
  the MIR static-only diagnostic, not an ICE).  Chain f→g→h→Callback
  pinned with the `__lambda_fn_` static-IR witness.
- P1-4 (pre-wrap rejection causality): the typed-let REJECTED branch
  marks the binding (`callback-wrap-rejected`) before binding Unknown.
  SCOPE EXTENSION 1: the natural use `cb.call()` still cascaded via
  method dispatch, so `resolve_method_call` gained receiver-position
  causal suppression (early bail when receiver types Unknown AND
  `expr_unknown_is_caused(receiver)`; marks `caused-receiver-method`;
  absent predicate fails toward the tripwire).  New optional ctx fields
  threaded: `expr_unknown_is_caused` on CallResolverContext +
  MethodResolverContext (+ `_make_method_ctx` pass-through of all three
  cause fields).
- SCOPE EXTENSION 2: `_expr_unknown_is_caused` sees through a
  PROJECTION-LESS canonical `HPlaceExpr` (normalized move/borrow
  operands wrap the HVar; `move bad` otherwise lost the cause —
  found by the move-propagation pin going red).  Projected places stay
  tripwire.

### Regression files (all four in-tree, 31 tests green)

- `lang/tests/type_checker/test_causal_unknown_provenance.py` (9)
- `lang/tests/driver/test_pending_lambda_value_finalization.py` (10)
- `lang/tests/driver/test_callback_slot_materialization.py` (7)
- `lang/tests/driver/test_fnptr_borrow_materialization.py` (5)

### Approved comment edits — DONE

- test_stored_capturing_lambda_diagnostic.py docstring: "a prior error
  already explains" → exact causal provenance wording.
- test_implicit_callback_wrap.py: Site-1 block reframed to the canonical
  wrapper contract with 2026-08-04 matrix evidence; Sites 2/5/6
  docstrings → historical regression descriptions; arity-mismatch note
  kept (matrix re-proved it).

### ADJACENT PRE-EXISTING DEFECT (found by the Site-1 matrix, NOT fixed)

`S::take_cb(|x: Int| => ...)` (associated-call Callback param, silent
coercion path) is checker-clean but emits INVALID LLVM IR e2e ("global
variable reference must have pointer type" at clang) — reproduced
identically on a clean `git archive HEAD` tree, so it predates this
slice.  Arity-mismatched lambda is also checker-silent on the same path.
Candidate follow-up finding: migrate Site 1 to the canonical wrapper.

### History fold — DONE (0.35.0 entry + header)

### First P1-round battery result + regression fix

Battery b62mzamk2: 1 failed / 179 passed (family) + 1 failed / 1131
(smoke) — same single red both runs:
`test_driver_mixed_prefix_contextual_single_declared_diagnostic`.  Root
cause: the classify refinement "drop context when ALL params annotated"
also discarded the contextual RETURN expectation, demoting the
declared-type diagnostic to the reconciliation error.  REVERTED the
context-drop (annotated lambdas keep the full callsite context as
before; the residual-Unknown branch handles Unknown pollution); the
unannotated-slot hopeless bail stays.  Reconciliation file 14/14, P1
pins 26/26 after the revert.

### Round 2 (review-2026-08-05T04-27-45Z) — ALL FIVE ITEMS DONE

- R2-1 (caused-callee HCall skipped independent args): ONE shared
  argument observation inserted before the suppression arm AND the
  tripwire in the binding-call not-a-function path (each returns; the
  FUNCTION path types args itself).  Bare-lambda args are skipped there
  (no callable context → an unconstrained-inference error would itself
  be a cascade).
- R2-2 (caused-receiver method suppression skipped args): the early
  bail RELOCATED to after ordinary argument typing in
  `resolve_method_call`; only receiver-derived resolution noise is
  suppressed; result node still marked `caused-receiver-method`.
- R2-3 (match/try declared compound joins without producers): real
  cause joins implemented — HMatchExpr collects every value-producing
  arm's Unknown results and marks `caused-match` only when ALL are
  caused (no scrutinee folding: treating every arm reachable can only
  WITHHOLD suppression); E-MATCH-NO-VALUE marks its own node
  (`caused-match-novalue`, producer-local primary ON the compound);
  HTryExpr joins the attempt + every value-producing catch arm
  (`caused-try`).  Reviewer probes probe_reviewer_round2.py: 4/4 green;
  six in-tree pins added (both sides for match and try + the two
  independent-argument pins).
- R2-4 (Site-1 assoc-call Callback defect — child finding
  `findings/finding-assoc-call-callback-silent-coercion/`):
  refactor_triggers.md scanned (no matching trigger → minimal
  root-cause repair).  RED-FIRST:
  `lang/tests/driver/test_assoc_call_callback_wrap.py` proved 3 defect
  shapes (bare lambda → invalid IR at clang; named-fn arg →
  NotImplementedError vtable ICE; arity mismatch checker-silent) with
  2 green controls (explicit wrap, free-fn).  FIX at the assoc-call
  success path in `resolve_call_expr` (before `record_call_info`):
  each concrete Callback* param with a bare-lambda/fn-typed arg routes
  through `_try_wrap_arg_for_callback_field` (WRAPPED → splice into
  expr.args; REJECTED → poison; SKIP for such an arg = ARITY mismatch
  → real diagnostic, fail-closed).  Named-fn detection: the silently
  coerced arg node already carries its registered static fnptr const —
  synthesize the thin fn type from the const's call_sig; fallback
  derivation of the expected fn shape from the interface's type args
  (params + ret last, throw-ness from kind).  5/5 green.  Free-fn
  arity mismatch verified already-clean ("no matching overload").
  Site-1 comment block updated to MIGRATED (comment-only edit);
  converting the two existing checker-only Site-1 assertions to e2e
  AWAITS Slawomir's explicit approval per the review.
- P2 (fnptr-borrow pin consumes the borrow): BORROW_NAMED_FN now
  returns `(*r)() - 7` (calls through the borrow); 5/5 green.

### Round-2 battery results

First rerun (bma4ydrcn): 2 failed / 193 — the R2-1 shared argument
observation re-typed args ALREADY visited by the pending-callee consumer
and duplicated the unknown-name primary.  Fix: the observation skips
args whose first visit already produced a causal primary (node cause /
caused-binding read via ctx.expr_unknown_is_caused) — consistent with
the FUNCTION arm's existing unconditional re-typing (which only re-types
clean args in practice).  FINAL battery (b8umpuw0o): 195 passed
(family incl. reviewer probes) + 1138 passed (5-suite smoke), all
green.

### Site-1 assertion conversion — APPROVED & DONE

Slawomir's approval (approval-decision-2026-08-05T04-28-32Z, claimed and
closed with outcome): test_implicit_callback_wrap.py Site-1 tests now pin
the corrected contract — bare-lambda acceptance asserts the static
`__lambda_fn_` wrap witness in the emitted IR (`_compile` gained opt-in
`return_ir`); NEW named-fn acceptance test (pre-fix: IR-emission crash,
vtable NotImplementedError); NEW arity-negative boundary test at this
level.  File 32/32 green.

### Round 3 (review-2026-08-05T05-08-53Z) — one P1, DONE

Reviewer verdict on round 2: all other items CLEAR; one P1 — the Site-1
fn-typed-argument contract lost STORED named-function bindings
(`val f = add1; S::take_cb(f)` → "MoveOut of uninitialized iface local"
MIR invariant; reviewer probe probe_assoc_callback_fn_alias.drift).

Fix (binding-provenance route, the review's first suggested option):
- HLet provenance seeding extended: `val f = add1` records the
  initializer HVar node's registered fnptr const into
  `finalized_fnptr_by_binding` (same immutability guards as the alias
  hop; the existing hop propagation then carries it to `val g = f`).
- Site-1 pass: `_s1_fp` lookup falls back from the ARG NODE's const to
  `ctx.fnptr_const_for_binding(binding_id)` — the silently
  interface-labeled read regains its static provenance; the wrap's
  centralized splice consumes the same table.
- Runtime-only fn values FAIL CLOSED at the checker: an
  interface-labeled HVar arg with NO static provenance whose binding
  re-types as a thin FUNCTION (e.g. `var f = add1`) now gets
  "callback argument must be a statically-known function in v1 ..."
  instead of reaching the MIR iface-init invariant (probed: clean
  single diagnostic).

Pins added to test_assoc_call_callback_wrap.py (9/9): stored named-fn
binding (reviewer repro) compile/RUN; immutable second alias hop
compile/RUN; stored-lambda binding at the assoc param compile/RUN;
mutable `var` binding → clean rejection (no MIR invariant, no
Traceback).  Reviewer's probe .drift compiles and runs exit 0.

Handoff bookkeeping (reviewer flag): the FINAL approved Site-1 test
state in test_implicit_callback_wrap.py (post approval
2026-08-05T04-28-32Z): bare-lambda test asserts diagnostics-clean AND
`__lambda_fn_` in the emitted IR (`_compile` gained opt-in
`return_ir`); NEW `test_site1_static_assoc_fn_named_fn_to_callback1`
(pre-fix: IR-emission crash); NEW
`test_site1_static_assoc_fn_arity_mismatch_rejected` ("arity does not
match callback parameter"); Site-1 comment block reframed to MIGRATED.
File 32/32.

### Round-3 battery + verdict

- Battery (bhnlto5t6): 201 passed (family incl. reviewer probes) +
  1138 passed (smoke) — ALL GREEN.
- Reviewer verdict (review-2026-08-05T05-38-40Z): **round 3 CLEAR** —
  provenance fix closes the Site-1 gap without widening the runtime
  Callback ABI; approved Site-1 assertion edits verified consistent;
  reviewer independently ran the alias probe (exit 0) and 32 Callback
  passes.  GREEN LIGHT for the full suite; NOT terminal signoff until
  the full-suite result is reviewed on the same thread.

### Full suite — ONE related failure, ruling requested

- `./run-all-tests.sh` (task b21vqc66u; run_all_tests.log): perf OK;
  memcheck driver stage 1 failed / 2396 passed / 10 skipped; suite
  aborted there (ASAN lane never ran).
- Failure: test_std_json_regressions.py::
  test_std_json_legacy_node_mutation_helpers_are_rejected — the
  array_push/object_set names were only ever surfaced by the
  "no matching method for receiver Unknown" CASCADE over receivers
  poisoned by the new_array/new_object primaries; the slice's
  caused-receiver suppression now (correctly per contract) withholds
  those.  new_array/new_object primaries still fire.
- Reported on thread b49addf2e8d5 (response 06-37-44Z) with
  recommendation: split/update the fixture so array_push/object_set
  are exercised on VALID receivers (their own rejections become
  primaries that name them); existing-test edit → approval requested.
  Alternative offered: carve method suppression for assoc-call-caused
  receivers (weakens the one-primary contract).
- RULING (review-2026-08-05T09-05-49Z, Outcome: approved): update the
  fixture per the reviewer's probe_std_json_legacy_primary.drift shape
  — four INDEPENDENT primaries (JsonNode::new_array / ::new_object
  assoc-ctor rejections + array_push / object_set on VALID
  json.new_array()/json.new_object() receivers with independent
  argument values), exact count == 4, negative pin excluding
  "receiver Unknown"; NO compiler carve-out.  Full-suite ownership:
  Slawomir runs the next run-all-tests.sh.

### std.json fixture edit — DONE

- test_std_json_regressions.py updated exactly per the approved spec
  (fixture + four name assertions kept, count==4 pin, receiver-Unknown
  negative pin, provenance comment).  Affected file 2/2.
- Focused causal-diagnostic battery (provenance + finalization +
  callback slots + stored-capturing + assoc wrap + implicit wrap +
  fnptr borrow + reviewer round-2 probes): 104 passed.
- READY for Slawomir's full-suite run; reported on thread
  b49addf2e8d5.  Corpus verify/promote separate.

## Alias-matrix probes (review-2026-08-04T21-46-28Z items 1-6;
probe_pending_alias_matrix.py, full driver compiles, ordered streams)

1. captureless_inferable_alias (`val f = || => {7}; val g = f; g()-7`):
   build exit 1, SOLE diagnostic `5:10 E-COPY-UNKNOWN` — a fully valid,
   inferable captureless lambda CANNOT be aliased today; only a direct
   call resolves the pending entry.  CANDIDATE LANGUAGE_BUG CHILD
   (valid program rejected), exactly as the review anticipated.
2. contextual_callback_alias (`val g: core.Callback1<Int,Int> = f`):
   same single `6:36 E-COPY-UNKNOWN` — the HLet's expected Callback type
   never reaches the pending resolution at the HVar read.  NOTE: v1 has
   NO bare function-type local annotation (only core.Callback*
   containers and core.Fn* require-bounds), so this is the nearest
   contextual-alias spelling.
3. unconstrained_alias (`val f = | x | => x; val g = f`): DOUBLE
   diagnostic, cascade FIRST — `5:10 E-COPY-UNKNOWN` then the clean
   primary `4:10 cannot infer type for lambda parameter(s) 'x'`.  Pins
   needed: cannot become silently accepted; one-primary presentation
   after the fix.
4. resolve_after_alias (alias taken, THEN f() resolves): still
   `5:10 E-COPY-UNKNOWN` — resolution is strictly order-sensitive; a
   later resolving call does not heal an earlier alias read.
5. nonlambda_causal_producer (`val bad = missing_name; bad();`):
   EXACTLY ONE primary `4:12 unknown name 'missing_name'
   [E-UNKNOWN-NAME]`, zero cascades — the ideal presentation (today via
   the global predicate, i.e. right output for the wrong reason; the
   causal patch must preserve it by exact-binding cause).
6. explicit_capture_alias (`captures(copy x)` stored + alias): DOUBLE
   diagnostic, cascade first — `6:14 E-COPY-UNKNOWN` then the approved
   primary `5:10 bare capturing lambdas cannot be stored in v1...` —
   same one-primary intent as the implicit-borrow case, own message.

CONCLUSION strengthening the reviewer's leading design: EVERY value read
of a pending lambda cascades today, including valid captureless shapes.
First-semantic-value-use finalization (shared helper with first-HCall/
first-HInvoke/end-flush; capturing → immediate primary + diagnosed-
Unknown cause; inferable captureless → install concrete type + ordinary
Copy path; unconstrained → clean cannot-infer primary + cause; never a
PENDING_MEANS_POISON shortcut) would simultaneously fix the candidate
child (1/2/4), the double-diagnostics (3/6, plus the earlier implicit-
borrow case), and give the binding-cause table its sound producers.
Cause propagation through `HLet(value=HVar(diagnosed))` remains required
(alias-hop probe); whether causes must also flow through suppressed
call RESULTS into new bindings needs one more probe during
implementation Phase 3.

Proposed in-tree red/green contracts (Phase 2, deferred to
implementation): tripwire pair (independent Unknown copy/call);
same-binding primary-only through BOTH HCall and HInvoke; captureless
alias compile/run == 0 (child positive); contextual callback alias
resolves; unconstrained alias one clean primary; resolve-after-alias
runs; explicit/implicit capture alias one primary each; shadowing;
concrete recovery; rollback/fingerprint teeth.

NEXT: awaiting suite completion + Slawomir's start clearance AND the
pending-value-read contract ruling (first-value-use finalization is now
the evidence-backed recommendation).  No shared files touched.
