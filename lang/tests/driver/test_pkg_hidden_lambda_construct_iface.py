# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: hidden lambda callback target from stdlib generic instantiation
must be included in package payload and resolved by consumers.

Self-contained test: builds a library package from source, signs it with an
ephemeral key, and consumes it through the full package-consumer path.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from lang.codegen.llvm.test_utils import sanitizer_timeout

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

LIB_SOURCE = """\
module mylib;

import std.core as core;
import std.concurrent as conc;

export { run_in_background };

pub fn run_in_background() nothrow -> conc.VirtualThread<Int> {
\tval cb = core.callback0(|| nothrow => { return 42; });
\treturn conc.spawn_cb(move cb);
}
"""

CONSUMER_SOURCE = """\
module consumer;

import mylib;

pub fn main() nothrow -> Int {
\tmylib.run_in_background();
\treturn 0;
}
"""


def test_pkg_hidden_lambda_construct_iface_resolved(tmp_path: Path) -> None:
	"""Package function calling conc.spawn_cb must include hidden lambda in payload."""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	from lang.tests.driver.pkg_test_helpers import publish_v1_pkg

	# Step 1: Build + sign library package via the shared v1
	# publisher (stamps SCI into the manifest and emits both
	# author and cert claim sidecars next to the .dmp).
	lib_dir = tmp_path / "lib_src"
	lib_dir.mkdir()
	(lib_dir / "mylib.drift").write_text(LIB_SOURCE)

	pkg_libs_root = tmp_path / "libs"
	trust_path = tmp_path / "trust.json"
	publish_v1_pkg(
		lib_dir=lib_dir,
		src_files=[lib_dir / "mylib.drift"],
		package_id="mylib",
		package_version="0.1.0",
		namespace_glob="mylib.*",
		dest_pkg_root=pkg_libs_root,
		dest_trust_path=trust_path,
		target="test-target",
		stdlib_root_override=stdlib,
	)
	pkg_root = pkg_libs_root / "mylib" / "0.1.0"

	# Step 5: Compile consumer against the signed library package
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(CONSUMER_SOURCE)

	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(consumer_dir / "consumer.drift"),
		"--stdlib-root", str(stdlib),
		"--target-word-bits", "64",
		"--package-root", str(tmp_path / "libs"),
		"--dep", "mylib@0.1.0",
		"--trust-store", str(trust_path),
		"--entry", "consumer::main",
		"--emit-ir", str(tmp_path / "consumer.ll"),
		"--test-build-only",
		"--json",
	]
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120))
	stdout = res.stdout.strip()
	if stdout:
		try:
			diag = json.loads(stdout)
			msgs = [d.get("message", "")[:200] for d in diag.get("diagnostics", [])]
		except json.JSONDecodeError:
			msgs = [stdout[:200]]
	else:
		msgs = []
	assert res.returncode == 0, f"consumer compilation failed — hidden lambda callback target not resolved: {msgs}"
