import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_atomic_store_uint_struct_field_ref_codegen(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(textwrap.dedent(
		"""
		module main;
		import lang.atomic as atomic;

		struct S { a: atomic.AtomicUint }

		pub fn main() nothrow -> Int {
			var s = S(a = atomic.atomic_uint(cast<Uint>(0)));
			atomic.atomic_store_uint(s.a, cast<Uint>(1), 0);
			if atomic.atomic_load_uint(s.a, 0) != cast<Uint>(1) { return 1; }
			return 0;
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
	assert not any(d.severity == "error" for d in checked.diagnostics)
	assert "define i64 @main(" in ir


def test_atomic_uint_field_ref_intrinsics_codegen_surface(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(textwrap.dedent(
		"""
		module main;
		import lang.atomic as atomic;

		struct S { a: atomic.AtomicUint }

		pub fn main() nothrow -> Int {
			var s = S(a = atomic.atomic_uint(cast<Uint>(10)));
			val l0 = atomic.atomic_load_uint(s.a, 0);
			if l0 != cast<Uint>(10) { return 1; }

			atomic.atomic_store_uint(s.a, cast<Uint>(20), 0);
			val ex = atomic.atomic_exchange_uint(s.a, cast<Uint>(30), 0);
			if ex != cast<Uint>(20) { return 2; }

			val c1 = atomic.atomic_compare_exchange_uint(s.a, cast<Uint>(30), cast<Uint>(31), 3, 1);
			if not c1 { return 3; }
			val c2 = atomic.atomic_compare_exchange_uint(s.a, cast<Uint>(30), cast<Uint>(32), 3, 1);
			if c2 { return 4; }

			val o1 = atomic.atomic_compare_exchange_observed_uint(s.a, cast<Uint>(31), cast<Uint>(40), 3, 1);
			if o1 != cast<Uint>(31) { return 5; }
			val o2 = atomic.atomic_compare_exchange_observed_uint(s.a, cast<Uint>(31), cast<Uint>(50), 3, 1);
			if o2 != cast<Uint>(40) { return 6; }

			val p = atomic.atomic_fetch_add_uint(s.a, cast<Uint>(2), 3);
			if p != cast<Uint>(40) { return 7; }
			val q = atomic.atomic_fetch_sub_uint(s.a, cast<Uint>(1), 3);
			if q != cast<Uint>(42) { return 8; }

			if atomic.atomic_load_uint(s.a, 0) != cast<Uint>(41) { return 9; }
			return 0;
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
	assert not any(d.severity == "error" for d in checked.diagnostics)
	assert "define i64 @main(" in ir
