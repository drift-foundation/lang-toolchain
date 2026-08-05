# Implementation handoff

Timestamp: 2026-08-05T01-40-42Z
Role: implementer
Actor: k

Responds to the exact incoming claim:

`CLAIMED--REVIEW-PENDING-2026-08-05T01-36-27Z--BY-k--SEED-46dae92ab18cc9f2a91412ac92fee356--AT-2026-08-05T01-36-27Z`
# Planning round 2: all four decisions resolved; two more family defects found (K)

Author: K (implementer), actor `k`, seed `46dae92ab18cc9f2a91412ac92fee356`

Responds to the claim recorded in the baton receipt for
`REVIEW-PENDING-2026-08-05T01-36-27Z` (target sha256
47d6e301b29b6d95fc843cefedc747ce94975f0e331fda6e6bbcb7b1729834e5).
Probe source: `probe_planning_round2.py` (work-only, narrow).

## 1. Provenance totality: call-only is ALSO disproved

`m = move bad; m()` and `t = (true ? bad : bad); t()` both present a
SINGLE unknown-name primary today — the transparent wrappers carry the
poisoned Unknown into a new binding silently, exactly like the call
result.  Under exact-binding cause both would regress to fresh
tripwires.  Evidence therefore selects Phase 5's EXPRESSION-FLOW arm
outright: the smallest transaction-owned node-cause channel must cover
at least {HVar-read-of-caused-binding, HMove subject, ternary arms
(literal-folded proven; general form to pin), causally-suppressed call
results}, attached to bindings at HLet.  I withdraw my round-1 "one
narrow call-result rule" proposal as insufficient.

## 2. Bare pending HVar argument: ANOTHER ICE-class family defect

`take_cb(f)` (bare pending lambda into a `core.Callback0<Int>` param)
fails with `MIR validation contract failure ... MoveOut of uninitialized
iface local 'f'` — the argument-position wrap path stamps the BINDING as
the iface without constructing anything, then MIR moves an uninitialized
iface local.  Same disease as the typed-let case: an interface label
without a wrapper.  Post-check structure capture was preempted by the
ICE; the defect itself is the finding.

## 3. Borrow contract: control decides FINALIZE-AND-ACCEPT

- Finalized-binding control (`val a = f(); val r = &f;`): compiles AND
  runs — borrows of a finalized thin-fnptr binding are accepted today.
- Named-fn control (`val r = &seven;`): raises a RAW AttributeError
  (`'HFnPtrConst' object has no attribute 'name'`) — a third ICE-class
  defect (borrow materializer meets HFnPtrConst).
Decision per the control: pending `&f` should FINALIZE-AND-ACCEPT
(consistent with the accepted finalized-binding behavior); the named-fn
borrow crash needs its own repair (likely the same borrow-walker gap)
and belongs in the family's regression matrix.

## 4. Typed-Callback HLet: exact bypass identified structurally

Captured post-check state for `val g: core.Callback1<Int,Int> = |x| => x`:
binding type IS `Callback1` (INTERFACE KIND), the HLet initializer node
REMAINS raw `HLambda`, and the checker emits ZERO diagnostics.  The
mechanism: the HLambda visit records `expected_type` ITSELF for callback
interface expectations (`return record_expr(expr, expected_type)` —
candidates at type_checker.py:8013 (`allow_capture_invoke` route) and
:8205; the firing line to be pinned with a trace at implementation), so
back at HLet `inferred == declared` and the Site-5 wrap branch
(:12452-12475, which only runs when inferred != declared) is
UNREACHABLE.  Restoration shape: the lambda visit must not label itself
with the callback interface outside the real wrap constructions; the
typed-let/argument slots then reach the intact `_try_callback_wrap_for_
iface_slot` authority.  This matches the reviewer's
finding-typed-let-callback-wrap-regression framing (restore Site 5, no
ruling needed) — and item 2 above shows the ARGUMENT slot needs the
same restoration.

## Plan corrections proposed

- Phase 5: commit to the minimal node-cause channel (expression-flow
  arm) with the four proven transparent shapes as its initial closed
  set + a fail-toward-tripwire default for unproven shapes.
- Family regression matrix additions: bare-pending-arg ICE (item 2),
  named-fn borrow AttributeError (item 3), pending-borrow
  finalize-and-accept pin, typed-let wrap restoration compile/RUN pins.
- Q4 stands as a planned regression (no capture effects on bare
  reference); no deeper probe needed.

Shared implementation remains blocked on the suite + terminal planning
acceptance.
