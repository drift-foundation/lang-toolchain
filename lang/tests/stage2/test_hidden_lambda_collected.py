# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

import lang.driftc.stage2.mir_nodes as M
from lang.driftc.core.function_id import function_symbol
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.parser import parse_drift_to_hir


def test_hidden_lambda_is_collected_into_mir_funcs(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
pub fn main() -> Int {
    val a: Int = 10;
    val r: Int = (|x| => { return a + x; })(2);
    return r;
}
"""
	)
	module, type_table, exc_env, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []

	mir_funcs = compile_stubbed_funcs(
		module.func_hirs,
		signatures=module.signatures_by_id,
		type_table=type_table,
		exc_env=exc_env,
	)
	hidden = [
		fn_id
		for fn_id in mir_funcs.keys()
		if function_symbol(fn_id).split("::")[-1].startswith("__lambda_")
	]
	assert len(hidden) == 1
	hidden_id = hidden[0]

	hidden_func = mir_funcs[hidden_id]
	hidden_instrs = [i for b in hidden_func.blocks.values() for i in b.instructions]
	assert any(isinstance(i, M.StructGetField) for i in hidden_instrs)

	main_id = next(fn_id for fn_id in mir_funcs.keys() if function_symbol(fn_id) == "main")
	main_instrs = [i for b in mir_funcs[main_id].blocks.values() for i in b.instructions]
	assert any(isinstance(i, M.Call) and i.fn_id == hidden_id for i in main_instrs)
