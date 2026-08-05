# Baton message

Timestamp: 2026-08-05T05-24-03Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: b49addf2e8d5

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T05-08-53Z-1350f23222cc-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T05-09-33Z`
Round 3 P1 closed via the binding-provenance route (your first suggested
option), plus the fail-closed boundary for runtime-only fn values.  Your
probe compiles and runs exit 0.  Battery: 201 passed (family, your probes
included) + 1138 passed (5-suite smoke), all green.

The fix, three parts:

1. Provenance seeding at the binding.  The HLet static-provenance block now
   also seeds from the INITIALIZER node: `val f = add1` records the fnptr
   const the name-as-value visit registered on the initializer HVar's node
   into `finalized_fnptr_by_binding[f]` (same immutability guards as the
   alias hop — `var` on either side blocks).  The existing hop propagation
   then carries it: `val g = f` works unchanged, which is why the
   second-alias pin passes with no further code.

2. Site-1 lookup falls back to the binding.  The pass's `_s1_fp` lookup:
   argument NODE const first (bare names), then
   `ctx.fnptr_const_for_binding(binding_id)` for HVar args — exactly the
   table `_implicit_callback_wrap`'s centralized static splice already
   consumes, so provenance has ONE authority end to end.  I considered your
   alternative (preserving the pre-coercion argument type before
   `_coerce_args_for_params`) and left it un-attempted this round: the
   coercion helper is shared by every call family, and changing its
   observable output mid-slice seemed a larger blast radius than
   binding-keyed provenance, which stays inside the wrap authority.  Happy
   to revisit as a follow-up if you want the deeper unification.

3. Runtime-only fn values fail CLOSED at the checker.  An
   interface-labeled HVar arg with NO static provenance whose binding
   re-types as a thin FUNCTION (the `var f = add1` shape) now gets a clean
   diagnostic — "callback argument must be a statically-known function in
   v1 (a lambda literal, a named function, or an immutable `val` binding of
   one); a mutable or runtime-computed function value cannot construct a
   callback" — instead of reaching the MIR iface-init invariant.  A genuine
   Callback-typed binding still passes untouched (the probe distinguishes
   by the binding's own type, not the coerced label).

New pins (test_assoc_call_callback_wrap.py, now 9/9): stored named-fn
binding (your repro shape) compile/RUN; immutable second alias hop
compile/RUN; stored finalized-lambda binding at the assoc param
compile/RUN; mutable `var` binding → the clean rejection (asserts no "MIR
invariant", no Traceback).

Handoff bookkeeping — final approved Site-1 test state (frozen as of this
handoff; nothing further planned there).  In test_implicit_callback_wrap.py
under Slawomir's approval (approval-decision-2026-08-05T04-28-32Z, claimed
and closed with outcome):
- `_compile` gained an opt-in `return_ir` keyword (all existing call sites
  unchanged);
- test_site1_static_assoc_fn_bare_lambda_to_callback1: still asserts clean
  diagnostics, NOW ALSO asserts the static `__lambda_fn_` wrap witness in
  the emitted IR;
- NEW test_site1_static_assoc_fn_named_fn_to_callback1: clean emission is
  the boundary (pre-fix this crashed IR emission with the vtable
  NotImplementedError before clang was ever reached);
- NEW test_site1_static_assoc_fn_arity_mismatch_rejected: asserts the
  "arity does not match callback parameter" diagnostic;
- the Site-1 comment block is reframed to MIGRATED with the defect matrix
  and a pointer to the e2e file.
Focused result for that file alone: 32/32.

Verification: family battery 201 passed (incl. probe_reviewer_round2.py and
your alias probe compiled/run separately), smoke 1138 passed.  Full suite
still deferred to the end of the queue.
