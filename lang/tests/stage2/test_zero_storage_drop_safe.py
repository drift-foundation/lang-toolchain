# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""string-arc-endgame-array-sweep — `zero_storage_drop_safe` pins.

The extracted zero-storage/drop-safe policy axis
(`drop_policy_compute.zero_storage_drop_safe`) replaces the
variant-only `variant_zero_tag_drop_safe` for EVERY production
decision (maintainer migration rule).  Pins here:

1. Predicate contract — Variant and Array return True; unrelated
   types (droppable struct, String, Int, interface) fail closed.
2. Retirement rule, fail-closed (Phase D) — the variant-only
   compatibility symbol `variant_zero_tag_drop_safe` died with
   string_arc.py: an AST scan proves the name no longer EXISTS
   anywhere under lang/driftc (no definition, no reference).  A
   reintroduction fails this pin with an explicit STOP message.
"""

from __future__ import annotations

from pathlib import Path

from lang.driftc.core.types_core import TypeTable, VariantArmSchema
from lang.driftc.stage2.drop_policy_compute import zero_storage_drop_safe


def _variant_ty(tt: TypeTable) -> int:
	return tt.declare_variant(
		"test", "Zv", [], [VariantArmSchema(name="A", fields=[])],
	)


def _droppable_struct_ty(tt: TypeTable) -> int:
	string_ty = tt.ensure_string()
	tid = tt.declare_struct(module_id="test", name="Zs", field_names=["inner"])
	tt.define_struct_fields(tid, field_types=[string_ty])
	return tid


def test_predicate_contract_variant_and_array_true_rest_fail_closed() -> None:
	"""Pin 1 (maintainer spec): Variant/Array → True; unrelated types
	fail closed."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	assert zero_storage_drop_safe(_variant_ty(tt), tt) is True
	assert zero_storage_drop_safe(tt.new_array(string_ty), tt) is True
	assert zero_storage_drop_safe(_droppable_struct_ty(tt), tt) is False
	assert zero_storage_drop_safe(string_ty, tt) is False
	assert zero_storage_drop_safe(tt.ensure_int(), tt) is False
	assert zero_storage_drop_safe(tt.ensure_bool(), tt) is False


def test_no_production_caller_of_variant_only_name() -> None:
	"""Retirement rule, fail-closed (Phase D): the variant-only
	compatibility symbol died with string_arc.py.  AST scan over
	lang/driftc — immune to aliasing — rejects EVERY definition of or
	reference to `variant_zero_tag_drop_safe` (import, attribute, bare
	name, def, async/class shadow).  Docstrings/comments are invisible
	to the AST walk and stay allowed.  If this pin fails: STOP — use
	`drop_policy_compute.zero_storage_drop_safe` instead of
	reintroducing the misleading variant-only name."""
	import ast as _ast
	sym = "variant_zero_tag_drop_safe"
	root = Path(__file__).resolve().parents[3] / "lang" / "driftc"
	offenders: list[str] = []
	for py in sorted(root.rglob("*.py")):
		tree = _ast.parse(py.read_text())
		rel = py.relative_to(root)
		for node in _ast.walk(tree):
			if isinstance(node, _ast.ImportFrom):
				for a in node.names:
					if a.name == sym:
						offenders.append(
							f"{rel}:{node.lineno}: import"
							+ (f" as {a.asname}" if a.asname else "")
						)
			elif isinstance(node, _ast.Attribute) and node.attr == sym:
				offenders.append(f"{rel}:{node.lineno}: attribute reference")
			elif isinstance(node, _ast.Name) and node.id == sym:
				offenders.append(f"{rel}:{node.lineno}: name reference")
			elif isinstance(node, _ast.FunctionDef) and node.name == sym:
				offenders.append(f"{rel}:{node.lineno}: definition")
			elif isinstance(node, (_ast.AsyncFunctionDef, _ast.ClassDef)) and node.name == sym:
				offenders.append(f"{rel}:{node.lineno}: async/class shadow")
	assert not offenders, (
		"reference(s) to the RETIRED variant-only compatibility symbol "
		"— use zero_storage_drop_safe (Phase D retirement rule):\n"
		+ "\n".join(offenders)
	)
