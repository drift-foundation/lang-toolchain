# PROGRESS: finding-pending-lambda-probe-rollback

Last updated: 2026-08-04 (K)

STATUS: SIGNED OFF (review-2026-08-04T23-45-29Z, terminal): reviewer
re-reviewed the production diff — owner bars every mutation before
external state changes, exact binding identity authoritative, nested
propagation correct, unsupported transactions fail closed; independent
module run 8/8.  Clear for the broader regression suites (user-run).
The parent finding (causal Unknown cascade suppression) remains the
next open implementation slice.

Previously: REVISION 3 READY FOR REVIEW — responds to
review-2026-08-04T23-32-40Z (two proof gaps, test-file-only additions).

## REVISION 3 (review-2026-08-04T23-32-40Z)

1. (P1 spec snapshot) `_spec_snapshot` now records EVERY LambdaFnSpec
   field: registry key, spec fn_id, origin_fn_id, lambda OBJECT identity
   + structural lambda state, param/return/throw, call-info map identity
   + live-frame alias bit, AND the map's full structure (the comment
   also states the division of labor: the FnCheckState fingerprint owns
   live-map content; this channel additionally pins per-spec reference
   identity and structure).
2. (P1 assertions) B5 control now pins the OUTER method CallInfo — the
   established receiver-inclusive layout (DIRECT `Holder::put2`,
   params (&Holder REF, Int), ret Int, nothrow — verified empirically
   before pinning) — plus `pass` nothrow.  The HInvoke contract pins
   callee_node_id == the callee NODE id (that route's established
   contract, matching test_lambda_callinfo_inference_boundary), empty
   params, nothrow, and binding-level identity separately (binding `f`
   finalized to () -> Int).  The shadowed-HCall pin gains empty-params +
   nothrow symmetry.
   Iteration notes (facts learned while pinning): the method symbol's
   FunctionId.name is the QUALIFIED `Holder::put2`; and preset binding
   ids on synthetic HIR are mapped to final ids internally by the
   renamer without rewriting the preset node fields — for HInvoke the
   target's callee_node_id is the callee NODE id by existing contract,
   so binding identity is asserted through binding_names/binding_types.

Gates re-run (assertion-only change; per review no new 5-suite smoke):
barrier module 8/8 + transaction tooth + callinfo boundary = 24 passed;
`git diff --check` clean.

Previous status: REVISION 2 READY FOR REVIEW — responds to
review-2026-08-04T23-19-58Z (four blocking gaps).  Gates LANDED green:
focused battery (barrier module 8/8 + tooth + consumer boundaries + B5
probes) 94 passed; 5-suite compiler smoke 1123 passed; `git diff
--check` clean.

## REVISION 2 (review-2026-08-04T23-19-58Z)

1. (P1 nested API) `CheckerStateTxn.rollback_report_outer()` added —
   rolls back AND reports whether an enclosing probe remains open; depth
   ownership stays in the transaction layer.  The resolver's catch path
   now delegates to a testable authority
   `CR._consume_pending_barrier(txn)`: True → outermost converted
   (counter); False → re-raise, covering BOTH the nested case (counter)
   and the FAIL-CLOSED case (a transaction object without the operation
   is rolled back and the barrier PROPAGATES, no counter, never a
   guessed "outermost" conversion).  The begin_state_txn contract doc on
   CallResolverContext updated to name the three operations.  New test
   test_barrier_consume_authority_production_branch exercises that exact
   production branch over REAL FnCheckState/CheckerStateTxn objects:
   inner rollback+False+nested-counter, outermost rollback+True+
   outer-counter, exact owner-fingerprint identity after both, no
   rollbacks_exception movement, and the legacy-txn fail-closed path
   with zero counter movement.  (A NATURAL nested source shape remains
   unconstructible: probed candidates do not re-probe their own nested
   args — verified empirically with `h.put(dflt2(pass(f())))`, which
   produces exactly one outermost barrier deferral; the review's
   factored-authority allowance applies.)
