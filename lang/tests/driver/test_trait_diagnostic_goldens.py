# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import FunctionId
from lang.driftc.impl_index import GlobalImplIndex
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex
from lang.driftc.type_checker import TypeChecker


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _build_registry(signatures: dict[FunctionId, object]) -> tuple[CallableRegistry, dict[object, int]]:
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in signatures.items():
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		module_id = module_ids.setdefault(sig.module, len(module_ids))
		if sig.is_method:
			if sig.impl_target_type_id is None or sig.self_mode is None:
				continue
			self_mode = {
				"value": SelfMode.SELF_BY_VALUE,
				"ref": SelfMode.SELF_BY_REF,
				"ref_mut": SelfMode.SELF_BY_REF_MUT,
			}.get(sig.self_mode)
			if self_mode is None:
				continue
			registry.register_inherent_method(
				callable_id=next_id,
				name=sig.method_name or sig.name,
				module_id=module_id,
				visibility=Visibility.public() if sig.is_pub else Visibility.private(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
				fn_id=fn_id,
				impl_id=next_id,
				impl_target_type_id=sig.impl_target_type_id,
				self_mode=self_mode,
				is_generic=bool(sig.type_params or getattr(sig, "impl_type_params", [])),
			)
		else:
			callable_name = fn_id.name if fn_id.module == "main" else f"{fn_id.module}::{fn_id.name}"
			registry.register_free_function(
				callable_id=next_id,
				name=callable_name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
				fn_id=fn_id,
				is_generic=bool(sig.type_params),
			)
		next_id += 1
	return registry, module_ids


def _visible_modules_for(module_name: str, module_deps: dict[str, set[str]], module_ids: dict[object, int]) -> tuple[int, ...]:
	visible = set(module_deps.get(module_name, set()))
	visible.add(module_name)
	return tuple(sorted(module_ids.setdefault(mod, len(module_ids)) for mod in visible))


def _typecheck_named_fn(
	tmp_path: Path,
	files: dict[Path, str],
	*,
	module_name: str,
	fn_name: str,
) -> object:
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	origin_by_fn_id: dict[FunctionId, Path] = {}
	for mod in modules.values():
		origin_by_fn_id.update(mod.origin_by_fn_id)
	registry, module_ids = _build_registry(signatures)
	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	trait_index = GlobalTraitIndex.from_trait_worlds(getattr(type_table, "trait_worlds", None))
	trait_impl_index = GlobalTraitImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	trait_scope_by_module: dict[str, list[object]] = {}
	for _mod, exp in module_exports.items():
		if isinstance(exp, dict):
			scope = exp.get("trait_scope", [])
			if isinstance(scope, list):
				trait_scope_by_module[_mod] = list(scope)
	linked_world, require_env = build_linked_world(type_table)
	fn_key = f"{module_name}::{fn_name}"
	fn_ids = fn_ids_by_name.get(fn_key) or []
	if not fn_ids:
		fn_ids = fn_ids_by_name.get(fn_name) or []
	if not fn_ids:
		qualified = [name for name in fn_ids_by_name.keys() if name.endswith(f"::{fn_name}")]
		if len(qualified) == 1:
			fn_ids = fn_ids_by_name.get(qualified[0]) or []
	assert len(fn_ids) == 1
	fn_id = fn_ids[0]
	fn_block = func_hirs[fn_id]
	fn_sig = signatures.get(fn_id)
	param_types = {}
	if fn_sig and fn_sig.param_names and fn_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(fn_sig.param_names, fn_sig.param_type_ids)}
	visible_mods = _visible_modules_for(module_name, module_deps, module_ids)
	tc = TypeChecker(type_table=type_table)
	return tc.check_function(
		fn_id,
		fn_block,
		param_types=param_types,
		return_type=fn_sig.return_type_id if fn_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module=module_ids.setdefault(module_name, len(module_ids)),
	)


def _diag_snapshot(diags: list[object]) -> list[dict[str, object]]:
	return [
		{
			"code": getattr(d, "code", None),
			"message": d.message,
			"notes": list(d.notes or []),
			"phase": getattr(d, "phase", None),
		}
		for d in diags
	]


