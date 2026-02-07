# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2026-01-30
r"""
Ensure try-expression hidden temp locals get a type assigned so SSA can zero-init.
"""

from lang.driftc import stage1 as H
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.core.function_id import FunctionId


def test_try_expr_temp_has_type():
	hir = H.HBlock(
		statements=[
			H.HLet(
				name="x",
				value=H.HTryExpr(
					attempt=H.HVar("a"),
					arms=[H.HTryExprArm(event_fqn=None, binder=None, block=H.HBlock(statements=[]), result=H.HVar("b"))],
				),
			)
		]
	)
	b = make_builder(FunctionId(module="main", name="f_try_expr_temp", ordinal=0))
	h2m = HIRToMIR(b)
	h2m.lower_block(normalize_hir(hir))
	temps = [name for name in h2m._local_types.keys() if name.startswith("__try_expr_tmp")]
	assert temps, "try expr lowering must assign a temp local"
	assert all(h2m._local_types[t] is not None for t in temps)
