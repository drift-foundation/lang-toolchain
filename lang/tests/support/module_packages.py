# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from typing import MutableMapping, Optional


def mk_module(target: object, module_name: str, pkg_id: str, module_ids: Optional[MutableMapping[object, int]] = None) -> str:
	"""
	Register a synthetic module package id for tests that emulate cross-package calls.
	"""
	if isinstance(target, dict):
		module_packages = target
	else:
		module_packages = getattr(target, "module_packages", None)
		if module_packages is None:
			module_packages = {}
			setattr(target, "module_packages", module_packages)
	module_packages[module_name] = pkg_id
	if module_ids is not None:
		module_ids.setdefault(module_name, len(module_ids))
	return module_name