def test_golden_requirement_not_satisfied(tmp_path: Path) -> None:
	files = {
		Path("main.drift"): """
module main;

trait Show { fn show(self: Self) -> Int }

struct Box<T> require T is Show { value: T }

fn f(x: Box<Int>) -> Int{ return 0; }
""",
	}
	result = _typecheck_named_fn(tmp_path, files, module_name="main", fn_name="f")
	assert _diag_snapshot(result.diagnostics) == [
		{
			"code": "E_REQUIREMENT_NOT_SATISFIED",
			"message": "requirement not satisfied: Int is __local__::main.Show (required by struct 'Box')",
			"notes": [
				"requirement_trait=__local__::main.Show",
				"requirement_subject=Int",
				"requirement_reason=no_impl",
				"requirement_origin=struct 'Box'",
			],
			"phase": "typecheck",
		}
	]


def test_golden_guard_not_decidable(tmp_path: Path) -> None:
	files = {
		Path("main.drift"): """
module main;

trait Show { fn show(self: Self) -> Int }

pub fn main() nothrow -> Int{
	if Int is Show { return 1; } else { return 0; }
}
""",
	}
	result = _typecheck_named_fn(tmp_path, files, module_name="main", fn_name="main")
	assert _diag_snapshot(result.diagnostics) == [
		{
			"code": "E-TRAIT-GUARD-NOT-DECIDABLE",
			"message": "internal: trait guard is not decidable for a concrete type",
			"notes": [],
			"phase": "typecheck",
		}
	]


def test_golden_orphan_impl_parser_phase(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "m_main.drift",
		"""
module m_main;
import m_a;
import m_b;

implement m_a.TA for m_b.SB {
	pub fn f(self: m_b.SB) -> Int { return 1; }
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, _table, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		package_id="pkg_main",
		external_module_packages={"m_a": "pkg_a", "m_b": "pkg_b"},
		external_module_exports={
			"m_a": {"traits": ["TA"]},
			"m_b": {"types": {"structs": ["SB"]}},
		},
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert _diag_snapshot(diagnostics) == [
		{
			"code": "E-IMPL-ORPHAN",
			"message": (
				"orphan trait impl is not allowed: trait 'pkg_a::m_a.TA' and "
				"type 'pkg_b::m_b.SB' are outside the current package"
			),
			"notes": [],
			"phase": "parser",
		}
	]


def test_golden_overlapping_impls_parser_phase(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "m_main.drift",
		"""
module m_main;

trait Debug { fn fmt(self: Self) -> Int }
struct Box<T> { value: T }

implement Debug for Box<T> { fn fmt(self: Box<T>) -> Int { return 1; } }
implement Debug for Box<U> { fn fmt(self: Box<U>) -> Int { return 2; } }
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, _table, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert _diag_snapshot(diagnostics) == [
		{
			"code": "E-IMPL-OVERLAP",
			"message": "overlapping impls for trait '__local__::m_main.Debug' on '__local__::m_main.Box'",
			"notes": [],
			"phase": "parser",
		}
	]


def test_golden_trait_method_ambiguity_message(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;
pub struct Box<T> { pub value: T }
export { Box };
""",
		Path("m_a.drift"): """
module m_a;
import m_box;
pub trait Show { fn show(self: m_box.Box<Int>) -> Int }
export { Show };
implement Show for m_box.Box<Int> { pub fn show(self: m_box.Box<Int>) -> Int { return 1; } }
""",
		Path("m_b.drift"): """
module m_b;
import m_box;
pub trait Show { fn show(self: m_box.Box<Int>) -> Int }
export { Show };
implement Show for m_box.Box<Int> { pub fn show(self: m_box.Box<Int>) -> Int { return 2; } }
""",
		Path("main.drift"): """
module main;
import m_box;
import m_a;
import m_b;
use trait m_a.Show;
use trait m_b.Show;

pub fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.show();
}
""",
	}
	result = _typecheck_named_fn(tmp_path, files, module_name="main", fn_name="main")
	assert _diag_snapshot(result.diagnostics) == [
		{
			"code": "E-METHOD-AMBIGUOUS",
			"message": (
				"ambiguous method 'show' for receiver __local__::m_box.Box<Int> and args [];"
				" candidates from traits: __local__::m_a.Show@m_a, __local__::m_b.Show@m_b"
			),
			"notes": [],
			"phase": "typecheck",
		}
	]
