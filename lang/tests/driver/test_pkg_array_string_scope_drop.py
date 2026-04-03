# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: Array<String> returned from a cross-package call must be
dropped (including its string elements) when the local goes out of scope.

This reproduces the drift-web router pattern where split_path() returns
Array<String> and the caller (add_group) never moves it — only borrows
elements.  On scope exit, the array and all contained strings must be freed.

Additionally tests a struct with Array<String> fields consumed from a
package, verifying that struct field drops recurse correctly.

Proven discriminator for the bookkeeper leak:
  - Source-built: 0 leaks (has_drop returns True via struct instance)
  - Consumer-built: potential leak (struct instance may be missing,
    has_drop falls through to False)
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root
from lang.language_runtime import build_runtime_archive, runtime_archive_path, runtime_archive_variant

ROOT = Path(__file__).resolve().parents[3]

_skip_no_valgrind = pytest.mark.skipif(
	shutil.which("valgrind") is None,
	reason="valgrind not available",
)

# Library: provides split_path returning Array<String> and a struct with
# Array<String> fields — mirrors the drift-web router.
#
# CRITICAL: The leak-relevant functions (add_group, add_route) are IN the
# library, not the consumer. The Array<String> local that leaks is created
# and dropped INSIDE a library function. This exercises the package MIR
# scope-exit drop path, not the consumer's.
LIB_SOURCE = """\
module pathlib;

import std.core as core;
import std.io as io;

export {
\tRoutePattern, Group, Registry,
\tsplit_path, parse_pattern,
\tnew_registry, add_group, add_route, route_count
};

pub struct RoutePattern {
\tpub raw: String,
\tpub segments: Array<String>,
\tpub param_names: Array<String>,
\tpub seg_count: Int
}

pub struct Group {
\tpub id: Int,
\tpub prefix: String
}

pub struct Registry {
\tpub patterns: Array<RoutePattern>,
\tpub group_prefixes: Array<String>,
\tpub group_count: Int
}

fn _substring(s: &String, start: Int, end_pos: Int) nothrow -> String {
\tval n = end_pos - start;
\tif n <= 0 {
\t\treturn "";
\t}
\tvar b = io.buffer(n);
\tvar i = 0;
\twhile i < n {
\t\tio.buffer_write(&mut b, i, core.string_byte_at(s, start + i));
\t\ti = i + 1;
\t}
\treturn core.string_from_utf8_bytes(io.buffer_ptr(&b), n);
}

fn _dup_string(s: &String) nothrow -> String {
\tval n = s.byte_length();
\tvar b = io.buffer(n);
\tvar i = 0;
\twhile i < n {
\t\tio.buffer_write(&mut b, i, core.string_byte_at(s, i));
\t\ti = i + 1;
\t}
\treturn core.string_from_utf8_bytes(io.buffer_ptr(&b), n);
}

// Split path on '/' — returns Array<String> of owned substrings.
pub fn split_path(path: &String) nothrow -> Array<String> {
\tvar result: Array<String> = [];
\tval len = path.byte_length();
\tif len == 0 {
\t\treturn move result;
\t}
\tvar start = 0;
\tif core.string_byte_at(path, 0) == cast<Byte>(47) {
\t\tstart = 1;
\t}
\tvar seg_start = start;
\tvar i = start;
\twhile i < len {
\t\tif core.string_byte_at(path, i) == cast<Byte>(47) {
\t\t\tif i > seg_start {
\t\t\t\tresult.push(_substring(path, seg_start, i));
\t\t\t}
\t\t\tseg_start = i + 1;
\t\t}
\t\ti = i + 1;
\t}
\tif seg_start < len {
\t\tresult.push(_substring(path, seg_start, len));
\t}
\treturn move result;
}

// Parse a route pattern — returns struct with Array<String> fields.
pub fn parse_pattern(path: &String) nothrow -> RoutePattern {
\tval segs = split_path(path);
\tvar segments: Array<String> = [];
\tvar param_names: Array<String> = [];
\tvar i = 0;
\twhile i < segs.len {
\t\tsegments.push(_dup_string(&segs[i]));
\t\tvar empty = "";
\t\tparam_names.push(move empty);
\t\ti = i + 1;
\t}
\tvar count = segs.len;
\tvar raw = _dup_string(path);
\treturn RoutePattern(
\t\traw = move raw,
\t\tsegments = move segments,
\t\tparam_names = move param_names,
\t\tseg_count = move count
\t);
}

pub fn new_registry() nothrow -> Registry {
\tvar patterns: Array<RoutePattern> = [];
\tvar prefixes: Array<String> = [];
\treturn Registry(
\t\tpatterns = move patterns,
\t\tgroup_prefixes = move prefixes,
\t\tgroup_count = 0
\t);
}

// EXACT add_group pattern from drift-web:
// split_path creates Array<String>, only borrowed for validation,
// never moved — must be dropped on scope exit INSIDE THIS FUNCTION.
pub fn add_group(reg: &mut Registry, prefix: String) nothrow -> Group {
\tval segs = split_path(&prefix);
\t// Borrow segments for validation — never move segs.
\tvar i = 0;
\twhile i < segs.len {
\t\tval seg = &segs[i];
\t\tval _ = seg.byte_length();
\t\ti = i + 1;
\t}
\tval id = reg.group_count;
\treg.group_prefixes.push(move prefix);
\treg.group_count = reg.group_count + 1;
\treturn Group(id = id, prefix = reg.group_prefixes[id]);
\t// segs (Array<String> with ["api", "v1"]) must be dropped here.
\t// If the compiler misses this scope-exit drop, the strings leak.
}

// EXACT add_route pattern: parse_pattern creates a RoutePattern (struct
// with Array<String> fields) that is moved into the registry. But
// parse_pattern internally creates a temporary segs Array<String> that
// must also be dropped.
pub fn add_route(reg: &mut Registry, path: String) nothrow -> Int {
\tval pattern = parse_pattern(&path);
\tval count = pattern.seg_count;
\treg.patterns.push(move pattern);
\treturn count;
}

pub fn route_count(reg: &Registry) nothrow -> Int {
\treturn reg.patterns.len;
}
"""

