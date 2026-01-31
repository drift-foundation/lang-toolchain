# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2026-01-30
r"""
Ensure ternary hidden temp locals get a type assigned so SSA can zero-init.
"""

from lang2.driftc import stage1 as H
from lang2.driftc.stage1.normalize import normalize_hir
from lang2.driftc.stage2 import HIRToMIR, make_builder
from lang2.driftc.core.function_id import FunctionId


def test_ternary_temp_has_type():
	hir = H.HBlock(
		statements=[
			H.HLet(
				name="x",
				value=H.HTernary(cond=H.HVar("c"), then_expr=H.HVar("a"), else_expr=H.HVar("b")),
			)
		]
	)
	b = make_builder(FunctionId(module="main", name="f_tern_temp", ordinal=0))
	h2m = HIRToMIR(b)
	h2m.lower_block(normalize_hir(hir))
	temps = [name for name in h2m._local_types.keys() if name.startswith("__tern_tmp")]
	assert temps, "ternary lowering must assign a temp local"
	assert all(h2m._local_types[t] is not None for t in temps)
