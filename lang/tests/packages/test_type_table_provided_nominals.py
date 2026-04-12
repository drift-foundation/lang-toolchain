# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.types_core import TypeTable
from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps


def _optional_type_table_obj(*, package_id: str, provided_nominals: list[dict[str, object]]) -> dict[str, object]:
	return {
		"package_id": package_id,
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
		},
		"struct_schemas": [],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {
			"1": {
				"module_id": "lang.core",
				"name": "Optional",
				"type_params": ["T"],
				"tombstone_ctor": "Tombstone",
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
					{"name": "Tombstone", "fields": []},
				],
			}
		},
		"provided_nominals": list(provided_nominals),
		"canonical_keys": {
			"1": ["nominal", "VARIANT", "lang.core", "lang.core", "Optional"],
		},
	}


def test_link_rejects_noncore_providing_lang_core_nominal() -> None:
	obj = _optional_type_table_obj(
		package_id="acme",
		provided_nominals=[{"kind": "VARIANT", "module_id": "lang.core", "name": "Optional"}],
	)
	with pytest.raises(ValueError, match="lang.core"):
		import_type_tables_and_build_typeid_maps([obj], TypeTable())


def test_link_allows_noncore_referencing_lang_core_optional() -> None:
	obj = _optional_type_table_obj(package_id="acme", provided_nominals=[])
	import_type_tables_and_build_typeid_maps([obj], TypeTable())


def test_missing_provided_nominals_rejected() -> None:
	obj = _optional_type_table_obj(package_id="acme", provided_nominals=[])
	obj.pop("provided_nominals", None)
	with pytest.raises(ValueError, match="provided_nominals"):
		import_type_tables_and_build_typeid_maps([obj], TypeTable())


def test_no_provider_inferred_from_foreign_nominals() -> None:
	obj = {
		"package_id": "pkgA",
		"defs": {
			"1": {
				"kind": "STRUCT",
				"name": "Foo",
				"param_types": [],
				"module_id": "other.mod",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": ["x"],
			},
		},
		"struct_schemas": [
			{
				"base_id": 1,
				"type_id": {"package_id": "pkgA", "module": "other.mod", "name": "Foo"},
				"module_id": "other.mod",
				"name": "Foo",
				"fields": [
					{
						"name": "x",
						"type_expr": {"name": "Int", "args": [], "param_index": None, "module_id": None},
					}
				],
				"type_params": [],
			}
		],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [],
		"canonical_keys": {
			"1": ["nominal", "STRUCT", "pkgA", "other.mod", "Foo"],
		},
	}
	host = TypeTable()
	import_type_tables_and_build_typeid_maps([obj], host)
	assert "other.mod" not in host.module_packages
