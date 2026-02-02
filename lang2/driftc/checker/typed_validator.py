# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from lang2.driftc.stage1 import hir_nodes as H
from lang2.driftc.stage1.call_info import CallInfo, CallTargetKind, IntrinsicKind
from lang2.driftc.checker.unsafe_gate import check_unsafe_call
from lang2.driftc.core.types_core import TypeKind


@dataclass
class TypedValidationResult:
	diagnostics: list


def validate_typed_hir(root: H.HNode, *, call_info_by_callsite_id: Mapping[int, CallInfo] | None, expr_types: Mapping[int, int] | None, type_table, tc_diag, current_module_name: str | None = None, unsafe_trusted_modules: set[str] | None = None, mir_bound: bool = False) -> TypedValidationResult:
	diagnostics: list = []
	allowed_qmem_nodes: set[int] = set()
	trusted_modules: set[str] = set(m for m in (unsafe_trusted_modules or set()) if isinstance(m, str))
	rawbuffer_intrinsics = {
		IntrinsicKind.RAW_ALLOC,
		IntrinsicKind.RAW_DEALLOC,
		IntrinsicKind.RAW_PTR_AT_REF,
		IntrinsicKind.RAW_PTR_AT_MUT,
		IntrinsicKind.RAW_WRITE,
		IntrinsicKind.RAW_READ,
	}

	def _iter_expr_children(e: H.HExpr) -> list[H.HExpr]:
		children: list[H.HExpr] = []
		for field_name in getattr(e, "__dataclass_fields__", {}) or {}:
			val = getattr(e, field_name, None)
			if isinstance(val, H.HExpr):
				children.append(val)
			elif isinstance(val, list):
				for item in val:
					if isinstance(item, H.HExpr):
						children.append(item)
		return children

	def _scan_calls(node: H.HNode) -> None:
		if isinstance(node, H.HCall):
			if isinstance(node.fn, H.HQualifiedMember):
				if isinstance(call_info_by_callsite_id, Mapping) and isinstance(getattr(node, "callsite_id", None), int) and node.callsite_id in call_info_by_callsite_id:
					allowed_qmem_nodes.add(node.fn.node_id)
		if isinstance(node, H.HTypeApp) and isinstance(getattr(node, "fn", None), H.HQualifiedMember):
			if isinstance(call_info_by_callsite_id, Mapping):
				allowed_qmem_nodes.add(node.fn.node_id)
		if isinstance(node, H.HBlock):
			for stmt in node.statements:
				_scan_calls(stmt)
			return
		if hasattr(H, "HUnsafeBlock") and isinstance(node, getattr(H, "HUnsafeBlock")):
			_scan_calls(node.block)
			return
		for field_name in getattr(node, "__dataclass_fields__", {}) or {}:
			val = getattr(node, field_name, None)
			if isinstance(val, (H.HExpr, H.HBlock)):
				_scan_calls(val)
			elif isinstance(val, list):
				for item in val:
					if isinstance(item, (H.HExpr, H.HBlock)):
						_scan_calls(item)

	def _walk_node(node: H.HNode) -> None:
		if isinstance(node, H.HExpr):
			expr_type_id = None
			if expr_types is not None and isinstance(getattr(node, "node_id", None), int):
				expr_type_id = expr_types.get(node.node_id)
			is_function_expr = False
			if expr_type_id is not None and type_table is not None:
				try:
					is_function_expr = type_table.get(expr_type_id).kind is TypeKind.FUNCTION
				except Exception:
					is_function_expr = False
			if isinstance(node, H.HTypeApp):
				if isinstance(getattr(node, "fn", None), H.HQualifiedMember):
					allowed_qmem_nodes.add(node.fn.node_id)
				if not is_function_expr:
					diagnostics.append(tc_diag(message="internal: HTypeApp survived typed mode (checker bug)", severity="error", span=getattr(node, "loc", None)))
			if isinstance(node, H.HQualifiedMember) and node.node_id not in allowed_qmem_nodes:
				allow_qmem = False
				if expr_type_id is not None and type_table is not None:
					try:
						td = type_table.get(expr_type_id)
						inst = None
						if td.kind is TypeKind.VARIANT_INSTANCE:
							inst = type_table.get_variant_instance(expr_type_id)
						elif td.kind is TypeKind.VARIANT:
							inst = type_table.get_variant_instance(expr_type_id)
						if inst is not None:
							arm = inst.arms_by_name.get(getattr(node, "member", ""))
							if arm is not None and not arm.field_types:
								allow_qmem = True
					except Exception:
						allow_qmem = False
				if not allow_qmem and type_table is not None:
					base_te = getattr(node, "base_type_expr", None)
					base_name = getattr(base_te, "name", None) if base_te is not None else None
					if isinstance(base_name, str):
						module_id = getattr(base_te, "module_id", None) or current_module_name
						try:
							base_tid = type_table.get_variant_base(module_id=module_id, name=base_name)
						except Exception:
							base_tid = None
						if base_tid is not None:
							try:
								schema = type_table.get_variant_schema(base_tid)
								arm = next((a for a in schema.arms if a.name == getattr(node, "member", "")), None)
								if arm is not None and not arm.fields:
									allow_qmem = True
							except Exception:
								pass
				if not is_function_expr and not allow_qmem:
					diagnostics.append(tc_diag(message="internal: HQualifiedMember survived typed mode (checker bug)", severity="error", span=getattr(node, "loc", None)))
			if getattr(node, "kwargs", None):
				diagnostics.append(tc_diag(message="internal: kwargs survived typed mode (checker bug)", severity="error", span=getattr(node, "loc", None)))
			if isinstance(node, (H.HCall, H.HMethodCall, H.HInvoke)):
				csid = getattr(node, "callsite_id", None)
				if not isinstance(call_info_by_callsite_id, Mapping):
					diagnostics.append(tc_diag(message="internal: missing CallInfo map for typed validation (checker bug)", severity="error", span=getattr(node, "loc", None)))
				elif not isinstance(csid, int) or csid not in call_info_by_callsite_id:
					diagnostics.append(tc_diag(message="internal: missing CallInfo for typed call (checker bug)", severity="error", span=getattr(node, "loc", None)))
				else:
					call_info = call_info_by_callsite_id.get(csid)
					if call_info is not None and call_info.target.kind is CallTargetKind.INTRINSIC and call_info.target.intrinsic in rawbuffer_intrinsics:
						check_unsafe_call(allow_unsafe=False, allow_unsafe_without_block=False, unsafe_context=False, trusted_module=bool(isinstance(current_module_name, str) and current_module_name in trusted_modules), rawbuffer_only=True, diagnostics=diagnostics, tc_diag=tc_diag, span=getattr(node, "loc", None))
					if call_info is not None and mir_bound and call_info.target.kind is CallTargetKind.TRAIT:
						msg = "internal: call resolved to trait target in typed mode (checker bug)"
						if isinstance(node, H.HMethodCall):
							msg = "internal: method call resolved to trait target in typed mode (checker bug)"
						diagnostics.append(tc_diag(message=msg, severity="error", span=getattr(node, "loc", None)))
			for child in _iter_expr_children(node):
				_walk_node(child)
			return
		if isinstance(node, H.HBlock):
			for stmt in node.statements:
				_walk_node(stmt)
			return
		if hasattr(H, "HUnsafeBlock") and isinstance(node, getattr(H, "HUnsafeBlock")):
			_walk_node(node.block)
			return
		for field_name in getattr(node, "__dataclass_fields__", {}) or {}:
			val = getattr(node, field_name, None)
			if isinstance(val, (H.HExpr, H.HBlock)):
				_walk_node(val)
			elif isinstance(val, list):
				for item in val:
					if isinstance(item, (H.HExpr, H.HBlock)):
						_walk_node(item)

	_scan_calls(root)
	_walk_node(root)
	return TypedValidationResult(diagnostics=diagnostics)
