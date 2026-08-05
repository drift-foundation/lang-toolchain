# Implementation handoff

Timestamp: 2026-08-05T01-33-02Z
Role: implementer
Actor: k

Responds to the exact incoming claim:

`CLAIMED--REVIEW-PENDING-2026-08-05T01-28-12Z--BY-k--SEED-46dae92ab18cc9f2a91412ac92fee356--AT-2026-08-05T01-28-12Z`
# Planning review: five premises probed; two discoveries change the plan (K)

Author: K (implementer), actor `k`, seed `46dae92ab18cc9f2a91412ac92fee356`

Responds to the claim recorded in the baton receipt for
`REVIEW-PENDING-2026-08-05T01-28-12Z` (target sha256
c888a31402977bd83da6dddcb1d0f144845369a374ac974f2099cd598a9cc4cb).
Probe source: `probe_planning_review_matrix.py` (work-only, narrow; ran
alongside the active suite without material competition).

## Q1 — binding-only cause state is NOT total (counterexample)

`bad = missing_name; x = bad(); x();` produces exactly ONE diagnostic
today (`E-UNKNOWN-NAME`), with `bad()`, the `x` binding, and `x()` all
silenced by the global scan.  Under exact-binding cause: `bad` is caused,
`bad()` suppresses correctly — but `x` inherits Unknown from the
SUPPRESSED CALL RESULT with no new primary and no cause, so `x()` would
emit a fresh call-target tripwire: a presentation REGRESSION vs today.
Binding-only therefore needs ONE narrow expression-provenance rule: when
the binding-call consumer suppresses causally, record the causal
suppression on THAT call node; `HLet` propagates it to the result
binding.  This is a single producer-shape rule (suppressed-call-result →
HLet), not a general node-provenance system; I propose amending Phase 5
to name it explicitly rather than choosing between the two extremes.

## Q2 — the single HVar hook is short two contexts; one silent hole found

- `val g = move f; g()` — cascades today (`E-COPY-UNKNOWN` on `g`): the
  hook must fire for `HMove` subjects too (move bypasses the copy check
  but not the Unknown inheritance).
- `val r = &f;` — compiles SILENTLY today (exit 0): a borrow of the
  pending binding's Unknown-typed storage is accepted with no
  finalization and no diagnostic.  This is a silent-acceptance hole the
  plan did not list as such; the hook (or an explicit tripwire) must
  cover borrow positions, and the choice deserves a pinned contract.
- Discarded `f;` compiles clean and flush-finalizes later — acceptable
  as-is (no value escapes) but worth a pin.
- Compatible-argument via direct `f()` already resolves (control).

## Q3 — BLOCKED by a newly found DIRECT-form defect (LANGUAGE_BUG-class)

`val g: core.Callback1<Int, Int> = | x: Int | => x;` (bare lambda
literal DIRECTLY into an annotated Callback slot) fails TODAY with an
INTERNAL error: `MIR lowering contract failure (raw HLambda reached HLet
lowering (checker bug))` — the implicit callback wrap does not exist for
HLet initializer slots, and nothing rejects cleanly first.  An ICE on
this source is a defect regardless of the intended contract.  The
contextual-ALIAS design target (wrap `f` when aliased into a Callback
slot) cannot be specified until the DIRECT form's contract is decided:
(a) implicit wrap at annotated HLet slots (consistent with the
argument-position Site-6 wrap), or (b) clean rejection telling the user
to write `core.callback1(...)`.  This is a language-contract choice —
SLAWOMIR'S RULING REQUESTED — and I propose adding it as a nested child
(direct-form first, alias behavior then defined to match it).

## Q4 — capture effects (existing evidence + construction-timing analysis)

The explicit-copy capture alias probe (earlier matrix) shows the double
diagnostic today; nothing in the current flush path constructs a
callback for the rejected bare storage, and the v1 contract places
capture effects at construction of the SUPPORTED wrapper only.  The
finalizer must emit the approved primary via the same flush
classification WITHOUT entering any callback-construction path; I found
no evidence that current code applies capture effects on bare-value
reference, and propose pinning exactly that (no effect side-channel) in
the Phase-3 capturing tests rather than probing deeper now.

## Q5 — confirmed safe today; preserve

The unconstrained uninvoked flush emits the single clean cannot-infer
primary and publishes NO `LambdaFnSpec` at all (registry empty; no
Unknown-ABI types).  The finalizer must preserve exactly this
reject-before-publish property; Phase 6 item 5 stands as written.

## Proposed ready-plan changes

1. Phase 5: name the suppressed-call-result → HLet propagation rule as
   the required narrow expression-provenance extension (Q1).
2. Phase 2/3: add move-subject and borrow-position contexts to the hook
   matrix; decide + pin the borrow contract (Q2).
3. New nested child for the Callback-slot DIRECT-form ICE with an
   APPROVAL gate for the contract ruling (wrap vs clean-reject); the
   contextual-alias acceptance criterion is deferred to match that
   ruling (Q3).
4. Phase 3 capturing tests additionally pin "no capture effects on bare
   reference" (Q4).
5. Otherwise the refreshed parent/child material matches the probe
   evidence; the fold-in premise HOLDS for everything except Q3's
   alias case, which waits on the ruling.

Shared implementation remains blocked on the suite + convergence, per
the gate.
