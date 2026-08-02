# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
HIR→MIR ternary lowering tests.
"""

from lang.driftc import stage1 as H
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.core.function_id import FunctionId


def test_ternary_lowering_builds_diamond_cfg():
	# x = cond ? a : b
	hir = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HTernary(cond=H.HVar("c"), then_expr=H.HVar("a"), else_expr=H.HVar("b")))
		]
	)
	b = make_builder(FunctionId(module="main", name="f_tern", ordinal=0))
	h2m = HIRToMIR(b)
	h2m.lower_block(normalize_hir(hir))
	func = b.func
	# Expect ternary blocks present.
	block_names = set(func.blocks.keys())
	assert any(name.startswith("tern_then") for name in block_names)
	assert any(name.startswith("tern_else") for name in block_names)
	assert any(name.startswith("tern_join") for name in block_names)
	# Entry should branch to ternary blocks.
	entry_term = func.blocks[func.entry].terminator
	from lang.driftc.stage2 import IfTerminator
	assert isinstance(entry_term, IfTerminator)
	# Join block should YIELD the hidden temp (into x).  The CFG-result
	# extraction MoveOuts a droppable/non-cheap-copy result (transferring
	# ownership to the single consumer) and LoadLocals a cheap-copy one;
	# these untyped synthetic vars are non-cheap-copy, so the join moves.
	join_block_name = [n for n in block_names if n.startswith("tern_join")][0]
	join_instrs = func.blocks[join_block_name].instructions
	from lang.driftc.stage2 import LoadLocal, MoveOut
	assert any(isinstance(ins, (LoadLocal, MoveOut)) for ins in join_instrs)
	# Final block (entry or join) should contain the store to x.
	stores = [ins for blk in func.blocks.values() for ins in blk.instructions if ins.__class__.__name__ == "StoreLocal" and ins.local == "x"]
	assert stores
