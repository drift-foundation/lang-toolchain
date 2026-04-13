# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Package-consumer e2e driver tests.

These tests compile programs against stdlib loaded as a signed .dmp package
(--package-root + --dep std@VERSION) and exercise code paths that are only
reachable through the package-consumer pipeline.

ASAN-compatible: the spawned driftc subprocess honors DRIFT_ASAN=1 and
selects the ASAN runtime archive + -fsanitize=address automatically.

Migrated from lang/tests/codegen/e2e/ cases marked package_consumer_only.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile_consumer(
	source: str,
	*,
	stdlib_pkg: "StdlibPackage",
	tmp_path: Path,
	entry: str = "main::main",
	expect_failure: bool = False,
) -> "subprocess.CompletedProcess[str] | Path":
	"""Compile a consumer program against stdlib as a package.

	When expect_failure is False (default), asserts compile succeeds and
	returns the path to the linked binary.

	When expect_failure is True, returns the CompletedProcess so the caller
	can assert on diagnostics and return code.
	"""
	from conftest import StdlibPackage  # type: ignore[import]

	src_dir = tmp_path / "src"
	src_dir.mkdir(exist_ok=True)
	(src_dir / "main.drift").write_text(source)

	out_bin = tmp_path / "test_bin"
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(exist_ok=True)
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(src_dir / "main.drift"),
		"--stdlib-root", str(empty_stdlib),
		"--target-word-bits", "64",
		"--package-root", str(stdlib_pkg.pkg_root),
		"--dep", f"std@{stdlib_pkg.version}",
		"--trust-store", str(stdlib_pkg.trust_path),
		"--dev", "--dev-core-trust-store", str(stdlib_pkg.trust_path),
		"--entry", entry,
		"-o", str(out_bin),
	]

	assert str(stdlib_pkg.stdlib_root) not in " ".join(cmd), (
		"consumer compile must not use the real stdlib source tree"
	)

	res = subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)

	if expect_failure:
		return res

	assert res.returncode == 0, (
		f"consumer compile failed (stdlib-as-package path):\n{res.stderr[:500]}"
	)
	assert out_bin.exists(), "binary not produced"
	return out_bin


