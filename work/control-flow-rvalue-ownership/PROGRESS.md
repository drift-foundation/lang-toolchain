# PROGRESS — control-flow-rvalue-ownership

- [x] 2026-08-02 0.34.0 WITHDRAWN (ternary-bind double-free found post-stage).
      Version -> 0.34.1. Ternary bind fix landed (MoveOut in
      _visit_expr_HTernary); e2e ternary_owned_result_bound_drops_once +
      ternary_owned_result_bind_then_borrow green (base+ASan+memcheck).
- [x] 2026-08-02 type_expr split landed: used_as_value vs defer_value_use;
      _require_copy_value gated on `_copy_use = used_as_value and not
      defer_value_use`. ~12 call_resolver arg sites -> used_as_value=True,
      defer_value_use=True. 751 checker/stage2/borrow green. Inline match
      no longer types [Void]; bitcopy &Int match arg compiles; ternary
      consistency preserved.
- [x] 2026-08-02 Reviewer redirect folded in: GENERALIZE (don't special-
      case), ACCEPT sound VCF borrows (delete cfv rejection), 2 new bugs
      confirmed (unsafe-block proj, try-expr proj, both exit 134).
      refactor_triggers scanned (String-authoring matrix fires).
- [x] P1 DONE: _validate_lifted_chain base gate generalized (safe owned-rvalue
      base: not-a-place/not-HMove; ref bases stay call-only; &mut bind-first).
      Unsafe-block proj + try-expr proj now rc0 (were 134). Call-base controls green.
- [x] P2 DONE: `_emit_cfg_result_extract(local)` helper (must_move = not
      _should_copy_value OR _needs_runtime_drop OR _type_is_destructible;
      else LoadLocal; stamp dest type). Applied to ternary join, variant/Bool/
      scalar match joins (~2314/2469/2618 -> single line), try-expr join.
      by-value Bool+variant match now rc0 (was 'cannot copy __match_bool_tmp').
      Updated 2 stage2 shape tests (ternary join now Move|Load; match binder
      test scoped to exclude the result-temp forwarding MoveOut).
- [~] P4 IN PROGRESS: probe confirmed whole ternary/match borrows now SOUND
      (rc0) with P1+P2, so CFV rejection is stale. DELETED across 13 files:
      type_checker (helpers _cfv_rvalue_borrow_hazard/_cfv_source_borrow_hazard/
      _reject_cfv_rvalue_borrow/_cfv_bind_first_diag + 3 call sites), stage1/
      borrow_materialize (materialized_rvalue_cfv stamp), stage1/hir_nodes
      (field + doc), packages/provisional_dmir_v0 (strip entry), checker/
      typed_validator (cfv_rvalue_binding branch), stale hir_to_mir comments.
      KEPT materialized_rvalue (mut-rvalue bind-first). Deleted 4 reject e2e
      fixtures + test_cfv_rvalue_borrow_codes.py; cleaned w0/provenance unit
      tests. Whole ternary+match borrows at &T now compile+run rc0.
      P4 suites: 1226 passed (checker+stage2+borrow+packages). Whole ternary
      borrow accept + memcheck-clean. Fixed a match-inference leak-adjacent bug
      (_infer_expr_type(HMatchExpr) returned None for value-BLOCK arms -> now
      reads the block trailing expr).
      *** P4 LEAK FIXED (2026-08-02): whole match borrow `inspect(match b{...})`
      leaked 18 bytes. ROOT CAUSE (traced via emit + ledger): in argument
      position `_current_expected_type()` for the match was VOID (and
      `_infer_expr_type(HMatchExpr)` also collapsed to VOID), so BOTH the match
      result local (line ~2376) AND the borrow temp inner_ty (line ~3342) were
      typed VOID -> neither drop-registered -> LEAK. FIX: `_cfg_result_type()`
      (prefers concrete per-branch/arm result value types over a mismatched
      VOID/ref expected type) applied to all 3 match lowerers (Bool/variant/
      scalar) AND to the borrow inner_ty (route CFG subjects through it when
      VOID). Also `_cfg_result_subexprs()` unwraps value-block arms. Match
      whole-borrow (Bool + variant) now base+ASan+MEMCHECK clean. Added stage2
      structural pin test_hir_to_mir_cfg_result_borrow.py (MoveOut match result
      -> StoreLocal __borrow_tmp -> __borrow_tmp in CleanupHook). Reinstated
      cfrv_match_whole_borrow + cfrv_match_variant_whole_borrow fixtures. ***
      SAFE memcheck-clean set (7): cfrv_unsafe_block_field_proj,
      cfrv_try_expr_field_proj, cfrv_match_bool_byvalue, cfrv_match_variant_byvalue,
      cfrv_ternary_whole_borrow, ternary_owned_result_bound_drops_once,
      ternary_owned_result_bind_then_borrow.
