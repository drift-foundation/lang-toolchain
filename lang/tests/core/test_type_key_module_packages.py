# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.types_core import TypeTable


def test_type_key_requires_module_packages_for_imported_modules() -> None:
	table = TypeTable()
	table.package_id = "pkgA"
	ty = table.declare_struct("other.mod", "Foo", [])
	with pytest.raises(ValueError, match="module_packages"):
		table.type_key_string(ty)


def test_type_key_is_stable_given_module_packages() -> None:
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["other.mod"] = "pkgB"
	ty = table.declare_struct("other.mod", "Foo", [])
	assert table.type_key_string(ty) == "pkgB::other.mod.Foo"


def test_type_key_string_does_not_raise_after_link() -> None:
	obj = {
		"package_id": "pkgB",
		"defs": {
			"1": {
				"kind": "SCALAR",
				"name": "Size",
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
		"provided_nominals": [{"kind": "SCALAR", "module_id": "b.mod", "name": "Size"}],
	}
	from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps

	host = TypeTable()
	host.package_id = "pkgMain"
	maps = import_type_tables_and_build_typeid_maps([obj], host)
	tid = maps[0][1]
	assert host.type_key_string(tid) == "pkgB::b.mod.Size"
