# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Dual-runtime selection regression — sentinel-symbol approach, staged-toolchain.

The agreed end-state contract is "one staged toolchain, two runtime choices":

  - normal      → contains sentinel symbol __drift_rt_mode_normal
  - debug-style → contains sentinel symbol __drift_rt_mode_debug

The driver picks one at link time based on the `--debug` flag or
`DRIFT_DEBUG=1`.  This regression proves the *selection* end-to-end on a
layout-faithful staged toolchain by:

  1. Staging a real on-disk toolchain tree under tmp_path containing both
     runtime variants at their contract paths, a real lib/manifest.json,
     symlinked compiler/stdlib roots, and the in-tree wrappers as bin/drift
     and bin/driftc, with DRIFT_HOME redirection so the wrappers resolve all
     siblings out of the staged tree (not the live repo).
  2. Building the same tiny consumer twice through the staged wrapper —
     once via `drift build --debug`, once via `DRIFT_DEBUG=1 drift build`.
  3. Inspecting each linked output with `nm` and asserting the four
     sentinel-selection invariants from the contract.

Catching staged layout / staged wrapper / staged manifest path drift is the
explicit value-add over an in-repo regression: the test only sees the
staged tree, never the dev paths.

The sentinels are internal runtime identity markers — paired-only, not
user-facing API, kept dead-strip-proof in the runtime sources.

This test is checked in BEFORE production code lands (see
optimized-build-refactor plan, step 1).  It is currently expected to fail
because (a) the runtime sources do not yet define the sentinels, (b) the
manifest does not yet declare the `runtimes` map, (c) the driver does not
yet honor `--debug` / `DRIFT_DEBUG=1`, and (d) only one runtime variant
ships under the contract filename.  When step 4 of the workstream lands,
this test must turn green and the xfail markers must be removed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

import pytest


_NORMAL_SENTINEL = "__drift_rt_mode_normal"
_DEBUG_SENTINEL = "__drift_rt_mode_debug"


def _repo_root() -> Path:
	return Path(__file__).resolve().parents[3]


_RUNNER_ENV_KEYS = {
	"DRIFT_MEMCHECK", "DRIFT_MASSIF",
	"DRIFT_ASAN", "DRIFT_UBSAN",
	"DRIFT_DEBUG",
	"DRIFT_HOME",
	"DRIFT_RUNTIME_LIB_CACHE_DIR",
	"DRIFT_STDLIB_ROOT", "DRIFT_PACKAGE_ROOT",
	"DRIFT_PYTHON_BIN",
}


def _clean_env() -> dict[str, str]:
	"""os.environ minus DRIFT_* mode/path toggles inherited from the harness."""
	return {k: v for k, v in os.environ.items() if k not in _RUNNER_ENV_KEYS}


# ── Staged toolchain fixture ─────────────────────────────────────────


def _stage_toolchain(tmp_path: Path) -> Path:
	"""Stage a layout-faithful toolchain rooted at tmp_path/dist.

	Returns the staged dist root. The resulting tree contains:

	  <dist>/bin/drift                    → in-tree bin/drift   (symlink)
	  <dist>/bin/driftc                   → in-tree bin/driftc  (symlink)
	  <dist>/stdlib                       → in-tree stdlib      (symlink)
	  <dist>/lib/runtime/default/libdrift_rt_abi<N>.a       (real archive)
	  <dist>/lib/runtime/debug/libdrift_rt_debug_abi<N>.a   (real archive, contract filename)
	  <dist>/lib/manifest.json                              (via generate_manifest)

	Tests run with DRIFT_HOME=<dist> so the in-tree wrappers resolve every
	sibling (stdlib, runtime archives, manifest) from the staged tree, never
	the live repo.  This is what catches staged layout / wrapper / manifest
	path drift.

	The runtime archives are built (or read from the existing dev cache) via
	the real `build_runtime_archive` infra so the staged files are linkable.
	"""
	from lang.language_runtime import (
		build_runtime_archive,
		runtime_archive_name,
	)
	from tools.deploy.steps.publish import generate_manifest
	from tools.deploy.steps.metadata import DeployMetadata

	repo = _repo_root()
	dist = tmp_path / "dist"
	bin_dir = dist / "bin"
	rt_dir = dist / "lib" / "runtime"
	(rt_dir / "default").mkdir(parents=True)
	(rt_dir / "debug").mkdir(parents=True)
	bin_dir.mkdir(parents=True)

	# Symlink wrappers + stdlib + lang import root from the live repo so the
	# staged tree behaves like a real toolchain without copying gigabytes.
	(bin_dir / "drift").symlink_to(repo / "bin" / "drift")
	(bin_dir / "driftc").symlink_to(repo / "bin" / "driftc")
	(dist / "stdlib").symlink_to(repo / "stdlib")

	# Build the two runtime variants into the live repo's runtime cache (the
	# default cache root) so subsequent runs are O(1).  The archives are then
	# copied into the staged layout under the contract filenames:
	#   normal      → libdrift_rt_abi<N>.a
	#   debug-style → libdrift_rt_debug_abi<N>.a   (the explicit `_debug` infix)
	clang = shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available; cannot stage runtime archives")

	default_archive = build_runtime_archive(repo, clang=clang, variant="default")
	debug_archive = build_runtime_archive(repo, clang=clang, variant="debug")

	ar_name = runtime_archive_name("default")
	debug_ar_name = runtime_archive_name("debug")

	shutil.copy2(str(default_archive), str(rt_dir / "default" / ar_name))
	shutil.copy2(str(debug_archive), str(rt_dir / "debug" / debug_ar_name))

	# Stage a real lib/manifest.json so the test exercises the manifest path
	# the orchestrator capability check will read.  The manifest contents will
	# only be schema-correct after step 3 lands; that's covered by the
	# manifest schema regression.
	from lang.driftc.driftc_versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION
	meta = DeployMetadata(
		driftc_version=DRIFTC_VERSION,
		abi_version=DRIFT_RT_ABI_VERSION,
		git_commit="staged",
		git_commit_full="staged-toolchain-fixture",
		build_utc="2026-04-08T00:00:00Z",
		host_platform="linux",
		host_arch="x86_64",
	)
	generate_manifest(dist, meta)

	return dist


