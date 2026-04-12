# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.types_core import TypeTable
from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps


def test_linked_template_round_trips() -> None:
	owner = {"module": "m", "name": "Box", "ordinal": 0}
	type_param_id = {"owner": owner, "index": 0}
	obj = {
		"package_id": "pkgA",
		"defs": {
			"1": {
				"kind": "STRUCT",
				"name": "Box",
				"param_types": [],
				"module_id": "m",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": ["value"],
			},
			"2": {
				"kind": "TYPEVAR",
				"name": "T",
				"param_types": [],
				"module_id": None,
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
				"type_param_id": type_param_id,
			},
			"3": {
				"kind": "STRUCT",
				"name": "Box",
				"param_types": [],
				"module_id": "m",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": ["value"],
			},
		},
		"struct_schemas": [
			{
				"base_id": 1,
				"type_id": {"package_id": "pkgA", "module": "m", "name": "Box"},
				"module_id": "m",
				"name": "Box",
				"fields": [
					{
						"name": "value",
						"type_expr": {"name": "", "args": [], "param_index": 0, "module_id": None},
					}
				],
				"type_params": ["T"],
			}
		],
		"struct_instances": [{"inst_id": 3, "base_id": 1, "type_args": [2]}],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [{"kind": "STRUCT", "module_id": "m", "name": "Box"}],
		"canonical_keys": {
			"1": ["nominal", "STRUCT", "pkgA", "m", "Box"],
			"2": ["typevar", "pkgA", ["owner", "m", "Box", 0], 0],
			"3": ["inst", ["nominal", "STRUCT", "pkgA", "m", "Box"], [["typevar", "pkgA", ["owner", "m", "Box", 0], 0]]],
		},
	}
	host = TypeTable()
	maps = import_type_tables_and_build_typeid_maps([obj], host)
	base_tid = host.get_struct_base(module_id="m", name="Box")
	assert base_tid is not None
	tvar_tid = maps[0][2]
	inst_tid = maps[0][3]
	assert host.ensure_struct_template(base_tid, [tvar_tid]) == inst_tid
