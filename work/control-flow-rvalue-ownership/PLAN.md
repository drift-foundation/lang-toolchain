# control-flow-rvalue-ownership — PLAN

Status: IN PROGRESS. Classification: LANGUAGE_BUG cluster, RELEASE-BLOCKING
memory-safety (teardown double-free, exit 134). Target release: 0.34.1
(ABI 22 unchanged). 0.34.0 is WITHDRAWN (dead).

## Problem

Owned rvalues produced by value-control-flow / block expressions
(ternary, match, try-expr, unsafe-block) are mis-owned:
- borrowing/projecting them, or binding them, double-frees the backing;
- inline `match` in argument/receiver position types as `[Void]`.

Root causes (reviewer-confirmed):
1. `type_expr` conflated `used_as_value` (semantic value context) with
   value-USE accounting (Copy check). A call arg is a value even when its
   move/borrow accounting is deferred. FIXED: split into `used_as_value`
   + `defer_value_use` (only `_require_copy_value` honors the latter).
2. CFG-result joins return the hidden result local with a shallow
   `LoadLocal`, leaving ownership ambiguous. Ternary FIXED (MoveOut);
   match×3 + try-expr still on LoadLocal → `cannot copy __match_bool_tmp`.
3. `_validate_lifted_chain` (projection lift) accepted only
   HCall/HMethodCall/HInvoke bases → unsafe-block / try-expr / VCF
   projections fell to the leaf-alias fallback and double-freed.
4. The `cfv_rvalue_binding` REJECTION machinery is now stale: with MoveOut
   + single-owner materialization, sound VCF borrows should be ACCEPTED,
   not rejected.

## Confirmed RED repros (exit 134 unless noted)

- `var x = cond ? a : b; return 0;` (ternary bind) — FIXED (0.34.1, MoveOut).
- `peek((unsafe { PR(...) }).root)` — unsafe-block projection.
- `peek((try make(false) catch {...}).root)` — try-expr projection.
- `consume(match b {...})` owned result — `cannot copy __match_bool_tmp`.
- inline `match` as by-value/`&T` arg — was `[Void]` (typing FIXED; move
  accounting + borrow acceptance remain).

## Plan (phases = tasks #1-#6)

- P1 Generalize `_validate_lifted_chain`: safe OWNED-rvalue base (not a
  place, not HMove) materialized once + address-project; ref-returning
  bases keep call-only; `&mut` stays bind-first. Fixes unsafe/try proj.
- P2 Centralize CFG-result extraction: MoveOut when
  `not should_copy OR needs_runtime_drop OR is_destructible`, else
  LoadLocal; stamp dest type. Apply to ternary, match×3
  (~hir_to_mir 2314/2469/2618), try-expr (`__try_expr_tmp`). Match/try
  locals are forwarding slots — MoveOut records transfer, do NOT blindly
  drop-register them.
- P3 `_type_user_arg(arg, expected_type)` = type_expr(used_as_value=True,
  defer_value_use=True); route EVERY source positional/keyword arg path
  (finish audit: call_resolver ~5048/5247/5662/5859/5964/5997/6024/6569/
  6615/6681/6773; type_checker ~10609). Probe/update semantic receiver
  typings; teach `_ultimate_base_is_rvalue_call` about match/ternary/try.
- P4 Accept sound whole-borrow of fresh rvalue (materialize→borrow→drop
  once); DELETE cfv machinery (materialized_rvalue_cfv, cfv_rvalue_binding,
  _cfv_rvalue_borrow_hazard, _cfv_source_borrow_hazard, W0 case, DMIR strip
  entry, rejection fixtures + code pins). KEEP materialized_rvalue
  (mut-rvalue bind-first). Generic/typevar fail-closed.
- P5 Restore E_REDUNDANT_ARG_BORROW where bare form now accepted; &mut
  bind-first; ownership matrix (String-authoring trigger FIRES) base+ASan+
  memcheck; rewrite doc/history.md; purge stale comments/fixtures.

## Semantic contract (0.34.1)

Match/ternary/try are values in every value context (args, receivers).
A CFG result is a fresh owned rvalue: by-value consumes without source
move; no internal temp name in diagnostics. Whole shared borrow of a
fresh result accepted (one owner). Field/index projection from a safe
owned rvalue accepted (materialize root once + address-project). `&mut`
of an owned rvalue stays bind-first. move-x-forwarding guard preserved.

## Refactor trigger

`refactor_triggers.md`: HIR-walker consolidation does NOT fire; String
ownership-authoring conformance matrix DOES fire → deliverable is the
bounded ownership matrix (NOT a String/Arc rewrite). Unification stays
completed; this is a coverage/ownership-transfer defect in it.
