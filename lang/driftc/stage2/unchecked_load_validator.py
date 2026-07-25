# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Mechanical provenance validation for `StringByteAt(unchecked=True)`.

An unchecked string byte load skips codegen's observation guard and
bounds check, so its safety rests entirely on the producing MIR shape.
Trusting the boolean alone would let ANY MIR producer (or any mutating
pass that damages the guard after initial construction) reach raw
memory.  This validator runs at the FINAL MIR→codegen boundary — after
every mutating pass — and PROVES, for every unchecked load, the
canonical guarded shape emitted by hir_to_mir's STRING_BYTE_AT
expansion:

	guard block G:
	    ...
	    len       = StringLen(value = S)          # observation guard
	    zero      = ConstInt 0
	    ge_zero   = GE(index = I, zero)
	    lt_len    = LT(index = I, len)
	    in_bounds = AND(ge_zero, lt_len)
	    IfTerminator(cond = in_bounds,
	                 then = OK, else = FAIL)
	FAIL: ... AssertLoc(cond = in_bounds, ...) ; Unreachable
	OK:   dest = StringByteAt(value = S, index = I, unchecked = True)

Checks (each with a dedicated negative tooth in
lang/tests/stage2/test_unchecked_load_validator.py):

  * the load is the FIRST instruction of its block (nothing can
    consume/release/mutate the guarded operands between edge and
    read), and the block is reachable ONLY through the guard's THEN
    edge (single predecessor; reversed branches fail);
  * the guard condition is AND(GE(I, 0-const), LT(I, StringLen(S)))
    over EXACTLY the load's own S and I (wrong string / wrong index
    fail);
  * the StringLen observation runs in the guard block (codegen's
    observation guard is attached to StringLen — a missing one fails);
  * the FAIL edge carries an AssertLoc on the same condition and
    terminates in Unreachable (fail-closed);
  * any post-construction mutation that breaks the shape fails at this
    boundary (the validator runs after ALL MIR passes).

Violations are compiler ICEs: an unchecked load with unproven
provenance must never reach codegen.
"""
from __future__ import annotations

from lang.driftc.stage2 import mir_nodes as M


class UncheckedLoadValidationError(AssertionError):
	"""An unchecked StringByteAt whose guard shape could not be proven."""


def _predecessors(fn: "M.MirFunc") -> dict[str, list[str]]:
	preds: dict[str, list[str]] = {name: [] for name in fn.blocks}
	for name, block in fn.blocks.items():
		term = block.terminator
		if term is None:
			continue
		for succ in term.successors():
			if succ in preds:
				preds[succ].append(name)
	return preds


def _defs_in_block(block: "M.BasicBlock") -> dict[str, "M.MInstr"]:
	out: dict[str, M.MInstr] = {}
	for instr in block.instructions:
		dest = getattr(instr, "dest", None)
		if dest is not None:
			out[dest] = instr
	return out


def validate_unchecked_string_loads(fn: "M.MirFunc") -> None:
	"""Raise UncheckedLoadValidationError unless every
	StringByteAt(unchecked=True) in `fn` sits in the canonical guarded
	shape."""
	preds = _predecessors(fn)
	for block_name, block in fn.blocks.items():
		for instr in block.instructions:
			if not isinstance(instr, M.StringByteAt):
				continue
			if not getattr(instr, "unchecked", False):
				continue
			_validate_one(fn, preds, block_name, instr)


def _fail(fn: "M.MirFunc", why: str) -> None:
	raise UncheckedLoadValidationError(
		f"unchecked StringByteAt in {fn.name}: {why} — provenance "
		f"unproven; unchecked loads may only be produced by the guarded "
		f"STRING_BYTE_AT expansion"
	)


def _validate_one(fn: "M.MirFunc", preds: dict[str, list[str]], block_name: str, load: "M.StringByteAt") -> None:
	# 0. The unchecked load must be the FIRST instruction of its block:
	#    no intervening instruction may consume/release/mutate the
	#    String or the operands between the guard's edge and the read.
	block = fn.blocks[block_name]
	if not block.instructions or block.instructions[0] is not load:
		_fail(fn, "unchecked load is not the FIRST instruction of its block "
		          "(intervening instructions could invalidate the guarded operands)")
	# 1. Single predecessor: the guard block.
	p = preds.get(block_name, [])
	if len(p) != 1:
		_fail(fn, f"load block '{block_name}' has {len(p)} predecessors (need exactly the guard)")
	guard = fn.blocks[p[0]]
	term = guard.terminator
	if not isinstance(term, M.IfTerminator):
		_fail(fn, f"guard block '{guard.name}' does not end in IfTerminator")
	# 2. Reachable only through the THEN (in-bounds) edge.
	if term.then_target != block_name:
		_fail(fn, f"load block '{block_name}' is not the guard's THEN target (reversed or rewired branch)")
	if term.else_target == block_name:
		_fail(fn, "load block is also the ELSE target")
	defs = _defs_in_block(guard)
	# 3. cond = AND(ge_zero, lt_len)
	cond_def = defs.get(term.cond)
	if not (isinstance(cond_def, M.BinaryOpInstr) and cond_def.op is M.BinaryOp.AND):
		_fail(fn, "guard condition is not an AND of the two range compares")
	ge_def = defs.get(cond_def.left)
	lt_def = defs.get(cond_def.right)
	if not (isinstance(ge_def, M.BinaryOpInstr) and ge_def.op is M.BinaryOp.GE):
		_fail(fn, "guard is missing the GE(index, 0) compare")
	if not (isinstance(lt_def, M.BinaryOpInstr) and lt_def.op is M.BinaryOp.LT):
		_fail(fn, "guard is missing the LT(index, len) compare")
	# 4. Same index everywhere.
	if ge_def.left != load.index or lt_def.left != load.index:
		_fail(fn, "guard compares a DIFFERENT index than the unchecked load reads")
	# 5. GE against a literal zero.
	zero_def = defs.get(ge_def.right)
	if not (isinstance(zero_def, M.ConstInt) and zero_def.value == 0):
		_fail(fn, "GE compare is not against a literal 0")
	# 6. LT against StringLen of the SAME string value — this is also
	#    the observation guard (codegen attaches it to StringLen).
	len_def = defs.get(lt_def.right)
	if not isinstance(len_def, M.StringLen):
		_fail(fn, "LT compare is not against a StringLen (missing observation guard)")
	if len_def.value != load.value:
		_fail(fn, "StringLen observes a DIFFERENT String than the unchecked load reads")
	# 7. Fail edge: AssertLoc on the same condition + Unreachable.
	fail_block = fn.blocks.get(term.else_target)
	if fail_block is None:
		_fail(fn, "guard's else target does not exist")
	has_assert = any(
		isinstance(i, M.AssertLoc) and i.cond == term.cond
		for i in fail_block.instructions
	)
	if not has_assert:
		_fail(fn, "fail edge has no AssertLoc on the guard condition (not fail-closed)")
	if not isinstance(fail_block.terminator, M.Unreachable):
		_fail(fn, "fail edge does not terminate in Unreachable")
