# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, TYPE_CHECKING, Tuple

from lang.driftc.core.function_id import FunctionId
from lang.driftc.checker import FnSignature
from lang.driftc.impl_index import ImplMeta

if TYPE_CHECKING:
	from lang.driftc import stage1 as H
	from lang.driftc import parser as parser_ast
	from lang.driftc.traits.world import TypeKey


@dataclass(frozen=True)
class ModuleLowered:
	"""
	Immutable-ish bundle for a lowered module.

	This keeps phase boundaries explicit and avoids passing loose maps around.
	"""

	module_id: str
	package_id: str | None
	source_path: Path
	func_hirs: Dict[FunctionId, "H.HBlock"]
	signatures_by_id: Dict[FunctionId, FnSignature]
	fn_ids_by_name: Dict[str, List[FunctionId]]
	requires_by_fn: Dict[FunctionId, "parser_ast.TraitExpr"]
	requires_by_struct: Dict["TypeKey", "parser_ast.TraitExpr"]
	type_defs: Dict[str, List[str]]
	impl_defs: List[ImplMeta]
	origin_by_fn_id: Dict[FunctionId, Path]


def flatten_modules(
	modules: Mapping[str, ModuleLowered],
) -> Tuple[Dict[FunctionId, "H.HBlock"], Dict[FunctionId, FnSignature], Dict[str, List[FunctionId]]]:
	"""
	Merge per-module maps into single lookup tables (legacy adapter).
	"""
	func_hirs: Dict[FunctionId, "H.HBlock"] = {}
	signatures: Dict[FunctionId, FnSignature] = {}
	fn_ids_by_name: Dict[str, List[FunctionId]] = {}
	for module_id in sorted(modules.keys()):
		mod = modules[module_id]
		func_hirs.update(mod.func_hirs)
		signatures.update(mod.signatures_by_id)
		for name, ids in mod.fn_ids_by_name.items():
			fn_ids_by_name.setdefault(name, []).extend(ids)
	return func_hirs, signatures, fn_ids_by_name
