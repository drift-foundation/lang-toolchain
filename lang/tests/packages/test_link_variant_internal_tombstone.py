# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.types_core import TypeTable
from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps


def test_link_non_generic_droppable_variant_synthesizes_internal_tombstone() -> None:
	obj = {
		"package_id": "acme",
		"defs": {
			"1": {
				"kind": "VARIANT",
				"name": "Choice",
				"param_types": [],
				"module_id": "acme.choice",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
			},
			"2": {
				"kind": "SCALAR",
				"name": "String",
				"param_types": [],
				"module_id": None,
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
				"module_id": "acme.choice",
				"name": "Choice",
				"type_params": [],
				"arms": [
					{"name": "None", "fields": []},
					{
						"name": "TextVal",
						"fields": [
							{
								"name": "s",
								"type_expr": {"name": "String", "args": [], "param_index": None, "module_id": None},
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
	choice_tid = maps[0][1]
	inst = host.get_variant_instance(choice_tid)
	assert inst is not None
	assert inst.internal_tombstone_ctor == "__drift_internal_tombstone"
	assert inst.internal_tombstone_tag == 2


def test_link_generic_droppable_variant_concrete_inst_synthesizes_internal_tombstone() -> None:
	obj = {
		"package_id": "acme",
		"defs": {
			"1": {
				"kind": "VARIANT",
				"name": "Maybe",
				"param_types": [],
				"module_id": "acme.maybe",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
			},
			"2": {
				"kind": "SCALAR",
				"name": "String",
				"param_types": [],
				"module_id": None,
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
			},
			"3": {
				"kind": "VARIANT",
				"name": "Maybe",
				"param_types": [2],
				"module_id": "acme.maybe",
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
				"module_id": "acme.maybe",
				"name": "Maybe",
				"type_params": ["T"],
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
	inst_tid = maps[0][3]
	inst = host.get_variant_instance(inst_tid)
	assert inst is not None
	assert inst.internal_tombstone_ctor == "__drift_internal_tombstone"
	assert inst.internal_tombstone_tag == 2
