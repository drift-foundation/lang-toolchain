# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.types_core import TypeKind, TypeTable
from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.type_checker import TypeChecker
from lang.driftc.stage1.call_info import CallSig, call_abi_ret_type


def test_call_abi_error_type_is_canonical() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()

	sig_a = CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=True)
	sig_b = CallSig(param_types=(), user_ret_type=int_ty, can_throw=True)

	ret_a = call_abi_ret_type(sig_a, table)
	ret_b = call_abi_ret_type(sig_b, table)

	assert ret_a == ret_b
	td = table.get(ret_a)
	assert td.kind is TypeKind.FNRESULT
	assert td.param_types and td.param_types[1] == table.ensure_error()


def test_call_abi_error_type_is_canonical_across_modules(tmp_path: Path) -> None:
	mod_a = tmp_path / "a.drift"
	mod_b = tmp_path / "b.drift"
	mod_a.write_text(
		"""
module a;
fn boom() -> Int { return 1; }
fn main() -> Int { return boom(); }
""".lstrip()
	)
	mod_b.write_text(
		"""
module b;
fn boom() -> Int { return 2; }
fn main() -> Int { return boom(); }
""".lstrip()
	)

	modules, table, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir(
		[mod_a, mod_b],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	func_hirs, sigs, _fn_ids = flatten_modules(modules)

	registry = CallableRegistry()
	next_callable_id = 1
	module_ids: dict[str, int] = {}
	next_mod_id = 0
	for fn_id, sig in sigs.items():
		if sig.is_method:
			continue
		mod_name = fn_id.module or "main"
		mod_id = module_ids.get(mod_name)
		if mod_id is None:
			mod_id = next_mod_id
			module_ids[mod_name] = mod_id
			next_mod_id += 1
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		registry.register_free_function(
			callable_id=next_callable_id,
			name=fn_id.name,
			module_id=mod_id,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
			fn_id=fn_id,
		)
		next_callable_id += 1
		if fn_id.name == "boom":
			sig.declared_can_throw = True

	checker = TypeChecker(type_table=table)
	call_infos = []
	for fn_id, block in func_hirs.items():
		if fn_id.name != "main":
			continue
		normalized = normalize_hir(block)
		sig = sigs.get(fn_id)
		result = checker.check_function(
			fn_id,
			normalized,
			param_types={},
			return_type=sig.return_type_id if sig is not None else None,
			signatures_by_id=sigs,
			callable_registry=registry,
			current_module=module_ids.get(fn_id.module or "main", 0),
			visible_modules=(module_ids.get(fn_id.module or "main", 0),),
		)
		call_infos.extend(result.typed_fn.call_info_by_callsite_id.values())

	assert len(call_infos) == 2
	ret_ids = [call_abi_ret_type(info.sig, table) for info in call_infos]
	assert ret_ids[0] == ret_ids[1]
	ret_def = table.get(ret_ids[0])
	assert ret_def.kind is TypeKind.FNRESULT
	assert ret_def.param_types and ret_def.param_types[1] == table.ensure_error()
