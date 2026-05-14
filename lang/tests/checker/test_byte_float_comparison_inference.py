# Regression: chained-`and` over `Byte == Byte` (and `Float == Float`)
# bound to a `val` was inferring `Unknown` in the shallow-checker path
# (`_TypingContext._infer_expr_type`), which then caused the downstream
# `if v3_ok { ... }` to fire `E-AUTO-84b36a12 — if condition must be Bool`
# at the use site rather than at the binding.
#
# Root cause (per K's narrowing): the shallow inference's binary-op
# whitelist handled Bool / Int / Uint / Uint64 / String comparisons but
# not Byte. So a single `bytes[0] == cast<Byte>(65)` already inferred
# `None`; chaining with `and` cascaded the `None`; the unannotated
# `val v3_ok = ...` was recorded as Unknown; the `if` validator
# correctly rejected an Unknown condition but reported it at the use
# site, hiding the upstream miss.
#
# Customer-visible workaround was `val v3_ok: Bool = …` (declared type
# masks the shallow-inference miss). This regression set pins:
#   1. Byte == Byte infers Bool (no diagnostic on `if`).
#   2. Float == Float infers Bool.
#   3. Chained `and` over Byte equalities still infers Bool.
#   4. Array<Byte> == Array<Byte> stays REJECTED (the negative
#      direction — fixing Byte must not accidentally widen the
#      accepted equality surface).
from lang.driftc import stage1 as H
from lang.driftc.checker import Checker, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeKind, TypeTable


def _has_bool_condition_diag(diags) -> bool:
	return any("if condition must be Bool" in d.message for d in diags)


def _run_with_byte_params(block: H.HBlock) -> list:
	"""Run the checker over `block` with `a: Byte, b: Byte, c: Byte` in scope.

	Using Byte-typed parameters avoids the HCast / AST type-expr
	plumbing that's irrelevant to the shallow inference rule under
	test — what matters is that *both sides of the `==` carry the
	Byte TypeId*, which is identically true for `bytes[0]` /
	`cast<Byte>(N)` / a `Byte` param.
	"""
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	table = TypeTable()
	byte_ty = table.ensure_byte()
	sig = FnSignature(
		name="main",
		param_names=["a", "b", "c"],
		param_type_ids=[byte_ty, byte_ty, byte_ty],
		return_type_id=table.ensure_void(),
		declared_can_throw=False,
	)
	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: block},
		call_info_by_callsite_id={},
		type_table=table,
	)
	return checker.check_by_id([fn_id]).diagnostics


def _run_no_params(block: H.HBlock) -> list:
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	table = TypeTable()
	sig = FnSignature(
		name="main",
		return_type_id=table.ensure_void(),
		param_type_ids=[],
		declared_can_throw=False,
	)
	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: block},
		call_info_by_callsite_id={},
		type_table=table,
	)
	return checker.check_by_id([fn_id]).diagnostics


def test_byte_eq_binding_infers_bool_for_if_condition() -> None:
	# Single `Byte == Byte` bound to a `val`, used as an `if` condition.
	# Before the fix this fires `if condition must be Bool` at the `if`
	# because the shallow inference returned None for the `Byte == Byte`
	# expression, the `val ok` was recorded as Unknown, and the `if`
	# validator rejected the Unknown condition.
	block = H.HBlock(
		statements=[
			H.HLet(
				name="ok",
				value=H.HBinary(
					op=H.BinaryOp.EQ,
					left=H.HVar("a"),
					right=H.HVar("b"),
				),
				is_mutable=False,
			),
			H.HIf(
				cond=H.HVar("ok"),
				then_block=H.HBlock(statements=[H.HReturn(value=None)]),
				else_block=None,
			),
		]
	)
	diags = _run_with_byte_params(block)
	assert not _has_bool_condition_diag(diags), (
		"Byte == Byte should infer Bool in the shallow checker; "
		"diagnostics: " + repr([d.message for d in diags])
	)


