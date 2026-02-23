from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _compile_workspace(tmp_path: Path, files: dict[str, str], *, entry: str) -> tuple[str, object]:
	for rel, content in files.items():
		p = tmp_path / rel
		p.parent.mkdir(parents=True, exist_ok=True)
		p.write_text(content)
	srcs = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		srcs,
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


def _errors(checked) -> list:
	return [d for d in checked.diagnostics if d.severity == "error"]


def _assert_no_internal(errors: list) -> None:
	for d in errors:
		assert not d.message.startswith("internal:"), d


@pytest.mark.parametrize(
	("name", "source", "expected_error"),
	[
		(
			"result_scalar_string_array_struct_copy_noncopy_and_nested_ok",
			"""
module m
import std.core as core;

struct CopyPair(a: Int, b: Int);
implement core.Copy for CopyPair {}
struct NonCopy(msg: String);

fn mk_i() nothrow -> core.Result<Int, Int> { return core.Result::Ok(7); }
fn mk_s() nothrow -> core.Result<String, Int> { return core.Result::Ok("ok"); }
fn mk_a() nothrow -> core.Result<Array<Byte>, Int> { return core.Result::Ok([cast<Byte>(1), cast<Byte>(2), cast<Byte>(3)]); }
fn mk_c() nothrow -> core.Result<CopyPair, Int> { return core.Result::Ok(CopyPair(a = 9, b = 11)); }
fn mk_n() nothrow -> core.Result<NonCopy, Int> { return core.Result::Ok(NonCopy(msg = "payload")); }
fn mk_nested() nothrow -> core.Result<core.Result<NonCopy, Int>, Int> {
	val inner: core.Result<NonCopy, Int> = core.Result::Ok(NonCopy(msg = "inner"));
	return core.Result::Ok(move inner);
}

fn main() nothrow -> Int {
	match mk_i() { core.Result::Ok(v) => { if v != 7 { return 1; } }, core.Result::Err(_) => { return 2; } }
	match mk_s() { core.Result::Ok(v) => { if v != "ok" { return 3; } }, core.Result::Err(_) => { return 4; } }
	match mk_a() { core.Result::Ok(v) => { if v.len != 3 { return 5; } }, core.Result::Err(_) => { return 6; } }
	match mk_c() { core.Result::Ok(v) => { if v.a != 9 or v.b != 11 { return 7; } }, core.Result::Err(_) => { return 8; } }
	match mk_n() { core.Result::Ok(v) => { if v.msg != "payload" { return 9; } }, core.Result::Err(_) => { return 10; } }
	match mk_nested() {
		core.Result::Ok(inner) => {
			match inner {
				core.Result::Ok(v2) => { if v2.msg != "inner" { return 11; } },
				core.Result::Err(_) => { return 12; }
			}
		},
		core.Result::Err(_) => { return 13; }
	}
	return 0;
}
""",
			None,
		),
		(
			"result_with_borrowed_aggregate_ok",
			"""
module m
import std.core as core;
struct Session(id: Int);
struct Statement(session: &mut Session);
fn query(s: &mut Session) nothrow -> core.Result<Statement, Int> {
	return core.Result::Ok(Statement(session = s));
}
fn main() nothrow -> Int {
	var sess = Session(id = 41);
	match query(&mut sess) {
		core.Result::Ok(st) => {
			if st.session.id != 41 { return 1; }
			return 0;
		},
		core.Result::Err(_) => { return 2; }
	}
}
""",
			None,
		),
		(
			"borrowed_aggregate_array_store_rejected",
			"""
module m
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
			"owning Array cannot contain borrowed aggregate element type in v1",
		),
		(
			"borrowed_aggregate_local_origin_return_rejected",
			"""
module m
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
			"borrowed aggregate return must derive from a reference parameter",
		),
	],
)
def test_result_variant_boundary_matrix(tmp_path: Path, name: str, source: str, expected_error: str | None) -> None:
	ir, checked = _compile_workspace(tmp_path, {"main.drift": source}, entry="m::main")
	errors = _errors(checked)
	if expected_error is None:
		assert errors == [], f"{name}: {errors}"
		assert 'define i64 @"m::main"(' in ir
		assert "define i32 @main()" in ir
		return
	assert any(expected_error in d.message for d in errors), f"{name}: {errors}"
	assert any(d.phase == "typecheck" for d in errors), f"{name}: {errors}"
	_assert_no_internal(errors)
