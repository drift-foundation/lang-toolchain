# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for `lang.driftc.packages.source_content_id`.

Focused on the v1-specific guards added in slice 2: duplicate-key
rejection across modules/assets/package_deps/native_deps so the
canonical sort doesn't tie-break on input order.

The shape and field set of SCI is exercised more broadly in the
existing `tools/drift_deploy/test_deploy.py` source-attestation
tests (which carry an identical computation until slice 4 deletes
that duplicate).  This file pins the v1-only strengthening.
"""
from __future__ import annotations

import pytest

from lang.driftc.packages.source_content_id import (
	SourceContentInputs,
	compute_source_content_id,
)


def _example_inputs(**overrides) -> SourceContentInputs:
	defaults = dict(
		kind="library",
		package_id="pkg",
		version="0.1.0",
		module_namespace="pkg",
		entry_module="src/lib.drift",
		modules=[("src/lib.drift", "a" * 64)],
		package_deps=[("foo", "^1.0.0")],
		native_deps=["ssl"],
		unsafe=False,
		assets=[],
		target_class="release",
	)
	defaults.update(overrides)
	return SourceContentInputs(**defaults)


def test_compute_sci_baseline() -> None:
	"""Sanity: well-formed inputs produce a sha256:<hex> id."""
	sci = compute_source_content_id(_example_inputs())
	assert sci.startswith("sha256:")
	assert len(sci) == len("sha256:") + 64


def test_reject_duplicate_module_path() -> None:
	"""Two module entries with the same path are rejected (canonical
	sort tie-break would otherwise depend on input order)."""
	with pytest.raises(ValueError, match="modules: duplicate path"):
		compute_source_content_id(_example_inputs(modules=[
			("src/a.drift", "a" * 64),
			("src/a.drift", "b" * 64),
		]))


def test_reject_duplicate_asset_path() -> None:
	with pytest.raises(ValueError, match="assets: duplicate path"):
		compute_source_content_id(_example_inputs(assets=[
			("data/x.bin", "a" * 64),
			("data/x.bin", "b" * 64),
		]))


def test_reject_duplicate_package_dep_name() -> None:
	"""Two package_deps with the same name (different ranges) are
	rejected -- ambiguous and order-dependent canonical bytes."""
	with pytest.raises(ValueError, match="package_deps: duplicate name"):
		compute_source_content_id(_example_inputs(package_deps=[
			("foo", "^1.0.0"),
			("foo", "^2.0.0"),
		]))


def test_reject_duplicate_native_dep() -> None:
	with pytest.raises(ValueError, match="native_deps: duplicate native dep"):
		compute_source_content_id(_example_inputs(native_deps=["ssl", "ssl"]))


def test_unique_entries_compute_clean() -> None:
	"""Non-duplicate inputs across all four lists compute a valid id."""
	sci = compute_source_content_id(_example_inputs(
		modules=[("src/a.drift", "a" * 64), ("src/b.drift", "b" * 64)],
		assets=[("data/x.bin", "c" * 64), ("data/y.bin", "d" * 64)],
		package_deps=[("foo", "^1.0.0"), ("bar", "^2.0.0")],
		native_deps=["ssl", "z"],
	))
	assert sci.startswith("sha256:")
