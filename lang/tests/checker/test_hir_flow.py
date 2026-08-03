# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Unit pins for the shared HIR flow/effect authorities (stage1.hir_flow).

These pin the contracts the driver level cannot isolate:

* the nested-lambda BOUNDARY: constructing / storing a throwing lambda does
  not make the enclosing body throwing; invoking it (IIFE) does.  (Driver
  compile/run companions live in test_uninvoked_stored_lambda.py.)
* wrapper descent: throws below map-literal entries and f-string holes are
  seen (the `.args`-only walker class of omission);
* unknown-variant conservatism: a node class this module does not know is
  treated as can-throw, never silently nothrow;
* `block_exits` kind classification: bare return vs valued return vs throw,
  if/else union, non-breaking loops, try/catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lang.driftc import stage1 as H
from lang.driftc.stage1 import hir_flow
from lang.driftc.stage1.hir_flow import Exit


def _no_calls_throw(_expr) -> bool:
	return False


def _all_calls_throw(_expr) -> bool:
	return True


def _lam(body_stmts=None, body_expr=None) -> H.HLambda:
	return H.HLambda(params=[], body_expr=body_expr, body_block=H.HBlock(statements=body_stmts) if body_stmts is not None else None)


def _throw() -> H.HThrow:
	return H.HThrow(value=H.HExceptionInit(event_fqn="E", pos_args=[], kw_args=[]))


def test_uninvoked_nested_lambda_is_a_boundary():
	# val t = || => { throw ...; };  0   — constructing does not throw.
	inner = _lam(body_stmts=[_throw()])
	outer = _lam(body_stmts=[H.HLet(name="t", value=inner), H.HExprStmt(expr=H.HLiteralInt(value=0))])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_no_calls_throw) is False


def test_iife_of_throwing_lambda_escapes():
	# (|| => { throw ...; })();  — invoking executes the body.
	inner = _lam(body_stmts=[_throw()])
	outer = _lam(body_stmts=[H.HExprStmt(expr=H.HCall(fn=inner, args=[]))])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_no_calls_throw) is True


def test_invoke_of_throwing_lambda_escapes():
	inner = _lam(body_stmts=[_throw()])
	outer = _lam(body_stmts=[H.HExprStmt(expr=H.HInvoke(callee=inner, args=[]))])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_no_calls_throw) is True


def test_throwing_lambda_as_uninvoked_call_argument_is_a_boundary():
	# f(|| => { throw ...; }) where f itself cannot throw: passing the lambda
	# does not execute it.
	inner = _lam(body_stmts=[_throw()])
	call = H.HCall(fn=H.HVar(name="f"), args=[inner])
	outer = _lam(body_stmts=[H.HExprStmt(expr=call)])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_no_calls_throw) is False


def test_map_literal_entry_throw_is_seen():
	entry = H.HMapEntry(key=H.HLiteralString(value="k"), value=H.HCall(fn=H.HVar(name="might"), args=[]))
	outer = _lam(body_stmts=[H.HExprStmt(expr=H.HMapLiteral(entries=[entry]))])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_all_calls_throw) is True
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_no_calls_throw) is False


def test_fstring_hole_throw_is_seen():
	hole = H.HFStringHole(expr=H.HCall(fn=H.HVar(name="might"), args=[]))
	outer = _lam(body_stmts=[H.HExprStmt(expr=H.HFString(parts=["x"], holes=[hole]))])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_all_calls_throw) is True


def test_call_kwarg_throw_is_seen():
	kw = H.HKwArg(name="x", value=H.HCall(fn=H.HVar(name="might"), args=[]))
	call = H.HCall(fn=H.HVar(name="Pt"), args=[], kwargs=[kw])
	outer = _lam(body_stmts=[H.HExprStmt(expr=call)])
	# The ctor call itself is nothrow; the kwarg VALUE is the throwing call.
	def ctor_nothrow(expr):
		return expr is not call
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=ctor_nothrow) is True


def test_unknown_node_variant_is_conservatively_throwing():
	@dataclass
	class HSomethingNew(H.HExpr):
		payload: int = 0
	outer = _lam(body_stmts=[H.HExprStmt(expr=HSomethingNew())])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_no_calls_throw) is True


def test_catch_all_try_swallows_attempt():
	arm = H.HTryExprArm(event_fqn=None, binder=None, block=H.HBlock(statements=[]), result=H.HLiteralInt(value=0))
	te = H.HTryExpr(attempt=H.HCall(fn=H.HVar(name="might"), args=[]), arms=[arm])
	outer = _lam(body_stmts=[H.HExprStmt(expr=te)])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_all_calls_throw) is False


