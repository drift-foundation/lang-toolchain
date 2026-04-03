# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: format_int String used directly as a map literal value must
not leak when compiled through the package-consumer path.

The bookkeeper pattern:
    val _ = logger.info("event", {"port": fmt.format_int(port)});

The format_int(port) temporary String must be consumed by the HashMap
insert, and released when the HashMap is destroyed. If the string_arc
pass fails to consume the temp (e.g., because the insert's param TypeId
is FORWARD_NOMINAL instead of SCALAR String), the temp leaks.

This test exercises the exact codepath: stdlib consumed as a signed
package (PEX path), map literal with function-call value expressions.
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
STDLIB_DIR = ROOT / "stdlib"
STD_VERSION = "0.0.0-test"

_skip_no_valgrind = pytest.mark.skipif(
	shutil.which("valgrind") is None,
	reason="valgrind not available",
)

# Consumer source: exact bookkeeper pattern.
# format_int result used directly in map literal, passed to logger.info.
CONSUMER_SIMPLE = """\
module consumer;

import std.core as core;
import std.format as fmt;
import std.containers as containers;

fn consume_map(m: containers.HashMap<String, String>) nothrow -> Int {
\treturn m.len();
}

pub fn main() nothrow -> Int {
\tval n = consume_map({"port": fmt.format_int(42)});
\tif n != 1 { return 1; }
\tval m = consume_map({"a": fmt.format_int(1), "b": fmt.format_int(2), "c": fmt.format_int(3)});
\tif m != 3 { return 2; }
\tval _ = consume_map({"port": fmt.format_int(18100)});
\treturn 0;
}
"""

# Consumer source: EXACT bookkeeper pattern with logger.info (log level filters).
CONSUMER_LOGGER_FILTERED = """\
module consumer;

import std.core as core;
import std.format as fmt;
import std.log as log;

pub fn main() nothrow -> Int {
\tvar cb = log.config_builder();
\tcb.sink(log.stderr_sink());
\tcb.min_level(log.Level::Error());
\tval cfg = cb.build();
\tval logger = log.create_logger("test", cfg);

\t// Exact bookkeeper pattern: format_int in map literal to logger.info.
\t// min_level=Error, so info is filtered — early exit path in _emit.
\tval _ = logger.info("startup", {"port": fmt.format_int(18100)});
\tval _ = logger.info("listening", {"port": fmt.format_int(18100)});
\tval _ = logger.info("shutdown", {"port": fmt.format_int(18100)});

\treturn 0;
}
"""

# Consumer source: logger.info with info-level enabled (full emit path).
# The HashMap goes through _emit_throwing, then is dropped at scope exit.
CONSUMER_LOGGER_EMIT = """\
module consumer;

import std.core as core;
import std.format as fmt;
import std.log as log;

pub fn main() nothrow -> Int {
\tvar cb = log.config_builder();
\tcb.sink(log.stderr_sink());
\tcb.min_level(log.Level::Debug());
\tval cfg = cb.build();
\tval logger = log.create_logger("test", cfg);

\t// Info level enabled — full emit path taken.
\t// HashMap goes through _emit_throwing, then core.drop_value(move attrs).
\tval _ = logger.info("startup", {"port": fmt.format_int(18100)});
\tval _ = logger.info("listening", {"port": fmt.format_int(18100)});
\tval _ = logger.info("shutdown", {"port": fmt.format_int(18100)});

\treturn 0;
}
"""


def _build_signed_stdlib(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
	"""Build signed stdlib package.

	Returns (pkg_root, trust_path, core_trust_path, empty_stdlib).
	"""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	stdlib_files = sorted(str(p) for p in STDLIB_DIR.rglob("*.drift"))
	assert stdlib_files, "no stdlib .drift files"

	pkg_dir = tmp_path / "libs"
	pkg_dir.mkdir(parents=True, exist_ok=True)
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)

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

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = (std_dest / "std.dmp").read_bytes()

	(std_dest / "std.sig").write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(priv.sign(pkg_bytes)).decode("ascii"),
			"pubkey": pub_b64}],
	}, separators=(",", ":"), sort_keys=True))

	core_trust_path = tmp_path / "core_trust.json"
	core_trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid], "lang.*": [kid], "drift.*": [kid]},
		"revoked": [],
	}, separators=(",", ":"), sort_keys=True))

	trust_path = tmp_path / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid]}, "revoked": [],
	}))

	return pkg_dir, trust_path, core_trust_path, empty_stdlib


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str]:
	"""Compile source against signed stdlib package and run under Valgrind.

	Returns (definitely_lost_bytes, valgrind_stderr).
	"""
	pkg_root, trust_path, core_trust_path, empty_stdlib = _build_signed_stdlib(tmp_path)

	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir(exist_ok=True)
	(consumer_dir / "consumer.drift").write_text(source)

	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--dev",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(empty_stdlib),
		 "--package-root", str(pkg_root),
		 "--dep", f"std@{STD_VERSION}",
		 "--trust-store", str(trust_path),
		 "--dev-core-trust-store", str(core_trust_path),
		 "--target-word-bits", "64",
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:2000]}"
	assert out_bin.exists(), f"[{label}] binary not produced"

	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, f"[{label}] binary returned {run.returncode}, expected 0"

	vg = subprocess.run(
		["valgrind", "--leak-check=full", "--error-exitcode=42", str(out_bin)],
		capture_output=True, text=True, timeout=30,
	)
	no_leaks = "no leaks are possible" in vg.stderr or "All heap blocks were freed" in vg.stderr
	lost_match = re.search(r"definitely lost: (\d+) bytes", vg.stderr)
	lost_bytes = int(lost_match.group(1)) if lost_match else (0 if no_leaks else -1)
	return lost_bytes, vg.stderr


@_skip_no_valgrind
def test_map_literal_format_int_simple(tmp_path: Path) -> None:
	"""format_int String in map literal passed to plain function — no leak."""
	lost, stderr = _compile_and_valgrind(tmp_path, CONSUMER_SIMPLE, label="simple")
	assert lost == 0, (
		f"Valgrind found {lost} bytes definitely lost (simple consume_map pattern).\n"
		f"Valgrind stderr:\n{stderr[-1000:]}"
	)


@_skip_no_valgrind
def test_map_literal_format_int_logger_filtered(tmp_path: Path) -> None:
	"""logger.info with min_level=Error — early exit path in _emit."""
	lost, stderr = _compile_and_valgrind(tmp_path, CONSUMER_LOGGER_FILTERED, label="filtered")
	assert lost == 0, (
		f"Valgrind found {lost} bytes definitely lost (logger filtered path).\n"
		f"Valgrind stderr:\n{stderr[-1000:]}"
	)


@_skip_no_valgrind
def test_map_literal_format_int_logger_emit(tmp_path: Path) -> None:
	"""EXACT bookkeeper pattern: logger.info with info-level ENABLED.

	The HashMap goes through the FULL emit path: _emit → _emit_throwing →
	sink → drop. This is the known-leaking pattern from the bookkeeper.
	"""
	lost, stderr = _compile_and_valgrind(tmp_path, CONSUMER_LOGGER_EMIT, label="emit")
	assert lost == 0, (
		f"Valgrind found {lost} bytes definitely lost (logger full emit path).\n"
		f"format_int String in map literal to logger.info leaks through PEX path.\n\n"
		f"Valgrind stderr:\n{stderr[-1500:]}"
	)
