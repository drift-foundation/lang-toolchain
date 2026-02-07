# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.types_core import TypeTable
from lang.driftc.packages.provisional_dmir_v0 import encode_type_table


def test_encode_type_table_requires_table_package_id_set() -> None:
	table = TypeTable()
	table.declare_struct("m", "Foo", [])
	with pytest.raises(ValueError, match="package_id"):
		encode_type_table(table, package_id="pkgA")
