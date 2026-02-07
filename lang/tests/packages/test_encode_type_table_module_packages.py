# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.types_core import TypeTable
from lang.driftc.packages.provisional_dmir_v0 import encode_type_table


def test_encode_type_table_requires_module_packages_for_declared_modules() -> None:
	table = TypeTable()
	table.package_id = "pkgA"
	table.declare_struct("m", "Foo", [])
	with pytest.raises(ValueError, match="module_packages"):
		encode_type_table(table, package_id="pkgA")
