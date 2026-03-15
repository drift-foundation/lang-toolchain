# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import fn_name_key
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.type_checker import TypeChecker
from lang.driftc.traits.enforce import collect_used_type_keys, enforce_struct_requires, enforce_fn_requires


# Known inline diagnostic patterns emitted by the type checker for trait-
# enforcement scenarios. These are expected when require clauses are active;
# post-hoc enforcement tests inspect the same behaviour through a separate API.
_INLINE_TRAIT_DIAG_PATTERNS = (
	"no matching overload",
	"requirement not satisfied",
)


def _typecheck_all(src: Path):
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	linked_world, require_env = build_linked_world(type_table)
	type_checker = TypeChecker(type_table=type_table)
	typed_fns = {}
	call_signatures = {
		fn_name_key(fid.module, fid.name): s
		for fid, s in module.signatures_by_id.items()
		if not s.is_method
	}
	for fn_id, hir_block in module.func_hirs.items():
		sig = module.signatures_by_id.get(fn_id)
		param_types = {}
		if sig and sig.param_names and sig.param_type_ids:
			param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids) if pty is not None}
		result = type_checker.check_function(
			fn_id,
			hir_block,
			param_types=param_types,
			return_type=sig.return_type_id if sig is not None else None,
			callable_registry=None,
			linked_world=linked_world,
			require_env=require_env,
			visible_modules=(),
			current_module=0,
		)
		# The type checker now emits inline trait-requirement diagnostics
		# (e.g. "no matching overload", "requirement not satisfied").
		# Filter those out; assert no other unexpected diagnostics leaked.
		unexpected = [
			d for d in result.diagnostics
			if not any(pat in d.message for pat in _INLINE_TRAIT_DIAG_PATTERNS)
		]
		assert unexpected == [], (
			f"unexpected diagnostics from type checker for {fn_id.name}: "
			f"{[d.message for d in unexpected]}"
		)
		typed_fns[fn_id] = result.typed_fn
	return module.func_hirs, module.signatures_by_id, type_table, typed_fns


def test_struct_require_enforced_when_used(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
trait A { fn a(self: Int) -> Int }
struct S require Self is A { }
fn main(x: S) -> Int { return 0; }
"""
	)
	_func_hirs, sigs, type_table, typed_fns = _typecheck_all(src)
	linked_world, require_env = build_linked_world(type_table)
	used = collect_used_type_keys(typed_fns, type_table, sigs)
	res = enforce_struct_requires(linked_world, require_env, used, module_name="main")
	assert any("trait requirements not met for struct" in d.message for d in res.diagnostics)


def test_struct_require_satisfied(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
trait A { fn a(self: Int) -> Int }
struct S require Self is A { }
implement A for S { fn a(self: S) -> Int { return 0; } }
fn main(x: S) -> Int { return 0; }
"""
	)
	_func_hirs, sigs, type_table, typed_fns = _typecheck_all(src)
	linked_world, require_env = build_linked_world(type_table)
	used = collect_used_type_keys(typed_fns, type_table, sigs)
	res = enforce_struct_requires(linked_world, require_env, used, module_name="main")
	assert res.diagnostics == []


def test_fn_require_enforced_at_call(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
trait A { fn a(self: Int) -> Int }
struct S { }
fn f<T>(x: T) -> Int require T is A { return 0; }
fn main(x: S) -> Int { return f(x); }
"""
	)
	_func_hirs, sigs, type_table, typed_fns = _typecheck_all(src)
	linked_world, require_env = build_linked_world(type_table)
	main_fn = next(fid for fid in typed_fns if fid.name == "main")
	res = enforce_fn_requires(
		linked_world,
		require_env,
		typed_fns[main_fn],
		type_table,
		module_name="main",
		signatures=sigs,
	)
	assert any("trait requirements not met for call" in d.message for d in res.diagnostics)


def test_fn_require_satisfied_at_call(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
trait A { fn a(self: Int) -> Int }
struct S { }
implement A for S { fn a(self: S) -> Int { return 0; } }
fn f<T>(x: T) -> Int require T is A { return 0; }
fn main(x: S) -> Int { return f(x); }
"""
	)
	_func_hirs, sigs, type_table, typed_fns = _typecheck_all(src)
	linked_world, require_env = build_linked_world(type_table)
	main_fn = next(fid for fid in typed_fns if fid.name == "main")
	res = enforce_fn_requires(
		linked_world,
		require_env,
		typed_fns[main_fn],
		type_table,
		module_name="main",
		signatures=sigs,
	)
	assert res.diagnostics == []
