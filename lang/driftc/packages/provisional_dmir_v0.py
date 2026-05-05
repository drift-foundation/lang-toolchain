# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Provisional DMIR payload (v0).

This is an intentionally unstable, compiler-internal IR encoding used for
package artifacts.

Goals:
- deterministic JSON encoding (stable keys, stable ordering),
- carries declared HIR for all functions so the consumer compiles them
  through the standard pipeline (Option B: packages as distribution containers),
- explicit versioning so we can evolve the format without rewriting
  the package container format.
"""

from __future__ import annotations

import dataclasses
import os
import struct
from enum import Enum
from typing import Any, Mapping

from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId, function_id_to_obj, function_symbol, parse_function_symbol
from lang.driftc.core.types_core import TypeKind
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeDef, TypeId, TypeParamId, TypeTable
from lang.driftc.parser import ast as parser_ast
from lang.driftc.packages.dmir_pkg_v0 import canonical_json_bytes, sha256_hex
from lang.driftc.core.function_key import FunctionKey, function_key_to_obj
from lang.driftc.traits.world import TraitKey


def _float64_bits_hex(value: float) -> str:
	"""Encode a Python float as IEEE754 bits for deterministic JSON."""
	bits = struct.unpack("<Q", struct.pack("<d", value))[0]
	return f"0x{bits:016x}"


def _to_jsonable(obj: Any) -> Any:
	"""
	Convert an arbitrary compiler object into JSONable structures.

	Rules:
	- dataclasses become dicts with a `_type` discriminator,
	- Enums are encoded by `name`,
	- floats are encoded by their IEEE754 bits (hex string),
	- dict keys are converted to strings (and callers must sort when serializing).
	"""
	if obj is None or isinstance(obj, (bool, int, str)):
		return obj
	if isinstance(obj, float):
		return {"_float64": _float64_bits_hex(obj)}
	if isinstance(obj, Enum):
		return {"_enum": type(obj).__name__, "name": obj.name}
	if dataclasses.is_dataclass(obj):
		out: dict[str, Any] = {"_type": type(obj).__name__}
		for f in dataclasses.fields(obj):
			out[f.name] = _to_jsonable(getattr(obj, f.name))
		return out
	if isinstance(obj, (list, tuple)):
		return [_to_jsonable(x) for x in obj]
	if isinstance(obj, dict):
		return {str(k): _to_jsonable(v) for k, v in obj.items()}
	return {"_unsupported": type(obj).__name__, "repr": repr(obj)}


def _float64_from_bits_hex(text: str) -> float:
	"""Decode a float encoded by `_float64_bits_hex`."""
	if text.startswith("0x"):
		text = text[2:]
	bits = int(text, 16)
	return struct.unpack("<d", struct.pack("<Q", bits))[0]


def build_dataclass_registry(*modules: Any) -> dict[str, type]:
	"""
	Build a dataclass name -> class registry.

	This is used to reconstruct stage2 MIR nodes and other internal dataclasses from
	the provisional JSON encoding.

	Bare-name discriminator collisions across modules (e.g.
	``parser_ast.TypeNameRef`` vs ``stage0.ast.TypeNameRef``) are resolved
	last-wins, on purpose: callers register modules in dependency order so
	the canonical (typically wider, HIR-bearing) variant wins.  See
	``docs/refactor_triggers.md`` § "Promote DMIR ``_to_jsonable``
	discriminators to module-qualified names" for the standing structural
	fix; a 0.31.36 attempt at a runtime defensive check false-fired on the
	16+ legitimate parser/stage0 divergences and was reverted in 0.31.37.
	"""
	out: dict[str, type] = {}
	for mod in modules:
		for v in vars(mod).values():
			if dataclasses.is_dataclass(v):
				out[v.__name__] = v
	return out


def build_enum_registry(*modules: Any) -> dict[str, type[Enum]]:
	"""Build an Enum name -> class registry."""
	out: dict[str, type[Enum]] = {}
	for mod in modules:
		for v in vars(mod).values():
			if isinstance(v, type) and issubclass(v, Enum):
				out[v.__name__] = v
	return out


def from_jsonable(obj: Any, *, dataclasses_by_name: Mapping[str, type], enums_by_name: Mapping[str, type[Enum]]) -> Any:
	"""Reconstruct Python objects encoded by `_to_jsonable`."""
	if obj is None or isinstance(obj, (bool, int, str)):
		return obj
	if isinstance(obj, list):
		return [from_jsonable(x, dataclasses_by_name=dataclasses_by_name, enums_by_name=enums_by_name) for x in obj]
	if isinstance(obj, dict):
		if "_float64" in obj:
			return _float64_from_bits_hex(str(obj["_float64"]))
		if "_enum" in obj:
			enum_name = str(obj.get("_enum"))
			member_name = str(obj.get("name"))
			cls = enums_by_name.get(enum_name)
			if cls is None:
				raise ValueError(f"unknown enum '{enum_name}' in provisional payload")
			return cls[member_name]
		if "_type" in obj:
			type_name = str(obj.get("_type"))
			cls = dataclasses_by_name.get(type_name)
			if cls is None:
				raise ValueError(f"unknown dataclass '{type_name}' in provisional payload")
			kwargs: dict[str, Any] = {}
			for f in dataclasses.fields(cls):
				if f.name in obj:
					kwargs[f.name] = from_jsonable(obj[f.name], dataclasses_by_name=dataclasses_by_name, enums_by_name=enums_by_name)
			return cls(**kwargs)  # type: ignore[misc]
		return {str(k): from_jsonable(v, dataclasses_by_name=dataclasses_by_name, enums_by_name=enums_by_name) for k, v in obj.items()}
	return obj


_BUILTIN_TYPE_NAMES = {
	"Int",
	"Uint",
	"Uint64",
	"Int32",
	"Uint32",
	"Byte",
	"Bool",
	"Float",
	"String",
	"Void",
	"Error",
	"DiagnosticValue",
	"Array",
	"FnResult",
	"&",
	"&mut",
}


def encode_span(span: Span | None) -> dict[str, Any] | None:
	if span is None:
		return None
	if not isinstance(span, Span):
		span = Span.from_loc(span)
	if span.file is None and span.line is None and span.column is None and span.end_line is None and span.end_column is None:
		return None
	file = span.file
	if isinstance(file, str) and os.path.isabs(file):
		file = None
	return {
		"file": file,
		"line": span.line,
		"column": span.column,
		"end_line": span.end_line,
		"end_column": span.end_column,
	}


def decode_span(obj: Any) -> Span | None:
	if not isinstance(obj, dict):
		return None
	file = obj.get("file")
	line = obj.get("line")
	column = obj.get("column")
	end_line = obj.get("end_line")
	end_column = obj.get("end_column")
	if file is not None and not isinstance(file, str):
		return None
	if line is not None and not isinstance(line, int):
		return None
	if column is not None and not isinstance(column, int):
		return None
	if end_line is not None and not isinstance(end_line, int):
		return None
	if end_column is not None and not isinstance(end_column, int):
		return None
	return Span(file=file, line=line, column=column, end_line=end_line, end_column=end_column)


def typeid_to_type_expr(
	tid: TypeId | None,
	type_table: TypeTable,
	*,
	type_param_names: dict[TypeParamId, str] | None = None,
	export_aliases: dict[TypeId, tuple[str | None, str]] | None = None,
	_visited: frozenset[TypeId] | None = None,
) -> parser_ast.TypeExpr | None:
	"""
	Reconstruct a TypeExpr from a TypeId by walking the TypeDef graph.

	This is the inverse of resolve_opaque_type: given a TypeId produced during
	source compilation, reconstruct the symbolic TypeExpr that the consumer can
	resolve back to a host TypeId via resolve_opaque_type.

	Handles the full TypeDef shape space: nominals, generic instantiations,
	refs, arrays, fn types, FnResult, RawPtr, scalars, builtins, TypeVars,
	and aliases.
	"""
	if tid is None:
		return None
	if _visited is None:
		_visited = frozenset()
	if tid in _visited:
		return None  # cycle guard
	_visited = _visited | {tid}

	# Alias check: if this TypeId maps to a public alias, use the alias spelling.
	if export_aliases and tid in export_aliases:
		alias_mod, alias_name = export_aliases[tid]
		return parser_ast.TypeExpr(name=alias_name, args=[], module_alias=None, module_id=alias_mod, loc=None)

	try:
		td = type_table.get(tid)
	except (KeyError, IndexError):
		return None

	kind = td.kind

	def _recurse(child_tid: TypeId) -> parser_ast.TypeExpr | None:
		return typeid_to_type_expr(
			child_tid, type_table,
			type_param_names=type_param_names,
			export_aliases=export_aliases,
			_visited=_visited,
		)

	# --- TypeVar ---
	if kind is TypeKind.TYPEVAR:
		name = td.name
		if type_param_names and td.type_param_id is not None and td.type_param_id in type_param_names:
			name = type_param_names[td.type_param_id]
		return parser_ast.TypeExpr(name=name, args=[], module_alias=None, module_id=None, loc=None)

	# --- Builtins / scalars ---
	if kind is TypeKind.VOID:
		return parser_ast.TypeExpr(name="Void", args=[], module_alias=None, module_id=None, loc=None)
	if kind is TypeKind.ERROR:
		return parser_ast.TypeExpr(name="Error", args=[], module_alias=None, module_id=None, loc=None)
	if kind is TypeKind.DIAGNOSTICVALUE:
		return parser_ast.TypeExpr(name="DiagnosticValue", args=[], module_alias=None, module_id=None, loc=None)
	if kind is TypeKind.UNKNOWN:
		return None
	if kind is TypeKind.SCALAR:
		# Builtins (Int, Bool, etc.) have module_id=None; module-scoped scalars
		# (e.g. m.Size) carry a module_id that must be preserved so the consumer
		# resolves the correct nominal type.
		return parser_ast.TypeExpr(name=td.name, args=[], module_alias=None, module_id=td.module_id, loc=None)

	# --- Ref ---
	if kind is TypeKind.REF:
		if not td.param_types:
			return None
		inner = _recurse(td.param_types[0])
		if inner is None:
			return None
		ref_name = "&mut" if td.ref_mut else "&"
		return parser_ast.TypeExpr(name=ref_name, args=[inner], module_alias=None, module_id=None, loc=None)

	# --- Array ---
	if kind is TypeKind.ARRAY:
		if not td.param_types:
			return None
		elem = _recurse(td.param_types[0])
		if elem is None:
			return None
		return parser_ast.TypeExpr(name="Array", args=[elem], module_alias=None, module_id=None, loc=None)

	# --- RawPtr ---
	if kind is TypeKind.RAW_PTR:
		if not td.param_types:
			return None
		inner = _recurse(td.param_types[0])
		if inner is None:
			return None
		return parser_ast.TypeExpr(name="RawPtr", args=[inner], module_alias=None, module_id=None, loc=None)

	# --- FnResult ---
	if kind is TypeKind.FNRESULT:
		if len(td.param_types) < 2:
			return None
		ok = _recurse(td.param_types[0])
		err = _recurse(td.param_types[1])
		if ok is None or err is None:
			return None
		return parser_ast.TypeExpr(name="FnResult", args=[ok, err], module_alias=None, module_id=None, loc=None)

	# --- Function type ---
	if kind is TypeKind.FUNCTION:
		if not td.param_types:
			return None
		# param_types layout: [param0, param1, ..., return_type]
		param_exprs = []
		for pt in td.param_types[:-1]:
			pe = _recurse(pt)
			if pe is None:
				return None
			param_exprs.append(pe)
		ret = _recurse(td.param_types[-1])
		if ret is None:
			return None
		param_exprs.append(ret)
		return parser_ast.TypeExpr(
			name="fn", args=param_exprs, fn_throws=td.fn_throws,
			module_alias=None, module_id=None, loc=None,
		)

	# --- Nominal types: STRUCT, VARIANT, INTERFACE, EXCEPTION ---
	if kind in (TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.INTERFACE):
		# Check for generic instantiation via instance tables.
		inst = None
		if kind is TypeKind.STRUCT:
			inst = type_table.get_struct_instance(tid)
		elif kind is TypeKind.VARIANT:
			inst = type_table.variant_instances.get(tid)
		elif kind is TypeKind.INTERFACE:
			inst = type_table.interface_instances.get(tid)

		if inst is not None and inst.type_args:
			# Generic instantiation: recurse on type_args.
			base_td = type_table.get(inst.base_id)
			arg_exprs = []
			for arg in inst.type_args:
				ae = _recurse(arg)
				if ae is None:
					return None
				arg_exprs.append(ae)
			return parser_ast.TypeExpr(
				name=base_td.name, args=arg_exprs,
				module_alias=None, module_id=base_td.module_id, loc=None,
			)
		# Non-generic nominal.
		return parser_ast.TypeExpr(
			name=td.name, args=[], module_alias=None, module_id=td.module_id, loc=None,
		)

	# --- FORWARD_NOMINAL fallback ---
	if kind is TypeKind.FORWARD_NOMINAL:
		args = []
		for pt in td.param_types:
			ae = _recurse(pt)
			if ae is None:
				return None
			args.append(ae)
		return parser_ast.TypeExpr(
			name=td.name, args=args, module_alias=None, module_id=td.module_id, loc=None,
		)

	return None


def encode_type_expr(
	expr: parser_ast.TypeExpr | None,
	*,
	default_module: str | None,
	type_param_names: set[str] | None = None,
) -> dict[str, Any] | None:
	if expr is None:
		return None
	name = getattr(expr, "name", None)
	if not isinstance(name, str) or not name:
		return None
	if name == "Self" or (type_param_names and name in type_param_names):
		return {"param": name}
	module_id = getattr(expr, "module_id", None)
	if module_id is None:
		if name == "Optional":
			module_id = "lang.core"
		elif default_module and name not in _BUILTIN_TYPE_NAMES:
			module_id = default_module
	args_obj = []
	for arg in list(getattr(expr, "args", []) or []):
		args_obj.append(
			encode_type_expr(
				arg,
				default_module=default_module,
				type_param_names=type_param_names,
			)
		)
	out: dict[str, Any] = {"name": name}
	if module_id:
		out["module"] = module_id
	if name == "fn":
		out["can_throw"] = bool(expr.can_throw())
	if args_obj:
		out["args"] = args_obj
	return out


def decode_type_expr(obj: Any) -> parser_ast.TypeExpr | None:
	if obj is None:
		return None
	if not isinstance(obj, dict):
		return None
	if "param" in obj:
		name = obj.get("param")
		if not isinstance(name, str) or not name:
			return None
		return parser_ast.TypeExpr(name=name, args=[], module_alias=None, module_id=None, loc=None)
	name = obj.get("name")
	if not isinstance(name, str) or not name:
		return None
	module_id = obj.get("module")
	if module_id is not None and not isinstance(module_id, str):
		return None
	args: list[parser_ast.TypeExpr] = []
	raw_args = obj.get("args")
	if raw_args is not None:
		if not isinstance(raw_args, list):
			return None
		for raw in raw_args:
			arg = decode_type_expr(raw)
			if arg is None:
				return None
			args.append(arg)
	fn_throws = False
	if name == "fn":
		if "can_throw" in obj:
			can_throw = obj.get("can_throw")
			if can_throw is None or not isinstance(can_throw, bool):
				return None
			fn_throws = bool(can_throw)
		else:
			fn_throws = True
	return parser_ast.TypeExpr(
		name=name,
		args=args,
		fn_throws=fn_throws,
		module_alias=None,
		module_id=module_id,
		loc=None,
	)


def encode_trait_expr(
	expr: parser_ast.TraitExpr | None,
	*,
	default_module: str | None,
	type_param_names: list[str] | None = None,
) -> dict[str, Any] | None:
	if expr is None:
		return None
	if isinstance(expr, parser_ast.TraitIs):
		subject = expr.subject
		if isinstance(subject, parser_ast.SelfRef):
			subject = "Self"
		if isinstance(subject, parser_ast.TypeNameRef):
			subject = subject.name
		if isinstance(subject, TypeParamId) and type_param_names is not None:
			idx = int(subject.index)
			if 0 <= idx < len(type_param_names):
				subject = type_param_names[idx]
		if not isinstance(subject, str):
			subject = str(subject)
		return {
			"kind": "is",
			"subject": subject,
			"trait": encode_type_expr(expr.trait, default_module=default_module, type_param_names=set(type_param_names or [])),
		}
	if isinstance(expr, parser_ast.TraitAnd):
		return {
			"kind": "and",
			"left": encode_trait_expr(expr.left, default_module=default_module, type_param_names=type_param_names),
			"right": encode_trait_expr(expr.right, default_module=default_module, type_param_names=type_param_names),
		}
	if isinstance(expr, parser_ast.TraitOr):
		return {
			"kind": "or",
			"left": encode_trait_expr(expr.left, default_module=default_module, type_param_names=type_param_names),
			"right": encode_trait_expr(expr.right, default_module=default_module, type_param_names=type_param_names),
		}
	if isinstance(expr, parser_ast.TraitNot):
		return {
			"kind": "not",
			"expr": encode_trait_expr(expr.expr, default_module=default_module, type_param_names=type_param_names),
		}
	return None


def decode_trait_expr(obj: Any) -> parser_ast.TraitExpr | None:
	if obj is None:
		return None
	if not isinstance(obj, dict):
		return None
	kind = obj.get("kind")
	if kind == "is":
		subject = obj.get("subject")
		if not isinstance(subject, str) or not subject:
			return None
		trait_obj = obj.get("trait")
		trait = decode_type_expr(trait_obj)
		if trait is None:
			return None
		return parser_ast.TraitIs(loc=None, subject=subject, trait=trait)
	if kind == "and":
		left = decode_trait_expr(obj.get("left"))
		right = decode_trait_expr(obj.get("right"))
		if left is None or right is None:
			return None
		return parser_ast.TraitAnd(loc=None, left=left, right=right)
	if kind == "or":
		left = decode_trait_expr(obj.get("left"))
		right = decode_trait_expr(obj.get("right"))
		if left is None or right is None:
			return None
		return parser_ast.TraitOr(loc=None, left=left, right=right)
	if kind == "not":
		inner = decode_trait_expr(obj.get("expr"))
		if inner is None:
			return None
		return parser_ast.TraitNot(loc=None, expr=inner)
	return None


def _collect_mir_type_ids(mir_funcs: Mapping[str, Any]) -> set[int]:
	"""Collect all TypeId integers referenced by MIR functions.

	Used by the package *producer* to determine which TypeDefs to include
	in the emitted .dmp package.  This walks source-compiled MIR to seed
	the reachability set; it is NOT part of package consumer machinery.
	"""
	from lang.driftc.stage2 import mir_nodes as M
	tids: set[int] = set()
	_TID_FIELD_NAMES = {
		"ty", "struct_ty", "variant_ty", "field_ty", "elem_ty",
		"inner_ty", "raw_ty", "ptr_ty", "src_ty", "dst_ty",
		"iface_ty", "data_ty", "env_ty", "value_ty", "user_ret_type",
	}
	for func in mir_funcs.values():
		if not isinstance(func, M.MirFunc):
			continue
		for tid in (getattr(func, "local_types", {}) or {}).values():
			if isinstance(tid, int):
				tids.add(tid)
		for block in func.blocks.values():
			for instr in block.instructions:
				for f in dataclasses.fields(instr):
					val = getattr(instr, f.name, None)
					if isinstance(val, int) and f.name in _TID_FIELD_NAMES:
						tids.add(val)
					elif isinstance(val, (list, tuple)) and f.name == "param_types":
						for item in val:
							if isinstance(item, int):
								tids.add(item)
				call_sig = getattr(instr, "call_sig", None)
				if call_sig is not None:
					for csf in dataclasses.fields(call_sig):
						csv = getattr(call_sig, csf.name, None)
						if isinstance(csv, int) and csf.name in _TID_FIELD_NAMES:
							tids.add(csv)
						elif isinstance(csv, (list, tuple)) and csf.name == "param_types":
							for item in csv:
								if isinstance(item, int):
									tids.add(item)
	return tids


def _transitive_type_closure(seeds: set[int], table: TypeTable) -> set[int]:
	"""Compute the transitive closure of TypeIds through TypeDef.param_types
	and instance type_args."""
	reachable: set[int] = set()
	worklist = list(seeds)
	while worklist:
		tid = worklist.pop()
		if tid in reachable:
			continue
		reachable.add(tid)
		try:
			td = table.get(tid)
		except (KeyError, IndexError):
			continue
		for pt in td.param_types:
			if pt not in reachable:
				worklist.append(pt)
		inst = table.struct_instances.get(tid)
		if inst is not None:
			if inst.base_id not in reachable:
				worklist.append(inst.base_id)
			for arg in inst.type_args:
				if arg not in reachable:
					worklist.append(arg)
			for ft in inst.field_types:
				if ft not in reachable:
					worklist.append(ft)
		iinst = table.interface_instances.get(tid)
		if iinst is not None:
			if iinst.base_id not in reachable:
				worklist.append(iinst.base_id)
			for arg in iinst.type_args:
				if arg not in reachable:
					worklist.append(arg)
		vinst = table.variant_instances.get(tid)
		if vinst is not None:
			if vinst.base_id not in reachable:
				worklist.append(vinst.base_id)
			for arg in getattr(vinst, "type_args", []) or []:
				if arg not in reachable:
					worklist.append(arg)
	return reachable


def _canonical_key_to_json(key: object) -> Any:
	"""Convert a canonical TypeKey tuple to a JSON-serializable structure."""
	if isinstance(key, tuple):
		return [_canonical_key_to_json(x) for x in key]
	if isinstance(key, bool):
		return key
	if isinstance(key, (int, str)):
		return key
	return str(key)


def _canonical_key_from_json(obj: Any) -> object:
	"""Reconstruct a canonical TypeKey from its JSON representation."""
	if isinstance(obj, list):
		return tuple(_canonical_key_from_json(x) for x in obj)
	return obj


def encode_type_table(table: TypeTable, *, package_id: str, canonical_keys: dict[int, object] | None = None, reachable_tids: set[int] | None = None) -> dict[str, Any]:
	"""Encode the TypeTable deterministically.

	When reachable_tids is provided, only emit defs for TypeIds in that set.
	Types outside the set are still covered by canonical_keys (Phase 9) so
	the linker can compute their keys without the TypeDef graph walk.
	"""
	if table.package_id is None:
		raise ValueError("type table missing package_id (set TypeTable.package_id before encoding)")
	if table.package_id != package_id:
		raise ValueError("type table package_id mismatch during encoding")
	# Module-packages validation: only for full defs. Slimmed packages may
	# include types from dependency modules via transitive closure.
	if reachable_tids is None:
		for key in getattr(table, "_nominal", {}).keys():  # type: ignore[attr-defined]
			if key.module_id is None:
				continue
			if key.module_id == "lang.core":
				table.module_packages.setdefault("lang.core", "lang.core")
				continue
			if key.package_id == package_id and table.module_packages.get(key.module_id) != package_id:
				raise ValueError(
					f"module_packages missing/incorrect for declared module '{key.module_id}'"
				)
	else:
		table.module_packages.setdefault("lang.core", "lang.core")

	def _def_to_obj(td: TypeDef) -> dict[str, Any]:
		out = {
			"kind": td.kind.name,
			"name": td.name,
			"param_types": list(td.param_types),
			"module_id": td.module_id,
			"ref_mut": td.ref_mut,
			"fn_throws": td.fn_throws_raw(),
			"field_names": list(td.field_names) if td.field_names is not None else None,
		}
		if td.kind is TypeKind.TYPEVAR and td.type_param_id is None:
			raise ValueError("type table TYPEVAR missing type_param_id")
		if td.type_param_id is not None:
			out["type_param_id"] = {
				"owner": function_id_to_obj(td.type_param_id.owner),
				"index": td.type_param_id.index,
			}
		return out

	def _encode_generic_type_expr(expr: GenericTypeExpr) -> dict[str, Any]:
		return {
			"name": expr.name,
			"args": [_encode_generic_type_expr(a) for a in expr.args],
			"param_index": expr.param_index,
			"module_id": expr.module_id,
			"fn_throws": expr.fn_throws_raw(),
		}

	def _encode_alias_target(expr: object) -> dict[str, Any]:
		"""Encode a type alias target (parser TypeExpr or GenericTypeExpr) for serialization."""
		name = getattr(expr, "name", "") or ""
		args = getattr(expr, "args", []) or []
		module_id = getattr(expr, "module_id", None)
		return {
			"name": name,
			"args": [_encode_alias_target(a) for a in args],
			"module_id": module_id,
		}

	def _encode_variant_schema(schema: Any) -> dict[str, Any]:
		# `VariantSchema` / `VariantArmSchema` / `VariantFieldSchema` are dataclasses,
		# but we encode them manually so the payload stays stable even if we later
		# refactor internal Python class names.
		out = {
			"module_id": schema.module_id,
			"name": schema.name,
			"type_params": list(schema.type_params),
			"arms": [
				{
					"name": arm.name,
					"fields": [{"name": f.name, "type_expr": _encode_generic_type_expr(f.type_expr)} for f in arm.fields],
				}
				for arm in schema.arms
			],
		}
		if getattr(schema, "tombstone_ctor", None) is not None:
			out["tombstone_ctor"] = schema.tombstone_ctor
		return out

	defs: dict[str, Any] = {}
	_emit_tids = reachable_tids  # None means emit all
	for tid in sorted(table._defs.keys()):  # type: ignore[attr-defined]
		if _emit_tids is not None and tid not in _emit_tids:
			continue
		defs[str(tid)] = _def_to_obj(table._defs[tid])  # type: ignore[attr-defined]
	variant_schemas: dict[str, Any] = {}
	for base_id in sorted(table.variant_schemas.keys()):
		variant_schemas[str(base_id)] = _encode_variant_schema(table.variant_schemas[base_id])
	struct_instances: list[dict[str, Any]] = []
	for inst_id, inst in sorted(table.struct_instances.items()):
		if inst_id == inst.base_id:
			continue
		struct_instances.append(
			{
				"inst_id": int(inst_id),
				"base_id": int(inst.base_id),
				"type_args": list(inst.type_args),
			}
		)
	interface_instances: list[dict[str, Any]] = []
	for inst_id, inst in sorted(table.interface_instances.items()):
		if inst_id == inst.base_id:
			continue
		interface_instances.append(
			{
				"inst_id": int(inst_id),
				"base_id": int(inst.base_id),
				"type_args": list(inst.type_args),
			}
		)
	struct_schema_entries: list[dict[str, Any]] = []
	for key, (_n, _fields) in sorted(
		table.struct_schemas.items(),
		key=lambda kv: ((kv[0].module_id or ""), kv[0].name),
	):
		base_id = table.get_struct_base(module_id=key.module_id or "", name=key.name)
		if base_id is None:
			raise ValueError(f"internal: missing struct base for '{key.module_id}::{key.name}'")
		schema = table.struct_bases.get(base_id)
		if schema is None:
			raise ValueError(f"internal: missing struct schema for '{key.module_id}::{key.name}'")
		struct_schema_entries.append(
			{
				"base_id": base_id,
				"type_id": {
					"package_id": key.package_id or package_id,
					"module": key.module_id,
					"name": key.name,
				},
				"module_id": key.module_id,
				"name": key.name,
				"fields": [
					{
						"name": f.name,
						"type_expr": _encode_generic_type_expr(f.type_expr),
						"is_pub": bool(getattr(f, "is_pub", False)),
					}
					for f in schema.fields
				],
				"type_params": list(schema.type_params),
			}
		)
	interface_schema_entries: list[dict[str, Any]] = []
	for base_id, schema in sorted(
		table.interface_bases.items(),
		key=lambda kv: ((table.get(kv[0]).module_id or ""), kv[1].name),
	):
		base_def = table.get(base_id)
		if base_def.kind is not TypeKind.INTERFACE:
			continue
		if base_def.module_id is None:
			raise ValueError(f"interface '{schema.name}' missing module_id")
		interface_schema_entries.append(
			{
				"base_id": base_id,
				"type_id": {
					"package_id": table.module_packages.get(base_def.module_id, package_id) or package_id,
					"module": base_def.module_id,
					"name": schema.name,
				},
				"module_id": base_def.module_id,
				"name": schema.name,
				"type_params": list(schema.type_params),
				"parents": [
					_encode_generic_type_expr(p)
					for p in (getattr(schema, "parents", None) or [])
				],
				"methods": [
					{
						"name": m.name,
						"type_params": list(m.type_params),
						"params": [
							{"name": p.name, "type_expr": _encode_generic_type_expr(p.type_expr)}
							for p in m.params
						],
						# Phase 1 v3 of terminal-`throws`: bare-terminal interface
						# methods (`fn f() throws`) carry `return_type=None` on the
						# schema and encode as null.
						"return_type": _encode_generic_type_expr(m.return_type) if m.return_type is not None else None,
						"declared_nothrow": bool(m.declared_nothrow),
						"is_unsafe": bool(m.is_unsafe),
						# Phase 3 of terminal-`throws`: round-trip both flags so
						# cross-package consumers see the same in-memory shape
						# the producer had. The decoder defaults missing fields
						# to False for forward compatibility with old packages.
						"declared_throws": bool(getattr(m, "declared_throws", False)),
						"declared_terminal_throws": bool(getattr(m, "declared_terminal_throws", False)),
					}
					for m in schema.methods
				],
			}
		)

	# Type aliases (module-scoped).
	type_aliases_entries: list[dict[str, Any]] = []
	for (alias_mid, alias_name), (alias_params, alias_target, _loc) in sorted(table.type_aliases.items()):
		if alias_mid is None:
			continue
		if alias_target is None:
			continue
		type_aliases_entries.append({
			"module_id": alias_mid,
			"name": alias_name,
			"type_params": list(alias_params),
			"target": _encode_alias_target(alias_target),
		})

	provided_nominals: list[dict[str, Any]] = []
	seen_provided: set[tuple[str, str, str]] = set()
	for key in sorted(
		(getattr(table, "_nominal", {}) or {}).keys(),  # type: ignore[attr-defined]
		key=lambda k: (k.package_id or "", k.module_id or "", k.kind.name, k.name),
	):
		if key.package_id != package_id:
			continue
		if key.kind not in (TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.SCALAR, TypeKind.INTERFACE):
			continue
		if key.module_id is None:
			continue
		if key.module_id == "lang.core" and package_id != "lang.core":
			continue
		item = (key.kind.name, key.module_id, key.name)
		if item in seen_provided:
			continue
		seen_provided.add(item)
		provided_nominals.append({"kind": key.kind.name, "module_id": key.module_id, "name": key.name})

	# Phase 9: serialize pre-computed canonical keys so the consumer can
	# look them up directly instead of walking the TypeDef graph.
	canonical_keys_obj: dict[str, Any] | None = None
	if canonical_keys is not None:
		canonical_keys_obj = {}
		for ck_tid in sorted(canonical_keys.keys()):
			if True:  # emit keys for ALL TypeIds, including those not in slimmed defs
				canonical_keys_obj[str(ck_tid)] = _canonical_key_to_json(canonical_keys[ck_tid])

	out = {
		"package_id": package_id,
		"defs": defs,
		"struct_schemas": struct_schema_entries,
		"interface_schemas": interface_schema_entries,
		"struct_instances": struct_instances,
		"interface_instances": interface_instances,
		"exception_schemas": {k: v for k, v in sorted(table.exception_schemas.items())},
		"variant_schemas": variant_schemas,
		"provided_nominals": provided_nominals,
		"type_aliases": type_aliases_entries,
	}
	# Slice 6: package-level FQN list of `pub error E` decls whose
	# Diagnostic projection is user-owned (manual `implement
	# core.Diagnostic for E`).  The consumer's
	# `parse_drift_workspace_to_hir` reads this back and seeds
	# `TypeTable.manual_diagnostic_pub_errors` so Sites A/B/C and
	# the typed-catch boundary (E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED)
	# fire identically across the package boundary.  Without this
	# explicit list, the consumer cannot distinguish a producer's
	# manual impl from the auto-synthesized one (impl_headers don't
	# carry the manual-vs-synthesized bit).
	manual_diag_set = getattr(table, "manual_diagnostic_pub_errors", None)
	if isinstance(manual_diag_set, set):
		out["manual_diagnostic_pub_errors"] = sorted(manual_diag_set)
	if canonical_keys_obj is not None:
		out["canonical_keys"] = canonical_keys_obj
	return out


def type_table_fingerprint(table_obj: Mapping[str, Any]) -> str:
	"""
	Hash a TypeTable JSON object deterministically.

	This is a compatibility guardrail for package consumption: packages produced
	independently must have matching fingerprints, otherwise their TypeIds are not
	comparable and embedding IR would be unsafe.
	"""
	return sha256_hex(canonical_json_bytes(dict(table_obj)))


def encode_signatures(
	signatures: Mapping[str, FnSignature],
	*,
	module_id: str,
	type_table: TypeTable | None = None,
) -> dict[str, Any]:
	"""Encode module-local signatures (deterministic ordering).

	When type_table is provided, any signature missing parser-originating
	TypeExprs (param_types, return_type) will have them reconstructed from
	TypeIds via typeid_to_type_expr. Additionally, impl_target_type and
	error_type are always populated from TypeIds when available.
	"""
	out: dict[str, Any] = {}
	for name in sorted(signatures.keys()):
		sig = signatures[name]
		is_inst_lambda = ("__lambda_cb_" in name or "__lambda_" in name) and "__inst__" in name
		if getattr(sig, "module", None) not in (module_id, None) and not getattr(sig, "is_instantiation", False) and not is_inst_lambda:
			continue
		fn_id = parse_function_symbol(name)
		sig_module = getattr(sig, "module", None) or module_id
		type_param_names = [p.name for p in getattr(sig, "type_params", []) or []]
		impl_type_param_names = [p.name for p in getattr(sig, "impl_type_params", []) or []]
		type_param_name_set = set(type_param_names) | set(impl_type_param_names)

		# Build TypeParamId -> name map for typeid_to_type_expr.
		tp_id_names: dict[TypeParamId, str] | None = None
		if type_table is not None:
			tp_id_names = {}
			for tp in getattr(sig, "impl_type_params", []) or []:
				if hasattr(tp, "id") and tp.id is not None:
					tp_id_names[tp.id] = tp.name
			for tp in getattr(sig, "type_params", []) or []:
				if hasattr(tp, "id") and tp.id is not None:
					tp_id_names[tp.id] = tp.name

		param_types_obj = None
		if sig.param_types is not None:
			param_types_obj = [
				encode_type_expr(p, default_module=sig_module, type_param_names=type_param_name_set)
				for p in list(sig.param_types)
			]
		elif type_table is not None and sig.param_type_ids is not None:
			# Reconstruct from TypeIds when parser TypeExprs are missing.
			reconstructed = []
			for ptid in sig.param_type_ids:
				expr = typeid_to_type_expr(ptid, type_table, type_param_names=tp_id_names)
				if expr is None:
					raise ValueError(
						f"typeid_to_type_expr failed for param TypeId {ptid} in signature '{name}'"
					)
				encoded = encode_type_expr(expr, default_module=sig_module, type_param_names=type_param_name_set)
				if encoded is None:
					raise ValueError(
						f"encode_type_expr failed for reconstructed param TypeExpr '{expr.name}' in signature '{name}'"
					)
				reconstructed.append(encoded)
			param_types_obj = reconstructed

		return_type_obj = None
		# Terminal-throws functions have no return type — skip encoding.
		_is_terminal_throws = bool(getattr(sig, "declared_terminal_throws", False))
		if _is_terminal_throws:
			pass  # return_type_obj stays None
		elif sig.return_type is not None:
			return_type_obj = encode_type_expr(
				sig.return_type,
				default_module=sig_module,
				type_param_names=type_param_name_set,
			)
		elif type_table is not None and sig.return_type_id is not None:
			expr = typeid_to_type_expr(sig.return_type_id, type_table, type_param_names=tp_id_names)
			if expr is None:
				raise ValueError(
					f"typeid_to_type_expr failed for return TypeId {sig.return_type_id} in signature '{name}'"
				)
			return_type_obj = encode_type_expr(expr, default_module=sig_module, type_param_names=type_param_name_set)
			if return_type_obj is None:
				raise ValueError(
					f"encode_type_expr failed for reconstructed return TypeExpr '{expr.name}' in signature '{name}'"
				)

		# impl_target_type: synthesize from TypeId when available.
		# For __inst__ (monomorphized) signatures, impl_target_type_id may be
		# Unknown/unresolvable because it references the generic base's TypeVar
		# context. Allow graceful failure in that case — the consumer resolves
		# __inst__ impl targets via tid_map, not TypeExpr.
		impl_target_type_obj = None
		impl_tid = getattr(sig, "impl_target_type_id", None)
		_is_inst_sig = "__inst__" in name
		if type_table is not None and impl_tid is not None:
			expr = typeid_to_type_expr(impl_tid, type_table, type_param_names=tp_id_names)
			if expr is None:
				if not _is_inst_sig:
					raise ValueError(
						f"typeid_to_type_expr failed for impl_target TypeId {impl_tid} in signature '{name}'"
					)
			else:
				# If the base TypeExpr has no args but the signature carries
				# impl_target_type_args, include them so the consumer can
				# resolve the concrete instantiation (e.g., ArrayRange<Int>
				# instead of just ArrayRange).
				impl_target_type_args = getattr(sig, "impl_target_type_args", None)
				if impl_target_type_args and not expr.args:
					arg_exprs = []
					for arg_tid in impl_target_type_args:
						arg_expr = typeid_to_type_expr(arg_tid, type_table, type_param_names=tp_id_names)
						if arg_expr is not None:
							arg_exprs.append(arg_expr)
					if len(arg_exprs) == len(impl_target_type_args):
						expr = parser_ast.TypeExpr(
							name=expr.name, args=arg_exprs,
							module_alias=expr.module_alias, module_id=expr.module_id,
							loc=expr.loc,
						)
				impl_target_type_obj = encode_type_expr(expr, default_module=sig_module, type_param_names=type_param_name_set)
				if impl_target_type_obj is None and not _is_inst_sig:
					raise ValueError(
						f"encode_type_expr failed for reconstructed impl_target TypeExpr '{expr.name}' in signature '{name}'"
					)

		# error_type: synthesize from error_type_id when available.
		error_type_obj = None
		err_tid = getattr(sig, "error_type_id", None)
		if type_table is not None and err_tid is not None:
			expr = typeid_to_type_expr(err_tid, type_table, type_param_names=tp_id_names)
			if expr is None:
				raise ValueError(
					f"typeid_to_type_expr failed for error TypeId {err_tid} in signature '{name}'"
				)
			error_type_obj = encode_type_expr(expr, default_module=sig_module, type_param_names=type_param_name_set)
			if error_type_obj is None:
				raise ValueError(
					f"encode_type_expr failed for reconstructed error TypeExpr '{expr.name}' in signature '{name}'"
				)

		entry: dict[str, Any] = {
			"name": sig.name,
			"module": sig_module,
			"fn_id": function_id_to_obj(fn_id),
			"is_method": sig.is_method,
			"method_name": getattr(sig, "method_name", None),
			"self_mode": getattr(sig, "self_mode", None),
			"is_pub": bool(getattr(sig, "is_pub", False)),
			"is_intrinsic": bool(getattr(sig, "is_intrinsic", False)),
			"intrinsic_kind": (
				getattr(sig, "intrinsic_kind", None).value
				if getattr(sig, "intrinsic_kind", None) is not None
				else None
			),
			"is_wrapper": bool(getattr(sig, "is_wrapper", False)),
			"wraps_target_symbol": (
				function_symbol(sig.wraps_target_fn_id)
				if getattr(sig, "wraps_target_fn_id", None) is not None
				else None
			),
			"wraps_target_fn_id": (
				function_id_to_obj(sig.wraps_target_fn_id)
				if getattr(sig, "wraps_target_fn_id", None) is not None
				else None
			),
			"param_names": list(sig.param_names or []),
			"param_mutable": list(sig.param_mutable or []),
			"declared_can_throw": sig.declared_can_throw,
			# Phase 3 of terminal-`throws`: round-trip both auto-try and
			# bare-terminal flags so cross-package consumers see the same
			# in-memory shape the producer had. The decoder defaults missing
			# fields to False for forward compatibility with old packages.
			"declared_throws": bool(getattr(sig, "declared_throws", False)),
			"declared_terminal_throws": bool(getattr(sig, "declared_terminal_throws", False)),
			"declared_unsafe": bool(getattr(sig, "declared_unsafe", False)),
			"is_exported_entrypoint": bool(getattr(sig, "is_exported_entrypoint", False)),
			"is_extern_c": bool(getattr(sig, "is_extern_c", False)),
			"type_params": type_param_names,
			"impl_type_params": impl_type_param_names,
			"param_types": param_types_obj,
			"return_type": return_type_obj,
			"impl_target_type": impl_target_type_obj,
			"error_type": error_type_obj,
		}
		# Raw TypeId fields are omitted for concrete non-generic signatures
		# (TypeExpr is authoritative). Without type_table (test helpers only),
		# raw fields are emitted for backward compatibility with test code.
		# __inst__ and generic (type_params/impl_type_params) sigs
		# keep raw TypeId fields because TypeExpr resolution with TypeVars may
		# produce different interned TypeIds than tid_map, and method resolution
		# depends on exact TypeId identity.
		_needs_raw = _is_inst_sig or bool(type_param_names) or bool(impl_type_param_names)
		if type_table is None or _needs_raw:
			entry["param_type_ids"] = list(sig.param_type_ids or []) if sig.param_type_ids is not None else None
			entry["return_type_id"] = sig.return_type_id
			entry["impl_target_type_id"] = getattr(sig, "impl_target_type_id", None)
		out[name] = entry
	return out


def _build_type_param_map(
	sig: FnSignature,
	impl_type_param_names: list[str],
	type_param_names: list[str],
) -> dict[object, dict[str, object]]:
	"""Map type param names/ids to canonical TyVar descriptors."""
	out: dict[object, dict[str, object]] = {}
	for idx, name in enumerate(impl_type_param_names):
		out[name] = {"scope": "impl", "index": idx}
	for idx, name in enumerate(type_param_names):
		out[name] = {"scope": "fn", "index": idx}
	for idx, tp in enumerate(getattr(sig, "impl_type_params", []) or []):
		out[getattr(tp, "id", None)] = {"scope": "impl", "index": idx}
	for idx, tp in enumerate(getattr(sig, "type_params", []) or []):
		out[getattr(tp, "id", None)] = {"scope": "fn", "index": idx}
	return out


def _generic_param_layout(
	impl_type_param_names: list[str],
	type_param_names: list[str],
) -> list[dict[str, object]]:
	layout: list[dict[str, object]] = []
	for idx in range(len(impl_type_param_names)):
		layout.append({"scope": "impl", "index": idx})
	for idx in range(len(type_param_names)):
		layout.append({"scope": "fn", "index": idx})
	return layout


def _canonical_type_expr(
	expr: parser_ast.TypeExpr | None,
	*,
	default_module: str | None,
	param_type_map: dict[object, dict[str, object]],
) -> dict[str, Any] | None:
	if expr is None:
		return None
	name = getattr(expr, "name", None)
	if not isinstance(name, str) or not name:
		return None
	if name == "Self":
		return {"param": {"scope": "trait_self", "index": 0}}
	if name in param_type_map:
		return {"param": param_type_map[name]}
	module_id = getattr(expr, "module_id", None)
	if module_id is None:
		if name == "Optional":
			module_id = "lang.core"
		elif default_module and name not in _BUILTIN_TYPE_NAMES:
			module_id = default_module
	args_obj = []
	for arg in list(getattr(expr, "args", []) or []):
		args_obj.append(
			_canonical_type_expr(
				arg,
				default_module=default_module,
				param_type_map=param_type_map,
			)
		)
	out: dict[str, Any] = {"name": name}
	if module_id:
		out["module"] = module_id
	if name == "fn":
		out["can_throw"] = bool(expr.can_throw())
	if args_obj:
		out["args"] = args_obj
	return out


def _canonical_trait_expr(
	expr: parser_ast.TraitExpr | None,
	*,
	default_module: str | None,
	default_package: str | None,
	module_packages: Mapping[str, str] | None,
	param_type_map: dict[object, dict[str, object]],
) -> dict[str, Any] | None:
	if expr is None:
		return None
	def _trait_key_from_expr_canonical(typ: parser_ast.TypeExpr) -> TraitKey:
		# Canonical identity for package fingerprinting must use resolved
		# module_id/default_module, never import aliases.
		module = getattr(typ, "module_id", None)
		if module is None:
			module = default_module
		pkg = None
		if module is not None:
			pkg = (module_packages or {}).get(module, default_package)
		return TraitKey(package_id=pkg, module=module, name=typ.name)
	def _subject_name(subject: object) -> str | None:
		if isinstance(subject, parser_ast.SelfRef):
			return "Self"
		if isinstance(subject, parser_ast.TypeNameRef):
			return subject.name
		if isinstance(subject, str):
			return subject
		return None
	if isinstance(expr, parser_ast.TraitIs):
		subject = expr.subject
		subj_name = _subject_name(subject)
		if subj_name == "Self":
			subj_obj = {"var": {"scope": "trait_self", "index": 0}}
		elif subj_name is not None and subj_name in param_type_map:
			subj_obj = {"var": param_type_map[subj_name]}
		elif isinstance(subject, TypeParamId) and subject in param_type_map:
			subj_obj = {"var": param_type_map[subject]}
		else:
			subj_obj = {"name": str(subject)}
		trait_key = _trait_key_from_expr_canonical(expr.trait)
		return {
			"kind": "is",
			"subject": subj_obj,
			"trait": {
				"package_id": trait_key.package_id,
				"module": trait_key.module,
				"name": trait_key.name,
			},
		}
	if isinstance(expr, parser_ast.TraitAnd):
		return {
			"kind": "and",
			"left": _canonical_trait_expr(
				expr.left,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
				param_type_map=param_type_map,
			),
			"right": _canonical_trait_expr(
				expr.right,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
				param_type_map=param_type_map,
			),
		}
	if isinstance(expr, parser_ast.TraitOr):
		return {
			"kind": "or",
			"left": _canonical_trait_expr(
				expr.left,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
				param_type_map=param_type_map,
			),
			"right": _canonical_trait_expr(
				expr.right,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
				param_type_map=param_type_map,
			),
		}
	if isinstance(expr, parser_ast.TraitNot):
		return {
			"kind": "not",
			"expr": _canonical_trait_expr(
				expr.expr,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
				param_type_map=param_type_map,
			),
		}
	return None


def _require_fingerprint(
	expr: parser_ast.TraitExpr | None,
	*,
	default_module: str | None,
	default_package: str | None,
	module_packages: Mapping[str, str] | None,
	param_type_map: dict[object, dict[str, object]],
) -> str:
	if expr is None:
		return "none"
	canon = _canonical_trait_expr(
		expr,
		default_module=default_module,
		default_package=default_package,
		module_packages=module_packages,
		param_type_map=param_type_map,
	)
	return sha256_hex(canonical_json_bytes(canon))


def _method_trait_key(declared_name: str) -> str | None:
	parts = declared_name.split("::")
	if len(parts) < 3:
		return None
	return parts[-2]


def _impl_receiver_head(declared_name: str) -> str | None:
	parts = declared_name.split("::")
	if len(parts) >= 2:
		target = parts[0]
		if "<" in target:
			return target.split("<", 1)[0]
		return target
	return None


def _decl_fingerprint(
	sig: FnSignature,
	*,
	declared_name: str,
	module_id: str,
	generic_param_layout_hash: str,
	require_fingerprint: str,
	param_type_map: dict[object, dict[str, object]],
) -> str:
	param_types = []
	for p in list(getattr(sig, "param_types", []) or []):
		param_types.append(_canonical_type_expr(p, default_module=module_id, param_type_map=param_type_map))
	fingerprint_obj = {
		"kind": "method" if getattr(sig, "is_method", False) else "function",
		"module": module_id,
		"name": declared_name,
		"arity": len(param_types),
		"param_types": param_types,
		"receiver_mode": getattr(sig, "self_mode", None),
		"trait_key": _method_trait_key(declared_name),
		"impl_receiver_head": _impl_receiver_head(declared_name),
		"generic_param_layout_hash": generic_param_layout_hash,
		"require_fingerprint": require_fingerprint,
	}
	return sha256_hex(canonical_json_bytes(fingerprint_obj))


def compute_template_decl_fingerprint(
	sig: FnSignature,
	*,
	declared_name: str,
	module_id: str,
	require_expr: parser_ast.TraitExpr | None,
	default_package: str | None = None,
	module_packages: Mapping[str, str] | None = None,
) -> tuple[str, list[dict[str, object]]]:
	"""Compute the decl fingerprint + generic param layout for a template signature."""
	type_param_names = [p.name for p in getattr(sig, "type_params", []) or []]
	impl_type_param_names = [p.name for p in getattr(sig, "impl_type_params", []) or []]
	param_type_map = _build_type_param_map(sig, impl_type_param_names, type_param_names)
	generic_param_layout = _generic_param_layout(impl_type_param_names, type_param_names)
	layout_hash = sha256_hex(canonical_json_bytes(generic_param_layout))
	require_fp = _require_fingerprint(
		require_expr,
		default_module=module_id,
		default_package=default_package,
		module_packages=module_packages,
		param_type_map=param_type_map,
	)
	decl_fp = _decl_fingerprint(
		sig,
		declared_name=declared_name,
		module_id=module_id,
		generic_param_layout_hash=layout_hash,
		require_fingerprint=require_fp,
		param_type_map=param_type_map,
	)
	return decl_fp, generic_param_layout


def compute_template_decl_fingerprint_debug(
	sig: FnSignature,
	*,
	declared_name: str,
	module_id: str,
	require_expr: parser_ast.TraitExpr | None,
	default_package: str | None = None,
	module_packages: Mapping[str, str] | None = None,
) -> dict[str, Any]:
	"""Debug variant: return pre-hash components for fingerprint diagnostics.

	Returns a dict with keys:
	- fingerprint_obj: the canonical dict fed to _decl_fingerprint
	- require_canonical: the canonical require clause dict (or "none")
	- layout_hash: the generic_param_layout_hash
	- generic_param_layout: the layout list
	- decl_fingerprint: the final hash
	"""
	type_param_names = [p.name for p in getattr(sig, "type_params", []) or []]
	impl_type_param_names = [p.name for p in getattr(sig, "impl_type_params", []) or []]
	param_type_map = _build_type_param_map(sig, impl_type_param_names, type_param_names)
	generic_param_layout = _generic_param_layout(impl_type_param_names, type_param_names)
	layout_hash = sha256_hex(canonical_json_bytes(generic_param_layout))
	require_fp = _require_fingerprint(
		require_expr,
		default_module=module_id,
		default_package=default_package,
		module_packages=module_packages,
		param_type_map=param_type_map,
	)
	# Build the fingerprint_obj explicitly (mirrors _decl_fingerprint).
	param_types = []
	for p in list(getattr(sig, "param_types", []) or []):
		param_types.append(_canonical_type_expr(p, default_module=module_id, param_type_map=param_type_map))
	fingerprint_obj = {
		"kind": "method" if getattr(sig, "is_method", False) else "function",
		"module": module_id,
		"name": declared_name,
		"arity": len(param_types),
		"param_types": param_types,
		"receiver_mode": getattr(sig, "self_mode", None),
		"trait_key": _method_trait_key(declared_name),
		"impl_receiver_head": _impl_receiver_head(declared_name),
		"generic_param_layout_hash": layout_hash,
		"require_fingerprint": require_fp,
	}
	decl_fp = sha256_hex(canonical_json_bytes(fingerprint_obj))
	# Require canonical form for diffing.
	if require_expr is not None:
		require_canonical = _canonical_trait_expr(
			require_expr,
			default_module=module_id,
			default_package=default_package,
			module_packages=module_packages,
			param_type_map=param_type_map,
		)
	else:
		require_canonical = None
	return {
		"fingerprint_obj": fingerprint_obj,
		"require_canonical": require_canonical,
		"layout_hash": layout_hash,
		"generic_param_layout": generic_param_layout,
		"decl_fingerprint": decl_fp,
	}


def encode_generic_templates(
	*,
	package_id: str,
	module_id: str,
	signatures: Mapping[str, FnSignature],
	hir_blocks: Mapping[str, Any],
	requires_by_symbol: Mapping[str, parser_ast.TraitExpr] | None = None,
	module_packages: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
	"""
	Encode generic TemplateHIR payload entries for a module.

	Each entry includes:
	- fn_symbol (fully-qualified symbol name),
	- fn_id (structured {module,name,ordinal}),
	- signature template (TypeExpr-based),
	- optional require clause,
	- TemplateHIR body (`ir_kind` + `ir`).
	"""
	reqs = dict(requires_by_symbol or {})
	sig_entries = encode_signatures(signatures, module_id=module_id)
	out: list[dict[str, Any]] = []
	for sym in sorted(signatures.keys()):
		sig = signatures[sym]
		if getattr(sig, "module", None) not in (module_id, None):
			continue
		if getattr(sig, "is_wrapper", False):
			continue
		if not (getattr(sig, "type_params", []) or getattr(sig, "impl_type_params", [])):
			continue
		hir = hir_blocks.get(sym)
		if hir is None:
			continue
		sig_entry = sig_entries.get(sym)
		if not isinstance(sig_entry, dict):
			continue
		if sig.param_types is None or sig.return_type is None:
			raise ValueError(f"TemplateHIR-v1 requires TypeExpr signatures for '{sym}'")
		fn_id = parse_function_symbol(sym)
		name = sym
		if sym.startswith(f"{module_id}::"):
			name = sym[len(f"{module_id}::") :]
		if "#" in name:
			base, ord_text = name.rsplit("#", 1)
			if ord_text.isdigit():
				name = base
				# Ordinal suffix is stripped from the declared name.
				# The template key fingerprint encodes identity instead.
		type_param_names = list(sig_entry.get("type_params") or [])
		impl_type_param_names = list(sig_entry.get("impl_type_params") or [])
		req_expr = reqs.get(sym)
		decl_fp, generic_param_layout = compute_template_decl_fingerprint(
			sig,
			declared_name=name,
			module_id=module_id,
			require_expr=req_expr,
			default_package=package_id,
			module_packages=module_packages,
		)
		if os.environ.get("DRIFTC_DEBUG_FINGERPRINT") == "1":
			import json as _json, sys as _sys
			_dbg = compute_template_decl_fingerprint_debug(
				sig,
				declared_name=name,
				module_id=module_id,
				require_expr=req_expr,
				default_package=package_id,
				module_packages=module_packages,
			)
			print(
				f"  [emit-time] template={name} module={module_id}\n"
				f"  [emit-time] decl_fp={decl_fp}\n"
				f"  [emit-time] fingerprint_obj={_json.dumps(_dbg['fingerprint_obj'], indent=2, default=str)}\n"
				f"  [emit-time] require_canonical={_json.dumps(_dbg['require_canonical'], indent=2, default=str)}",
				file=_sys.stderr,
			)
		template_id = function_key_to_obj(
			FunctionKey(
				package_id=package_id,
				module_path=module_id,
				name=name,
				decl_fingerprint=decl_fp,
			)
		)
		entry: dict[str, Any] = {
			"fn_symbol": sym,
			"fn_id": function_id_to_obj(fn_id),
			"template_id": template_id,
			"signature": sig_entry,
			"ir_kind": "TemplateHIR-v1",
			"ir": _to_jsonable(hir),
			"generic_param_layout": list(generic_param_layout),
		}
		if req_expr is not None:
			entry["require"] = encode_trait_expr(
				req_expr,
				default_module=module_id,
				type_param_names=type_param_names + impl_type_param_names,
			)
		else:
			entry["require"] = None
		out.append(entry)
	return out


def encode_hir_funcs(
	*,
	module_id: str,
	signatures: Mapping[str, FnSignature],
	hir_blocks: Mapping[str, Any],
) -> dict[str, Any]:
	"""
	Encode HIR function bodies for ALL non-generic, non-wrapper functions.

	This is Phase 1 of Option B (packages as distribution containers):
	serialize declared HIR for all functions, not just generic templates.
	The consumer will compile these through the standard pipeline instead
	of loading pre-lowered MIR.

	Returns a dict of {fn_symbol: serialized_hir_body}.
	"""
	out: dict[str, Any] = {}
	for sym in sorted(signatures.keys()):
		sig = signatures[sym]
		if getattr(sig, "module", None) not in (module_id, None):
			continue
		if getattr(sig, "is_wrapper", False):
			continue
		# Skip generics — they're in generic_templates.
		if getattr(sig, "type_params", []) or getattr(sig, "impl_type_params", []):
			continue
		hir = hir_blocks.get(sym)
		if hir is None:
			continue
		out[sym] = _to_jsonable(hir)
	return out


def decode_hir_funcs(
	hir_funcs_obj: Mapping[str, Any],
) -> dict[str, Any]:
	"""
	Decode `hir_funcs` as encoded by `encode_hir_funcs`.

	Returns a dict of {fn_symbol: HBlock} (stage1 HIR dataclasses).

	Uses the same dataclass/enum registry as decode_generic_templates
	to ensure all HIR node kinds are handled identically.
	"""
	from lang.driftc.stage1 import hir_nodes as H  # local import
	from lang.driftc.stage1 import closures as closures_mod  # local import
	from lang.driftc.core import function_id as fn_id_mod  # local import
	from lang.driftc.core import span as span_mod  # local import
	from lang.driftc.stage0 import ast as stage0_ast  # local import

	# Register `stage0.ast` last so its dataclass variants win the
	# `_to_jsonable` discriminator collision with `parser_ast`.  HIR
	# field types reference `stage0.ast.*` (e.g.,
	# `HQualifiedMember.base_type_expr` is a `stage0.ast.TypeNameRef`
	# carrying a `module_id`).  Both modules define a `TypeNameRef`,
	# but `parser_ast.TypeNameRef` lacks `module_id` and silently drops
	# it during `from_jsonable` reconstruction.  That dropped field was
	# the underlying cause of `E_INTERNAL_MISSING_CALLSITE_CALLINFO`
	# on package consumers of source containing `captures(share x)`:
	# the synthesized `Share::share(&x)` HCall's
	# `base_type_expr.module_id="std.core.shareable"` round-tripped to
	# `module_id=None`, then `trait_key_from_expr` fell back to the
	# current module (`web.rest.app`), the trait_index lookup missed,
	# and the resolver returned without recording CallInfo.  Putting
	# `stage0_ast` after `parser_ast` keeps `module_id` intact across
	# the .dmp boundary.
	dc = build_dataclass_registry(H, parser_ast, fn_id_mod, span_mod, closures_mod, stage0_ast)
	enums = build_enum_registry(H, fn_id_mod, closures_mod)
	out: dict[str, Any] = {}
	for sym, obj in hir_funcs_obj.items():
		decoded = from_jsonable(obj, dataclasses_by_name=dc, enums_by_name=enums)
		out[str(sym)] = decoded
	return out


def encode_module_payload_v0(
	*,
	package_id: str,
	module_id: str,
	type_table: TypeTable,
	signatures: Mapping[str, FnSignature],
	generic_templates: list[dict[str, Any]] | None = None,
	hir_funcs: dict[str, Any] | None = None,
	exported_values: list[str],
	exported_types: dict[str, list[str]],
	exported_traits: list[str] | None = None,
	exported_consts: list[str] | None = None,
	reexports: dict[str, Any] | None = None,
	trait_metadata: list[dict[str, Any]] | None = None,
	impl_headers: list[dict[str, Any]] | None = None,
	trait_scope: list[dict[str, Any]] | None = None,
	canonical_keys: dict[int, object] | None = None,
	reachable_tids: set[int] | None = None,
) -> dict[str, Any]:
	"""Build the provisional payload object (not yet canonical-JSON encoded)."""
	tt_obj = encode_type_table(type_table, package_id=package_id, canonical_keys=canonical_keys, reachable_tids=reachable_tids)
	consts: list[str] = list(exported_consts or [])
	const_table: dict[str, Any] = {}
	exported_const_set: set[str] = set(consts)
	def _encode_const_value(sym: str, val: object) -> Any:
		if isinstance(val, bool):
			return bool(val)
		if isinstance(val, int):
			return int(val)
		if isinstance(val, float):
			return float(val)
		if isinstance(val, str):
			return str(val)
		if isinstance(val, list):
			return [_encode_const_value(f"{sym}[{i}]", v) for i, v in enumerate(val)]
		raise ValueError(f"internal: unsupported const value type for '{sym}': {type(val).__name__}")
	for name in consts:
		sym = f"{module_id}::{name}"
		entry = type_table.lookup_const(sym)
		if entry is None:
			raise ValueError(f"internal: exported const '{sym}' missing from TypeTable const table")
		ty_id, val = entry
		const_table[name] = {"type_id": int(ty_id), "value": _encode_const_value(sym, val)}
	# Include ALL module constants (including private ones) so that generic
	# template re-instantiation in the consumer can resolve module-scoped
	# constant references (e.g. HASH_MAP_STATE_EMPTY inside HashMapCore methods).
	internal_const_table: dict[str, Any] = {}
	prefix = f"{module_id}::"
	for sym, (ty_id, val) in sorted(type_table.consts.items()):
		if not sym.startswith(prefix):
			continue
		cname = sym[len(prefix):]
		if cname in exported_const_set:
			continue
		if not isinstance(val, (bool, int, float, str, list)):
			continue
		internal_const_table[cname] = {"type_id": int(ty_id), "value": _encode_const_value(sym, val)}
	types_obj = {
		"structs": list(exported_types.get("structs", [])),
		"variants": list(exported_types.get("variants", [])),
		"exceptions": list(exported_types.get("exceptions", [])),
		"interfaces": list(exported_types.get("interfaces", [])),
		"aliases": list(exported_types.get("aliases", [])),
	}
	reexports_obj = reexports if isinstance(reexports, dict) else {}
	trait_meta_obj = list(trait_metadata or [])
	impl_headers_obj = list(impl_headers or [])
	return {
		"payload_kind": "provisional-dmir",
		"payload_version": 2,
		"unstable_format": True,
		"module_id": module_id,
		"exports": {
			"values": list(exported_values),
			"types": types_obj,
			"consts": consts,
			"traits": list(exported_traits or []),
		},
		"reexports": _to_jsonable(reexports_obj),
		"trait_metadata": _to_jsonable(trait_meta_obj),
		"impl_headers": _to_jsonable(impl_headers_obj),
		"consts": const_table,
		"internal_consts": internal_const_table,
		"type_table": tt_obj,
		"type_table_fingerprint": type_table_fingerprint(tt_obj),
		"signatures": encode_signatures(signatures, module_id=module_id, type_table=type_table),
		"generic_templates": _to_jsonable(list(generic_templates or [])),
		"hir_funcs": hir_funcs if isinstance(hir_funcs, dict) else {},
		"trait_scope": list(trait_scope) if trait_scope is not None else [],
	}


def decode_generic_templates(generic_templates_obj: Any) -> list[dict[str, Any]]:
	"""
	Decode `generic_templates` entries (TemplateHIR) from a payload.
	"""
	if not isinstance(generic_templates_obj, list):
		return []
	from lang.driftc.stage1 import hir_nodes as H  # local import
	from lang.driftc.stage1 import closures as closures_mod  # local import
	from lang.driftc.parser import ast as parser_ast  # local import
	from lang.driftc.core import function_id as fn_id_mod  # local import
	from lang.driftc.core import span as span_mod  # local import
	from lang.driftc.stage0 import ast as stage0_ast  # local import

	# See `decode_hir_funcs` for why `stage0_ast` is appended LAST.
	dc = build_dataclass_registry(H, parser_ast, fn_id_mod, span_mod, closures_mod, stage0_ast)
	enums = build_enum_registry(H, fn_id_mod, closures_mod)
	out: list[dict[str, Any]] = []
	for entry in generic_templates_obj:
		if not isinstance(entry, dict):
			continue
		decoded = dict(entry)
		req = entry.get("require")
		if req is not None:
			decoded_req = decode_trait_expr(req)
			decoded["require"] = decoded_req if decoded_req is not None else None
		ir = entry.get("ir")
		if ir is not None:
			decoded["ir"] = from_jsonable(ir, dataclasses_by_name=dc, enums_by_name=enums)
		out.append(decoded)
	return out
