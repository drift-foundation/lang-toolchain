# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.types_core import TypeTable
from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps


def _pkg_with_util_struct(*, package_id: str) -> dict[str, object]:
	return {
		"package_id": package_id,
		"defs": {
			"1": {
				"kind": "STRUCT",
				"name": "Foo",
				"param_types": [],
				"module_id": "util",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": [],
			},
		},
		"struct_schemas": [],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [{"kind": "STRUCT", "module_id": "util", "name": "Foo"}],
	}


def test_link_rejects_module_id_collision_across_packages() -> None:
	pkg_a = _pkg_with_util_struct(package_id="pkgA")
	pkg_b = _pkg_with_util_struct(package_id="pkgB")
	with pytest.raises(ValueError, match="module id collision"):
		import_type_tables_and_build_typeid_maps([pkg_a, pkg_b], TypeTable())


def test_link_populates_host_module_packages() -> None:
	pkg_a = {
		"package_id": "pkgA",
		"defs": {
			"1": {
				"kind": "SCALAR",
				"name": "SizeA",
				"param_types": [],
				"module_id": "a.mod",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
			},
		},
		"struct_schemas": [],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [{"kind": "SCALAR", "module_id": "a.mod", "name": "SizeA"}],
	}
	pkg_b = {
		"package_id": "pkgB",
		"defs": {
			"1": {
				"kind": "SCALAR",
				"name": "SizeB",
				"param_types": [],
				"module_id": "b.mod",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": None,
			},
		},
		"struct_schemas": [],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [{"kind": "SCALAR", "module_id": "b.mod", "name": "SizeB"}],
	}
	host = TypeTable()
	import_type_tables_and_build_typeid_maps([pkg_a, pkg_b], host)
	assert host.module_packages["a.mod"] == "pkgA"
	assert host.module_packages["b.mod"] == "pkgB"


def test_link_populates_host_module_packages_with_structs() -> None:
	pkg_a = {
		"package_id": "pkgA",
		"defs": {
			"1": {
				"kind": "STRUCT",
				"name": "A",
				"param_types": [],
				"module_id": "a.mod",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": ["x"],
			},
		},
		"struct_schemas": [
			{
				"base_id": 1,
				"type_id": {"package_id": "pkgA", "module": "a.mod", "name": "A"},
				"module_id": "a.mod",
				"name": "A",
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
		"provided_nominals": [{"kind": "STRUCT", "module_id": "a.mod", "name": "A"}],
	}
	pkg_b = {
		"package_id": "pkgB",
		"defs": {
			"1": {
				"kind": "STRUCT",
				"name": "B",
				"param_types": [],
				"module_id": "b.mod",
				"ref_mut": None,
				"fn_throws": False,
				"field_names": ["y"],
			},
		},
		"struct_schemas": [
			{
				"base_id": 1,
				"type_id": {"package_id": "pkgB", "module": "b.mod", "name": "B"},
				"module_id": "b.mod",
				"name": "B",
				"fields": [
					{
						"name": "y",
						"type_expr": {"name": "Int", "args": [], "param_index": None, "module_id": None},
					}
				],
				"type_params": [],
			}
		],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [{"kind": "STRUCT", "module_id": "b.mod", "name": "B"}],
	}
	host = TypeTable()
	import_type_tables_and_build_typeid_maps([pkg_a, pkg_b], host)
	assert host.module_packages["a.mod"] == "pkgA"
	assert host.module_packages["b.mod"] == "pkgB"


def test_link_seeds_lang_core_provider() -> None:
	host = TypeTable()
	import_type_tables_and_build_typeid_maps([], host)
	assert host.module_packages["lang.core"] == "lang.core"


def test_link_rejects_provided_nominals_without_matching_def() -> None:
	obj = {
		"package_id": "pkgA",
		"defs": {},
		"struct_schemas": [],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [{"kind": "STRUCT", "module_id": "util", "name": "Foo"}],
	}
	with pytest.raises(ValueError, match="provided_nominals"):
		import_type_tables_and_build_typeid_maps([obj], TypeTable())


def test_link_rejects_provided_nominals_with_non_nominal_kind() -> None:
	obj = {
		"package_id": "pkgA",
		"defs": {},
		"struct_schemas": [],
		"struct_instances": [],
		"exception_schemas": {},
		"variant_schemas": {},
		"provided_nominals": [{"kind": "FUNCTION", "module_id": "m", "name": "f"}],
	}
	with pytest.raises(ValueError, match="provided_nominals"):
		import_type_tables_and_build_typeid_maps([obj], TypeTable())
