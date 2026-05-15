# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Dual-runtime manifest schema regression.

The staged toolchain manifest at lib/manifest.json must declare both runtime
variants the orchestrator's capability check keys on:

  - runtimes.normal.lib   → normal/optimized runtime archive (default lane)
  - runtimes.debug.lib    → debug-style runtime archive (opt-in diagnostic lane)

Both keys must be present, the referenced files must exist on disk relative to
the toolchain root, and both files must be non-empty static archives.

This test is checked in BEFORE production code lands (see
optimized-build-refactor plan, step 1).  It is currently expected to fail
because tools/deploy/steps/publish.py:generate_manifest does not yet emit the
``runtimes`` map.  When step 3 of the workstream lands, this test must turn
green and the xfail marker must be removed.
"""

from __future__ import annotations

import json
import tempfile
from lang.test_support.drift_tmp import session_root
from pathlib import Path

import pytest

from tools.deploy.steps.metadata import DeployMetadata
from tools.deploy.steps.publish import generate_manifest


def _stub_metadata() -> DeployMetadata:
	return DeployMetadata(
		driftc_version="0.0.0-test",
		abi_version=8,
		git_commit="deadbee",
		git_commit_full="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
		build_utc="2026-04-08T00:00:00Z",
		host_platform="linux",
		host_arch="x86_64",
	)


def _stage_runtime_archive(dist: Path, variant_dir: str, archive_name: str) -> Path:
	"""Create a stub static archive at lib/runtime/<variant_dir>/<archive_name>."""
	rt_dir = dist / "lib" / "runtime" / variant_dir
	rt_dir.mkdir(parents=True, exist_ok=True)
	archive = rt_dir / archive_name
	# Minimal valid ar(1) archive header — gives the file a recognizable
	# format so the regression's "parseable as archive" check is meaningful
	# even though no real objects are inside.
	archive.write_bytes(b"!<arch>\n")
	return archive


def test_manifest_declares_normal_and_debug_runtimes() -> None:
	"""lib/manifest.json must declare both runtimes.normal.lib and runtimes.debug.lib."""
	from lang.language_runtime import runtime_archive_name

	with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
		dist = Path(tmpdir) / "dist"
		dist.mkdir()

		# Stage stub runtime archives at the layout the manifest will reference.
		# Filenames per the workstream contract:
		#   normal      → lib/runtime/default/libdrift_rt_abi<N>.a
		#   debug-style → lib/runtime/debug/libdrift_rt_debug_abi<N>.a
		ar_name_default = runtime_archive_name("default")
		# debug-style archive name carries the explicit `_debug` infix per
		# the contract — production releases must avoid this filename.
		ar_name_debug = runtime_archive_name("debug")
		normal_archive = _stage_runtime_archive(dist, "default", ar_name_default)
		debug_archive = _stage_runtime_archive(dist, "debug", ar_name_debug)

		generate_manifest(dist, _stub_metadata())

		manifest_path = dist / "lib" / "manifest.json"
		assert manifest_path.exists(), "manifest.json was not written"
		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

		# Schema assertions: the orchestrator capability check keys on these.
		assert "runtimes" in manifest, "manifest is missing top-level `runtimes` map"
		runtimes = manifest["runtimes"]
		assert isinstance(runtimes, dict), "`runtimes` must be a JSON object"
		assert "normal" in runtimes, "manifest is missing runtimes.normal entry"
		assert "debug" in runtimes, "manifest is missing runtimes.debug entry"

		normal_entry = runtimes["normal"]
		debug_entry = runtimes["debug"]
		assert isinstance(normal_entry, dict) and "lib" in normal_entry
		assert isinstance(debug_entry, dict) and "lib" in debug_entry

		# Both referenced files must exist on disk relative to the toolchain root,
		# be non-empty, and look like a static archive.
		for entry, expected_path in (
			(normal_entry, normal_archive),
			(debug_entry, debug_archive),
		):
			rel = Path(entry["lib"])
			assert not rel.is_absolute(), f"runtime lib path must be relative, got {rel}"
			on_disk = dist / rel
			assert on_disk.exists(), f"manifest references missing file: {rel}"
			assert on_disk.resolve() == expected_path.resolve(), (
				f"manifest path {rel} does not point at the staged archive"
			)
			data = on_disk.read_bytes()
			assert len(data) > 0, f"runtime archive is empty: {rel}"
			assert data.startswith(b"!<arch>\n"), (
				f"runtime archive at {rel} is not a valid ar(1) archive"
			)

		# Naming contract: the debug-style archive filename must contain the
		# explicit `_debug` infix to discourage production use by inspection.
		assert "_debug" in Path(debug_entry["lib"]).name, (
			"debug-style runtime archive filename must contain `_debug` infix"
		)
		assert "_debug" not in Path(normal_entry["lib"]).name, (
			"normal runtime archive filename must NOT contain `_debug` infix"
		)
