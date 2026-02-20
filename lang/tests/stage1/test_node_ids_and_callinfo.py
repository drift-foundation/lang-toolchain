# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId, fn_name_key
from lang.driftc.core.types_core import TypeTable, TypeKind
from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.stage1.call_info import CallTargetKind, call_abi_ret_type
from lang.driftc.stage1.node_ids import assign_node_ids, assign_callsite_ids, validate_callsite_ids
from lang.driftc.type_checker import TypeChecker


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _dump_node_ids(block: H.HBlock) -> list[str]:
	assign_node_ids(block)
	nodes: list[tuple[int, str]] = []

	def walk(obj: object) -> None:
		if isinstance(obj, H.HNode):
			nodes.append((obj.node_id, type(obj).__name__))
		for field in getattr(obj, "__dataclass_fields__", {}) or {}:
			val = getattr(obj, field, None)
			if isinstance(val, list):
				for item in val:
					walk(item)
			else:
				walk(val)

	walk(block)
	nodes.sort(key=lambda pair: pair[0])
	return [f"{nid}:{name}" for nid, name in nodes]


def _collect_node_ids(block: H.HBlock) -> set[int]:
	assign_node_ids(block)
	ids: set[int] = set()

	def walk(obj: object) -> None:
		if isinstance(obj, H.HNode):
			ids.add(obj.node_id)
		for field in getattr(obj, "__dataclass_fields__", {}) or {}:
			val = getattr(obj, field, None)
			if isinstance(val, list):
				for item in val:
					walk(item)
			else:
				walk(val)

	walk(block)
	return ids


def _collect_callsite_ids(block: H.HBlock) -> list[int]:
	assign_callsite_ids(block)
	ids: list[int] = []

	def walk(obj: object) -> None:
		if isinstance(obj, (H.HCall, H.HMethodCall, H.HInvoke)):
			callsite_id = getattr(obj, "callsite_id", None)
			if callsite_id is not None:
				ids.append(int(callsite_id))
		for field in getattr(obj, "__dataclass_fields__", {}) or {}:
			val = getattr(obj, field, None)
			if isinstance(val, list):
				for item in val:
					walk(item)
			else:
				walk(val)

	walk(block)
	return ids


def _collect_direct_calls(block: H.HBlock) -> list[H.HCall]:
	calls: list[H.HCall] = []

	def walk_expr(expr: H.HExpr) -> None:
		if isinstance(expr, H.HCall):
			calls.append(expr)
			walk_expr(expr.fn)
			for arg in expr.args:
				walk_expr(arg)
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
	return [c for c in calls if isinstance(c.fn, H.HVar)]


def _collect_invokes(block: H.HBlock) -> list[H.HInvoke]:
	invokes: list[H.HInvoke] = []

	def walk_expr(expr: H.HExpr) -> None:
		if isinstance(expr, H.HInvoke):
			invokes.append(expr)
			walk_expr(expr.callee)
			for arg in expr.args:
				walk_expr(arg)
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
	return invokes


def test_callsite_ids_assigned_and_dense() -> None:
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HCall(fn=H.HVar("foo"), args=[H.HLiteralInt(1)])),
			H.HExprStmt(expr=H.HMethodCall(receiver=H.HVar("x"), method_name="bar", args=[])),
			H.HReturn(value=H.HInvoke(callee=H.HVar("f"), args=[H.HLiteralInt(2)])),
		]
	)
	ids = sorted(_collect_callsite_ids(block))
	assert ids == [0, 1, 2]
	assign_callsite_ids(block)
	validate_callsite_ids(block)


def test_node_ids_deterministic(tmp_path: Path) -> None:
	path = tmp_path / "main.drift"
	_write_file(
		path,
		"""
fn foo(x: Int) -> Int { return x + 1; }
fn bar() -> Int { return foo(1) + foo(2); }
""",
	)
	mod1, _table1, _excs1, _diags1 = parse_drift_to_hir(path)
	mod2, _table2, _excs2, _diags2 = parse_drift_to_hir(path)
	block1 = next(iter(mod1.func_hirs.values()))
	block2 = next(iter(mod2.func_hirs.values()))
	assert _dump_node_ids(block1) == _dump_node_ids(block2)


