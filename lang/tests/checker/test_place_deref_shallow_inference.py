# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Focused unit tests for explicit-deref typing in the shallow place walker.

These pin the inferred `TypeId` of hand-built `HPlaceExpr` nodes directly via
`Checker._TypingContext.infer`, exercising the restructured projection loop in
`Checker._TypingContext._infer_expr_type`:

  - implicit single-level ref unwrap lives INSIDE the `HPlaceField`/`HPlaceIndex`
    arms (so `&Struct.f` / `&Array[i]` still type), and
  - explicit `HPlaceDeref` peels EXACTLY one reference level with no implicit
    pre-unwrap, so `*p` on `&T` -> `T` and on `&&T` -> `&T` (NOT `T`).

Driving `_TypingContext` directly lets us assert the exact `&T`-vs-`T` outcome
that a compile/run test cannot distinguish. The end-to-end user-visible behavior
(borrow-of-deref-place bound to a local, then reused) is covered separately in
`lang/tests/driver/test_borrowed_local_deref_field_string_copy.py`.
"""

from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.checker import Checker, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeKind, TypeTable


def _make_env():
	"""Build a TypeTable with `struct Node(text: String, children: Array<Node>)`
	plus the reference/array types we project through, and a `_TypingContext`
	whose locals bind the place bases.
	"""
	table = TypeTable()
	string_ty = table.ensure_string()
	node_ty = table.declare_struct(module_id="m", name="Node", field_names=["text", "children"])
	array_node_ty = table.new_array(node_ty)
	table.define_struct_fields(node_ty, field_types=[string_ty, array_node_ty])

	ref_node = table.ensure_ref(node_ty)            # &Node
	ref_mut_node = table.ensure_ref_mut(node_ty)    # &mut Node
	refref_node = table.ensure_ref(ref_node)        # &&Node
	ref_array_node = table.ensure_ref(array_node_ty)  # &Array<Node>

	fn_id = FunctionId(module="m", name="main", ordinal=0)
	sig = FnSignature(
		name="main",
		param_type_ids=[],
		return_type_id=table.ensure_void(),
		declared_can_throw=False,
	)
	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: H.HBlock(statements=[])},
		call_info_by_callsite_id={},
		type_table=table,
	)
	diagnostics: list = []
	ctx = checker._TypingContext(
		checker=checker,
		table=table,
		fn_infos={},
		current_fn=None,
		call_info_by_callsite_id=None,
		locals={
			"p": ref_node,        # p: &Node
			"pm": ref_mut_node,   # pm: &mut Node
			"pp": refref_node,    # pp: &&Node
			"ap": ref_array_node,  # ap: &Array<Node>
		},
		diagnostics=diagnostics,
	)
	return ctx, diagnostics, {
		"string": string_ty,
		"node": node_ty,
		"array_node": array_node_ty,
		"ref_node": ref_node,
		"ref_mut_node": ref_mut_node,
		"refref_node": refref_node,
		"ref_array_node": ref_array_node,
	}


def _place(base: str, *projs) -> H.HPlaceExpr:
	return H.HPlaceExpr(base=H.HVar(base), projections=list(projs))


def test_bare_deref_shared_ref_yields_pointee() -> None:
	# *p where p: &Node  ->  Node
	ctx, _diags, t = _make_env()
	got = ctx.infer(_place("p", H.HPlaceDeref()))
	assert got == t["node"], (got, t["node"])


def test_bare_deref_mut_ref_yields_pointee() -> None:
	# *pm where pm: &mut Node  ->  Node  (REF kind covers &mut)
	ctx, _diags, t = _make_env()
	got = ctx.infer(_place("pm", H.HPlaceDeref()))
	assert got == t["node"], (got, t["node"])


def test_bare_deref_double_ref_peels_exactly_one_level() -> None:
	# *pp where pp: &&Node  ->  &Node  (NOT Node).
	# This is the case the unconditional leading-REF unwrap got wrong
	# (it double-peeled). Pin the exact one-level result.
	ctx, _diags, t = _make_env()
	got = ctx.infer(_place("pp", H.HPlaceDeref()))
	assert got == t["ref_node"], (got, t["ref_node"], t["node"])
	assert got != t["node"], "deref of &&Node must not collapse to Node"


def test_deref_then_field_shared_ref() -> None:
	# (*p).text where p: &Node  ->  String
	ctx, _diags, t = _make_env()
	got = ctx.infer(_place("p", H.HPlaceDeref(), H.HPlaceField(name="text")))
	assert got == t["string"], (got, t["string"])


def test_deref_then_field_double_ref_uses_field_arm_autoderef() -> None:
	# (*pp).text where pp: &&Node  ->  String.
	# Deref peels &&Node -> &Node; the Field arm's implicit single-level
	# auto-deref then peels &Node -> Node to read `text`.
	ctx, _diags, t = _make_env()
	got = ctx.infer(_place("pp", H.HPlaceDeref(), H.HPlaceField(name="text")))
	assert got == t["string"], (got, t["string"])


def test_deref_then_field_then_index() -> None:
	# (*p).children[0] where p: &Node  ->  Node
	ctx, _diags, t = _make_env()
	got = ctx.infer(
		_place("p", H.HPlaceDeref(), H.HPlaceField(name="children"), H.HPlaceIndex(index=H.HLiteralInt(0)))
	)
	assert got == t["node"], (got, t["node"])


def test_index_through_ref_array_still_types() -> None:
	# ap[0] where ap: &Array<Node>  ->  Node.
	# Confirms the Index arm's implicit auto-deref of the ref base survives
	# the restructure (the 0.33.43 behavior, now without the leading unwrap).
	ctx, _diags, t = _make_env()
	got = ctx.infer(_place("ap", H.HPlaceIndex(index=H.HLiteralInt(0))))
	assert got == t["node"], (got, t["node"])


def test_field_through_ref_struct_still_types() -> None:
	# p.text where p: &Node  ->  String (implicit field auto-deref).
	ctx, _diags, t = _make_env()
	got = ctx.infer(_place("p", H.HPlaceField(name="text")))
	assert got == t["string"], (got, t["string"])


def test_place_typing_emits_no_copy_diagnostic_for_non_copy_payload() -> None:
	# Typing a place that yields a non-Copy value (Node, which owns a String
	# field and is non-Copy) must NOT emit any diagnostic — a place projection
	# is a borrow, not a copy. This guards against a false accept/reject: the
	# walker reports the element/field TYPE only and never runs the Copy check
	# that the value-context HIndex branch does.
	ctx, diags, t = _make_env()
	# *p -> Node, (*p).children[0] -> Node, ap[0] -> Node : all non-Copy.
	assert ctx.infer(_place("p", H.HPlaceDeref())) == t["node"]
	assert ctx.infer(
		_place("p", H.HPlaceDeref(), H.HPlaceField(name="children"), H.HPlaceIndex(index=H.HLiteralInt(0)))
	) == t["node"]
	assert ctx.infer(_place("ap", H.HPlaceIndex(index=H.HLiteralInt(0)))) == t["node"]
	assert diags == [], (
		"shallow place typing must not emit diagnostics (esp. Copy) for "
		f"non-Copy payloads; got: {[getattr(d, 'message', d) for d in diags]!r}"
	)
