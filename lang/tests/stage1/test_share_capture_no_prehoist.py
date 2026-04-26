# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: `captures(share x)` must NOT pre-hoist a let-statement
into the enclosing block.

The Share desugar evaluates `Share::share(&x)` INLINE at the lambda's
env-construction site (the `share_value` HExpr on `HExplicitCapture`).
Pre-hoisting would put the `Share::share(&x)` call BEFORE the entire
enclosing statement, breaking user-visible left-to-right evaluation
order in shapes like `foo(side_effect(), || captures(share x) => ...)`.

This test pins the AST→HIR-level invariant: after lowering a function
whose body has a single statement containing a share-capturing
lambda, the lowered block contains EXACTLY that single statement
(no synthesized `__share_tmp` HLet hoisted ahead of it), and the
lambda's explicit_capture carries a non-null `share_value` HExpr.
"""
from __future__ import annotations

from lang.driftc.parser.parser import parse_program
from lang.driftc.stage1.ast_to_hir import AstToHIR
from lang.driftc.stage1 import hir_nodes as H


def _find_lambdas(node: object):
	if isinstance(node, H.HLambda):
		yield node
		if node.body_block is not None:
			for s in node.body_block.statements:
				yield from _find_lambdas(s)
	elif hasattr(node, "__dataclass_fields__"):
		for fld in node.__dataclass_fields__:
			val = getattr(node, fld, None)
			if isinstance(val, list):
				for it in val:
					yield from _find_lambdas(it)
			else:
				yield from _find_lambdas(val)


def test_share_capture_does_not_inject_pre_hoist_let() -> None:
	source = """
module main;

import std.concurrent as conc;

struct App { v: Int }

fn main() nothrow -> Int {
	val app = conc.arc(App(v = 1));
	val r = (| | captures(share app) => { return 0; })();
	return r;
}
"""
	prog = parse_program(source)
	lowerer = AstToHIR()
	main_fn = next(fn for fn in prog.functions if fn.name == "main")
	hir_block = lowerer.lower_function_block(
		main_fn.body.statements,
		param_names=[p.name for p in main_fn.params],
	)
	# Two original statements (`val app = ...`, `val r = ...`) plus
	# the trailing `return r;` — three total.  No pre-hoisted
	# `__share_tmp` let was injected.
	stmt_kinds = [type(s).__name__ for s in hir_block.statements]
	assert stmt_kinds == ["HLet", "HLet", "HReturn"], (
		f"share-capture must NOT inject pre-hoisted statements; "
		f"got {stmt_kinds}"
	)
	# Confirm the let-bindings are the user's (`app`, `r`) — not a
	# synthesized share-tmp.
	let_names = [s.name for s in hir_block.statements if isinstance(s, H.HLet)]
	assert let_names == ["app", "r"], let_names
	assert not any(
		"share_tmp" in name for name in let_names
	), f"no synthesized share_tmp let should appear: {let_names}"


def test_share_capture_carries_share_value_hexpr() -> None:
	source = """
module main;

import std.concurrent as conc;

struct App { v: Int }

fn main() nothrow -> Int {
	val app = conc.arc(App(v = 1));
	val r = (| | captures(share app) => { return 0; })();
	return r;
}
"""
	prog = parse_program(source)
	lowerer = AstToHIR()
	main_fn = next(fn for fn in prog.functions if fn.name == "main")
	hir_block = lowerer.lower_function_block(
		main_fn.body.statements,
		param_names=[p.name for p in main_fn.params],
	)
	lambdas = [lam for lam in _find_lambdas(hir_block)]
	share_caps = [
		c
		for lam in lambdas
		for c in (lam.explicit_captures or [])
		if c.kind == "share"
	]
	assert len(share_caps) == 1, share_caps
	cap = share_caps[0]
	# Capture binding_id points at the user's original local (NOT a
	# synthesized tmp).  Capture name is the user-spelled name.
	assert cap.name == "app"
	assert cap.binding_id is not None
	# `share_value` is a synthesized HCall through the trait machinery.
	assert cap.share_value is not None
	assert isinstance(cap.share_value, H.HCall)
	assert isinstance(cap.share_value.fn, H.HQualifiedMember)
	assert cap.share_value.fn.member == "share"
	# The share call's origin marker so call_resolver / future passes
	# can identify it as compiler-synthesized.
	assert cap.share_value.origin == "share_capture"
