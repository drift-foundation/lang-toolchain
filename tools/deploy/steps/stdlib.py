# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy step: build, sign, and install stdlib package + core trust store.

Produces:
  ${DIST}/lib/stdlib/std.dmp
  ${DIST}/lib/stdlib/std.sig
  ${DIST}/lib/stdlib/stdlib_dep.txt
  ${DIST}/lib/compiler/lang/driftc/packages/core_trust.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def build_stdlib_package(repo_root: Path, stage: Path, version: str) -> Path:
	"""Build stdlib .dmp package. Returns path to .dmp file."""
	print("[deploy] building stdlib package...", flush=True)

	dmp_path = stage / "std.dmp"
	empty_stdlib = stage / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)

	# Find all stdlib .drift files.
	stdlib_dir = repo_root / "stdlib"
	sources = sorted(str(p) for p in stdlib_dir.rglob("*.drift"))
	if not sources:
		raise RuntimeError("no .drift files found under stdlib/")

	python = repo_root / ".venv" / "bin" / "python3"
	env = dict(os.environ)
	env["PYTHONPATH"] = str(repo_root)

	cmd = [
		str(python), "-m", "lang.driftc",
		"--dev",
		"--stdlib-root", str(empty_stdlib),
		"-M", "stdlib",
	] + sources + [
		"--package-id", "std",
		"--package-version", version,
		"--package-target", "drift-dev",
		# std.codec.gzip_encode / gzip_decode call into libz via the
		# runtime-owned shim in lang/language_runtime/codec_gzip_runtime.c.
		# The shim's deflate / inflate symbols are unresolved at the .o
		# level; consumers of the stdlib package auto-link -lz from this
		# native_deps.link_libs entry. Note: because stdlib is compiled
		# monolithically, every consumer Drift binary will carry libz.so.1
		# in DT_NEEDED at runtime regardless of whether it calls into the
		# gzip surface (std.codec's wrappers are emitted into the IR
		# unconditionally and reference codec_gzip_runtime.o, so
		# -Wl,--as-needed cannot drop libz). Accepted cost — libz is
		# universal on x86_64 Linux, the only supported target.
		"--native-link-lib", "z",
		"--emit-package", str(dmp_path),
		"--json",
	]

	result = subprocess.run(
		cmd, env=env, cwd=str(repo_root),
		capture_output=True,
	)
	if result.returncode != 0:
		sys.stderr.buffer.write(result.stderr)
		raise RuntimeError("stdlib package build failed")

	if not dmp_path.exists():
		raise RuntimeError("stdlib package build produced no output")

	return dmp_path


def sign_stdlib(repo_root: Path, dmp_path: Path) -> Path:
	"""Sign stdlib package. Returns path to .sig sidecar."""
	print("[deploy] signing stdlib package...", flush=True)

	python = repo_root / ".venv" / "bin" / "python3"
	env = dict(os.environ)
	env["PYTHONPATH"] = str(repo_root)

	result = subprocess.run(
		[str(python), "-m", "lang.drift", "sign",
		 str(dmp_path), "--include-pubkey"],
		env=env, cwd=str(repo_root),
		capture_output=True,
	)
	if result.returncode != 0:
		sys.stderr.buffer.write(result.stderr)
		raise RuntimeError("stdlib signing failed")

	sig_path = dmp_path.with_suffix(".sig")
	if not sig_path.exists():
		raise RuntimeError("signing produced no sidecar")

	return sig_path


def install_stdlib(dmp: Path, sig: Path, dist: Path) -> None:
	"""Install stdlib .dmp, .sig, and stdlib_dep.txt into dist."""
	import shutil

	stdlib_dir = dist / "lib" / "stdlib"
	stdlib_dir.mkdir(parents=True, exist_ok=True)

	shutil.copy2(str(dmp), str(stdlib_dir / "std.dmp"))
	shutil.copy2(str(sig), str(stdlib_dir / "std.sig"))

	# Single source of truth: read the actual package manifest.
	write_stdlib_dep(dmp, dist)


def write_stdlib_dep(dmp: Path, dist: Path) -> None:
	"""Write stdlib_dep.txt by peeking the actual package manifest."""
	# Import inline — this module is available once bundle_compiler has run.
	sys.path.insert(0, str(dist / "lib" / "compiler"))
	try:
		from lang.driftc.packages.dmir_pkg_v0 import peek_package_id_and_version
	finally:
		sys.path.pop(0)

	result = peek_package_id_and_version(dmp)
	if result is None:
		raise RuntimeError(f"failed to peek package id/version from {dmp}")

	dep_spec = f"{result[0]}@{result[1]}"
	stdlib_dep_file = dist / "lib" / "stdlib" / "stdlib_dep.txt"
	stdlib_dep_file.write_text(dep_spec + "\n", encoding="utf-8")


def generate_core_trust_store(sig_path: Path, dist: Path) -> None:
	"""Generate core trust store from stdlib sidecar."""
	print("[deploy] generating core trust store...", flush=True)

	sidecar = json.loads(sig_path.read_text(encoding="utf-8"))

	fmt = sidecar.get("format")
	if fmt != "dmir-pkg-sig":
		raise RuntimeError(f"unexpected sidecar format: {fmt!r}")

	ver = sidecar.get("version")
	if ver != 0:
		raise RuntimeError(f"unexpected sidecar version: {ver!r}")

	sigs = sidecar.get("signatures")
	if not isinstance(sigs, list) or len(sigs) == 0:
		raise RuntimeError("sidecar has no signatures")
	if len(sigs) > 1:
		raise RuntimeError(f"sidecar has {len(sigs)} signatures; expected exactly 1")

	entry = sigs[0]
	algo = entry.get("algo")
	if algo != "ed25519":
		raise RuntimeError(f"unsupported signature algorithm: {algo!r}")

	kid = entry.get("kid")
	if not kid or not isinstance(kid, str):
		raise RuntimeError("signature entry missing 'kid'")

	pubkey = entry.get("pubkey")
	if not pubkey or not isinstance(pubkey, str):
		raise RuntimeError("signature entry missing 'pubkey'")

	namespaces = ["std.*", "lang.*", "drift.*"]
	trust_store = {
		"format": "drift-trust",
		"version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pubkey}},
		"namespaces": {ns: [kid] for ns in namespaces},
		"revoked": [],
	}

	out_path = dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust.json"
	out_path.parent.mkdir(parents=True, exist_ok=True)
	out_path.write_text(
		json.dumps(trust_store, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	print(f"wrote trust store: {out_path} (kid={kid[:24]}...)", flush=True)


def build_and_install_stdlib(
	repo_root: Path,
	stage: Path,
	dist: Path,
	version: str,
) -> tuple[Path, Path]:
	"""Full stdlib pipeline: build → sign → install → trust store.

	Returns (dmp_path, sig_path).
	"""
	dmp = build_stdlib_package(repo_root, stage, version)
	sig = sign_stdlib(repo_root, dmp)
	install_stdlib(dmp, sig, dist)
	generate_core_trust_store(sig, dist)

	# Verify outputs.
	expected = [
		dist / "lib" / "stdlib" / "std.dmp",
		dist / "lib" / "stdlib" / "std.sig",
		dist / "lib" / "compiler" / "lang" / "driftc" / "packages" / "core_trust.json",
	]
	for f in expected:
		if not f.exists():
			raise RuntimeError(f"expected output not found: {f}")

	print("[deploy] stdlib package installed and signed", flush=True)
	return dmp, sig
