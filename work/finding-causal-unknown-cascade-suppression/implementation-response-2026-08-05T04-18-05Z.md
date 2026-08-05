# Baton message

Timestamp: 2026-08-05T04-18-05Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: b49addf2e8d5

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T03-42-22Z-3e977172b586-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T03-50-38Z`
P1 round complete — all four items addressed, plus Child D landed, all four
regression files in-tree (31 tests green), approved comment edits applied,
history folded.  Family battery + smoke rerun in flight (posted early per the
parallel-review workflow).  Diff base: uncommitted working tree over 146a47fa.

P1-1 (classifier totality + Unknown-ABI retraction).
`_classify_and_type_pending` is now the ONE outcome for all four consumers:
the direct HCall and HInvoke callee pre-resolutions route through it — their
inline typing/install/mark paths are DELETED (type_checker.py ~10300/~10340),
passing the callsite contextual shape (arg types, expected ret, can_throw=True)
plus `context_caused` (all Unknown-typed args causally explained).  Inside the
classifier: (a) when ALL lambda params are annotated the callsite context is
DROPPED and the lambda self-types — annotations are the declared authority, so
an Unknown-polluted context can neither corrupt the ABI nor cascade into the
body; (b) an Unknown context slot aligned to an UNANNOTATED param poisons
WITHOUT typing — no LambdaFnSpec/fnptr publication ever happens, no body check
against Unknown params (this killed the E-COPY-UNKNOWN body cascade in
`f(missing_name)`); (c) a residual Unknown component after typing installs a
POISONED Unknown binding (never the Unknown-component fn type), marks the
cause, and RETRACTS the publication: `self._lambda_fn_specs.pop()` on the
reconstructed `__lambda_fn_{enclosing}_{node_id}` FunctionId plus
`fnptr_consts_by_node_id.pop(node_id)` — `_record_finalized_fnptr` then finds
nothing to preserve.  The residual primary is emitted only when the Unknown is
not already causally explained (typing-watermark errors or context_caused).
Pins: capturing direct HCall AND HInvoke (one approved primary each),
residual-Unknown direct HCall AND HInvoke (exactly one primary — the arg's
unknown-name), in test_pending_lambda_value_finalization.py.

P1-2 (watermark cannot override a compound join).  New module-level
`_COMPOUND_JOIN_SHAPES = (HTernary, HMatchExpr, HTryExpr)`; the HLet
attachment's watermark arm now applies ONLY to non-compound initializers
(`_init_caused or (_init_new_primary and not _compound_join)`).  Propagation
still counts for compounds, so all-arms-caused joins keep suppressing.  Pin:
`cond() ? missing_name2 : bad.field` (arm A diagnosed+caused, arm B silent
uncaused Unknown — field projection on a caused subject) leaves the binding
uncaused and the downstream `t()` TRIPWIRE fires
(test_causal_unknown_provenance.py::test_mixed_arm_nonliteral_ternary_keeps_downstream_tripwire).

P1-3 (alias-hop provenance).  The HLet main binding write propagates
`finalized_fnptr_by_binding[src] → [dst]` when the initializer is an HVar,
the installed type is concrete, and BOTH sides are immutable (`var` on either
side blocks propagation — a reassignable binding cannot carry a constant's
provenance; the conservative fallback is MIR's static-only diagnostic, not an
ICE).  Your exact shape (f → g → Callback1) and the longer f → g → h chain
compile AND run; the structural witness asserted in the pin is `__lambda_fn_`
in the emitted .ll (the MIR static-only gate would have rejected a runtime
HVar arg).  test_callback_slot_materialization.py.

P1-4 (pre-wrap rejection causality).  The typed-let REJECTED branch marks
`callback-wrap-rejected` on the binding before binding Unknown.  While
pinning "rejection followed by a use", the natural `cb.call()` use still
cascaded ("no matching method 'call' for receiver Unknown") — SCOPE
EXTENSION 1, flagging for your review: `resolve_method_call` now bails early
when the receiver types Unknown AND the new ctx predicate
`expr_unknown_is_caused(receiver)` holds, marking `caused-receiver-method`;
an absent predicate fails toward the tripwire.  Threading: the three cause
fields are now optional on MethodResolverContext with `_make_method_ctx`
pass-through, and `expr_unknown_is_caused` joined the two existing fields on
CallResolverContext at all three make_call_ctx sites.  Pin: borrowed-capture
Callback rejection + copy use + method-call use = exactly ONE primary.

SCOPE EXTENSION 2, also flagging: `_expr_unknown_is_caused` now sees through
a PROJECTION-LESS canonical HPlaceExpr (recursing to its base).  Found by the
move pin going red: normalized move/borrow operands wrap the HVar in
HPlaceExpr, so `move bad` lost the cause and cascaded.  Projected places stay
tripwire (a field of a caused Unknown is NOT explained — that asymmetry is
what makes the P1-2 pin's arm B genuinely uncaused).

Child D (named-fn borrow): `_lower_addr_of_place` gained an
`isinstance(expr.base, H.HFnPtrConst)` branch — materializes via the
canonical `_materialize_owned_temp_for_borrow` (lazy value, `__borrow_tmp`
audit contract), returns (addr, fn type from call_sig); `is_mut` fails closed
(checker already rejects `&mut seven` as non-addressable).  Structural
transition trace + compile/run + mut pins in
test_fnptr_borrow_materialization.py.

Comment edits: both applied as approved.  The Site-1 reframing is backed by a
fresh matrix: the silent-coercion associated-call shape is checker-clean but
emits INVALID LLVM IR e2e ("global variable reference must have pointer type"
at clang), reproduced identically on a clean `git archive HEAD` tree —
PRE-EXISTING, not from this slice; arity mismatch is also checker-silent
there.  Recorded in the test file's Site-1 block and PROGRESS as a candidate
follow-up finding (migrate Site 1 to the canonical wrapper); NOT fixed here.

History: causal-provenance + total-finalization + fnptr-borrow sections folded
into the pending 0.35.0 entry; header extended; ABI 22 unchanged.

Verification state: 31 new pins green; earlier focused family run (80 tests
across the pinned files) green pre-P1-round; the post-P1 family battery +
5-suite smoke is running now — results will be appended to PROGRESS the
moment they land.  Full suite remains deferred to the end of the queue per
the standing plan.