def _run_binary(binary: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
	"""Run a compiled binary and return the result."""
	return subprocess.run(
		[str(binary)], capture_output=True, text=True, timeout=sanitizer_timeout(timeout),
	)


# ---------------------------------------------------------------------------
# 1. pkg_vis_source_private_method_rejected
#    K25-guard: calling private/non-exported method from a package module
#    must be rejected at the consumer compile boundary.
# ---------------------------------------------------------------------------


def test_pkg_vis_source_private_method_rejected(stdlib_package, tmp_path: Path) -> None:
	"""Calling @test_build_only __test_validate from nothrow context must be rejected.

	K25-guard: the method is pub but returns Bool (potentially throwing).
	Calling it from a nothrow entrypoint must produce a diagnostic that
	references the offending method call.  This validates that the
	package-consumer type checker enforces nothrow discipline across the
	package boundary.
	"""
	source = """\
module m;

import std.containers as c;

fn main() nothrow -> Int {
\tvar tm: c.TreeMap<Int, Int> = c.tree_map();
\ttm.__test_validate();
\treturn 0;
}
"""
	res = _compile_consumer(
		source,
		stdlib_pkg=stdlib_package,
		tmp_path=tmp_path,
		entry="m::main",
		expect_failure=True,
	)
	assert res.returncode != 0, (
		"compile should have failed: calling throwing __test_validate "
		"from nothrow context must be rejected across package boundary"
	)
	assert "nothrow" in res.stderr or "__test_validate" in res.stderr, (
		f"diagnostic should reference nothrow violation, got:\n{res.stderr[:500]}"
	)


# ---------------------------------------------------------------------------
# 2. pkg_wrap_method_fnresult_boundary
#    FnResult canonicalization: nothrow method wrapper with generic return
#    type across package boundary must not trigger FnResult ok-type divergence.
# ---------------------------------------------------------------------------


def test_pkg_wrap_method_fnresult_boundary(stdlib_package, tmp_path: Path) -> None:
	"""FnResult wrapper return types must stay consistent across package boundary."""
	source = """\
module m;

import std.containers as c;
import std.iter as iter;

use trait iter.Iterable;
use trait iter.SinglePassIterator;

fn main() nothrow -> Int {
\tvar map: c.HashMap<String, Int> = {"a": 1, "b": 2};

\tmatch map.remove("a") {
\t\tSome(v) => {
\t\t\tif v != 1 { return 1; }
\t\t},
\t\tdefault => { return 2; }
\t}

\tvar arr = [10, 20, 30];
\tvar it = arr.iter();
\tvar count = 0;
\twhile true {
\t\tmatch it.next() {
\t\t\tSome(_) => { count = count + 1; },
\t\t\tdefault => { break; }
\t\t}
\t}
\tif count != 3 { return 3; }

\tif map.len() != 1 { return 4; }

\treturn 0;
}
"""
	binary = _compile_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, entry="m::main",
	)
	res = _run_binary(binary)
	assert res.returncode == 0, (
		f"binary exited with code {res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)


# ---------------------------------------------------------------------------
# 3. pkg_env_get_has_boundary
#    std.env.get/has must work through signed package boundary.
# ---------------------------------------------------------------------------


def test_pkg_env_get_has_boundary(stdlib_package, tmp_path: Path) -> None:
	"""std.env get/has runtime helpers must function across package boundary."""
	source = """\
module m;

import std.env as env;

fn main() nothrow -> Int {
\tmatch env.get("HOME") {
\t\tOptional::Some(v) => {
\t\t\tif v.byte_length() == 0 {
\t\t\t\treturn 1;
\t\t\t}
\t\t},
\t\tOptional::None() => {
\t\t\treturn 2;
\t\t}
\t}
\tmatch env.get("DRIFT_PKG_TEST_UNSET_XYZ_99") {
\t\tOptional::Some(_v) => {
\t\t\treturn 3;
\t\t},
\t\tOptional::None() => { }
\t}
\tif !env.has("HOME") {
\t\treturn 4;
\t}
\tif env.has("DRIFT_PKG_TEST_UNSET_XYZ_99") {
\t\treturn 5;
\t}
\treturn 0;
}
"""
	binary = _compile_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, entry="m::main",
	)
	res = _run_binary(binary)
	assert res.returncode == 0, (
		f"binary exited with code {res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)


# ---------------------------------------------------------------------------
# 4. pkg_ext_module_trait_scope
#    K25: external module trait scope + visibility must be populated for
#    generic template re-instantiation (e.g. iter/next in std.log._attrs_json).
# ---------------------------------------------------------------------------


def test_pkg_ext_module_trait_scope(stdlib_package, tmp_path: Path) -> None:
	"""log.create_logger must work across package boundary (K25 trait scope)."""
	source = """\
module m;

import std.log as log;

fn main() nothrow -> Int {
\tvar cfg_builder = log.config_builder();
\tcfg_builder.min_level(log.Level::Debug());
\tcfg_builder.sink(log.stderr_sink());
\tval cfg = cfg_builder.build();
\tval logger = log.create_logger("test", cfg);
\tif not logger.info("ev", {"k": 1}) {
\t\treturn 1;
\t}
\treturn 0;
}
"""
	binary = _compile_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, entry="m::main",
	)
	res = _run_binary(binary)
	assert res.returncode == 0, (
		f"binary exited with code {res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)
	# Verify stderr contains the expected JSON log line.
	assert res.stderr.strip(), "expected JSON log output on stderr"
	log_obj = json.loads(res.stderr.strip().splitlines()[-1])
	assert log_obj["level"] == "info"
	assert log_obj["ev"] == "ev"
	assert log_obj["logger"] == "test"
	assert log_obj["attrs"] == {"k": 1}


# ---------------------------------------------------------------------------
# 5. pkg_iface_impl_vtable
#    K26: interface impl vtable must be populated for external package trait
#    impls (Sink for StdErrSink).
# ---------------------------------------------------------------------------


def test_pkg_iface_impl_vtable(stdlib_package, tmp_path: Path) -> None:
	"""K26 vtable for external trait impls must work across package boundary."""
	source = """\
module m;

import std.log as log;

fn main() nothrow -> Int {
\tvar cfg_builder = log.config_builder();
\tcfg_builder.min_level(log.Level::Debug());
\tcfg_builder.sink(log.stderr_sink());
\tval cfg = cfg_builder.build();
\tval logger = log.create_logger("test", cfg);
\treturn 0;
}
"""
	binary = _compile_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, entry="m::main",
	)
	res = _run_binary(binary)
	assert res.returncode == 0, (
		f"binary exited with code {res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)