# ── Build + nm helpers ───────────────────────────────────────────────


def _nm_symbols(binary: Path) -> set[str]:
	"""Return the set of symbol names in `binary` via nm."""
	nm = shutil.which("nm")
	if nm is None:
		pytest.skip("nm not available; cannot inspect binary symbol table")
	res = subprocess.run(
		[nm, str(binary)],
		text=True,
		capture_output=True,
	)
	assert res.returncode == 0, (
		f"nm failed on {binary}: rc={res.returncode}\nstderr: {res.stderr}"
	)
	symbols: set[str] = set()
	for line in res.stdout.splitlines():
		parts = line.split()
		if parts:
			symbols.add(parts[-1])
	return symbols


def _write_consumer_manifest(consumer_dir: Path) -> Path:
	"""Write a tiny single-app manifest + main.drift in consumer_dir.

	Drops a `.drift-lane-audit-skip` marker so the session-end conftest
	sentinel audit ignores this subtree — this regression intentionally
	builds the SAME consumer in both lanes from one staged toolchain, so
	mixed sentinels are the contract being verified, not a leak.
	"""
	consumer_dir.mkdir(parents=True, exist_ok=True)
	(consumer_dir / ".drift-lane-audit-skip").write_text("", encoding="utf-8")
	src_dir = consumer_dir / "src"
	src_dir.mkdir(exist_ok=True)
	(src_dir / "main.drift").write_text(
		"\n".join(
			[
				"module main;",
				"import std.core;",
				"fn main() nothrow -> Int {",
				"\treturn 0;",
				"}",
				"",
			]
		),
		encoding="utf-8",
	)
	manifest = consumer_dir / "drift-manifest.json"
	manifest.write_text(
		'{\n'
		'  "schema_version": 1,\n'
		'  "project": {"name": "sentinel-test", "license": "MIT"},\n'
		'  "artifacts": [\n'
		'    {\n'
		'      "kind": "app",\n'
		'      "name": "sentinel-app",\n'
		'      "version": "0.0.1",\n'
		'      "description": "selection regression consumer",\n'
		'      "entry_module": "src/main.drift",\n'
		'      "modules": ["src/main.drift"]\n'
		'    }\n'
		'  ]\n'
		'}\n',
		encoding="utf-8",
	)
	return manifest