2. (P1 audit) The state-identity audit now snapshots the PENDING OWNER
   explicitly (exact binding ids + lambda OBJECT identity + structural
   HLambda state via _stable_state_repr) and takes a FULL structural
   `_lambda_fn_specs` snapshot (per-spec param/return/throw structure +
   call-info map id + a separate live-frame-map alias identity bit),
   both at transaction begin and after rollback, alongside the retained
   whole-body HIR / frame-locals / owner-fingerprint channels.
3. (P1 retry metadata) The B5 control is now
   test_complete_control_preserves_retry_metadata_and_runs: captures the
   finalized main TypedFn (check_function wrap) and pins the stored
   lambda's function type (() -> Int nothrow), the exact INDIRECT `f()`
   CallInfo (callee == f's binding id, ret Int, no params, nothrow), and
   the DIRECT `pass` CallInfo ((Int) -> Int) — runtime exit 0 retained
   as the companion, not the only assertion.
4. (P1 linking/HInvoke) test_unlinked_shadowed_hcall_links_exact_inner_binding:
   an UNLINKED call HVar with two same-named pending lambdas in scope
   (outer String, inner Int) — the live lexical scope links the INNER
   binding id before pending resolution; CallInfo callee id and Int
   result pinned (a name-history authority would select String).
   test_hinvoke_consumer_resolves_pending_through_owner reaches the
   explicit HInvoke pending consumer (synthetic-HIR contract; source
   stored calls parse as HCall) and pins its INDIRECT Int CallInfo.
   EXPLICIT SCOPE STATEMENT: the parent finding's HVar VALUE-USE
   totalization is FUTURE WORK — it has no consumer yet and is NOT
   claimed covered; when that consumer is written it must obtain
   lambdas via owner.begin_resolution, and its tests belong to that
   slice.

Original implementation record (constraints C1-C6) follows below.

## IMPLEMENTATION (2026-08-04, all six constraints)

- refactor_triggers.md rescanned at start: no matching entry (walkers/
  DMIR/RawBuffer/MIR-classification/String-matrix/FFI-lint/site-3 —
  none concern checker transactions or pending-lambda state).
- (C1) `PendingLambdaOwner` (type_checker.py, module-level): private
  backing map; register / peek (read-only) / begin_resolution
  (exact-binding, DECLARES mutation intent) / consume / drain.  All five
  former direct-map sites rewired (HLet registration, HCall + HInvoke
  consumers, end-of-function drain); `grep pending_lambda_by_binding`
  over the compiler tree is ZERO.  The owner covers the parent finding's
  eventual HVar value-use route by construction (any future consumer
  must call begin_resolution to obtain the lambda).
- (C2) Barrier fires in begin_resolution/register/consume/drain BEFORE
  any external mutation; the consumers' first mutation
  (`setattr(pending, "expected_fn_inferred", ...)`) now happens only
  after begin_resolution returned.  Lexical binding linking is untouched
  (scope link precedes the pending lookup exactly as before); no
  binding_id_by_name involvement.
- (C3) `PendingLambdaBarrier(BaseException)` defined in
  call_resolver.py (BaseException so broad `except Exception` typing
  guards cannot swallow it).  Probe machinery: dedicated except clause
  BEFORE the generic handler — rolls back; if the state's `_txn_depth`
  is still > 0 (nested) increments `pending_barrier_nested` and
  re-raises; the outermost increments `deferrals_pending_barrier` and
  returns the ordinary silent Unknown deferral.  NO diagnostic, NO
  `_defer_probe_hard_error`, NO `rollbacks_exception` increment
  (asserted in the regression).  Both counters documented at the stats
  dict.
- (C4) Red-first regression
  lang/tests/checker/test_pending_lambda_probe_barrier.py::
  test_probe_rollback_preserves_full_state_identity — full state
  identity across every audited rollback: owner fingerprint, raw frame
  locals, whole-body HIR, `_lambda_fn_specs` incl. the live
  call-info-map alias (id-keyed).  RED PROVEN on the pre-barrier
  committed tree via a read-only `git archive HEAD` scratch run: fails
  with FRAME-LOCALS + BODY-HIR + `_lambda_fn_specs` mismatches
  (verbatim failure preserved in this pass's transcript evidence).
  GREEN on the fixed tree; the retry emits the one real diagnostic
  exactly once.
- (C5) Nested pin over REAL FnCheckState/CheckerStateTxn objects:
  owner barred while ANY txn is open (inner rollback alone does not
  unbar — the machinery's re-raise predicate), unbarred only at
  outermost close, refused ops mutate nothing.  B5 COMPLETE control
  retained: compile + RUN exit 0 post-barrier (the barred candidates
  defer and the expected-context retry resolves the same program;
  forcing-shape counters: 53 commits + 1 barrier deferral; control:
  110 commits + 2 barrier deferrals, run exit 0).
- (C6) Ordinary safe probes unchanged (tooth suite in the gate battery);
  exact-id shadowing/unlinked coverage in the owner unit tests; HCall +
  HInvoke consumers share the single owner (structural parity; the
  HInvoke-under-probe SOURCE shape remains unconstructible — stored
  source calls parse as HCall — so its parity is pinned at the owner
  boundary).  Version: pending 0.35.0 retained; ABI 22; history
  paragraph folded into the pending 0.35.0 entry.

## Gate results (LANDED, all green)

- Focused battery (barrier module + transaction tooth + callinfo/
  reconciliation boundaries + stored/uninvoked lambda guards +
  hidden-lambda boundary + nested-callinfo ownership + B5 signature
  probes): 91 passed.
- 5-suite compiler smoke: 1120 passed.  One iteration finding: the
  repo-wide CleanupPlan bypass lint
  (test_cleanup_plan.py::test_production_consumes_via_emitter_phase...)
  pattern-matches any production `.consume(` call and flagged the
  owner's original `consume` method — a pure name collision with the
  CleanupPlan session API.  Resolved by renaming the owner method to
  `retire` (no lint/test edit needed; the lint's tripwire stays fully
  intact for real CleanupPlan bypasses).
- Barrier regression module standalone: 5/5; red-first proof recorded
  above.  `git diff --check` clean.

Previous status: DESIGN SELECTED (review-2026-08-04T22-38-58Z) — the
MUTATION-SITE BARRIER accepted as leading Option A form.  The six
acceptance constraints from that review are adopted verbatim as the
implementation contract:
1. explicit pending-lambda OWNER (registration/lookup/resolve/consume/
   drain all routed through it; direct map mutation structurally
   unavailable; covers HCall+HInvoke+the parent's eventual HVar path);
2. barrier check BEFORE any stamp/type/pop/rebind/global publication,
   with lexical scope linking preserved ahead of the check (never
   binding_id_by_name);
3. private structured barrier signal distinct from NEEDS_EXPECTED:
   nested probes roll back + re-raise; only the OUTERMOST converts to
   silent expected-context deferral; no _defer_probe_hard_error, no
   diagnostic, no rollbacks_exception increment; NEW distinct counters
   (outermost deferrals separate from nested plumbing);
4. red-first regression asserting full state identity (whole-fn HIR +
   unowned frame channels + owner fingerprint/allocators +
   _lambda_fn_specs incl. the live-map alias check) — never weakened to
   user output;
5. nested-propagation unit pin via explicitly opened outer+inner probes;
   COMPLETE control retained with CallInfo/type equality after retry;
6. ordinary safe probes stay enabled; direct HCall+HInvoke coverage;
   exact-id shadowing/unlinked-linking coverage; refactor_triggers.md
   rescan at start; pending 0.35.0, ABI 22.

Reviewer confirmation of the evidence: the escaped LambdaFnSpec
publication is real (checker-global, outside CheckerStateTxn, retains
the live frame call-info map) — not TypeTable interning or a fingerprint
artifact.  Option D remains the escalation path only.

## Forcing probe result (probe_forcing_rollback.py, work-only)

Shape: `h.put(dflt2(f()))` — the method-argument candidate `dflt2(f())`
first resolves pending stored lambda `f`, then fails T-inference,
forcing THAT candidate's transaction to NEEDS_EXPECTED rollback.
Audit: the tooth's independent technique (owner fingerprint + raw
check_function frame locals + whole-body HIR), narrowed to main::main.

Observed (1 audited rollback; stats delta probes 54 / commits 53 /
rollbacks_needs_expected 1):

- `pending_lambda_by_binding`: `{3: HLambda(...)}` → `{}` — popped
  inside the probe, NOT restored by rollback.
- `binding_types`: `{3: 1(Unknown), 4: 15}` → `{3: 1801(fn type), 4: 15}`
  — the pending binding left REBOUND to the resolved function TypeId.
- `binding_for_var`: gained `{17: 3}` — the callee HVar's binding
  association from inside the rolled-back probe survived.
- BODY-HIR mismatch: the stored HLambda initializer (OUTSIDE the probed
  subtree) was mutated and the mutation survived rollback.
- NO owner-fingerprint mismatch: `FnCheckState.OWNED_TABLES` rolled back
  exactly — confirming the leak lives precisely in the UNOWNED channels
  the child finding predicted (frame dicts + out-of-subtree HIR).
- The subsequent expected-type retry emitted the real diagnostic exactly
  once (`cannot infer type arguments for 'dflt2': T`), so THIS shape's
  user-visible outcome happens to stay correct — the breach is in the
  transaction contract, not (yet) in observed output.  A shape where the
  retry consumes the leaked resolved binding differently remains the
  candidate for a user-visible symptom; not yet constructed.

This satisfies the child's first acceptance criterion (forced rollback +
audit naming the unowned channels) and is the seed for the mandatory
red-first regression once shared edits open.

## Classification

CONFIRMED LANGUAGE_BUG-class transaction-contract breach (internal
correctness; user-visible impact shape not yet demonstrated).  The
finding is NOT rejected; the reviewer's mutation-closure inventory is
consistent with the observed channels (binding metadata + external
HLambda confirmed empirically; `_lambda_fn_specs`/allocator/capture
channels not yet isolated in this shape — the resolved captureless
lambda here exercised fnptr/spec publication paths only on the retry,
not observably inside the rolled-back probe; a capture-bearing variant
is the next probe if the design choice needs that closure bound).

## Design-matrix implications (DESIGN.md options, with this evidence)

- The proof confirms the syntactic subtree snapshot is insufficient for
  semantically-reached state — ruling out "do nothing" and any partial
  Option B (adding just the two dicts would still leak the external
  HLambda mutation and binding_for_var, and the BODY-HIR channel needs
  dependency-closed snapshots).
- Option A (centralized semantic barrier) matches the observed leak
  surface: every leaked channel was reached THROUGH the pending-binding
  resolution — a fail-closed "candidate references deferred semantic
  state" gate at probe admission would have prevented the breach here.
  Its totality obligation (HCall/HInvoke/aliases/future forms) is
  exactly the parent finding's consumer inventory, which we now have.
- Option D (staged/pure resolution) remains the strongest long-term
  shape; this evidence alone does not yet force it (one escape family,
  all through one pending-resolution entry point).
- Options C/E: no new evidence for or against; both remain larger than
  the demonstrated defect.
- PROVISIONAL POSITION (K): Option A implemented as a centralized
  transaction-purity authority (exact-binding, fail-closed, covering
  HCall+HInvoke+HVar-value reads, pinned by a new-producer tripwire
  test), with Option D recorded as the escalation path if a second
  escape family appears.  Final selection deferred to the reviewer
  round + Slawomir's implementation clearance per the child plan.

## Boundary discipline

Only files under this child folder were created; no shared compiler,
test, spec, history, fixture, or infrastructure file was touched.
refactor_triggers.md rescan still owed at implementation start.
