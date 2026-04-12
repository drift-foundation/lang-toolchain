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
module main;

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
module main;

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


def test_try_trait_method_on_ref_rejected_no_borrowed_impl(tmp_path: Path) -> None:
	"""Borrowed &Result does not implement Try — users must own the Result
	before calling into_try()/or_throw(). This is intentional: the owned
	Try impl uses Throw::throw_self which consumes the error value."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;
use trait core.Try;

	fn main() -> Int {
	val r: core.Result<Int, Int> = core.Result::Ok(1);
	val v = (&r).into_try();
	return v;
}
""",
		tmp_path,
	)
	assert len(diagnostics) > 0, "borrowed &Result should not have a Try impl"
	assert any("into_try" in d.message or "method" in d.message.lower() for d in diagnostics)


def test_try_trait_requires_throw_impl(tmp_path: Path) -> None:
	"""Error type without Throw impl cannot use into_try/or_throw — even
	if it implements Diagnostic. The Try constraint is ErrT is Throw."""
	diagnostics = _compile_source(
		"""
module main;

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
	assert any("into_try" in d.message or "Try" in d.message or "Throw" in d.message for d in diagnostics)


def test_try_trait_into_try_uses_err_type_for_result_variant(tmp_path: Path) -> None:
	diagnostics = _compile_source(
		"""
module main;

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

def test_try_trait_diagnostic_alone_not_sufficient(tmp_path: Path) -> None:
	"""Diagnostic alone is NOT sufficient for into_try/or_throw — the
	error type must implement Throw. This test pins that Diagnostic
	without Throw is rejected."""
	diagnostics = _compile_source(
		"""
module main;

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
	assert diagnostics, "Diagnostic-only error type should not satisfy Try constraint (requires Throw)"
	assert any("Throw" in d.message or "into_try" in d.message for d in diagnostics)