def test_typed_tables_keyed_by_node_id() -> None:
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), binding_id=1),
			H.HExprStmt(expr=H.HCall(fn=H.HVar("f"), args=[H.HVar("x", binding_id=1)])),
		]
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	fn_id_f = FunctionId(module="main", name="f", ordinal=0)
	sig = FnSignature(name="f", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False)
	registry = CallableRegistry()
	registry.register_free_function(
		callable_id=1,
		name="f",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=int_ty),
		fn_id=fn_id_f,
	)
	res = tc.check_function(
		fn_id,
		block,
		callable_registry=registry,
		signatures_by_id={fn_id_f: sig},
		visible_modules=(0,),
	)
	node_ids = _collect_node_ids(block)
	callsite_ids = set(_collect_callsite_ids(block))
	for key in res.typed_fn.expr_types.keys():
		assert key in node_ids
	for key in res.typed_fn.binding_for_var.keys():
		assert key in node_ids
	for key in res.typed_fn.call_resolutions.keys():
		assert key in node_ids
	for key in res.typed_fn.call_info_by_callsite_id.keys():
		assert key in callsite_ids


def test_call_info_emitted_for_direct_calls() -> None:
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HCall(fn=H.HVar("abs"), args=[H.HLiteralInt(1)])),
			H.HExprStmt(
				expr=H.HCall(
					fn=H.HVar("foo"),
					args=[H.HCall(fn=H.HVar("bar"), args=[H.HLiteralInt(2)])],
				)
			),
		]
	)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	fn_id_abs = FunctionId(module="main", name="abs", ordinal=0)
	fn_id_foo = FunctionId(module="main", name="foo", ordinal=0)
	fn_id_bar = FunctionId(module="main", name="bar", ordinal=0)
	signatures_by_id = {
		fn_id_abs: FnSignature(name="abs", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False),
		fn_id_foo: FnSignature(name="foo", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False),
		fn_id_bar: FnSignature(name="bar", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False),
	}
	registry = CallableRegistry()
	registry.register_free_function(
		callable_id=1,
		name="abs",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=int_ty),
		fn_id=fn_id_abs,
	)
	registry.register_free_function(
		callable_id=2,
		name="foo",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=int_ty),
		fn_id=fn_id_foo,
	)
	registry.register_free_function(
		callable_id=3,
		name="bar",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=int_ty),
		fn_id=fn_id_bar,
	)
	res = tc.check_function(
		fn_id,
		block,
		callable_registry=registry,
		signatures_by_id=signatures_by_id,
		visible_modules=(0,),
	)
	calls = _collect_direct_calls(block)
	assert calls
	expected_ids = {
		"abs": fn_id_abs,
		"foo": fn_id_foo,
		"bar": fn_id_bar,
	}
	for call in calls:
		info = res.typed_fn.call_info_by_callsite_id.get(call.callsite_id)
		assert info is not None
		assert info.target.kind is CallTargetKind.DIRECT
		assert info.target.symbol == expected_ids.get(call.fn.name)


