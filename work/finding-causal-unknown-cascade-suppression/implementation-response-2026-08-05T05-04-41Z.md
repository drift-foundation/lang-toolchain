# Baton message

Timestamp: 2026-08-05T05-04-41Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: b49addf2e8d5

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T04-27-45Z-31adeb828b0c-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T04-27-45Z`
Round 2 complete — all four P1s and the P2 addressed; your four probes are
4/4 green in-tree; final family battery 195 passed (your probes included) +
5-suite smoke 1138 passed.  Also folded in here: the first P1-round battery
turned up one regression I introduced and have reverted (details at the end).

R2-1 (caused-callee HCall skipped independent arguments).  One SHARED
argument observation now sits before both failure arms of the binding-call
not-a-function path (the caused-suppression arm and the tripwire both
return; the FUNCTION arm keeps typing args itself).  Two exclusions, each
reasoned: bare HLambda args (no callable target → no typing context; an
unconstrained-inference error would itself be a cascade) and args whose
FIRST visit already produced a causal primary (ctx.expr_unknown_is_caused —
the pending-callee consumer pre-types arguments, and re-visiting a diagnosed
arg re-emits its primary; the first battery caught exactly that duplicate on
the residual-Unknown pins before this guard).  In-tree pin: both
unknown-name primaries, len==2, no call-target cascade.

R2-2 (caused-receiver method suppression skipped arguments).  The early bail
in resolve_method_call is RELOCATED to after ordinary argument typing —
only the receiver-derived no-matching-method noise is suppressed, and the
result node still inherits `caused-receiver-method` after the args are
checked.  In-tree pin mirrors R2-1's (both primaries, len==2).

R2-3 (match/try compound joins now have real producers).  HMatchExpr
collects every value-producing arm's result expr that typed Unknown/None and
marks `caused-match` only when the result is Unknown AND that set is
non-empty AND every member is caused.  No scrutinee folding — deliberately:
treating every arm as reachable can only WITHHOLD suppression (one uncaused
arm keeps the tripwire), so the conservative join fails toward diagnosing;
that is the narrowed contract I am declaring rather than a literal-scrutinee
reachability mirror, and the mixed-arm pins hold it.  E-MATCH-NO-VALUE now
marks its own node (`caused-match-novalue`) — the primary is ON the
compound, producer-local, so the compound-excluded HLet watermark is not
needed.  HTryExpr joins the attempt plus every value-producing catch arm
(`caused-try`), same all-or-tripwire rule.  Your two probes plus four
in-tree pins (both sides for match AND try) are green.

R2-4 (Site-1 assoc-call Callback defect — fixed now, as a child finding).
`findings/finding-assoc-call-callback-silent-coercion/` with FINDING.md;
refactor_triggers.md scanned — no registered trigger covers callback-wrap
site unification, so the deliverable stayed a minimal root-cause repair
routing Site 1 through the SAME canonical wrapper authority.  RED-FIRST:
lang/tests/driver/test_assoc_call_callback_wrap.py proved THREE defect
shapes before the fix (bare lambda → invalid IR at clang; named-fn arg →
`interface impl not found for interface value` NotImplementedError — a
second crash shape the matrix hadn't caught; arity-1 lambda at a Callback2
param checker-silent) with two green controls (explicit wrap, free-fn).
FIX at the assoc-call success path in resolve_call_expr, before
record_call_info: each concrete Callback* param with a bare-lambda or
fn-typed arg routes through `_try_wrap_arg_for_callback_field` — WRAPPED
splices into expr.args; REJECTED poisons; SKIP for such an arg is an ARITY
mismatch and now emits a real diagnostic ("function value arity does not
match callback parameter ...") instead of silently accepting invalid IR.
Named-fn detection detail: by the time the wrap pass runs, the silent
coercion has already recorded the arg AS the interface — but the
name-as-value path registered the node's STATIC fnptr const, so the pass
recognizes the shape by that const and synthesizes the thin fn type from
its call_sig (fallback: derive the expected fn shape from the interface's
instantiated type args, params + return last, throw-ness from the kind).
Free-fn arity mismatch verified already-clean ("no matching overload") —
the old note's free-fn-leniency claim was stale.  The arity-negative pin is
in the new file.  History: folded into 0.35.0 as a pre-existing
LANGUAGE_BUG entry.  Per your authorization boundary I did NOT touch the
two existing Site-1 checker-only assertions — the Site-1 comment block is
updated (comment-only, prior authorization) to MIGRATED status; converting
those assertions to e2e awaits Slawomir's explicit approval.

P2 (fnptr-borrow pin consumes the borrow).  BORROW_NAMED_FN now returns
`(*r)() - 7` — calling through the borrow; file 5/5.

Also fixed since the last handoff (first P1-round battery, task b62mzamk2):
my "drop context when all params annotated" classify refinement demoted the
contextual declared-type diagnostic to the reconciliation error
(test_driver_mixed_prefix_contextual_single_declared_diagnostic red).  The
context-drop is REVERTED — annotated lambdas keep the full callsite context
exactly as before; the unannotated-slot hopeless bail and the
residual-Unknown poisoning/retraction stand.  Reconciliation file 14/14.

Verification: final battery 195 passed across the full pinned family
INCLUDING probe_reviewer_round2.py, 5-suite smoke 1138 passed, both fully
green.  Full suite remains deferred to the end of the queue.
