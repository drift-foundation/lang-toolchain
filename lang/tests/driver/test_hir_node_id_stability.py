# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.stage1.normalize import normalize_hir


def _collect_expr_node_id_dups(block: H.HBlock) -> list[tuple[int, str, str]]:
	seen: dict[int, str] = {}
	dups: list[tuple[int, str, str]] = []

	def walk(obj: object) -> None:
		if isinstance(obj, H.HExpr):
			node_id = getattr(obj, "node_id", 0)
			kind = type(obj).__name__
			prev = seen.get(node_id)
			if prev is None:
				seen[node_id] = kind
			elif prev != kind:
				dups.append((node_id, prev, kind))
		if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
			return
		if is_dataclass(obj):
			for f in fields(obj):
				walk(getattr(obj, f.name))
			return
		if isinstance(obj, (list, tuple)):
			for item in obj:
				walk(item)
			return
		if isinstance(obj, dict):
			for key in sorted(obj.keys(), key=repr):
				walk(obj[key])
			return

	walk(block)
	return dups


def test_compile_stubbed_funcs_preserves_node_ids_on_normalized_hir() -> None:
	"""
	Compile a real-world HIR through the stubbed pipeline and assert that
	normalized HIR node ids are not corrupted by checker passes.
	"""
	path = Path("lang/tests/codegen/e2e/hashmap_iter_all/main.drift")
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(path)
	assert not diagnostics
	normalized = {fn_id: normalize_hir(block) for fn_id, block in module.func_hirs.items()}

	compile_stubbed_funcs(
		func_hirs=normalized,
		signatures=module.signatures_by_id,
		type_table=type_table,
		return_checked=True,
	)

	for fn_id, block in normalized.items():
		dups = _collect_expr_node_id_dups(block)
		assert not dups, f"duplicate node_id values detected in {fn_id}: {dups[:5]}"