def test_chained_and_over_byte_eq_infers_bool() -> None:
	# The customer-reported magic-byte shape — chained `and` over
	# Byte equalities. Used to be:
	#   val v3_ok = bytes.len == 3
	#       and bytes[0] == cast<Byte>(65)
	#       and bytes[1] == cast<Byte>(66)
	#       and bytes[2] == cast<Byte>(67);
	# We exercise the operative core: two Byte equalities AND'd
	# together, bound to a val, used as an `if` condition.
	left_cmp = H.HBinary(op=H.BinaryOp.EQ, left=H.HVar("a"), right=H.HVar("b"))
	right_cmp = H.HBinary(op=H.BinaryOp.EQ, left=H.HVar("b"), right=H.HVar("c"))
	block = H.HBlock(
		statements=[
			H.HLet(
				name="ok",
				value=H.HBinary(op=H.BinaryOp.AND, left=left_cmp, right=right_cmp),
				is_mutable=False,
			),
			H.HIf(
				cond=H.HVar("ok"),
				then_block=H.HBlock(statements=[H.HReturn(value=None)]),
				else_block=None,
			),
		]
	)
	diags = _run_with_byte_params(block)
	assert not _has_bool_condition_diag(diags), (
		"`Byte == Byte and Byte == Byte` should infer Bool; "
		"diagnostics: " + repr([d.message for d in diags])
	)


def test_float_eq_binding_infers_bool_for_if_condition() -> None:
	# Float was also missing from the shallow whitelist even though the
	# diagnostic message at HBinary fall-through already claimed it was
	# supported. Pin the fix.
	block = H.HBlock(
		statements=[
			H.HLet(
				name="ok",
				value=H.HBinary(
					op=H.BinaryOp.EQ,
					left=H.HLiteralFloat(1.0),
					right=H.HLiteralFloat(2.0),
				),
				is_mutable=False,
			),
			H.HIf(
				cond=H.HVar("ok"),
				then_block=H.HBlock(statements=[H.HReturn(value=None)]),
				else_block=None,
			),
		]
	)
	diags = _run_no_params(block)
	assert not _has_bool_condition_diag(diags), (
		"Float == Float should infer Bool in the shallow checker; "
		"diagnostics: " + repr([d.message for d in diags])
	)


def test_array_byte_eq_stays_rejected_with_specific_diagnostic() -> None:
	# Negative direction: extending Byte must NOT accidentally accept
	# Array<Byte> == Array<Byte>. The existing rejection diagnostic
	# in `_TypingContext._infer_expr_type` for unsupported equality
	# types must still fire.
	#
	# Asserting the specific message — not just "any diagnostic" —
	# matters because a weaker `len(diags) > 0` check would pass even
	# if the test fixture broke in an unrelated way (e.g. parameter
	# type-resolution diagnostics, scope errors). The whole point of
	# this regression is "the *equality surface* hasn't widened", so
	# we must verify the *equality-rule* diagnostic.
	table = TypeTable()
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	array_byte_ty = table.new_array(table.ensure_byte())
	sig = FnSignature(
		name="main",
		param_names=["a", "b"],
		param_type_ids=[array_byte_ty, array_byte_ty],
		return_type_id=table.ensure_void(),
		declared_can_throw=False,
	)
	block = H.HBlock(
		statements=[
			H.HLet(
				name="ok",
				value=H.HBinary(
					op=H.BinaryOp.EQ,
					left=H.HVar("a"),
					right=H.HVar("b"),
				),
				is_mutable=False,
			),
			H.HIf(
				cond=H.HVar("ok"),
				then_block=H.HBlock(statements=[H.HReturn(value=None)]),
				else_block=None,
			),
		]
	)
	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: block},
		call_info_by_callsite_id={},
		type_table=table,
	)
	diags = checker.check_by_id([fn_id]).diagnostics
	# The exact diagnostic emitted by the unsupported-equality branch
	# at lang/driftc/checker/__init__.py (in _TypingContext._infer_expr_type,
	# after the per-scalar whitelist). If the message text changes,
	# update this test alongside the diagnostic.
	expected_substr = "==/!= are only supported for"
	matching = [d.message for d in diags if expected_substr in d.message]
	assert matching, (
		f"Expected the unsupported-equality diagnostic containing "
		f"{expected_substr!r}; got: {[d.message for d in diags]!r}. "
		"If the diagnostic message was reworded, update this test. "
		"If the diagnostic stopped firing, the Byte fix has widened "
		"the accepted-equality surface — investigate _infer_expr_type."
	)
