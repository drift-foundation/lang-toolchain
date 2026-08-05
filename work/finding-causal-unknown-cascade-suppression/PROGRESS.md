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
