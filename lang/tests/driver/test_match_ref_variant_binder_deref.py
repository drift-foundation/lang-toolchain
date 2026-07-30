import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_match_ref_variant_binder_deref_infers_payload_types(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(textwrap.dedent(
		"""
		module main;

		variant Arg {
			Bool(value: Bool),
			Int(value: Int),
			Float(value: Float)
		}

		fn enc(a: &Arg) nothrow -> String {
			match a {
				Arg::Bool(v) => { val x: Bool = *v; if x { return "1"; } return "0"; },
				Arg::Int(v) => { val x: Int = *v; if x > 0 { return "i"; } return "z"; },
				Arg::Float(v) => { val x: Float = *v; if x > 0.0 { return "f"; } return "g"; }
			}
		}

		pub fn main() nothrow -> Int {
			val a = Arg::Int(7);
			val s = enc(a);
			if s == "i" { return 0; }
			return 1;
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[src],
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
	assert not any(d.severity == "error" for d in checked.diagnostics), checked.diagnostics
	assert "define i64 @main(" in ir


def test_match_value_variant_binder_deref_reports_user_error_not_internal(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(textwrap.dedent(
		"""
		module main;

		variant Arg {
			Int(value: Int)
		}

		fn enc(a: Arg) nothrow -> Int {
			match a {
				Arg::Int(v) => { val x: Int = *v; return x; }
			}
		}

		pub fn main() nothrow -> Int {
			val a = Arg::Int(7);
			return enc(a);
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not diags
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
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors, checked.diagnostics
	assert any("deref requires a reference value" in d.message for d in errors), errors
	assert not any(d.message.startswith("internal:") for d in errors), errors
