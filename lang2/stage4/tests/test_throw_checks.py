# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Tests for stage4 throw_checks helpers.

We deliberately keep these tests tiny: they prove that stage4 can consume
stage3 ThrowSummary data, combine it with a `declared_can_throw` view, and
enforce basic invariants (no ConstructError in non-can-throw functions).
"""

from lang2.stage3 import ThrowSummary
from lang2.stage4 import (
	FuncThrowInfo,
	build_func_throw_info,
	enforce_can_throw_invariants,
	enforce_return_shape_for_can_throw,
	enforce_fnresult_returns_for_can_throw,
)
from lang2.stage2 import BasicBlock, MirFunc, Return
from lang2.stage2 import ConstructResultErr


def test_build_func_throw_info_combines_summary_and_decl():
	summaries = {
		"f": ThrowSummary(
			constructs_error=True,
			exception_types={"MyExc"},
			may_fail_sites={("entry", 0)},
		)
	}
	func_infos = build_func_throw_info(summaries, declared_can_throw={"f": True})
	assert "f" in func_infos
	info = func_infos["f"]
	assert isinstance(info, FuncThrowInfo)
	assert info.constructs_error is True
	assert info.exception_types == {"MyExc"}
	assert ("entry", 0) in info.may_fail_sites
	assert info.declared_can_throw is True


def test_enforce_can_throw_invariants_raises_for_non_declared_thrower():
	summaries = {
		"g": ThrowSummary(
			constructs_error=True,
			exception_types=set(),
			may_fail_sites=set(),
		)
	}
	func_infos = build_func_throw_info(summaries, declared_can_throw={"g": False})
	try:
		enforce_can_throw_invariants(func_infos)
		raised = False
	except RuntimeError:
		raised = True
	assert raised, "expected invariant violation for non-can-throw function"


def test_enforce_can_throw_invariants_allows_declared_thrower():
	summaries = {
		"h": ThrowSummary(
			constructs_error=True,
			exception_types=set(),
			may_fail_sites=set(),
		)
	}
	func_infos = build_func_throw_info(summaries, declared_can_throw={"h": True})
	# Should not raise
	enforce_can_throw_invariants(func_infos)


def test_return_shape_enforced_for_can_throw():
	# Function h is declared can-throw but has a bare return -> should fail.
	summaries = {
		"h": ThrowSummary(
			constructs_error=False,
			exception_types=set(),
			may_fail_sites=set(),
		)
	}
	func_infos = build_func_throw_info(summaries, declared_can_throw={"h": True})
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
	# Function k is declared can-throw but returns a value with no ConstructResultOk/Err -> should fail.
	summaries = {
		"k": ThrowSummary(
			constructs_error=False,
			exception_types=set(),
			may_fail_sites=set(),
		)
	}
	func_infos = build_func_throw_info(summaries, declared_can_throw={"k": True})
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