def test_block_exits_kinds():
	b = H.HBlock

	def exits(stmts):
		return hir_flow.block_exits(b(statements=stmts))

	assert exits([]) == frozenset({Exit.FALLTHROUGH})
	assert exits([H.HReturn(value=None)]) == frozenset({Exit.RETURNS_BARE})
	assert exits([H.HReturn(value=H.HLiteralInt(value=1))]) == frozenset({Exit.RETURNS_VALUE})
	assert exits([_throw()]) == frozenset({Exit.THROWS})
	# if without else: fallthrough survives.
	half = H.HIf(cond=H.HVar(name="c"), then_block=b(statements=[_throw()]), else_block=None)
	assert Exit.FALLTHROUGH in exits([half])
	# if/else both throwing: throw-only.
	full = H.HIf(cond=H.HVar(name="c"), then_block=b(statements=[_throw()]), else_block=b(statements=[_throw()]))
	assert exits([full]) == frozenset({Exit.THROWS})
	# literal-true fold: dead else-break ignored (the `while true` desugar).
	folded = H.HIf(cond=H.HLiteralBool(value=True), then_block=b(statements=[_throw()]), else_block=b(statements=[H.HBreak()]))
	assert exits([folded]) == frozenset({Exit.THROWS})
	# non-breaking loop with a conditional throw: only the throw exits.
	loop = H.HLoop(body=b(statements=[half]))
	assert exits([loop]) == frozenset({Exit.THROWS})
	# loop with a reachable break: falls through.
	breaking = H.HLoop(body=b(statements=[H.HBreak()]))
	assert Exit.FALLTHROUGH in exits([breaking])
	# try whose body and catch-all arm both throw: throw-only.
	tr = H.HTry(body=b(statements=[_throw()]), catches=[H.HCatchArm(event_fqn=None, binder=None, block=b(statements=[_throw()]))])
	assert exits([tr]) == frozenset({Exit.THROWS})
	# mixed: one branch returns a value, other bare-returns.
	mixed = H.HIf(cond=H.HVar(name="c"), then_block=b(statements=[H.HReturn(value=H.HLiteralInt(value=1))]), else_block=b(statements=[H.HReturn(value=None)]))
	assert exits([mixed]) == frozenset({Exit.RETURNS_VALUE, Exit.RETURNS_BARE})


def test_break_continue_are_not_fallthrough():
	b = H.HBlock

	def exits(stmts):
		return hir_flow.block_exits(b(statements=stmts))

	# break/continue leave THIS block without exiting the function: empty set.
	assert exits([H.HBreak()]) == frozenset()
	assert exits([H.HContinue()]) == frozenset()
	# Statements after continue are unreachable: the dead `break` must not
	# make the loop fall through, and the dead bare `return;` must not enter
	# the exit set (both produced false rejections of permanently-divergent
	# non-Void lambdas when break/continue counted as FALLTHROUGH).
	dead_break_body = b(statements=[H.HContinue(), H.HBreak()])
	assert hir_flow.block_contains_reachable_break(dead_break_body) is False
	assert hir_flow.block_exits(b(statements=[H.HLoop(body=dead_break_body)])) == frozenset()
	dead_return_body = b(statements=[H.HContinue(), H.HReturn(value=None)])
	assert hir_flow.block_exits(b(statements=[H.HLoop(body=dead_return_body)])) == frozenset()
	# A genuinely reachable break still gives the loop its FALLTHROUGH exit.
	live_break_body = b(statements=[H.HIf(cond=H.HVar(name="c"), then_block=b(statements=[H.HBreak()]), else_block=None), _throw()])
	assert hir_flow.block_contains_reachable_break(live_break_body) is True
	assert Exit.FALLTHROUGH in hir_flow.block_exits(b(statements=[H.HLoop(body=live_break_body)]))


