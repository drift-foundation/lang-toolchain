# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Tuple


@dataclass(frozen=True)
class FunctionId:
	"""
Stable identity for a function definition within a module.

`ordinal` is the declaration order within the module after merge.
"""

	module: str
	name: str
	ordinal: int


FnNameKey = Tuple[str | None, str]


def fn_name_key(module_id: str | None, name: str) -> FnNameKey:
	return (module_id, name)


def function_symbol(fn_id: FunctionId) -> str:
	"""Return a stable, unique symbol name for the function."""
	if fn_id.module == "main":
		base = fn_id.name
	else:
		base = f"{fn_id.module}::{fn_id.name}"
	if fn_id.ordinal == 0:
		return base
	return f"{base}#{fn_id.ordinal}"


def parse_function_symbol(symbol: str) -> FunctionId:
	ordinal = 0
	base = symbol
	if "#" in symbol:
		head, tail = symbol.rsplit("#", 1)
		if tail.isdigit():
			ordinal = int(tail)
			base = head
	if "::" in base:
		module, name = base.split("::", 1)
	else:
		module, name = "main", base
	return FunctionId(module=module, name=name, ordinal=ordinal)


def function_id_to_obj(fn_id: FunctionId) -> dict[str, object]:
	return {"module": fn_id.module, "name": fn_id.name, "ordinal": fn_id.ordinal}


def function_id_from_obj(obj: object) -> FunctionId | None:
	if not isinstance(obj, dict):
		return None
	module = obj.get("module")
	name = obj.get("name")
	ordinal = obj.get("ordinal")
	if not isinstance(module, str) or not module:
		return None
	if not isinstance(name, str) or not name:
		return None
	if not isinstance(ordinal, int):
		return None
	return FunctionId(module=module, name=name, ordinal=ordinal)


class FunctionRefKind(Enum):
	WRAPPER = auto()
	IMPL = auto()
	THUNK_OK_WRAP = auto()
	THUNK_BOUNDARY = auto()


@dataclass(frozen=True)
class FunctionRefId:
	"""Canonical identity for a function *value* reference (wrapper vs impl)."""

	fn_id: FunctionId
	kind: FunctionRefKind
	has_wrapper: bool = False


def function_ref_symbol(ref: FunctionRefId) -> str:
	"""Return the symbol name for a function reference."""
	base = function_symbol(ref.fn_id)
	if ref.kind is FunctionRefKind.IMPL and ref.has_wrapper:
		return f"{base}__impl"
	return base


def method_wrapper_id(target: FunctionId) -> FunctionId:
	"""Return a stable wrapper FunctionId for a method boundary Ok-wrap."""
	return FunctionId(module=target.module, name=f"__wrap_method::{target.name}", ordinal=target.ordinal)


__all__ = [
	"FunctionId",
	"FnNameKey",
	"fn_name_key",
	"function_symbol",
	"parse_function_symbol",
	"function_id_to_obj",
	"function_id_from_obj",
	"FunctionRefId",
	"FunctionRefKind",
	"function_ref_symbol",
	"method_wrapper_id",
]
