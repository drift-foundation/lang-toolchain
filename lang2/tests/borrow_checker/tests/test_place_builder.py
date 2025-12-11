#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09

from lang2 import stage1 as H
from lang2.driftc.borrow_checker import (
	FieldProj,
	IndexProj,
	IndexKind,
	PlaceBase,
	PlaceKind,
	is_lvalue,
	place_from_expr,
)


def test_var_is_lvalue_place():
	expr = H.HVar(name="x")
	lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	assert is_lvalue(expr, base_lookup=lookup)
	place = place_from_expr(expr, base_lookup=lookup)
	assert place is not None
	assert place.base.name == "x"
	assert place.projections == ()


def test_field_chain_builds_projections():
	expr = H.HField(subject=H.HVar(name="foo"), name="bar")
	lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	place = place_from_expr(expr, base_lookup=lookup)
	assert place is not None
	assert place.base.name == "foo"
	assert place.projections == (FieldProj("bar"),)


def test_index_projection_appended():
	expr = H.HIndex(subject=H.HVar(name="arr"), index=H.HLiteralInt(0))
	lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	place = place_from_expr(expr, base_lookup=lookup)
	assert place is not None
	assert place.base.name == "arr"
	assert isinstance(place.projections[0], IndexProj)
	assert place.projections[0].kind is IndexKind.CONST
	assert place.projections[0].value == 0


def test_index_unknown_projects_any():
	expr = H.HIndex(subject=H.HVar(name="arr"), index=H.HVar("i"))
	lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	place = place_from_expr(expr, base_lookup=lookup)
	assert place is not None
	assert place.projections[0].kind is IndexKind.ANY


def test_rvalues_are_not_lvalues():
	lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	assert not is_lvalue(H.HLiteralInt(1), base_lookup=lookup)
	assert not is_lvalue(H.HBinary(op=H.BinaryOp.ADD, left=H.HLiteralInt(1), right=H.HLiteralInt(2)), base_lookup=lookup)


def test_nested_chain_places():
	expr = H.HIndex(
		subject=H.HField(subject=H.HField(subject=H.HVar("foo"), name="bar"), name="baz"),
		index=H.HLiteralInt(3),
	)
	lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	place = place_from_expr(expr, base_lookup=lookup)
	assert place is not None
	assert place.base.name == "foo"
	assert len(place.projections) == 3
	assert place.projections[0] == FieldProj("bar")
	assert place.projections[1] == FieldProj("baz")
	assert isinstance(place.projections[2], IndexProj)


def test_field_on_rvalue_not_lvalue():
	expr = H.HField(subject=H.HBinary(op=H.BinaryOp.ADD, left=H.HLiteralInt(1), right=H.HLiteralInt(2)), name="x")
	lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	assert place_from_expr(expr, base_lookup=lookup) is None