# Consumer: calls library functions that internally create and drop
# Array<String> locals. The leak-relevant drops happen INSIDE the library,
# not here.
CONSUMER_SOURCE = """\
module consumer;

import std.core as core;
import pathlib;

pub fn main() nothrow -> Int {
\tvar reg = pathlib.new_registry();

\t// add_group internally splits "/api/v1" into ["api", "v1"],
\t// borrows for validation, then drops the Array<String> on scope exit.
\tval g = pathlib.add_group(&mut reg, "/api/v1");

\t// add_route internally creates a RoutePattern with Array<String> fields,
\t// moves it into registry. The temporary segs array inside parse_pattern
\t// must also be dropped.
\tval n1 = pathlib.add_route(&mut reg, "/api/v1/submit");
\tval n2 = pathlib.add_route(&mut reg, "/api/v1/status");

\tval total = pathlib.route_count(&reg);

\t// Expected: g.id=0, n1=3, n2=3, total=2
\t// Return 0 on success.
\tif g.id != 0 { return 1; }
\tif n1 != 3 { return 2; }
\tif n2 != 3 { return 3; }
\tif total != 2 { return 4; }
\treturn 0;
\t// reg (Registry) goes out of scope — must drop patterns Array<RoutePattern>,
\t// recursing into each pattern's Array<String> fields.
}
"""


