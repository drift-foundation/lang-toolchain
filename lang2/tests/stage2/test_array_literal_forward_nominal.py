# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.driftc.module_lowered import flatten_modules
from lang2.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_array_literal_forward_nominal_copy_allowed(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(
		src,
		"""
module m_main

	fn main() nothrow -> Int {
		val pairs = [Pair(a = 1, b = 2), Pair(a = 3, b = 4)];
		val points = [Point(x = 1, y = 2), Point(x = 3, y = 4)];
		val variants = [Choice::PointVal(Point(x = 5, y = 6)), Choice::PairVal(Pair(a = 7, b = 8))];
		val total = pairs.len + points.len + variants.len;
		return total;
	}

	struct Pair { a: Int, b: Int }
	struct Point { x: Int, y: Int }
	variant Choice {
		PointVal(p: Point),
		PairVal(p: Pair),
	}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, sigs, _fn_ids_by_name = flatten_modules(modules)
	_mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert checked.diagnostics == []


def test_array_literal_forward_nominal_non_copy_rejected(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(
		src,
		"""
module m_main

	fn main() nothrow -> Int {
		val files = [File(data = [1, 2]), File(data = [3, 4])];
		return 0;
	}

	struct File { data: Array<Int> }
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, sigs, _fn_ids_by_name = flatten_modules(modules)
	_mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert any("array literal element type must be Copy" in d.message for d in checked.diagnostics)


def test_array_literal_typevar_copy_unknown(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(
		src,
		"""
module m_main

	fn mk<T>(x: T) nothrow -> Int {
		val xs = [x];
		return xs.len;
	}

	fn main() nothrow -> Int {
		return mk(1);
	}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, sigs, fn_ids_by_name = flatten_modules(modules)
	mk_ids = fn_ids_by_name.get("mk") or fn_ids_by_name.get("m_main::mk")
	assert mk_ids is not None
	mk_id = mk_ids[0]
	_mir_funcs, checked = compile_stubbed_funcs(
		func_hirs={mk_id: func_hirs[mk_id]},
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert any(d.code == "E-ARRAY-LITERAL-COPY-UNKNOWN" for d in checked.diagnostics)


def test_array_literal_forward_generic_wrapper_no_non_copy(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(
		src,
		"""
module m_main

	fn mk<T>(x: T) nothrow -> Int {
		val xs = [x];
		return xs.len;
	}

	struct Pair { a: Int, b: Int }

	fn main() nothrow -> Int {
		return mk(Pair(a = 1, b = 2));
	}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, sigs, fn_ids_by_name = flatten_modules(modules)
	mk_ids = fn_ids_by_name.get("mk") or fn_ids_by_name.get("m_main::mk")
	assert mk_ids is not None
	mk_id = mk_ids[0]
	_mir_funcs, checked = compile_stubbed_funcs(
		func_hirs={mk_id: func_hirs[mk_id]},
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert not any(d.code == "E-ARRAY-LITERAL-NON-COPY" for d in checked.diagnostics)
