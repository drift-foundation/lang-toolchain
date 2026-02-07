# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.types_core import TypeTable
from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps


def test_link_allows_variant_template_instantiation() -> None:
	type_param_id = {"owner": {"module": "m", "name": "Opt", "ordinal": 0}, "index": 0}
	obj = {
		"package_id": "pkgA",
		"defs": {
			"1": {
				"kind": "VARIANT",
				"name": "Optional",
				"param_types": [],
				"module_id": "lang.core",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
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
				"kind": "VARIANT",
				"name": "Optional",
				"param_types": [2],
				"module_id": "lang.core",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
			},
		},
		"struct_schemas": [],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {
			"1": {
				"module_id": "lang.core",
				"name": "Optional",
				"type_params": ["T"],
				"tombstone_ctor": "None",
				"arms": [
					{"name": "None", "fields": []},
					{
						"name": "Some",
						"fields": [
							{
								"name": "value",
								"type_expr": {"name": "", "args": [], "param_index": 0, "module_id": None},
							}
						],
					},
				],
			}
		},
		"provided_nominals": [],
	}
	host = TypeTable()
	maps = import_type_tables_and_build_typeid_maps([obj], host)
	base_tid = host.ensure_optional_base()
	inst_tid = maps[0][3]
	tvar_tid = maps[0][2]
	assert inst_tid == host.ensure_variant_template(base_tid, [tvar_tid])
