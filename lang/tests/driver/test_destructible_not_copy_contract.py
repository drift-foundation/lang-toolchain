# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Pin: Destructible types must never be classified as Copy.

Root cause of the Arc-in-struct drop leak: _is_copy_structural did not
consult is_destructible, so types wrapping RawBuffer (Arc, Mutex) were
misclassified as structurally Copy.  A struct containing such a type
inherited the wrong Copy status, causing MIR scope-exit drops to be
skipped and the Arc allocation to leak.

This test validates the type-system contract directly against the
TypeTable API, complementing the e2e runtime leak check in
arc_struct_field_get_drop_leak.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.types_core import TypeKind
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _build(tmp_path: Path, content: str):
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(src, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	func_hirs, sigs, _fn_ids = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	all_diags = list(diagnostics) + list(checked.diagnostics)
	return type_table, all_diags


PROGRAM = """\
module m_main

import std.concurrent as conc;
import std.sync as sync;

struct Wrapper {
	arc_field: conc.Arc<sync.AtomicBool>,
	plain: Int
}

fn main() nothrow -> Int {
	var w = Wrapper(arc_field = conc.arc(sync.atomic_bool(false)), plain = 0);
	return 0;
}
"""


def _find_type(type_table, module_id: str, name: str, kind=TypeKind.STRUCT):
	"""Resolve a nominal TypeId by module and name."""
	tid = type_table.get_nominal(kind=kind, module_id=module_id, name=name)
	if tid is None:
		tid = type_table.find_unique_nominal_by_name(kind=kind, name=name)
	return tid


def test_arc_copy_status_is_false(tmp_path: Path) -> None:
	"""Arc<T> must not be Copy — it is Destructible."""
	tt, diags = _build(tmp_path, PROGRAM)
	assert not any(d.severity == "error" for d in diags), [d.message for d in diags if d.severity == "error"]
	arc_tid = _find_type(tt, "std.concurrent", "Arc")
	if arc_tid is None:
		# Try instantiated form
		for tid in range(tt.next_id):
			td = tt.get(tid)
			if td.kind is TypeKind.STRUCT and td.name == "Arc":
				arc_tid = tid
				break
	assert arc_tid is not None, "Arc type not found in TypeTable"
	assert tt.is_destructible(arc_tid), f"Arc (tid={arc_tid}) must be Destructible"
	cs = tt.copy_status(arc_tid)
	assert cs is not True, f"Arc (tid={arc_tid}) copy_status must not be True, got {cs}"


def test_struct_containing_arc_copy_status_is_false(tmp_path: Path) -> None:
	"""A struct with an Arc<T> field must not be Copy."""
	tt, diags = _build(tmp_path, PROGRAM)
	assert not any(d.severity == "error" for d in diags), [d.message for d in diags if d.severity == "error"]
	wrapper_tid = _find_type(tt, "m_main", "Wrapper")
	assert wrapper_tid is not None, "Wrapper type not found in TypeTable"
	cs = tt.copy_status(wrapper_tid)
	assert cs is not True, f"Wrapper (tid={wrapper_tid}) copy_status must not be True, got {cs}"
	assert tt.has_drop(wrapper_tid), f"Wrapper (tid={wrapper_tid}) must have has_drop=True"


def test_destructible_implies_not_copy(tmp_path: Path) -> None:
	"""No type that is Destructible should report copy_status=True."""
	tt, diags = _build(tmp_path, PROGRAM)
	assert not any(d.severity == "error" for d in diags), [d.message for d in diags if d.severity == "error"]
	violations = []
	for tid, td in tt._defs.items():
		if td.kind in {TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL, TypeKind.TYPEVAR}:
			continue
		if not tt.is_destructible(tid):
			continue
		cs = tt.copy_status(tid)
		if cs is True:
			violations.append(f"tid={tid} name={td.name} module={td.module_id} kind={td.kind.name}")
	assert not violations, f"Destructible types with copy_status=True: {violations}"
