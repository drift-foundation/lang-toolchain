# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Backstop validator for the 2026-05-17 Void-callback-lambda bug.

The originating production bug was fixed at two layers in
`driftc.py` (~6625) and `checker/__init__.py` (~4995); its
end-to-end carrier is pinned by
`lang/tests/driver/test_lambda_void_callback_throw_check.py`.
This test pins the *backstop validator* itself, so that any
future synthesis pass that re-introduces
`M.Return(value=<synth_void>)` on a nothrow `-> Void` fn trips
the diagnostic at the MIR boundary instead of bubbling up as an
opaque `KeyError` in `throw_checks`.

If this test ever flakes, check whether
`validate_mir_void_return_shape` in `lang/driftc/mir_validate.py`
still:
  - excludes can-throw Void fns (their `Ok(Void)` carrier
    legitimately fills `term.value`); and
  - is still wired into the `validator_plan` in
    `lang/driftc/driftc.py` under the
    `if shared_type_table is not None:` branch.
"""
from __future__ import annotations

import pytest

from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.mir_validate import validate_mir_void_return_shape
from lang.driftc.stage2 import BasicBlock, ConstVoid, MirFunc, Return


def _fn(name: str = "f") -> FunctionId:
	return FunctionId(module="main", name=name, ordinal=0)


def test_nothrow_void_return_none_is_accepted() -> None:
	table = TypeTable()
	fn_id = _fn()
	mir = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=[],
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=[],
				terminator=Return(value=None),
			)
		},
		entry="entry",
	)
	sig = FnSignature(
		name="f",
		return_type_id=table.ensure_void(),
		declared_can_throw=False,
	)
	validate_mir_void_return_shape({fn_id: mir}, {fn_id: sig}, table)


def test_nothrow_void_return_with_value_is_rejected() -> None:
	"""Pre-fix shape — synthesized Void carrier on a nothrow Void fn.

	This is the exact MIR the Void-callback-lambda lowering at
	`driftc.py` (~6625) used to produce before the 2026-05-17 fix.
	"""
	table = TypeTable()
	fn_id = _fn()
	mir = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=[],
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=[ConstVoid(dest="t0")],
				terminator=Return(value="t0"),
			)
		},
		entry="entry",
	)
	sig = FnSignature(
		name="f",
		return_type_id=table.ensure_void(),
		declared_can_throw=False,
	)
	with pytest.raises(AssertionError, match="nothrow Void fn"):
		validate_mir_void_return_shape({fn_id: mir}, {fn_id: sig}, table)


def test_canthrow_void_return_with_ok_carrier_is_accepted() -> None:
	"""Can-throw Void fns legitimately return an `Ok(Void)` carrier
	built upstream by `M.ConstructResultOk`; the validator must
	NOT fire on them, otherwise every can-throw Void function in
	the codebase would trip.
	"""
	table = TypeTable()
	fn_id = _fn()
	mir = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=[],
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=[ConstVoid(dest="ok")],
				terminator=Return(value="ok"),
			)
		},
		entry="entry",
	)
	sig = FnSignature(
		name="f",
		return_type_id=table.ensure_void(),
		declared_can_throw=True,
	)
	validate_mir_void_return_shape({fn_id: mir}, {fn_id: sig}, table)


def test_nonvoid_return_with_value_is_accepted() -> None:
	"""Non-Void return types are entirely out of scope — the
	validator's filter must skip them before inspecting terminator
	values.  Sanity check that the gate works.
	"""
	table = TypeTable()
	fn_id = _fn()
	mir = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=[],
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=[],
				terminator=Return(value="r0"),
			)
		},
		entry="entry",
	)
	sig = FnSignature(
		name="f",
		return_type_id=table.ensure_int(),
		declared_can_throw=False,
	)
	validate_mir_void_return_shape({fn_id: mir}, {fn_id: sig}, table)


def test_missing_signature_is_skipped() -> None:
	"""Synthesized helper fns may not always have a signature
	registered; the validator must not blow up on them.  Skipping
	is safe because the original bug class only fires when a
	signature exists and declares `-> Void` nothrow."""
	table = TypeTable()
	fn_id = _fn()
	mir = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=[],
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=[ConstVoid(dest="t0")],
				terminator=Return(value="t0"),
			)
		},
		entry="entry",
	)
	validate_mir_void_return_shape({fn_id: mir}, {}, table)
