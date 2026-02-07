# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile_source(src: str, tmp_path: Path):
	path = tmp_path / "main.drift"
	_write_file(path, src)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	return checked.diagnostics


def test_try_trait_method_requires_use_trait(tmp_path: Path) -> None:
	diagnostics = _compile_source(
		"""
module main

import std.core as core;

	fn main() -> Int {
	val r: core.Result<Int, Int> = core.Result::Ok(1);
	val v = r.into_try();
	return v;
}
""",
		tmp_path,
	)
	assert diagnostics
	assert any("into_try" in d.message for d in diagnostics)


def test_try_trait_method_succeeds_with_use_trait(tmp_path: Path) -> None:
	diagnostics = _compile_source(
		"""
module main

import std.core as core;
use trait core.Try;
use trait core.Diagnostic;

	fn main() -> Int {
	val r: core.Result<Int, Int> = core.Result::Ok(1);
	val v = r.into_try();
	return v;
}
""",
		tmp_path,
	)
	assert diagnostics == []


def test_try_trait_method_on_ref_succeeds_with_use_trait(tmp_path: Path) -> None:
	diagnostics = _compile_source(
		"""
module main

import std.core as core;
use trait core.Try;
use trait core.Diagnostic;

	fn main() -> Int {
	val r: core.Result<Int, Int> = core.Result::Ok(1);
	val v = (&r).into_try();
	return v;
}
""",
		tmp_path,
	)
	assert diagnostics == []


def test_try_trait_requires_diagnostic_impl(tmp_path: Path) -> None:
	diagnostics = _compile_source(
		"""
module main

import std.core as core;
use trait core.Try;
use trait core.Diagnostic;

pub variant MyErr {
	Msg(m: String),
	@tombstone None
}

	fn main() -> Int {
	val r: core.Result<Int, MyErr> = core.Result::Err(MyErr::Msg("oops"));
	val v = r.into_try();
	return v;
}
""",
		tmp_path,
	)
	assert diagnostics
	assert any("into_try" in d.message or "Try" in d.message for d in diagnostics)


def test_try_trait_into_try_uses_err_type_for_result_variant(tmp_path: Path) -> None:
	diagnostics = _compile_source(
		"""
module main

import std.core as core;
import std.net as net;
use trait core.Try;
use trait core.Diagnostic;

	fn main() -> Int {
	val r: core.Result<net.TcpListener, net.NetError> = Err(net.NetError::WouldBlock());
	val _v = r.into_try();
	return 0;
}
""",
		tmp_path,
	)
	assert diagnostics == []

def test_try_trait_accepts_diagnostic_impl(tmp_path: Path) -> None:
	diagnostics = _compile_source(
		"""
module main

import std.core as core;
use trait core.Try;
use trait core.Diagnostic;

pub variant MyErr {
	Msg(m: String),
	@tombstone None
}

	implement core.Diagnostic for MyErr {
		pub fn to_diag(self: &MyErr) nothrow -> DiagnosticValue {
			return match self {
				Msg(m) => {
					m.to_diag()
				},
				default => { DiagnosticValue::Int(0) }
			};
		}
	}

	fn main() -> Int {
	val r: core.Result<Int, MyErr> = core.Result::Ok(1);
	val v = r.into_try();
	return v;
}
""",
		tmp_path,
	)
	assert diagnostics == []