def test_catch_all_try_swallows_body_throw_exit():
	b = H.HBlock
	# Caught body throw with a falling-through catch-all arm: the construct
	# falls through; the handled THROWS must not leak into the exit set.
	tr = H.HTry(body=b(statements=[_throw()]), catches=[H.HCatchArm(event_fqn=None, binder=None, block=b(statements=[]))])
	assert hir_flow.block_exits(b(statements=[tr, H.HReturn(value=H.HLiteralInt(value=1))])) == frozenset({Exit.RETURNS_VALUE})
	# A throw ORIGINATING in the catch arm still escapes.
	tr2 = H.HTry(body=b(statements=[_throw()]), catches=[H.HCatchArm(event_fqn=None, binder=None, block=b(statements=[_throw()]))])
	assert hir_flow.block_exits(b(statements=[tr2])) == frozenset({Exit.THROWS})
	# Without a catch-all, an unmatched body throw escapes.
	tr3 = H.HTry(body=b(statements=[_throw()]), catches=[H.HCatchArm(event_fqn="m::Other", binder=None, block=b(statements=[]))])
	ex3 = hir_flow.block_exits(b(statements=[tr3]))
	assert Exit.THROWS in ex3 and Exit.FALLTHROUGH in ex3


def test_effect_walk_respects_cfg_reachability():
	b = H.HBlock
	# Literal-false arm: the throw cannot execute — effectless.
	dead_if = H.HIf(cond=H.HLiteralBool(value=False), then_block=b(statements=[_throw()]), else_block=None)
	outer = _lam(body_stmts=[dead_if, H.HExprStmt(expr=H.HLiteralInt(value=0))])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_no_calls_throw) is False
	# Literal-true selected arm still throws.
	live_if = H.HIf(cond=H.HLiteralBool(value=True), then_block=b(statements=[_throw()]), else_block=b(statements=[]))
	outer2 = _lam(body_stmts=[live_if])
	assert hir_flow.lambda_body_can_throw(outer2, call_can_throw=_no_calls_throw) is True
	# A throw after `return`/`continue` is dead and contributes no effect —
	# and it must not make a try's catch arms reachable either.
	outer3 = _lam(body_stmts=[H.HReturn(value=H.HLiteralInt(value=1)), _throw()])
	assert hir_flow.lambda_body_can_throw(outer3, call_can_throw=_no_calls_throw) is False
	tr = H.HTry(body=b(statements=[H.HContinue(), _throw()]), catches=[H.HCatchArm(event_fqn=None, binder=None, block=b(statements=[H.HBreak()]))])
	loop_body = b(statements=[tr])
	assert hir_flow.block_contains_reachable_break(loop_body, call_can_throw=_no_calls_throw) is False
	assert hir_flow.block_exits(b(statements=[H.HLoop(body=loop_body)]), call_can_throw=_no_calls_throw) == frozenset()
	# A REACHABLE attempt throw still activates the handler.
	tr2 = H.HTry(body=b(statements=[_throw()]), catches=[H.HCatchArm(event_fqn=None, binder=None, block=b(statements=[_throw()]))])
	outer4 = _lam(body_stmts=[tr2, H.HExprStmt(expr=H.HLiteralInt(value=0))])
	assert hir_flow.lambda_body_can_throw(outer4, call_can_throw=_no_calls_throw) is True


def test_unreachable_catch_arms_are_dead_with_effect_predicate():
	b = H.HBlock
	# `try { continue; } catch { break; }` inside a loop: the attempt cannot
	# throw, so the catch `break` is dead — the loop must NOT gain a
	# FALLTHROUGH exit (previously this falsely un-terminated the loop).
	tr = H.HTry(body=b(statements=[H.HContinue()]), catches=[H.HCatchArm(event_fqn=None, binder=None, block=b(statements=[H.HBreak()]))])
	loop_body = b(statements=[tr])
	assert hir_flow.block_contains_reachable_break(loop_body, call_can_throw=_no_calls_throw) is False
	assert hir_flow.block_exits(b(statements=[H.HLoop(body=loop_body)]), call_can_throw=_no_calls_throw) == frozenset()
	# WITHOUT the effect predicate the analysis stays conservative.
	assert hir_flow.block_contains_reachable_break(loop_body) is True
	# Dead catch `return`/`throw` likewise stay out of the exit set.
	tr2 = H.HTry(body=b(statements=[H.HLet(name="a", value=H.HLiteralInt(value=1))]), catches=[H.HCatchArm(event_fqn=None, binder=None, block=b(statements=[H.HReturn(value=None)]))])
	assert hir_flow.block_exits(b(statements=[tr2]), call_can_throw=_no_calls_throw) == frozenset({Exit.FALLTHROUGH})
	# Effect side: an effectless attempt with a THROWING catch arm is not a
	# throwing body (the handler can never run).
	tr3 = H.HTry(body=b(statements=[H.HLet(name="a", value=H.HLiteralInt(value=1))]), catches=[H.HCatchArm(event_fqn=None, binder=None, block=b(statements=[_throw()]))])
	outer = _lam(body_stmts=[tr3, H.HExprStmt(expr=H.HLiteralInt(value=0))])
	assert hir_flow.lambda_body_can_throw(outer, call_can_throw=_no_calls_throw) is False
	# ...but with a genuinely throwing attempt the handler counts.
	tr4 = H.HTry(body=b(statements=[H.HExprStmt(expr=H.HCall(fn=H.HVar(name="might"), args=[]))]), catches=[H.HCatchArm(event_fqn=None, binder=None, block=b(statements=[_throw()]))])
	outer4 = _lam(body_stmts=[tr4, H.HExprStmt(expr=H.HLiteralInt(value=0))])
	assert hir_flow.lambda_body_can_throw(outer4, call_can_throw=_all_calls_throw) is True


