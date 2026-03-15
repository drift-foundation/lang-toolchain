# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId, function_id_to_obj
from lang.driftc.core.types_core import TypeTable
from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps


def test_typevar_identity_ignores_display_name() -> None:
	host = TypeTable()
	owner = FunctionId(module="m", name="Box", ordinal=0)
	type_param_id = {"owner": function_id_to_obj(owner), "index": 0}
	pkg = {
		"package_id": "pkgA",
		"defs": {
			"1": {
				"kind": "TYPEVAR",
				"name": "Z",
				"param_types": [],
				"module_id": None,
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
				"type_param_id": type_param_id,
			},
			"2": {
				"kind": "TYPEVAR",
				"name": "A",
				"param_types": [],
				"module_id": None,
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
				"type_param_id": type_param_id,
			},
		},
		"struct_schemas": [],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [],
	}
	maps = import_type_tables_and_build_typeid_maps([pkg], host)
	type_map = maps[0]
	assert type_map[1] == type_map[2]
	assert host.get(type_map[1]).name == "Z"
