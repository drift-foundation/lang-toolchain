# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang2.driftc import stage1 as H
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
from lang2.driftc.parser import parse_drift_workspace_to_hir
from lang2.driftc.type_checker import TypeChecker


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _callable_name(fn_id: FunctionId) -> str:
	return fn_id.name if fn_id.module == "main" else f"{fn_id.module}::{fn_id.name}"


def _build_registry(signatures: dict[FunctionId, object]) -> tuple[CallableRegistry, dict[object, int]]:
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in signatures.items():
		if sig.is_method:
			continue
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		module_id = module_ids.setdefault(sig.module, len(module_ids))
		registry.register_free_function(
			callable_id=next_id,
			name=_callable_name(fn_id),
			module_id=module_id,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
			fn_id=fn_id,
			is_generic=False,
		)
		next_id += 1
	return registry, module_ids


def _visible_modules_for(
	module_name: str, module_deps: dict[str, set[str]], module_ids: dict[object, int]
) -> tuple[int, ...]:
	visible = set(module_deps.get(module_name, set()))
	visible.add(module_name)
	return tuple(sorted(module_ids.get(mod, 0) for mod in visible))


def _collect_calls(block: H.HBlock) -> list[H.HCall]:
	calls: list[H.HCall] = []

	def walk_expr(expr: H.HExpr) -> None:
		if isinstance(expr, H.HCall):
			calls.append(expr)
			walk_expr(expr.fn)
			for a in expr.args:
				walk_expr(a)
			for kw in getattr(expr, "kwargs", []) or []:
				if getattr(kw, "value", None) is not None:
					walk_expr(kw.value)
			return
		for child in getattr(expr, "__dict__", {}).values():
			if isinstance(child, H.HExpr):
				walk_expr(child)
			elif isinstance(child, H.HBlock):
				walk_block(child)
			elif isinstance(child, list):
				for it in child:
					if isinstance(it, H.HExpr):
						walk_expr(it)
					elif isinstance(it, H.HBlock):
						walk_block(it)

	def walk_block(b: H.HBlock) -> None:
		for st in b.statements:
			if isinstance(st, H.HExprStmt):
				walk_expr(st.expr)
			elif isinstance(st, H.HReturn) and st.value is not None:
				walk_expr(st.value)
			else:
				for child in getattr(st, "__dict__", {}).values():
					if isinstance(child, H.HExpr):
						walk_expr(child)
					elif isinstance(child, H.HBlock):
						walk_block(child)
					elif isinstance(child, list):
						for it in child:
							if isinstance(it, H.HExpr):
								walk_expr(it)
							elif isinstance(it, H.HBlock):
								walk_block(it)

	walk_block(block)
	return calls


def _find_fn_id_by_param_type(signatures: dict[FunctionId, object], *, name: str, param_type_id: int) -> FunctionId:
	for fn_id, sig in signatures.items():
		if fn_id.name != name:
			continue
		if sig.param_type_ids and list(sig.param_type_ids) == [param_type_id]:
			return fn_id
	raise AssertionError(f"missing overload for {name}({param_type_id})")


def test_overloads_across_modules_with_qualified_calls(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "a" / "lib.drift",
		"""
module a

export { f }

pub fn f(x: Int) returns Int { return x + 1; }
pub fn f(x: String) returns Int { return 2; }
""",
	)
	_write_file(
		mod_root / "b" / "main.drift",
		"""
module b

import a

fn main() returns Int {
    val r1: Int = a.f(1);
    val r2: Int = a.f("hi");
    return r1 + r2;
}
""",
	)
	paths = [mod_root / "a" / "lib.drift", mod_root / "b" / "main.drift"]
	func_hirs, signatures, fn_ids_by_name, type_table, _exc_catalog, _exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
	)
	assert diagnostics == []
	registry, module_ids = _build_registry(signatures)
	main_ids = fn_ids_by_name.get("b::main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = _visible_modules_for("b", module_deps, module_ids)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		visible_modules=visible_mods,
		current_module=current_mod,
	)
	assert result.diagnostics == []
	calls = _collect_calls(main_block)
	assert len(calls) >= 2
	int_ty = type_table.ensure_int()
	string_ty = type_table.ensure_string()
	f_int = _find_fn_id_by_param_type(signatures, name="f", param_type_id=int_ty)
	f_str = _find_fn_id_by_param_type(signatures, name="f", param_type_id=string_ty)
	seen_int = False
	seen_str = False
	for call in calls:
		decl = result.typed_fn.call_resolutions.get(id(call))
		if decl is None or decl.fn_id is None:
			continue
		arg = call.args[0] if call.args else None
		if isinstance(arg, H.HLiteralInt):
			assert decl.fn_id == f_int
			seen_int = True
		if isinstance(arg, H.HLiteralString):
			assert decl.fn_id == f_str
			seen_str = True
	assert seen_int and seen_str