def _sign_package(pkg_path: Path, pkg_id: str, version: str, tmp_path: Path, trust_path: Path | None = None) -> tuple[str, str]:
	"""Sign a .dmp and write .sig sidecar. Returns (kid, pub_b64)."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = pkg_path.read_bytes()

	sig_path = pkg_path.with_suffix(".sig")
	sig_path.write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(priv.sign(pkg_bytes)).decode("ascii"),
			"pubkey": pub_b64}],
	}, separators=(",", ":"), sort_keys=True))
	return kid, pub_b64


def _build_signed_package(tmp_path: Path) -> tuple[Path, Path]:
	"""Build, sign, and set up pathlib package (source stdlib). Returns (pkg_root, trust_path)."""
	lib_dir = tmp_path / "lib_src"
	lib_dir.mkdir()
	(lib_dir / "pathlib.drift").write_text(LIB_SOURCE)

	pkg_path = tmp_path / "pathlib.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(lib_dir), str(lib_dir / "pathlib.drift"),
		 "--stdlib-root", str(stdlib_root()),
		 "--target-word-bits", "64",
		 "--package-id", "pathlib", "--package-version", "0.1.0",
		 "--package-target", "test-target",
		 "--emit-package", str(pkg_path), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"lib build failed: {res.stderr[:500]}"

	pkg_root = tmp_path / "libs" / "pathlib" / "0.1.0"
	pkg_root.mkdir(parents=True)
	shutil.copy2(str(pkg_path), str(pkg_root / "pathlib.dmp"))
	kid, pub_b64 = _sign_package(pkg_root / "pathlib.dmp", "pathlib", "0.1.0", tmp_path)

	(tmp_path / "trust.json").write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"pathlib.*": [kid]}, "revoked": [],
	}))
	return tmp_path / "libs", tmp_path / "trust.json"


def _build_two_layer_packages(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
	"""Build signed stdlib + signed pathlib (full PEX path).

	Returns (pkg_root, trust_path, core_trust_path, empty_stdlib).
	"""
	STDLIB_DIR = ROOT / "stdlib"
	STD_VERSION = "0.0.0-test"

	# 1. Build signed stdlib package
	pkg_dir = tmp_path / "libs"
	pkg_dir.mkdir(parents=True, exist_ok=True)
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)

	stdlib_files = sorted(str(p) for p in STDLIB_DIR.rglob("*.drift"))
	assert stdlib_files, "no stdlib .drift files"

	std_pkg_path = tmp_path / "std_build" / "std.dmp"
	std_pkg_path.parent.mkdir(parents=True, exist_ok=True)
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--dev", "-M", str(STDLIB_DIR),
		 "--stdlib-root", str(empty_stdlib),
		 *stdlib_files,
		 "--package-id", "std",
		 "--package-version", STD_VERSION,
		 "--package-target", "test-target",
		 "--emit-package", str(std_pkg_path),
		 "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"stdlib build failed: {res.stderr[:500]}"

	std_dest = pkg_dir / "std" / STD_VERSION
	std_dest.mkdir(parents=True)
	shutil.copy2(str(std_pkg_path), str(std_dest / "std.dmp"))
	std_kid, std_pub = _sign_package(std_dest / "std.dmp", "std", STD_VERSION, tmp_path)

	# 2. Build pathlib package against stdlib-as-package
	lib_dir = tmp_path / "lib_src"
	lib_dir.mkdir(exist_ok=True)
	(lib_dir / "pathlib.drift").write_text(LIB_SOURCE)

	pathlib_pkg_path = tmp_path / "pathlib_build" / "pathlib.dmp"
	pathlib_pkg_path.parent.mkdir(parents=True, exist_ok=True)

	# Core trust for stdlib namespace
	core_trust_path = tmp_path / "core_trust.json"
	core_trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {std_kid: {"algo": "ed25519", "pubkey": std_pub}},
		"namespaces": {"std.*": [std_kid], "lang.*": [std_kid], "drift.*": [std_kid]},
		"revoked": [],
	}, separators=(",", ":"), sort_keys=True))

	# Build pathlib consuming stdlib as package (NOT source)
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--dev",
		 "-M", str(lib_dir), str(lib_dir / "pathlib.drift"),
		 "--stdlib-root", str(empty_stdlib),
		 "--package-root", str(pkg_dir),
		 "--dep", f"std@{STD_VERSION}",
		 "--dev-core-trust-store", str(core_trust_path),
		 "--target-word-bits", "64",
		 "--package-id", "pathlib", "--package-version", "0.1.0",
		 "--package-target", "test-target",
		 "--emit-package", str(pathlib_pkg_path),
		 "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"pathlib build (pkg stdlib) failed: {res.stderr[:500]}"

	pathlib_dest = pkg_dir / "pathlib" / "0.1.0"
	pathlib_dest.mkdir(parents=True)
	shutil.copy2(str(pathlib_pkg_path), str(pathlib_dest / "pathlib.dmp"))
	pathlib_kid, pathlib_pub = _sign_package(pathlib_dest / "pathlib.dmp", "pathlib", "0.1.0", tmp_path)

	# Trust store covering both namespaces
	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {
			std_kid: {"algo": "ed25519", "pubkey": std_pub},
			pathlib_kid: {"algo": "ed25519", "pubkey": pathlib_pub},
		},
		"namespaces": {
			"std.*": [std_kid],
			"pathlib.*": [pathlib_kid],
		},
		"revoked": [],
	}, separators=(",", ":"), sort_keys=True))

	return pkg_dir, trust_path, core_trust_path, empty_stdlib


@_skip_no_valgrind
def test_library_internal_array_scope_drop(tmp_path: Path) -> None:
	"""Array<String> created and dropped INSIDE a package function must not leak.

	This is the exact bookkeeper/drift-web pattern: add_group() internally
	calls split_path(), borrows the result, then the Array<String> goes out
	of scope inside the library function. The consumer never sees it.
	"""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	pkg_root, trust_path = _build_signed_package(tmp_path)

	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(CONSUMER_SOURCE)

	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(stdlib), "--target-word-bits", "64",
		 "--package-root", str(pkg_root), "--dep", "pathlib@0.1.0",
		 "--trust-store", str(trust_path),
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:2000]}"
	assert out_bin.exists(), "binary not produced"

	# Verify correct exit code first
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, f"binary returned {run.returncode}, expected 0"

	# Run under Valgrind
	vg = subprocess.run(
		["valgrind", "--leak-check=full", "--error-exitcode=42", str(out_bin)],
		capture_output=True, text=True, timeout=30,
	)
	no_leaks = "no leaks are possible" in vg.stderr or "All heap blocks were freed" in vg.stderr
	lost_match = re.search(r"definitely lost: (\d+) bytes", vg.stderr)
	lost_bytes = int(lost_match.group(1)) if lost_match else (0 if no_leaks else -1)

	assert lost_bytes == 0, (
		f"Valgrind found {lost_bytes} bytes definitely lost.\n"
		f"Array<String> scope-exit drop inside package function is missing.\n"
		f"Valgrind stderr:\n{vg.stderr[-1000:]}"
	)


@_skip_no_valgrind
def test_two_layer_package_array_scope_drop(tmp_path: Path) -> None:
	"""Full PEX path: stdlib=package, pathlib=package (built against pkg stdlib).

	This is the exact bookkeeper topology:
	  bookkeeper → drift-web.dmp (built against std.dmp) → std.dmp
	The pathlib MIR was generated with stdlib as a package, and the consumer
	also consumes both as packages. This is the path most likely to have
	missing struct instances or stale type table entries.
	"""
	pkg_root, trust_path, core_trust_path, empty_stdlib = _build_two_layer_packages(tmp_path)

	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir(exist_ok=True)
	(consumer_dir / "consumer.drift").write_text(CONSUMER_SOURCE)

	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--dev",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(empty_stdlib),
		 "--package-root", str(pkg_root),
		 "--dep", "std@0.0.0-test",
		 "--dep", "pathlib@0.1.0",
		 "--trust-store", str(trust_path),
		 "--dev-core-trust-store", str(core_trust_path),
		 "--target-word-bits", "64",
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:2000]}"
	assert out_bin.exists(), "binary not produced"

	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, f"binary returned {run.returncode}, expected 0"

	vg = subprocess.run(
		["valgrind", "--leak-check=full", "--error-exitcode=42", str(out_bin)],
		capture_output=True, text=True, timeout=30,
	)
	no_leaks = "no leaks are possible" in vg.stderr or "All heap blocks were freed" in vg.stderr
	lost_match = re.search(r"definitely lost: (\d+) bytes", vg.stderr)
	lost_bytes = int(lost_match.group(1)) if lost_match else (0 if no_leaks else -1)

	assert lost_bytes == 0, (
		f"Valgrind found {lost_bytes} bytes definitely lost on full PEX path.\n"
		f"Two-layer package: stdlib=pkg, pathlib=pkg (built against pkg stdlib).\n"
		f"Valgrind stderr:\n{vg.stderr[-1000:]}"
	)
