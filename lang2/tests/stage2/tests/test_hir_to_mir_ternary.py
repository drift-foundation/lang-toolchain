# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
HIR→MIR ternary lowering tests.
"""

from lang2 import stage1 as H
from lang2.stage2 import MirBuilder, HIRToMIR


def test_ternary_lowering_builds_diamond_cfg():
	# x = cond ? a : b
	hir = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HTernary(cond=H.HVar("c"), then_expr=H.HVar("a"), else_expr=H.HVar("b")))
		]
	)
	b = MirBuilder(name="f_tern")
	h2m = HIRToMIR(b)
	h2m.lower_block(hir)
	func = b.func
	# Expect ternary blocks present.
	block_names = set(func.blocks.keys())
	assert any(name.startswith("tern_then") for name in block_names)
	assert any(name.startswith("tern_else") for name in block_names)
	assert any(name.startswith("tern_join") for name in block_names)
	# Entry should branch to ternary blocks.
	entry_term = func.blocks[func.entry].terminator
	from lang2.stage2 import IfTerminator
	assert isinstance(entry_term, IfTerminator)
	# Join block should load the hidden temp and store into x.
	join_block_name = [n for n in block_names if n.startswith("tern_join")][0]
	join_instrs = func.blocks[join_block_name].instructions
	from lang2.stage2 import LoadLocal
	assert any(isinstance(ins, LoadLocal) for ins in join_instrs)
	# Final block (entry or join) should contain the store to x.
	stores = [ins for blk in func.blocks.values() for ins in blk.instructions if ins.__class__.__name__ == "StoreLocal" and ins.local == "x"]
	assert stores
