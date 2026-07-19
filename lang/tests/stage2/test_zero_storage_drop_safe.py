# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""string-arc-endgame-array-sweep — `zero_storage_drop_safe` pins.

The extracted zero-storage/drop-safe policy axis
(`drop_policy_compute.zero_storage_drop_safe`) replaces the
variant-only `variant_zero_tag_drop_safe` for EVERY production
decision (maintainer migration rule).  Pins here:

1. Predicate contract — Variant and Array return True; unrelated
   types (droppable struct, String, Int, interface) fail closed.
2. Compat-shim semantics — the retained `variant_zero_tag_drop_safe`
   stays VARIANT-ONLY (tests/back-compat depend on the original
   meaning; a widened wrapper under the variant name would lie).
3. Migration rule, fail-closed — a SOURCE SCAN proves no production
   call site outside the shim's own definition remains.  A new
   production caller of the variant-only name fails this pin with an
   explicit STOP message.
"""

from __future__ import annotations

from pathlib import Path

from lang.driftc.core.types_core import TypeTable, VariantArmSchema
from lang.driftc.stage2.drop_policy_compute import zero_storage_drop_safe
from lang.driftc.stage2.string_arc import variant_zero_tag_drop_safe


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


def test_compat_shim_stays_variant_only() -> None:
	"""The retained shim keeps the ORIGINAL variant-only semantics —
	back-compat consumers (tests) must not silently see arrays admitted
	through the variant name."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	assert variant_zero_tag_drop_safe(_variant_ty(tt), tt) is True
	assert variant_zero_tag_drop_safe(tt.new_array(string_ty), tt) is False


def test_no_production_caller_of_variant_only_name() -> None:
	"""Migration rule, fail-closed (maintainer, string-arc-endgame-
	array-sweep; review-hardened 2026-07-19): no production decision
	may flow through the variant-only compatibility symbol.  AST scan
	over lang/driftc — immune to aliasing — rejects EVERY reference to
	`variant_zero_tag_drop_safe` outside its own definition in
	string_arc.py:

	- `from ... import variant_zero_tag_drop_safe [as alias]`
	  (ImportFrom, any alias),
	- `anything.variant_zero_tag_drop_safe` (Attribute — catches
	  module-qualified and aliased-module references),
	- any bare `variant_zero_tag_drop_safe` Name use.

	Docstrings/comments are invisible to the AST walk and stay
	allowed.  The ONLY permitted occurrence anywhere is the single
	sync-`def` shim in string_arc.py — a second definition there, or
	an async/class shadow anywhere (string_arc included), is an
	offender.  If this pin fails: STOP — migrate the new reference to
	`drop_policy_compute.zero_storage_drop_safe` instead of relaxing
	the scan."""
	import ast as _ast
	sym = "variant_zero_tag_drop_safe"
	root = Path(__file__).resolve().parents[3] / "lang" / "driftc"
	offenders: list[str] = []
	shim_defs = 0
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
				if py.name == "string_arc.py":
					shim_defs += 1
					if shim_defs > 1:
						offenders.append(
							f"{rel}:{node.lineno}: duplicate shim definition"
						)
				else:
					offenders.append(f"{rel}:{node.lineno}: shadow definition")
			elif isinstance(node, (_ast.AsyncFunctionDef, _ast.ClassDef)) and node.name == sym:
				offenders.append(f"{rel}:{node.lineno}: async/class shadow")
	assert shim_defs == 1, (
		f"expected exactly one shim def in string_arc.py, found {shim_defs}"
	)
	assert not offenders, (
		"production reference(s) to the variant-only compatibility "
		"symbol — migrate to zero_storage_drop_safe (maintainer "
		"migration rule):\n" + "\n".join(offenders)
	)
