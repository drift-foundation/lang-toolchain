#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.type_checker import TypeChecker


def _find_calls(block: H.HBlock) -> list[H.HCall]:
	calls: list[H.HCall] = []
	for stmt in block.statements:
		if isinstance(stmt, H.HLet) and isinstance(stmt.value, H.HCall):
			calls.append(stmt.value)
		elif isinstance(stmt, H.HExprStmt) and isinstance(stmt.expr, H.HCall):
			calls.append(stmt.expr)
		elif isinstance(stmt, H.HReturn) and isinstance(stmt.value, H.HCall):
			calls.append(stmt.value)
	return calls


def _check_main(src: str, tmp_path: Path):
	src_path = tmp_path / "struct_ctor_field_infer.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	fn_ids = module.fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_block = module.func_hirs[main_id]
	main_sig = module.signatures_by_id.get(main_id)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=module.signatures_by_id,
		callable_registry=None,
	)
	return result, type_table, main_block


def test_struct_ctor_infers_from_fields(tmp_path: Path) -> None:
	result, type_table, main_block = _check_main(
		"""
struct Box<T> { value: T }

fn main() -> Int {
	val b = Box(1);
	return b.value;
}
""",
		tmp_path,
	)
	assert result.diagnostics == []
	calls = _find_calls(main_block)
	assert len(calls) == 1
	box_base = type_table.get_struct_base(module_id="main", name="Box")
	assert box_base is not None
	expected = type_table.ensure_struct_instantiated(box_base, [type_table.ensure_int()])
	call_ty = result.typed_fn.expr_types.get(calls[0].node_id)
	assert call_ty == expected


def test_struct_ctor_field_conflict_is_error(tmp_path: Path) -> None:
	result, _type_table, _main_block = _check_main(
		"""
struct Pair<T> { a: T, b: T }

fn main() -> Int {
	val p = Pair(1, "s");
	return 0;
}
""",
		tmp_path,
	)
	assert any("cannot infer type arguments for struct 'Pair'" in d.message for d in result.diagnostics)
