# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: checker must propagate payload types into match arm binder scopes.

Before fix: match core.Result::Ok(vals) — vals is Unknown, so vals.len or
vals[i] emit spurious "unknown name" or "indexing requires Array" errors.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile(tmp_path: Path, content: str):
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(src, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	func_hirs, sigs, _fn_ids = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	return list(diagnostics) + list(checked.diagnostics)


def _errors(diags):
	return [d for d in diags if getattr(d, "severity", None) == "error"]


def test_result_ok_binder_array_len(tmp_path: Path) -> None:
	"""match Result::Ok(vals) => vals.len — must not error."""
	diags = _compile(
		tmp_path,
		"""
module m_main
import std.codec as codec;
import std.core as core;

fn _decode(s: &String) nothrow -> Int {
	val result = codec.hex_decode(s);
	match result {
		core.Result::Err(e) => { return 1; },
		core.Result::Ok(vals) => {
			if vals.len != 3 {
				return 2;
			}
		}
	}
	return 0;
}

pub fn main() nothrow -> Int {
	return _decode("aabbcc");
}
""",
	)
	errors = _errors(diags)
	assert errors == [], errors


def test_result_ok_binder_array_index(tmp_path: Path) -> None:
	"""match Result::Ok(vals) => vals[0] — direct indexing must not error."""
	diags = _compile(
		tmp_path,
		"""
module m_main
import std.codec as codec;
import std.core as core;

fn _decode_first(s: &String) nothrow -> Int {
	val result = codec.hex_decode(s);
	match result {
		core.Result::Err(e) => { return 1; },
		core.Result::Ok(vals) => {
			if vals.len == 0 { return 2; }
			val first = vals[0];
			if cast<Int>(first) != 170 {
				return 3;
			}
		}
	}
	return 0;
}

pub fn main() nothrow -> Int {
	return _decode_first("aa");
}
""",
	)
	errors = _errors(diags)
	assert errors == [], errors


def test_result_err_binder_field_access(tmp_path: Path) -> None:
	"""match Result::Err(e) => e.tag — binder should have error type."""
	diags = _compile(
		tmp_path,
		"""
module m_main
import std.codec as codec;
import std.core as core;

fn _check_err(s: &String) nothrow -> Int {
	val result = codec.hex_decode(s);
	match result {
		core.Result::Ok(vals) => { return 0; },
		core.Result::Err(e) => {
			if e.tag != "hex-invalid-char" {
				return 2;
			}
		}
	}
	return 1;
}

pub fn main() nothrow -> Int {
	return _check_err("zz");
}
""",
	)
	errors = _errors(diags)
	assert errors == [], errors