- [x] P4 COMPLETE: whole-borrow leak fixed + memcheck-clean; CFV machinery
      deleted; 751 checker/stage2/borrow green; 22 cfrv/ternary/rvalue_field_proj
      fixtures base+memcheck clean. "whole match borrow sound" now HOLDS.
- [x] P1.1 DONE (2026-08-02): `_cfg_result_type()` rewritten — precedence is
      (1) checker-RECORDED whole-expr type when concrete non-Void/non-Unknown
      (TypeVar PRESERVED, not collapsed); (2) a legitimate non-ref value-context
      expected type; (3) common type derived from EVERY value-producing arm,
      with `raise AssertionError` (fail loud) if concrete arms disagree and no
      recorded common type. Never lets the first arm alone decide.
      Pins: (a) value-block arms same owned type — cfrv_match_bool_byvalue /
      cfrv_match_whole_borrow (have, memcheck-clean); (b) generic result
      instantiated droppable(String)+bitcopy(Int) — NEW fixture
      cfrv_match_generic_result (base+memcheck clean); (c) unit pins
      test_hir_to_mir_cfg_result_type.py (recorded-wins-over-first-arm,
      fail-loud-on-disagreeing-arms, agreeing-arms-common-type, ternary-branches).
      FINDING: interface/common-type ARM CONVERGENCE is NOT expressible in Drift
      v1 — the checker rejects disagreeing arms with E-MATCH-ARM-TYPE ("arms must
      produce the same type") BEFORE lowering. So `_cfg_result_type` never sees
      disagreeing concrete arms from valid source; recorded-first + arm-agreement
      is correct and the fail-loud is a defensive checker-invariant assertion.
- [x] P1.2 DONE (2026-08-02): test_hir_to_mir_cfg_result_borrow.py capture GATED
      on `self._current_fn_id == main_id` (MIR temp IDs are function-local; the
      full pipeline lowers stdlib too, so cross-fn false matches were possible).
      MoveOut(__match)->StoreLocal(__borrow_tmp)->CleanupHook chain asserted
      entirely within `main`. `_lift_rvalue_ref_base_for_borrow` docstring
      updated (removed stale "call-only roots / index-bearing owned chains").
- [x] P3 DONE (2026-08-02) — reviewer-corrected: fix the CALLERS, don't
      compensate in the match handler.
      ROOT CAUSE: numerous user-VALUE-position sites typed their expression with
      `used_as_value=False`, which overloads False to mean BOTH "statement /
      non-value inspection" AND "value probe".  A `match` typed False collapses
      to Void (method lookup "receiver Void", overload "[Unknown]").
      FIX (semantic contract at the callers):
        * NEW `call_resolver._type_user_arg(type_expr, arg, expected_type=None)`
          = `type_expr(..., used_as_value=True, defer_value_use=True)` — the
          single source of the value-context contract for user args.  A value in
          this position (match/ternary types to its result) with move accounting
          deferred to autoborrow (re-typing during overload resolution never
          consumes).
        * Routed through it: method receivers (encode_compact 2155; main 2183;
          trait/UFCS receivers 4347/4608), std.mem intrinsic args (5247),
          callback/fn args (5859×2), kwarg values (5997/6569), variant+struct
          constructor args (6024/6681/6773).
        * The 3 PRIMARY projection-BASE value-typing sites in type_checker.py
          (HField subject 10602, HField-of-HIndex hint 10593, HIndex subject
          11307) now type the base as a value (used_as_value=True, defer) — a
          projection base that is an rvalue (match/ternary/call) is the value
          being read; places type identically and defer keeps non-consumption.
        * REVERTED the earlier match-handler compensation: `used_as_value=False`
          now cleanly means non-value inspection → the match types to Void.
          `used_as_value=False` remains ONLY on genuine non-value sites: place
          bases (5500/5537/5544), scrutinees, assignment targets, and the
          documented callback-call probes (411/4103/4135/7522/7524/7761/7795).
        * FIXED a coupled regression: the E_IFACE_FIELD_COPY check (type_checker
          ~10621) gated on `used_as_value` instead of `_copy_use`, so a borrowed
          interface-field RECEIVER (`subject.dropper.method(...)`) tripped it
          once receivers became used_as_value=True.  Now gated on `_copy_use`
          (= used_as_value and not defer_value_use), matching the `_require_copy_
          value` call directly above it — deferred value-use never trips a copy
          check; real bindings/returns still do.
      RESULT: `cfrv_match_receiver_disagree_rejected` now gets the PRECISE
      E-MATCH-ARM-TYPE diagnostic (a method receiver is a real value context),
      not a downstream "no matching method" cascade — fixture updated.
      FINDING: `_ultimate_base_is_rvalue_call` still does NOT need extending —
      the generalized P1 owned-CF-base lift already handles field/index-projected
      owned CF bases; all shapes rc0 + memcheck-clean.
      Fixtures (base+ASan+memcheck): cfrv_match_receiver_method,
      cfrv_match_field_proj_arg, cfrv_match_index_receiver, cfrv_try_expr_receiver,
      cfrv_match_generic_result; + cfrv_match_receiver_disagree_rejected (exit 1,
      E-MATCH-ARM-TYPE).
      P2 (reviewer): `_cfg_result_type` no longer returns the concrete type from
      a mixed TypeVar+concrete arm set — with no recorded/coercion result, arms
      must be UNANIMOUS (same concrete, or the same shared TypeVar) or fail loud.
      Pins added (test_hir_to_mir_cfg_result_type.py): mixed_typevar_and_concrete
      _arms_fail_loud, shared_typevar_arms_preserved.
      GATES: 757 checker+stage2+borrow green (+2 P2 pins); 364 match/ternary/try/
      proj/ctor/receiver e2e (361 ok / 3 skip / 0 fail); 97/97 mem/ptr/callback/
      closure/kwarg e2e; all P3 fixtures base+ASan+memcheck clean.
- [x] P3 FINAL BOUNDARY CLOSURE (2026-08-02, reviewer-reopened): `record_expr()`
      overwrites expr_types on every visit, so LATER passes re-typing match nodes
      with used_as_value=False collapsed them to Void in the FINAL typed HIR —
      masked at runtime by stage2's _cfg_result_type arm fallback, but producing
      wrong final HIR + wrong diagnostic (or_throw match receiver gave
      E_REQUIREMENT_NOT_SATISFIED "Int is Throw" instead of E_OR_THROW_NOT_ERROR_
      TYPE). Fixes (all → used_as_value=True, defer_value_use=True):
        * or_throw preflight receiver (type_checker.py:10135).
        * 3 HBorrow subject sites (9279 repeated-in-stmt, 9344 initial, 9400
          rvalue-materialization) — defer_value_use replaces the old
          used_as_value=False copy-suppression; comment refreshed.
        * 3 later retyping sites (10350 post-resolution/autoborrow receiver,
          10421 generic-require receiver, 10499 method-arg expected-param retyping)
          — no longer overwrite a match result with Void.
      Item 6: `_type_user_arg` is now the SINGLE source — 12 direct
      used_as_value=True/defer sites in call_resolver routed through it (only the
      helper body spells the flags). Item 7: stale comments refreshed (2 ConstShare
      walker comments; test_autoborrow_receiver_place.py docstring). Item 5: left
      the legitimate used_as_value=False inventory (places/scrutinees/HMove·HCopy
      subjects/assignment targets/callback probes) untouched.
      CHECKER-BOUNDARY PIN (item 4): test_cfrv_match_typed_boundary.py inspects
      TypedFn.expr_types after the whole method/call path — every match node stays
      Node, never Void (receiver, argument-after-expected-param-retyping,
      HBorrow-wrapped). Verified adversarial: reverting the 9400 fix leaves the
      program COMPILING yet the pin FAILS on the Void — the stage2 fallback can no
      longer mask a checker regression.
      Fixtures: cfrv_match_receiver_or_throw_not_error_rejected (exit 1,
      E_OR_THROW_NOT_ERROR_TYPE). GATES: 769 checker+stage2+borrow+boundary+
      autoborrow green; 6 _cfg_result_type unit pins; P3 fixtures base+ASan+
      memcheck (6/6) clean; broad 523-case borrow/proj/receiver/result/mem e2e
      sweep running. Review packet: REVIEW_P3_BOUNDARY.md.
- [x] P3 COVERAGE/TRUTHFULNESS closure (2026-08-02, reviewer accepted impl,
      3 coverage gaps): (1) Added driver pin test_or_throw_match_receiver_diag.py
      asserting the invalid match receiver emits exactly E_OR_THROW_NOT_ERROR_TYPE
      and NOT E_REQUIREMENT_NOT_SATISFIED (e2e runner only matches message text,
      not codes). (2) Rewrote test_cfrv_match_typed_boundary.py: direct match
      method receiver `(match).size()`, match arg to a `&Node` METHOD param
      `s.absorb(match)` with STRUCTURAL assertion HBorrow(subject=HMatchExpr), and
      a generic-require receiver `(match).peek()` on Box<T> with `require T is
      core.Copy` — exact HMethodCall.receiver / HBorrow.subject shapes + exact
      count==3, not "all matches Node". Verified: 10499 (method-arg) AND 10421
      (generic-require) are REACHED (reachability-instrumented); (B) is
      adversarially load-bearing (revert 9400 HBorrow-subject → compiles but pin
      FAILS on Void). (3) Updated the stale ConstShare comment at ~13143 (call
      args typed as values w/ defer_value_use, not used_as_value=False).
      GATES: 19 focused pins green (boundary+or_throw+6 cfg_result_type+autoborrow);
      e2e or_throw fixture ok. Full suite: USER-run on frozen tree.
- [ ] P5 restore E_REDUNDANT_ARG_BORROW + ownership matrix + docs.

## Worktree files touched so far
lang/versions.py (0.34.1), doc/history.md (0.34.1 ternary entry),
lang/driftc/stage2/hir_to_mir.py (ternary MoveOut; P1 lift in progress),
lang/driftc/type_checker.py (used_as_value/defer_value_use split),
lang/driftc/checker/call_resolver.py (arg sites),
e2e: ternary_owned_result_bound_drops_once, ternary_owned_result_bind_then_borrow.

## Key files / lines
- _validate_lifted_chain: stage2/hir_to_mir.py ~3454-3564 (base gate ~3501).
- _visit_expr_HTernary join: ~8458-8530 (MoveOut done).
- match joins: variant ~2314, Bool ~2469, scalar ~2618 (LoadLocal -> helper).
- HTryExpr __try_expr_tmp join: audit.
- cfv machinery: type_checker _cfv_rvalue_borrow_hazard/_cfv_source_borrow_hazard,
  _reject_cfv_rvalue_borrow; hir_nodes materialized_rvalue_cfv; typed_validator
  cfv_rvalue_binding; provisional_dmir_v0 strip list.
- Deferred: inline `match` as direct arg types [Void] resolved via the split;
  match-as-arg move/borrow needs P2+P4.
- SEPARATE (deferred, not memory-safety): inline match-arg was also blocked by
  overload seeing [Void]; fixed. Match arm result type-unify diagnostics
  (E-MATCH-ARM-TYPE) must stay fail-closed.
