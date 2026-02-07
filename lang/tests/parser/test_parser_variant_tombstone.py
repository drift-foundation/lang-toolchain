# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.parser import parse_drift_to_hir


def test_variant_tombstone_duplicate_rejected(tmp_path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
variant Maybe<T> {
	@tombstone None,
	@tombstone Empty,
	Some(value: T),
}
"""
	)
	_module, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics
	assert any("multiple @tombstone" in d.message for d in diagnostics)


def test_variant_tombstone_payload_rejected(tmp_path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
variant Maybe<T> {
	@tombstone None(value: T),
	Some(value: T),
}
"""
	)
	_module, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics
	assert any("tombstone arm 'None' must have no payload" in d.message for d in diagnostics)
