from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _compile(tmp_path: Path, source: str, *, entry: str = "m::main"):
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == []
	func_hirs, signatures, _ = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry=entry,
	)
	return ir, checked


def _assert_no_internal_contract_errors(checked) -> None:
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	for d in errors:
		assert not d.message.startswith("internal:"), d


def test_struct_ref_field_result_return_reaches_codegen_boundary(tmp_path: Path) -> None:
	ir, checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn query(s: &mut Session) nothrow -> core.Result<Statement, Int> {
	return core.Result::Ok(Statement(session = s));
}

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	match query(&mut sess) {
		core.Result::Ok(st) => {
			if st.session.id != 1 {
				return 1;
			}
			return 0;
		},
		core.Result::Err(_) => { return 1; }
	}
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []
	assert 'define i64 @"m::main"(' in ir
	assert "define i32 @main()" in ir


def test_struct_ref_field_result_return_via_local_wrapper_reaches_codegen_boundary(tmp_path: Path) -> None:
	ir, checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn query(s: &mut Session) nothrow -> core.Result<Statement, Int> {
	val st = Statement(session = s);
	val out: core.Result<Statement, Int> = core.Result::Ok(move st);
	return move out;
}

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	match query(&mut sess) {
		core.Result::Ok(st) => {
			if st.session.id != 1 {
				return 1;
			}
			return 0;
		},
		core.Result::Err(_) => { return 1; }
	}
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []
	assert 'define i64 @"m::main"(' in ir
	assert "define i32 @main()" in ir


def test_struct_ref_field_array_store_rejected_at_checker_boundary(tmp_path: Path) -> None:
	_ir, checked = _compile(
		tmp_path,
		"""
module m;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	val st = Statement(session = &mut sess);
	var xs: Array<Statement> = [];
	xs.push(st);
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("owning Array cannot contain borrowed aggregate element type in v1" in d.message for d in errors), errors
	assert any(d.phase == "typecheck" for d in errors), errors
	_assert_no_internal_contract_errors(checked)


def test_struct_ref_field_local_return_rejected_at_checker_boundary(tmp_path: Path) -> None:
	_ir, checked = _compile(
		tmp_path,
		"""
module m;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn bad() nothrow -> Statement {
	var sess = Session(id = 1);
	val st = Statement(session = &mut sess);
	return st;
}

fn main() nothrow -> Int {
	val _ = bad();
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("borrowed aggregate return must derive from a reference parameter" in d.message for d in errors), errors
	assert any(d.phase == "typecheck" for d in errors), errors
	_assert_no_internal_contract_errors(checked)


def test_struct_ref_field_callback_capture_rejected_at_checker_boundary(tmp_path: Path) -> None:
	_ir, checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn run(var cb: core.Callback0<Int>) nothrow -> Int { return cb.call(); }

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	val st = Statement(session = &mut sess);
	val n = run(core.callback0(| | captures(move st) => { return 1; }));
	if n != 1 {
		return 1;
	}
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("lambda capturing borrowed aggregate cannot escape through retaining" in d.message for d in errors), errors
	assert any(d.phase == "typecheck" for d in errors), errors
	_assert_no_internal_contract_errors(checked)


def test_struct_ref_field_hashmap_store_rejected_at_checker_boundary(tmp_path: Path) -> None:
	_ir, checked = _compile(
		tmp_path,
		"""
module m;

import std.containers as containers;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	val st = Statement(session = &mut sess);
	var m = containers.hash_map<type Int, Statement>();
	val _ = m.insert(1, move st);
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("borrowed aggregate argument cannot flow through retaining parameter 'value' of 'HashMapCore<K, V, B>::insert'" in d.message for d in errors), errors
	assert any(d.phase == "typecheck" for d in errors), errors
	_assert_no_internal_contract_errors(checked)


def test_struct_ref_field_treemap_store_rejected_at_checker_boundary(tmp_path: Path) -> None:
	_ir, checked = _compile(
		tmp_path,
		"""
module m;

import std.containers as containers;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	val st = Statement(session = &mut sess);
	var m = containers.tree_map<type Int, Statement>();
	val _ = m.insert(1, move st);
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("borrowed aggregate argument cannot flow through retaining parameter 'value' of 'TreeMap<K, V>::insert'" in d.message for d in errors), errors
	assert any(d.phase == "typecheck" for d in errors), errors
	_assert_no_internal_contract_errors(checked)


def test_struct_ref_field_treemap_entry_receiver_not_false_positive(tmp_path: Path) -> None:
	ir, checked = _compile(
		tmp_path,
		"""
module m;

import std.containers as containers;

fn run() -> Int {
	var m = containers.tree_map<type Int, Int>();
	val k0 = 5;
	var e0 = m.entry_mut(&k0);
	if e0.is_occupied() {
		return 1;
	}
	val ins0 = e0.or_insert(5, 10);
	if not ins0 {
		return 2;
	}
	var e1 = m.entry_mut(&k0);
	val old_opt = e1.insert(5, 20);
	val _ = match old_opt {
		Some(_v) => { 0 },
		None => { 0 },
	};
	var e2 = m.entry_mut(&k0);
	val _ = e2.remove();
	return 0;
}

fn main() nothrow -> Int {
	return try run() catch { 99 };
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []
	assert 'define i64 @"m::main"(' in ir
