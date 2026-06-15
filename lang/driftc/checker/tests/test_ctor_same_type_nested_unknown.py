# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""The shared constructor type-equivalence predicate does not let a NESTED
`Unknown` bypass validation.

After the strict variant-constructor boundary landed, the `_ctor_contains_unknown`
tolerance (which skipped any pair carrying `Unknown` anywhere) was removed once
the generic-struct-field producer defect was fixed.  A nested `Unknown` must now
be treated as a real difference: `Pair<Int, Unknown>` is NOT the same type as
`Pair<Int, String>`.
"""
from __future__ import annotations

from lang.driftc.checker.call_resolver import _ctor_canonical_identity, _ctor_same_type
from lang.driftc.core.types_core import TypeTable


def _pair_base(t: TypeTable) -> int:
	return t.declare_struct("m", "Pair", [], type_params=["K", "V"])


def test_nested_unknown_is_not_equivalent_to_concrete() -> None:
	t = TypeTable()
	base = _pair_base(t)
	concrete = t.ensure_struct_instantiated(base, [t.ensure_int(), t.ensure_string()])
	with_unknown = t.ensure_struct_instantiated(base, [t.ensure_int(), t.ensure_unknown()])
	assert not _ctor_same_type(
		t, with_unknown, concrete,
		current_module_name="m", default_package=None, module_packages={},
	), "Pair<Int, Unknown> must NOT be equivalent to Pair<Int, String>"


def test_identical_concrete_instantiations_are_equivalent() -> None:
	t = TypeTable()
	base = _pair_base(t)
	a = t.ensure_struct_instantiated(base, [t.ensure_int(), t.ensure_string()])
	b = t.ensure_struct_instantiated(base, [t.ensure_int(), t.ensure_string()])
	assert _ctor_same_type(
		t, a, b,
		current_module_name="m", default_package=None, module_packages={},
	), "Pair<Int, String> must be equivalent to itself"


def test_canonical_identity_rejects_nothrow_throwing_fn_subtyping() -> None:
	"""`_ctor_canonical_identity` (used for the unsafe `Ptr<T> -> &T` pointee
	check) is STRICTER than `_ctor_same_type`: it does not honour the
	nothrow->throwing-fn subtyping carve-out, because a `Ptr<fn() nothrow>` is
	not the same referent as a `&fn()` (throwing).  `_ctor_same_type` keeps that
	carve-out for ordinary constructor assignment."""
	t = TypeTable()
	void = t.ensure_void()
	fn_nothrow = t.ensure_function([], void, can_throw=False)
	fn_throwing = t.ensure_function([], void, can_throw=True)
	assert fn_nothrow != fn_throwing, "test setup: the two fn types must be distinct"
	ctx = dict(current_module_name="m", default_package=None, module_packages={})
	# Assignment equivalence accepts nothrow -> throwing (subtyping)...
	assert _ctor_same_type(t, fn_nothrow, fn_throwing, **ctx)
	# ...but strict canonical identity does NOT.
	assert not _ctor_canonical_identity(t, fn_nothrow, fn_throwing, **ctx)
	# And a type IS canonically identical to itself.
	assert _ctor_canonical_identity(t, fn_nothrow, fn_nothrow, **ctx)
