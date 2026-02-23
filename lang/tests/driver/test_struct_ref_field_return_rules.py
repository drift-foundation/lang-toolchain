from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _compile(tmp_path: Path, source: str):
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
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="m::main",
	)
	return checked


def test_borrowed_aggregate_return_single_origin_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

import std.core as core;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn query(s: &mut Session) nothrow -> core.Result<Statement, Int> {
	return core.Result::Ok(Statement(session = s));
}

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	match query(&mut sess) {
		core.Result::Ok(_) => { return 0; },
		core.Result::Err(_) => { return 1; }
	}
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []


def test_borrowed_aggregate_return_single_origin_via_local_wrapper_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

import std.core as core;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn query(s: &mut Session) nothrow -> core.Result<Statement, Int> {
	val st = Statement(session = s);
	val out: core.Result<Statement, Int> = core.Result::Ok(st);
	return move out;
}

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	match query(&mut sess) {
		core.Result::Ok(_) => { return 0; },
		core.Result::Err(_) => { return 1; }
	}
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []


def test_borrowed_aggregate_return_from_local_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct Session(id: Int);
struct Statement(session: &mut Session);

fn bad() nothrow -> Statement {
	var sess = Session(id = 1);
	return Statement(session = &mut sess);
}

fn main() nothrow -> Int {
	val _ = bad();
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "borrowed aggregate return must derive from a reference parameter" in d.message]
	assert matches, errors


def test_borrowed_aggregate_return_from_local_binding_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
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
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "borrowed aggregate return must derive from a reference parameter" in d.message]
	assert matches, errors


def test_borrowed_aggregate_return_multi_origin_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct PairRefs(a: &Int, b: &Int);

fn bad(a: &Int, b: &Int) nothrow -> PairRefs {
	return PairRefs(a = a, b = b);
}

fn main() nothrow -> Int {
	val x = 1;
	val y = 2;
	val _ = bad(&x, &y);
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "borrowed aggregate return must derive from a single reference parameter" in d.message]
	assert matches, errors


def test_borrowed_aggregate_pass_through_generic_default_retaining_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct Session(id: Int);
struct Statement(session: &mut Session);

fn id<T>(x: T) nothrow -> T {
	return x;
}

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	val st = Statement(session = &mut sess);
	val _ = id<type Statement>(st);
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "borrowed aggregate argument cannot flow through retaining" in d.message]
	assert matches, errors


def test_borrowed_aggregate_store_in_array_push_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
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
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "owning Array cannot contain borrowed aggregate element type in v1" in d.message]
	assert matches, errors


def test_borrowed_aggregate_pass_by_ref_param_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct Session(id: Int);
struct Statement(session: &mut Session);

fn inspect(s: &Statement) nothrow -> Int {
	return s.session.id;
}

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	val st = Statement(session = &mut sess);
	val n = inspect(st);
	if n != 1 {
		return 1;
	}
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []


def test_borrowed_aggregate_callback_capture_escape_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

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
	matches = [d for d in errors if "lambda capturing borrowed aggregate cannot escape through retaining parameter" in d.message]
	assert matches, errors


def test_borrowed_aggregate_registry_store_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

import std.runtime as rt;

struct Session(id: Int);
struct Statement(session: &mut Session);

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	val st = Statement(session = &mut sess);
	val reg = rt.global_registry();
	reg.set<type Statement>(move st);
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "borrowed aggregate argument cannot flow through retaining parameter 'value' of 'GlobalRegistry::set'" in d.message]
	assert matches, errors