def test_call_info_duplicate_callsite_ids_are_reassigned_per_call_node() -> None:
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	call_foo = H.HCall(fn=H.HVar("foo"), args=[H.HLiteralInt(1)], callsite_id=15)
	call_bar = H.HCall(fn=H.HVar("bar"), args=[H.HLiteralInt(1), H.HLiteralInt(2), H.HLiteralInt(3)], callsite_id=15)
	block = H.HBlock(statements=[H.HExprStmt(expr=call_foo), H.HExprStmt(expr=call_bar)])
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	fn_id_foo = FunctionId(module="main", name="foo", ordinal=0)
	fn_id_bar = FunctionId(module="main", name="bar", ordinal=0)
	signatures_by_id = {
		fn_id_foo: FnSignature(name="foo", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False),
		fn_id_bar: FnSignature(name="bar", param_type_ids=[int_ty, int_ty, int_ty], return_type_id=int_ty, declared_can_throw=False),
	}
	registry = CallableRegistry()
	registry.register_free_function(callable_id=1, name="foo", module_id=0, visibility=Visibility.public(), signature=CallableSignature(param_types=(int_ty,), result_type=int_ty), fn_id=fn_id_foo)
	registry.register_free_function(callable_id=2, name="bar", module_id=0, visibility=Visibility.public(), signature=CallableSignature(param_types=(int_ty, int_ty, int_ty), result_type=int_ty), fn_id=fn_id_bar)
	res = tc.check_function(fn_id, block, callable_registry=registry, signatures_by_id=signatures_by_id, visible_modules=(0,))
	assert not any(d.severity == "error" for d in res.diagnostics), [d.message for d in res.diagnostics]
	calls = _collect_direct_calls(block)
	assert len(calls) == 2
	callsite_ids = [int(getattr(c, "callsite_id", -1)) for c in calls]
	assert len(set(callsite_ids)) == 2
	for call in calls:
		info = res.typed_fn.call_info_by_callsite_id.get(call.callsite_id)
		assert info is not None
		assert len(info.sig.param_types) == len(call.args)


def test_call_info_emitted_for_invoke() -> None:
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	fn_ty = table.ensure_function([int_ty], int_ty, can_throw=False)
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HInvoke(callee=H.HVar("fp"), args=[H.HLiteralInt(1)])),
		]
	)
	assign_callsite_ids(block)
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	res = tc.check_function(fn_id, block, param_types={"fp": fn_ty})
	invokes = _collect_invokes(block)
	assert invokes
	for call in invokes:
		info = res.typed_fn.call_info_by_callsite_id.get(call.callsite_id)
		assert info is not None
		assert info.target.kind is CallTargetKind.INDIRECT


def test_invoke_rejects_kwargs() -> None:
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	fn_ty = table.ensure_function([int_ty], int_ty, can_throw=False)
	call = H.HInvoke(
		callee=H.HVar("fp"),
		args=[],
		kwargs=[H.HKwArg(name="x", value=H.HLiteralInt(1))],
	)
	block = H.HBlock(statements=[H.HExprStmt(expr=call)])
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	res = tc.check_function(fn_id, block, param_types={"fp": fn_ty})
	assert res.diagnostics


def test_call_sig_abi_return_type() -> None:
	table = TypeTable()
	tc = TypeChecker(table)
	int_ty = table.ensure_int()
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HCall(fn=H.HVar("nothrow"), args=[H.HLiteralInt(1)])),
			H.HExprStmt(expr=H.HCall(fn=H.HVar("may_throw"), args=[H.HLiteralInt(2)])),
		]
	)
	fn_id_nothrow = FunctionId(module="main", name="nothrow", ordinal=0)
	fn_id_throw = FunctionId(module="main", name="may_throw", ordinal=0)
	signatures_by_id = {
		fn_id_nothrow: FnSignature(name="nothrow", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=False),
		fn_id_throw: FnSignature(name="may_throw", param_type_ids=[int_ty], return_type_id=int_ty, declared_can_throw=True),
	}
	registry = CallableRegistry()
	registry.register_free_function(
		callable_id=1,
		name="nothrow",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=int_ty),
		fn_id=fn_id_nothrow,
	)
	registry.register_free_function(
		callable_id=2,
		name="may_throw",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=int_ty),
		fn_id=fn_id_throw,
	)
	res = tc.check_function(
		fn_id,
		block,
		callable_registry=registry,
		signatures_by_id=signatures_by_id,
		visible_modules=(0,),
	)
	calls = {call.fn.name: call for call in _collect_direct_calls(block)}
	info_no = res.typed_fn.call_info_by_callsite_id[calls["nothrow"].callsite_id]
	info_yes = res.typed_fn.call_info_by_callsite_id[calls["may_throw"].callsite_id]
	assert call_abi_ret_type(info_no.sig, table) == int_ty
	abi_yes = call_abi_ret_type(info_yes.sig, table)
	assert table.get(abi_yes).kind is TypeKind.FNRESULT
	assert abi_yes == table.ensure_fnresult(int_ty, table.ensure_error())
