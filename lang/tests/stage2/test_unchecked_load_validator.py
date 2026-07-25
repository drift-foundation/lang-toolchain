# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Negative teeth for the unchecked-string-load provenance validator
(lang/driftc/stage2/unchecked_load_validator.py, wired at the FINAL
MIR→codegen boundary in lower_module_to_llvm).

Each tooth hand-builds a MirFunc violating exactly one clause of the
canonical guarded shape and proves the validator rejects it; the
canonical shape itself passes.  A source scan proving one producer is
SUPPLEMENTAL — these teeth are the mechanical proof that a boolean on
a public MIR node cannot smuggle an unguarded raw read past codegen,
including damage introduced AFTER initial construction (the validator
runs post-all-mutations, so the "mutated guard" tooth models exactly
that failure)."""
from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.mir_nodes import function_symbol
from lang.driftc.stage2.unchecked_load_validator import (
	UncheckedLoadValidationError,
	validate_unchecked_string_loads,
)


def _mk_fn(blocks: dict[str, M.BasicBlock], entry: str = "entry") -> M.MirFunc:
	fn_id = FunctionId(module="t", name="probe", ordinal=0)
	return M.MirFunc(
		name=function_symbol(fn_id), params=[], locals=[], fn_id=fn_id,
		blocks=blocks, entry=entry,
	)


def _canonical(*, swap_branch: bool = False, wrong_string: bool = False,
               wrong_index: bool = False, drop_stringlen: bool = False,
               drop_assert: bool = False, mutate_cond: bool = False,
               unguarded: bool = False) -> M.MirFunc:
	"""The canonical guarded shape, with exactly one optional defect."""
	if unguarded:
		blk = M.BasicBlock(name="entry", instructions=[
			M.StringByteAt(dest="%b", value="%s", index="%i", unchecked=True),
		], terminator=M.Return(value=None))
		return _mk_fn({"entry": blk})

	guard_instrs: list[M.MInstr] = []
	if not drop_stringlen:
		guard_instrs.append(M.StringLen(dest="%len", value="%s"))
	else:
		# len arrives from something that is NOT a StringLen observation
		guard_instrs.append(M.ConstInt(dest="%len", value=64))
	guard_instrs += [
		M.ConstInt(dest="%zero", value=0),
		M.BinaryOpInstr(dest="%ge", op=M.BinaryOp.GE, left="%i", right="%zero"),
		M.BinaryOpInstr(dest="%lt", op=M.BinaryOp.LT, left="%i", right="%len"),
		M.BinaryOpInstr(dest="%inb", op=M.BinaryOp.AND, left="%ge", right="%lt"),
	]
	if mutate_cond:
		# a post-construction pass replaced the branch condition with
		# something other than the range AND
		guard_instrs.append(M.ConstInt(dest="%whatever", value=1))
		cond = "%whatever"
	else:
		cond = "%inb"
	then_t, else_t = ("fail", "ok") if swap_branch else ("ok", "fail")
	guard = M.BasicBlock(name="entry", instructions=guard_instrs,
		terminator=M.IfTerminator(cond=cond, then_target=then_t, else_target=else_t))

	fail_instrs: list[M.MInstr] = [
		M.ConstString(dest="%f", value="t"),
		M.ConstInt(dest="%l", value=1),
		M.ConstString(dest="%e", value="probe"),
		M.ConstString(dest="%m", value="oob"),
	]
	if not drop_assert:
		fail_instrs.append(M.AssertLoc(cond=cond, file="%f", line="%l", expr="%e", msg="%m"))
	fail = M.BasicBlock(name="fail", instructions=fail_instrs, terminator=M.Unreachable())

	load = M.StringByteAt(
		dest="%b",
		value="%other_s" if wrong_string else "%s",
		index="%other_i" if wrong_index else "%i",
		unchecked=True,
	)
	ok = M.BasicBlock(name="ok", instructions=[load],
		terminator=M.Return(value=None))
	return _mk_fn({"entry": guard, "ok": ok, "fail": fail})


def test_canonical_shape_passes() -> None:
	validate_unchecked_string_loads(_canonical())


def test_unguarded_load_rejected() -> None:
	with pytest.raises(UncheckedLoadValidationError, match="predecessors|IfTerminator"):
		validate_unchecked_string_loads(_canonical(unguarded=True))


def test_wrong_string_rejected() -> None:
	with pytest.raises(UncheckedLoadValidationError, match="DIFFERENT String"):
		validate_unchecked_string_loads(_canonical(wrong_string=True))


def test_wrong_index_rejected() -> None:
	with pytest.raises(UncheckedLoadValidationError, match="DIFFERENT index"):
		validate_unchecked_string_loads(_canonical(wrong_index=True))


def test_reversed_branch_rejected() -> None:
	with pytest.raises(UncheckedLoadValidationError, match="THEN target"):
		validate_unchecked_string_loads(_canonical(swap_branch=True))


def test_missing_observation_rejected() -> None:
	with pytest.raises(UncheckedLoadValidationError, match="StringLen"):
		validate_unchecked_string_loads(_canonical(drop_stringlen=True))


def test_guard_mutated_after_construction_rejected() -> None:
	with pytest.raises(UncheckedLoadValidationError, match="AND of the two range compares"):
		validate_unchecked_string_loads(_canonical(mutate_cond=True))


def test_fail_edge_without_assert_rejected() -> None:
	with pytest.raises(UncheckedLoadValidationError, match="AssertLoc"):
		validate_unchecked_string_loads(_canonical(drop_assert=True))


def test_release_inserted_before_load_rejected() -> None:
	"""A drop/release slipped in front of the unchecked load (e.g. by a
	buggy mutating pass) must fail: the load is required to be the
	FIRST instruction of its block."""
	fn = _canonical()
	ok = fn.blocks["ok"]
	ok.instructions.insert(0, M.DropValue(value="%s", ty=None))
	with pytest.raises(UncheckedLoadValidationError, match="FIRST instruction"):
		validate_unchecked_string_loads(fn)


def test_checked_loads_are_not_constrained() -> None:
	"""A CHECKED StringByteAt anywhere passes — codegen emits its full
	observation + bounds machinery for it."""
	blk = M.BasicBlock(name="entry", instructions=[
		M.StringByteAt(dest="%b", value="%s", index="%i"),
	], terminator=M.Return(value=None))
	validate_unchecked_string_loads(_mk_fn({"entry": blk}))
