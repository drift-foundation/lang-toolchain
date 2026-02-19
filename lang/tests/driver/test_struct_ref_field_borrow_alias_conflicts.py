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


def _assert_conflict(checked) -> None:
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "cannot take mutable borrow while borrow active on 'st'" in d.message]
	assert matches, errors
	assert all(d.phase == "borrowcheck" for d in matches), matches
	for d in errors:
		assert not d.message.startswith("internal:"), d


def test_struct_ref_field_mut_self_alias_direct_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct Session(id: Int);
struct Statement(session: &mut Session);

implement Statement {
	fn bump(self: &mut Statement) nothrow -> Int {
		self.session.id = self.session.id + 1;
		return self.session.id;
	}
}

fn read(r: &Int) nothrow -> Int { return r; }

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	var st = Statement(session = &mut sess);
	val r = &st.session.id;
	val n = st.bump();
	return n + read(r);
}
""",
	)
	_assert_conflict(checked)


def test_struct_ref_field_mut_self_alias_if_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct Session(id: Int);
struct Statement(session: &mut Session);

implement Statement {
	fn bump(self: &mut Statement) nothrow -> Int {
		self.session.id = self.session.id + 1;
		return self.session.id;
	}
}

fn read(r: &Int) nothrow -> Int { return r; }

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	var st = Statement(session = &mut sess);
	val r = &st.session.id;
	if true {
		val n = st.bump();
		return n + read(r);
	}
	return 0;
}
""",
	)
	_assert_conflict(checked)


def test_struct_ref_field_mut_self_alias_match_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct Session(id: Int);
struct Statement(session: &mut Session);

implement Statement {
	fn bump(self: &mut Statement) nothrow -> Int {
		self.session.id = self.session.id + 1;
		return self.session.id;
	}
}

fn read(r: &Int) nothrow -> Int { return r; }

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	var st = Statement(session = &mut sess);
	val r = &st.session.id;
	val _ = st.bump();
	match Optional::Some(1) {
		Optional::Some(_) => { return read(r); },
		Optional::None() => { return 0; }
	}
}
""",
	)
	_assert_conflict(checked)


def test_struct_ref_field_mut_self_alias_loop_rejected(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m

struct Session(id: Int);
struct Statement(session: &mut Session);

implement Statement {
	fn bump(self: &mut Statement) nothrow -> Int {
		self.session.id = self.session.id + 1;
		return self.session.id;
	}
}

fn read(r: &Int) nothrow -> Int { return r; }

fn main() nothrow -> Int {
	var sess = Session(id = 1);
	var st = Statement(session = &mut sess);
	val r = &st.session.id;
	while st.bump() < 3 {
		if st.session.id > 100 { return 9; }
	}
	return read(r);
}
""",
	)
	_assert_conflict(checked)
