# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
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


def test_destructible_impl_wrong_self_param_is_rejected(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module main
import std.core as core;

struct Session { v: Int }
struct Statement { session: &mut Session }

implement core.Destructible for Statement {
	pub fn destroy(self: &mut Statement) nothrow -> Void {
		self.session.v = self.session.v + 1;
	}
}

fn main() nothrow -> Int { return 0; }
""",
	)
	assert any(
		d.code == "E_TRAIT_METHOD_PARAM_MISMATCH" and "trait impl method 'destroy' parameter 1 expects Statement" in (d.message or "")
		for d in diagnostics
	), diagnostics


def test_copy_impl_on_noncopy_field_struct_is_rejected(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module main
import std.core as core;

struct BadCopy { v: String }

implement core.Copy for BadCopy {
}

fn main() nothrow -> Int { return 0; }
""",
	)
	assert any(
		d.code == "E_COPY_IMPL_NONCOPY_TARGET" and "core.Copy impl target must be structurally Copy in MVP" in (d.message or "")
		for d in diagnostics
	), diagnostics


def test_copy_impl_allows_struct_with_repeated_uint_fields(tmp_path: Path) -> None:
	diagnostics = _compile(
		tmp_path,
		"""
module main
import std.core as core;

struct Pair {
	a: Uint,
	b: Uint
}

implement core.Copy for Pair {
}

fn main() nothrow -> Int { return 0; }
""",
	)
	assert diagnostics == [], diagnostics
