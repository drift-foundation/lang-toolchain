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


# ── Regression: SCI must reject symlinks that escape source_root ──


def test_sci_rejects_module_symlink_outside_source_root(tmp_path):
	"""A `src/foo.drift` symlink pointing at a file OUTSIDE
	source_root must be rejected at SCI compute time.  Without this
	check, an attacker controlling bytes outside the project tree
	could silently change SCI by writing to the symlink target.
	"""
	import pytest
	from lang.driftc.packages.source_content_id import (
		compute_artifact_source_content_id,
	)

	# A "project" tree at tmp_path/proj.
	proj = tmp_path / "proj"
	proj.mkdir()
	(proj / "src").mkdir()

	# Outside-the-project content the attacker controls.
	outside = tmp_path / "outside"
	outside.mkdir()
	(outside / "evil.drift").write_text("module proj.foo;\npub fn x() -> Int { return 1; }\n")

	# The malicious symlink: src/foo.drift -> ../outside/evil.drift
	(proj / "src" / "foo.drift").symlink_to(outside / "evil.drift")

	with pytest.raises(ValueError, match="resolves outside the declared source_root"):
		compute_artifact_source_content_id(
			kind="library",
			package_id="proj",
			version="0.0.0",
			module_namespace="proj",
			entry_module="proj",
			module_paths=["src/foo.drift"],
			package_deps=[],
			native_deps=[],
			unsafe=False,
			asset_paths=[],
			source_root=proj,
		)


def test_sci_accepts_symlink_inside_source_root(tmp_path):
	"""A symlink whose resolved target stays INSIDE source_root is
	allowed (alias for an in-tree file; bytes the project controls).
	"""
	from lang.driftc.packages.source_content_id import (
		compute_artifact_source_content_id,
	)

	proj = tmp_path / "proj"
	proj.mkdir()
	(proj / "src").mkdir()
	(proj / "shared").mkdir()
	# In-tree target.
	(proj / "shared" / "helper.drift").write_text("module proj.helper;\n")
	# Symlink within source_root → in-tree target.
	(proj / "src" / "helper.drift").symlink_to(proj / "shared" / "helper.drift")

	sci = compute_artifact_source_content_id(
		kind="library",
		package_id="proj",
		version="0.0.0",
		module_namespace="proj",
		entry_module="proj",
		module_paths=["src/helper.drift"],
		package_deps=[],
		native_deps=[],
		unsafe=False,
		asset_paths=[],
		source_root=proj,
	)
	assert sci.startswith("sha256:")
	assert len(sci) == len("sha256:") + 64


def test_sci_symlink_alias_matches_direct_file_with_same_bytes(tmp_path):
	"""SCI is over (logical rel, file bytes).  A `rel` whose
	on-disk file is a regular file with content X and a `rel`
	whose on-disk file is an in-tree symlink to content X MUST
	produce the same SCI (when every other input matches).

	This pins the deliberate semantics: bytes follow the symlink,
	logical path stays as declared.  Without this guarantee an
	aliased source tree would silently shift SCI even though the
	declared source content is identical.
	"""
	from lang.driftc.packages.source_content_id import (
		compute_artifact_source_content_id,
	)

	# Tree A: regular file.
	proj_a = tmp_path / "tree_a"
	proj_a.mkdir()
	(proj_a / "src").mkdir()
	(proj_a / "src" / "helper.drift").write_text("module proj.helper;\n")

	# Tree B: in-tree symlink to an aliased target with the SAME bytes.
	proj_b = tmp_path / "tree_b"
	proj_b.mkdir()
	(proj_b / "src").mkdir()
	(proj_b / "shared").mkdir()
	(proj_b / "shared" / "helper.drift").write_text("module proj.helper;\n")
	(proj_b / "src" / "helper.drift").symlink_to(proj_b / "shared" / "helper.drift")

	args = dict(
		kind="library", package_id="proj", version="0.0.0",
		module_namespace="proj", entry_module="proj",
		module_paths=["src/helper.drift"],
		package_deps=[], native_deps=[], unsafe=False,
		asset_paths=[], 	)
	sci_a = compute_artifact_source_content_id(source_root=proj_a, **args)
	sci_b = compute_artifact_source_content_id(source_root=proj_b, **args)
	assert sci_a == sci_b, (
		f"SCI must be byte-equivalent for in-tree symlink alias:\n"
		f"  regular-file SCI: {sci_a}\n"
		f"  symlink-alias SCI: {sci_b}"
	)
