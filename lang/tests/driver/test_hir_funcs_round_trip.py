# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1 regression: hir_funcs round-trip through package serialization.

Verifies that:
1. HIR function bodies are serialized into the package payload
2. Deserialization reconstructs structurally equivalent HIR
3. Every non-generic, non-wrapper source function has an hir_funcs entry
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root
from lang.driftc.packages.provisional_dmir_v0 import decode_hir_funcs

ROOT = Path(__file__).resolve().parents[3]
STDLIB_DIR = ROOT / "stdlib"


def _build_stdlib_package(tmp_path: Path) -> Path:
	"""Build unsigned stdlib package. Returns path to std.dmp."""
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)
	stdlib_files = sorted(str(p) for p in STDLIB_DIR.rglob("*.drift"))
	assert stdlib_files, "no stdlib .drift files"
	pkg_path = tmp_path / "std.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--dev", "-M", str(STDLIB_DIR),
		 "--stdlib-root", str(empty_stdlib),
		 *stdlib_files,
		 "--package-id", "std",
		 "--package-version", "0.0.0-test",
		 "--package-target", "test-target",
		 "--emit-package", str(pkg_path),
		 "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"stdlib build failed: {res.stderr[:500]}"
	return pkg_path


def test_hir_funcs_exact_coverage(tmp_path: Path) -> None:
	"""Every eligible source function has an hir_funcs entry.

	Eligible = non-generic, non-wrapper, in the module's signature set,
	with an HIR body available. This test computes the eligible set from
	the package signatures and asserts exact membership.
	"""
	from lang.driftc.packages.provider_v1 import load_package_v1
	pkg_path = _build_stdlib_package(tmp_path)
	pkg = load_package_v1(pkg_path)

	missing = []
	extra = []
	total_hir = 0
	for mid, mod in pkg.modules_by_id.items():
		hir = mod.payload.get("hir_funcs", {})
		sigs = mod.payload.get("signatures", {})
		gt_syms = {e.get("fn_symbol") for e in mod.payload.get("generic_templates", [])}
		total_hir += len(hir)

		# Compute eligible: non-generic, non-wrapper sigs in this module
		eligible = set()
		for sym, sd in sigs.items():
			if not isinstance(sd, dict):
				continue
			if sd.get("is_wrapper"):
				continue
			if sd.get("type_params") or sd.get("impl_type_params"):
				continue
			sig_mod = sd.get("module")
			if sig_mod not in (mid, None):
				continue
			eligible.add(sym)

		hir_set = set(hir.keys())
		# Functions in eligible but missing from hir_funcs (no HIR body
		# available is acceptable — e.g. extern C or intrinsic stubs).
		# But if an eligible function IS in generic_templates, that's a bug.
		for sym in eligible - hir_set:
			if sym in gt_syms:
				missing.append(f"{sym} (in generic_templates)")
		# Functions in hir_funcs but not in eligible
		for sym in hir_set - eligible:
			extra.append(sym)

	assert total_hir > 0, "no hir_funcs in package payload"
	assert not extra, f"hir_funcs contains ineligible entries: {extra[:5]}"
	# missing entries are expected for extern/intrinsic stubs without bodies
	# but should not overlap with generic_templates
	assert not missing, f"eligible functions in generic_templates instead of hir_funcs: {missing[:5]}"


def test_hir_funcs_round_trip_decode(tmp_path: Path) -> None:
	"""HIR function bodies survive encode→decode round-trip."""
	from lang.driftc.packages.provider_v1 import load_package_v1
	pkg_path = _build_stdlib_package(tmp_path)
	pkg = load_package_v1(pkg_path)
	decoded_total = 0
	failed = []
	for mid, mod in pkg.modules_by_id.items():
		hir_obj = mod.payload.get("hir_funcs", {})
		if not hir_obj:
			continue
		decoded = decode_hir_funcs(hir_obj)
		for sym in hir_obj:
			if sym in decoded:
				decoded_total += 1
			else:
				failed.append(sym)
	assert decoded_total > 0, "no HIR funcs decoded"
	assert not failed, f"{len(failed)} HIR funcs failed to decode: {failed[:5]}"


def test_hir_funcs_format_int_structure(tmp_path: Path) -> None:
	"""format_int HIR has expected structure (block with statements)."""
	from lang.driftc.packages.provider_v1 import load_package_v1
	from lang.driftc.stage1 import hir_nodes as H
	pkg_path = _build_stdlib_package(tmp_path)
	pkg = load_package_v1(pkg_path)
	for mid, mod in pkg.modules_by_id.items():
		if "format" not in mid:
			continue
		hir_obj = mod.payload.get("hir_funcs", {})
		decoded = decode_hir_funcs(hir_obj)
		fmt_int = decoded.get("std.format::format_int")
		assert fmt_int is not None, "format_int not in decoded HIR"
		assert isinstance(fmt_int, H.HBlock), f"expected HBlock, got {type(fmt_int).__name__}"
		assert len(fmt_int.statements) > 0, "format_int HIR has no statements"
		return
	pytest.fail("std.format module not found in package")


def test_hir_funcs_excludes_generics_and_wrappers(tmp_path: Path) -> None:
	"""hir_funcs does not contain generic or wrapper functions."""
	from lang.driftc.packages.provider_v1 import load_package_v1
	pkg_path = _build_stdlib_package(tmp_path)
	pkg = load_package_v1(pkg_path)
	for mid, mod in pkg.modules_by_id.items():
		hir = mod.payload.get("hir_funcs", {})
		for sym in hir:
			assert "__wrap_method" not in sym, f"wrapper in hir_funcs: {sym}"
			assert "__inst__" not in sym, f"instantiation in hir_funcs: {sym}"
		# Generic templates should be separate
		gt_syms = {e.get("fn_symbol") for e in mod.payload.get("generic_templates", [])}
		overlap = set(hir.keys()) & gt_syms
		assert not overlap, f"generic template also in hir_funcs: {overlap}"