def _drift_build_via_staged(
	staged: Path,
	consumer_dir: Path,
	*,
	debug_flag: bool,
	debug_env: bool,
) -> Path:
	"""Invoke `<staged>/bin/drift build [--debug]` against the consumer.

	Returns the path to the produced binary.  Sets DRIFT_HOME so the staged
	wrappers resolve sibling files (stdlib, runtime archives, manifest) out
	of the staged tree, not the live repo.
	"""
	manifest = _write_consumer_manifest(consumer_dir)

	args = [str(staged / "bin" / "drift"), "build", "--manifest", str(manifest)]
	if debug_flag:
		args.append("--debug")

	repo = _repo_root()
	env = _clean_env()
	env["DRIFT_HOME"] = str(staged)
	# Put the staged bin/ at the head of PATH so drift_build's `resolve_driftc`
	# picks the staged driftc — mirrors how a deployed toolchain is consumed.
	env["PATH"] = f"{staged / 'bin'}:{env.get('PATH', '')}"
	# Use the repo venv interpreter (has lark, etc.) since the staged tree is
	# minimal and ships no .venv.  Also extend PYTHONPATH to the repo so
	# `lang.*` modules are importable when bin/drift execs `python -m lang.drift`.
	venv_python = repo / ".venv" / "bin" / "python3"
	if venv_python.exists():
		env["DRIFT_PYTHON_BIN"] = str(venv_python)
	env["PYTHONPATH"] = f"{repo}{os.pathsep}{env.get('PYTHONPATH', '')}"
	# Production-style runtime resolution: the deployed wrapper points the
	# compiler at <DIST_ROOT>/lib/runtime via DRIFT_RUNTIME_LIB_CACHE_DIR.
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(staged / "lib" / "runtime")
	if debug_env:
		env["DRIFT_DEBUG"] = "1"

	cp = subprocess.run(
		["/bin/bash", *args],
		text=True,
		capture_output=True,
		env=env,
	)
	assert cp.returncode == 0, (
		f"drift build failed (debug_flag={debug_flag}, debug_env={debug_env}):\n"
		f"args: {args}\n"
		f"stdout: {cp.stdout}\nstderr: {cp.stderr}"
	)
	out = consumer_dir / "build" / "sentinel-app"
	assert out.exists(), f"drift build reported success but {out} missing"
	return out


def _assert_sentinel_invariants(
	normal_bin: Path, debug_bin: Path,
) -> None:
	"""Assert the four selection invariants from the workstream contract."""
	normal_syms = _nm_symbols(normal_bin)
	debug_syms = _nm_symbols(debug_bin)

	assert _NORMAL_SENTINEL in normal_syms, (
		f"normal build missing sentinel {_NORMAL_SENTINEL}; "
		f"the normal runtime variant was not selected"
	)
	assert _DEBUG_SENTINEL not in normal_syms, (
		f"normal build unexpectedly contains debug sentinel {_DEBUG_SENTINEL}; "
		f"both runtime variants are bleeding into the same link"
	)
	assert _DEBUG_SENTINEL in debug_syms, (
		f"debug build missing sentinel {_DEBUG_SENTINEL}; "
		f"the debug-style runtime variant was not selected"
	)
	assert _NORMAL_SENTINEL not in debug_syms, (
		f"debug build unexpectedly contains normal sentinel {_NORMAL_SENTINEL}; "
		f"both runtime variants are bleeding into the same link"
	)


# ── Tests ────────────────────────────────────────────────────────────


def test_runtime_selection_via_debug_flag_in_staged_toolchain(
	tmp_path: Path,
) -> None:
	"""`drift build --debug` selects the debug-style runtime; default selects normal.

	Pins the `--debug` flag surface from the agreed driver interface, and
	exercises the staged toolchain layout + wrapper end-to-end so any drift
	in lib/runtime path, lib/manifest.json schema, or wrapper sibling
	resolution is caught here.
	"""
	staged = _stage_toolchain(tmp_path)
	normal_bin = _drift_build_via_staged(
		staged, tmp_path / "consumer-normal-flag",
		debug_flag=False, debug_env=False,
	)
	debug_bin = _drift_build_via_staged(
		staged, tmp_path / "consumer-debug-flag",
		debug_flag=True, debug_env=False,
	)
	_assert_sentinel_invariants(normal_bin, debug_bin)


def test_runtime_selection_via_debug_env_in_staged_toolchain(
	tmp_path: Path,
) -> None:
	"""`DRIFT_DEBUG=1 drift build` selects the debug-style runtime; unset selects normal.

	Pins the env-var surface co-equal with `--debug` from the agreed driver
	interface, on the same staged-toolchain fixture as the flag test.
	"""
	staged = _stage_toolchain(tmp_path)
	normal_bin = _drift_build_via_staged(
		staged, tmp_path / "consumer-normal-env",
		debug_flag=False, debug_env=False,
	)
	debug_bin = _drift_build_via_staged(
		staged, tmp_path / "consumer-debug-env",
		debug_flag=False, debug_env=True,
	)
	_assert_sentinel_invariants(normal_bin, debug_bin)
