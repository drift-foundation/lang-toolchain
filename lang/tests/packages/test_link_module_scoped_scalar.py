# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.types_core import TypeKind, TypeTable
from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps


def test_link_imports_module_scoped_scalar_nominal() -> None:
	obj = {
		"package_id": "pkgA",
		"defs": {
			"1": {
				"kind": "SCALAR",
				"name": "Size",
				"param_types": [],
				"module_id": "m",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
			}
		},
		"struct_schemas": [],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [{"kind": "SCALAR", "module_id": "m", "name": "Size"}],
		"canonical_keys": {"1": ["nominal", "SCALAR", "pkgA", "m", "Size"]},
	}
	host = TypeTable()
	import_type_tables_and_build_typeid_maps([obj], host)
	tid = host.get_nominal(kind=TypeKind.SCALAR, module_id="m", name="Size")
	assert tid is not None
	assert host.get(tid).kind is TypeKind.SCALAR
