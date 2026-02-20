import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_alias_return_assigns_into_struct_field_without_mismatch(tmp_path: Path) -> None:
	wire_types_src = tmp_path / "wire_types.drift"
	wire_src = tmp_path / "wire.drift"
	main_src = tmp_path / "main.drift"
	wire_types_src.write_text(textwrap.dedent(
		"""
		module wire.types
		pub struct S { pub x: Int }
		export { S };
		"""
	))
	wire_src.write_text(textwrap.dedent(
		"""
		module wire
		import wire.types as types;
		pub type S = types.S;
		export { S, mk };
		pub fn mk() nothrow -> S { return types.S(x = 1); }
		"""
	))
	main_src.write_text(textwrap.dedent(
		"""
		module main
		import wire as wire;
		struct C { pub s: wire.S }
		pub fn main() nothrow -> Int {
			val s = wire.mk();
			val c = C(s = s);
			return c.s.x;
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[wire_types_src, wire_src, main_src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	func_hirs, signatures, _fn_ids = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=excs,
		entry="main",
		type_table=table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not any(d.severity == "error" for d in checked.diagnostics)
	assert "define i64 @main(" in ir


def test_alias_to_missing_nominal_reports_user_diagnostic_not_internal(tmp_path: Path) -> None:
	wire_src = tmp_path / "wire.drift"
	main_src = tmp_path / "main.drift"
	wire_src.write_text(textwrap.dedent(
		"""
		module wire
		pub type S = missing.Nope;
		export { S };
		"""
	))
	main_src.write_text(textwrap.dedent(
		"""
		module main
		import wire as wire;
		struct C { pub s: wire.S }
		pub fn main() nothrow -> Int { return 0; }
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[wire_src, main_src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert any(d.severity == "error" for d in diags), diags
	assert not any(d.message.startswith("internal:") for d in diags), diags
	func_hirs, signatures, _fn_ids = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=excs,
		entry="main",
		type_table=table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not any(
		d.severity == "error"
		and d.phase in ("mir_validate", "codegen")
		and d.message.startswith("internal:")
		for d in checked.diagnostics
	), checked.diagnostics


def test_cross_module_alias_variant_ref_payload_match_does_not_trip_mir_invariant(tmp_path: Path) -> None:
	types_src = tmp_path / "types.drift"
	api_src = tmp_path / "api.drift"
	main_src = tmp_path / "main.drift"
	types_src.write_text(textwrap.dedent(
		"""
		module repro.types
		export { Cell };
		pub variant Cell {
			Null,
			Text(value: String)
		}
		"""
	))
	api_src.write_text(textwrap.dedent(
		"""
		module repro.api
		import repro.types as types;
		export { Cell, cell_is_null, cell_text };
		pub type Cell = types.Cell;
		pub fn cell_is_null(cell: &Cell) nothrow -> Bool {
			match cell {
				Cell::Null => { return true; },
				Cell::Text(_) => { return false; }
			}
		}
		pub fn cell_text(cell: &Cell) nothrow -> Optional<&String> {
			match cell {
				Cell::Null => { return Optional::None(); },
				Cell::Text(v) => { return Optional::Some(v); }
			}
		}
		"""
	))
	main_src.write_text(textwrap.dedent(
		"""
		module main
		import std.core as core;
		import repro.api as api;
		struct Row {
			vals: Array<api.Cell>
		}
		fn row_cell_ref(row: &Row, idx: Int) nothrow -> core.Result<&api.Cell, Int> {
			if idx < 0 or idx >= row.vals.len { return core.Result::Err(1); }
			var i = 0;
			while i < row.vals.len {
				if i == idx { return core.Result::Ok(&row.vals[i]); }
				i = i + 1;
			}
			return core.Result::Err(2);
		}
		implement Row {
			fn is_null(self: &Row, idx: Int) nothrow -> core.Result<Bool, Int> {
				match row_cell_ref(self, idx) {
					core.Result::Err(e) => { return core.Result::Err(e); },
					core.Result::Ok(cell) => { return core.Result::Ok(api.cell_is_null(cell)); }
				}
			}
			fn get_string(self: &Row, idx: Int) nothrow -> core.Result<String, Int> {
				match row_cell_ref(self, idx) {
					core.Result::Err(e) => { return core.Result::Err(e); },
					core.Result::Ok(cell) => {
						match api.cell_text(cell) {
							Optional::None => { return core.Result::Err(3); },
							Optional::Some(v) => { return core.Result::Ok(*v); }
						}
					}
				}
			}
		}
		pub fn main() nothrow -> Int {
			var arr: Array<api.Cell> = [];
			arr.push(api.Cell::Null());
			val row = Row(vals = move arr);
			match row.is_null(0) {
				core.Result::Ok(v) => { if v { return 0; } return 3; },
				core.Result::Err(_) => { return 4; }
			}
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[types_src, api_src, main_src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not any(d.severity == "error" for d in diags), diags
	func_hirs, signatures, _fn_ids = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=excs,
		entry="main",
		type_table=table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not any(d.severity == "error" for d in checked.diagnostics), checked.diagnostics
	assert "define i64 @main(" in ir
