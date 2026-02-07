# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class FunctionKey:
	"""Stable cross-package identity for generic templates."""

	package_id: str
	module_path: str
	name: str
	decl_fingerprint: str


def function_key_to_obj(key: FunctionKey) -> dict[str, Any]:
	return {
		"package": key.package_id,
		"module": key.module_path,
		"name": key.name,
		"fingerprint": key.decl_fingerprint,
	}


def function_key_from_obj(obj: Any) -> Optional[FunctionKey]:
	if not isinstance(obj, dict):
		return None
	pkg = obj.get("package")
	mod = obj.get("module")
	name = obj.get("name")
	fp = obj.get("fingerprint")
	if not isinstance(pkg, str) or not pkg:
		return None
	if not isinstance(mod, str) or not mod:
		return None
	if not isinstance(name, str) or not name:
		return None
	if not isinstance(fp, str) or not fp:
		return None
	return FunctionKey(package_id=pkg, module_path=mod, name=name, decl_fingerprint=fp)


def function_key_str(key: FunctionKey) -> str:
	return f"{key.package_id}:{key.module_path}::{key.name}@{key.decl_fingerprint}"


__all__ = ["FunctionKey", "function_key_to_obj", "function_key_from_obj", "function_key_str"]
