# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Tests for stage4 throw_checks helpers.

We deliberately keep these tests tiny: they prove that stage4 can consume
stage3 ThrowSummary data, combine it with a `declared_can_throw` view, and
enforce basic invariants (no ConstructError in non-can-throw functions).
"""

from lang2.driftc.stage3 import ThrowSummary
from lang2.driftc.stage4 import (
	FuncThrowInfo,
	build_func_throw_info,
	enforce_can_throw_invariants,
	enforce_return_shape_for_can_throw,
	enforce_fnresult_returns_for_can_throw,
	run_throw_checks,
)
from lang2.driftc.stage2 import BasicBlock, MirFunc, Return, StoreLocal
from lang2.driftc.stage2 import ConstructResultErr
from lang2.driftc.stage3 import ThrowSummaryBuilder
import pytest
from lang2.driftc.checker import FnInfo, FnSignature


def test_build_func_throw_info_combines_summary_and_decl():
	"""build_func_throw_info should merge summaries with checker-declared throw intent."""
	summaries = {
		"f": ThrowSummary(
			constructs_error=True,
			exception_types={"MyExc"},
			may_fail_sites={("entry", 0)},
			call_sites=set(),
		)
	}
	func_infos = build_func_throw_info(
		summaries,
		declared_can_throw={"f": True},
	)
	assert "f" in func_infos
	info = func_infos["f"]
	assert isinstance(info, FuncThrowInfo)
	assert info.constructs_error is True
	assert info.exception_types == {"MyExc"}
	assert ("entry", 0) in info.may_fail_sites
	assert info.declared_can_throw is True


def test_enforce_can_throw_invariants_is_lenient_without_explicit_nothrow():
	"""
	Non-can-throw functions may still construct/catch errors locally.

	Stage4 invariants are intentionally lenient unless the checker supplies an
	explicit nothrow declaration (to avoid penalizing untyped/shim paths).
	"""
	summaries = {
		"g": ThrowSummary(
			constructs_error=True,
			exception_types=set(),
			may_fail_sites=set(),
			call_sites=set(),
		)
	}
	func_infos = build_func_throw_info(
		summaries,
		declared_can_throw={"g": False},
	)
	# Should not raise: no explicit nothrow metadata was supplied.
	enforce_can_throw_invariants(func_infos)


def test_enforce_can_throw_invariants_raises_for_explicit_nothrow_thrower():
	"""Explicit nothrow + escaping error construction should fail invariants."""
	summaries = {
		"g": ThrowSummary(
			constructs_error=True,
			exception_types=set(),
			may_fail_sites=set(),
			call_sites=set(),
		)
	}
	func_infos = build_func_throw_info(
		summaries,
		declared_can_throw={"g": False},
		fn_infos={"g": FnInfo(name="g", declared_can_throw=False, signature=FnSignature(name="g", return_type="Int", declared_can_throw=False))},
	)
	with pytest.raises(RuntimeError):
		enforce_can_throw_invariants(func_infos)


def test_enforce_can_throw_invariants_allows_declared_thrower():
	"""Declared can-throw functions should pass invariant checks."""
	summaries = {
		"h": ThrowSummary(
			constructs_error=True,
			exception_types=set(),
			may_fail_sites=set(),
			call_sites=set(),
		)
	}
	func_infos = build_func_throw_info(
		summaries,
		declared_can_throw={"h": True},
	)
	# Should not raise
	enforce_can_throw_invariants(func_infos)


def test_return_shape_enforced_for_can_throw():
	"""Can-throw function with bare return should fail; value-bearing return should pass."""
	summaries = {
		"h": ThrowSummary(
			constructs_error=False,
			exception_types=set(),
			may_fail_sites=set(),
			call_sites=set(),
		)
	}
	func_infos = build_func_throw_info(
		summaries,
		declared_can_throw={"h": True},
	)
	# MIR with a bare return
	entry = BasicBlock(name="entry", instructions=[], terminator=Return(value=None))
	funcs = {"h": MirFunc(name="h", params=[], locals=[], blocks={"entry": entry}, entry="entry")}
	try:
		enforce_return_shape_for_can_throw(func_infos, funcs)
		raised = False
	except RuntimeError:
		raised = True
	assert raised, "expected bare return in can-throw function to violate invariant"

	# Now give h a value-bearing return; should pass.
	entry_ok = BasicBlock(name="entry", instructions=[], terminator=Return(value="v0"))
	funcs_ok = {"h": MirFunc(name="h", params=[], locals=[], blocks={"entry": entry_ok}, entry="entry")}
	enforce_return_shape_for_can_throw(func_infos, funcs_ok)


def test_fnresult_return_shape_enforced_for_can_throw():
	"""FnResult invariant should reject returns not produced by ConstructResultOk/Err."""
	summaries = {
		"k": ThrowSummary(
			constructs_error=False,
			exception_types=set(),
			may_fail_sites=set(),
			call_sites=set(),
		)
	}
	func_infos = build_func_throw_info(
		summaries,
		declared_can_throw={"k": True},
	)
	entry = BasicBlock(name="entry", instructions=[], terminator=Return(value="v0"))
	funcs = {"k": MirFunc(name="k", params=[], locals=[], blocks={"entry": entry}, entry="entry")}
	try:
		enforce_fnresult_returns_for_can_throw(func_infos, funcs)
		raised = False
	except RuntimeError:
		raised = True
	assert raised, "expected FnResult return shape invariant to fail"

	# Now ensure ConstructResultErr defines the returned value; should pass.
	entry_ok = BasicBlock(
		name="entry",
		instructions=[
			ConstructResultErr(dest="r0", error="e0"),
		],
		terminator=Return(value="r0"),
	)
	funcs_ok = {"k": MirFunc(name="k", params=[], locals=[], blocks={"entry": entry_ok}, entry="entry")}
	enforce_fnresult_returns_for_can_throw(func_infos, funcs_ok)


def test_run_throw_checks_wrapper_executes_all_invariants():
	"""run_throw_checks should wire summaries + declared intent through all invariants."""
	entry = BasicBlock(
		name="entry",
		instructions=[ConstructResultErr(dest="r0", error="e0")],
		terminator=Return(value="r0"),
	)
	funcs = {"w": MirFunc(name="w", params=[], locals=[], blocks={"entry": entry}, entry="entry")}
	summary = ThrowSummary(
		constructs_error=True,
		exception_types={"E"},
		may_fail_sites=set(),
		call_sites=set(),
	)
	func_infos = run_throw_checks(
		funcs=funcs,
		summaries={"w": summary},
		declared_can_throw={"w": True},
	)
	assert "w" in func_infos
	assert func_infos["w"].declared_can_throw is True


def test_structural_fnresult_check_flags_forwarding_cases():
	"""
	The structural FnResult check is intentionally strict: it only accepts
	return values defined by ConstructResultOk/Err. Forwarding a FnResult param
	or returning a local that aliases a FnResult will fail until a type-aware
	check replaces this.
	"""
	func_infos = {
		"fwd_param": FuncThrowInfo(
			constructs_error=False,
			exception_types=set(),
			may_fail_sites=set(),
			declared_can_throw=True,  # local fixture; ok to keep literal here
		),
		"alias_local": FuncThrowInfo(
			constructs_error=False,
			exception_types=set(),
			may_fail_sites=set(),
			declared_can_throw=True,  # local fixture; ok to keep literal here
		),
	}

	# Case 1: returning a FnResult parameter directly
	entry_param = BasicBlock(name="entry", instructions=[], terminator=Return(value="param_res"))
	funcs_param = {
		"fwd_param": MirFunc(name="fwd_param", params=[], locals=[], blocks={"entry": entry_param}, entry="entry")
	}
	with pytest.raises(RuntimeError):
		enforce_fnresult_returns_for_can_throw(func_infos, funcs_param)

	# Case 2: returning a local that aliases a FnResult (via StoreLocal)
	entry_alias = BasicBlock(
		name="entry",
		instructions=[
			ConstructResultErr(dest="r0", error="e0"),
			# Alias r0 through a local; structural check still fails because it only
			# recognizes ConstructResultOk/Err destinations, not aliases.
			StoreLocal(local="x", value="r0"),
		],
		terminator=Return(value="x"),
	)
	funcs_alias = {
		"alias_local": MirFunc(name="alias_local", params=[], locals=[], blocks={"entry": entry_alias}, entry="entry")
	}
	with pytest.raises(RuntimeError):
		enforce_fnresult_returns_for_can_throw(func_infos, funcs_alias)
