# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang2.driftc.module_lowered import flatten_modules
from lang2.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang2.driftc.type_checker import TypeChecker
from lang2.driftc.test_helpers import build_linked_world


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _typecheck_workspace(tmp_path: Path, content: str) -> list[str]:
	src = tmp_path / "main.drift"
	_write_file(src, content)
	modules, type_table, _exc, module_exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	module_deps = _deps if isinstance(_deps, dict) else {}
	assert diagnostics == []
	func_hirs, signatures_by_id, _fn_ids = flatten_modules(modules)
	linked_world, require_env = build_linked_world(type_table)
	checker = TypeChecker(type_table)
	messages: list[str] = []
	for fn_id, hir in func_hirs.items():
		sig = signatures_by_id.get(fn_id)
		if sig is None or sig.param_names is None or sig.param_type_ids is None:
			continue
		if sig.module != "main":
			continue
		param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids)}
		param_mutable = None
		if sig.param_mutable is not None and len(sig.param_mutable) == len(sig.param_names):
			param_mutable = {pname: bool(flag) for pname, flag in zip(sig.param_names, sig.param_mutable)}
		result = checker.check_function(
			fn_id,
			hir,
			param_types=param_types,
			param_mutable=param_mutable,
			return_type=sig.return_type_id,
			signatures_by_id=signatures_by_id,
			linked_world=linked_world,
			require_env=require_env,
			current_module=0,
			visibility_imports=set(module_deps.get(sig.module or fn_id.module, set())),
		)
		messages.extend(d.message for d in result.diagnostics)
	return messages


def test_move_var_param_allowed(tmp_path: Path) -> None:
	diags = _typecheck_workspace(
		tmp_path,
		"""
module main

struct File { fd: Int }

fn id(var x: File) -> File {
	return move x;
}
""",
	)
	assert diags == []


def test_move_val_param_allowed(tmp_path: Path) -> None:
	diags = _typecheck_workspace(
		tmp_path,
		"""
module main

struct File { fd: Int }

fn id(x: File) -> File {
	return move x;
}
""",
	)
	assert diags == []


def test_move_self_allowed(tmp_path: Path) -> None:
	diags = _typecheck_workspace(
		tmp_path,
		"""
module main

struct Token { id: Int }

implement Token {
	fn into(self: Token) -> Token {
		return move self;
	}
}
""",
	)
	assert diags == []