def test_lazy_expression_effects_follow_evaluation_reachability():
	def lam_with_expr(e):
		return _lam(body_stmts=[H.HExprStmt(expr=e)])

	throw_call = lambda: H.HCall(fn=H.HVar(name="might"), args=[])
	# Short-circuit AND/OR: a literal LHS that decides the result makes the
	# RHS dead; the live sides still count.
	dead_and = H.HBinary(op=H.BinaryOp.AND, left=H.HLiteralBool(value=False), right=throw_call())
	live_and = H.HBinary(op=H.BinaryOp.AND, left=H.HLiteralBool(value=True), right=throw_call())
	dead_or = H.HBinary(op=H.BinaryOp.OR, left=H.HLiteralBool(value=True), right=throw_call())
	live_or = H.HBinary(op=H.BinaryOp.OR, left=H.HLiteralBool(value=False), right=throw_call())
	assert hir_flow.lambda_body_can_throw(lam_with_expr(dead_and), call_can_throw=_all_calls_throw) is False
	assert hir_flow.lambda_body_can_throw(lam_with_expr(live_and), call_can_throw=_all_calls_throw) is True
	assert hir_flow.lambda_body_can_throw(lam_with_expr(dead_or), call_can_throw=_all_calls_throw) is False
	assert hir_flow.lambda_body_can_throw(lam_with_expr(live_or), call_can_throw=_all_calls_throw) is True
	# A non-literal LHS keeps both sides reachable.
	open_and = H.HBinary(op=H.BinaryOp.AND, left=H.HVar(name="c"), right=throw_call())
	assert hir_flow.lambda_body_can_throw(lam_with_expr(open_and), call_can_throw=_all_calls_throw) is True
	# Ternary: only the selected branch of a literal condition evaluates.
	dead_tern = H.HTernary(cond=H.HLiteralBool(value=True), then_expr=H.HLiteralInt(value=1), else_expr=throw_call())
	live_tern = H.HTernary(cond=H.HLiteralBool(value=False), then_expr=H.HLiteralInt(value=1), else_expr=throw_call())
	open_tern = H.HTernary(cond=H.HVar(name="c"), then_expr=H.HLiteralInt(value=1), else_expr=throw_call())
	assert hir_flow.lambda_body_can_throw(lam_with_expr(dead_tern), call_can_throw=_all_calls_throw) is False
	assert hir_flow.lambda_body_can_throw(lam_with_expr(live_tern), call_can_throw=_all_calls_throw) is True
	assert hir_flow.lambda_body_can_throw(lam_with_expr(open_tern), call_can_throw=_all_calls_throw) is True
	# Assert: the message only evaluates on the failure path.
	dead_msg = _lam(body_stmts=[H.HAssert(cond=H.HLiteralBool(value=True), msg=throw_call())])
	live_msg = _lam(body_stmts=[H.HAssert(cond=H.HLiteralBool(value=False), msg=throw_call())])
	open_msg = _lam(body_stmts=[H.HAssert(cond=H.HVar(name="c"), msg=throw_call())])
	assert hir_flow.lambda_body_can_throw(dead_msg, call_can_throw=_all_calls_throw) is False
	assert hir_flow.lambda_body_can_throw(live_msg, call_can_throw=_all_calls_throw) is True
	assert hir_flow.lambda_body_can_throw(open_msg, call_can_throw=_all_calls_throw) is True


def test_terminal_call_predicate_is_consulted():
	call = H.HCall(fn=H.HVar(name="fail"), args=[])
	stmt = H.HExprStmt(expr=call)
	block = H.HBlock(statements=[stmt])
	assert hir_flow.block_exits(block) == frozenset({Exit.FALLTHROUGH})
	assert hir_flow.block_exits(block, is_terminal_call=lambda e: e is call) == frozenset({Exit.THROWS})
