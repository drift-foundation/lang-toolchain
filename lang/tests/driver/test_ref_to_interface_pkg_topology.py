# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Three-package interface topology: interface in pkg A, impls + type in
pkg B, use from app C — for GENERIC interface instances, multi-instance,
owned AND widened dispatch.

This is where instance identity must survive two package boundaries: the
canonical keys (`type_key_string`) carry package/module identity, so pkg
A's `Sink<Int>` can never collide with a local or foreign `Sink<Int>`, and
`Sink<Int>` never satisfies `Sink<String>`. The consuming compiler sees
A + B + C in one linked semantic world (packages ship HIR) and emits every
vtable/drop helper itself — ABI stays 20.

Pins three fixes from the generic-instance widening slice:
- codegen impl index keyed on the canonical interface INSTANCE (was
  base-keyed with first-impl-wins merge — a silent wrong-dispatch
  miscompile with two instance impls, present on ≤certified 0.33.77);
- trait-world coherence is per instance (`implement sink.Sink<Int>` +
  `implement sink.Sink<String>` for one type are NOT E-IMPL-DUPLICATE);
- checker implements relation accepts exact-instance widening across
  package boundaries.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.packages.cert_claim_v1 import DepGraphEntry
from lang.driftc.parser import stdlib_root
from lang.tests.driver.pkg_test_helpers import publish_v1_pkg

ROOT = Path(__file__).resolve().parents[3]

_SINKLIB_SOURCE = """\
module sinklib;

export { Sink, use_int, use_str };

pub interface Sink<T> {
\tfn tag(self: &Self, v: T) nothrow -> Int;
}

pub fn use_int(s: &Sink<Int>) nothrow -> Int { return s.tag(2); }
pub fn use_str(s: &Sink<String>) nothrow -> Int { return s.tag("x"); }
"""

_BOXLIB_SOURCE = """\
module boxlib;
import sinklib;

export { Box, mk };

pub struct Box { pub n: Int }

pub fn mk(n: Int) nothrow -> Box { return Box(n = n); }

implement sinklib.Sink<Int> for Box {
\tfn tag(self: &Box, v: Int) nothrow -> Int { return self.n + v; }
}

implement sinklib.Sink<String> for Box {
\tfn tag(self: &Box, v: String) nothrow -> Int { return self.n * 100; }
}
"""

# Owned (s1/s2) and widened (b3/b4) dispatch, both instances, across both
# package boundaries. Exit code encodes the first failing check.
_APP_SOURCE = """\
module main;
import sinklib;
import boxlib;

pub fn main() nothrow -> Int {
\tvar b1 = boxlib.mk(40);
\tval s1: sinklib.Sink<Int> = move b1;
\tvar b2 = boxlib.mk(40);
\tval s2: sinklib.Sink<String> = move b2;
\tval r1 = sinklib.use_int(s1);
\tval r2 = sinklib.use_str(s2);
\tval b3 = boxlib.mk(40);
\tval b4 = boxlib.mk(40);
\tval r3 = sinklib.use_int(&b3);
\tval r4 = sinklib.use_str(&b4);
\tif r1 == 42 {
\t\tif r2 == 4000 {
\t\t\tif r3 == 42 {
\t\t\t\tif r4 == 4000 { return 0; }
\t\t\t\treturn 4;
\t\t\t}
\t\t\treturn 3;
\t\t}
\t\treturn 2;
\t}
\treturn 1;
}
"""


@pytest.fixture(scope="module")
def _abc_pkgs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	base = tmp_path_factory.mktemp("iface_abc")
	pkg_root = base / "pkgs"
	trust_path = base / "trust.json"
	trust: dict = {}

	def _write_trust() -> None:
		# `merge_into_trust` only updates the dict — the caller owns the
		# write (it is mutually exclusive with `dest_trust_path` inside
		# the helper).
		trust_path.write_text(
			json.dumps(trust, separators=(",", ":"), sort_keys=True),
			encoding="utf-8",
		)

	a_dir = base / "a"
	a_dir.mkdir()
	(a_dir / "sinklib.drift").write_text(_SINKLIB_SOURCE, encoding="utf-8")
	a_pub = publish_v1_pkg(
		lib_dir=a_dir,
		src_files=[a_dir / "sinklib.drift"],
		package_id="sinklib",
		package_version="0.0.1",
		dest_pkg_root=pkg_root,
		merge_into_trust=trust,
		stdlib_root_override=stdlib_root(),
	)
	_write_trust()

	b_dir = base / "b"
	b_dir.mkdir()
	(b_dir / "boxlib.drift").write_text(_BOXLIB_SOURCE, encoding="utf-8")
	publish_v1_pkg(
		lib_dir=b_dir,
		src_files=[b_dir / "boxlib.drift"],
		package_id="boxlib",
		package_version="0.0.1",
		dest_pkg_root=pkg_root,
		merge_into_trust=trust,
		package_deps=(("sinklib", "0.0"),),
		dep_pins=(("sinklib", "0.0.1"),),
		package_root_overrides=[pkg_root],
		trust_store_for_build=trust_path,
		stdlib_root_override=stdlib_root(),
		# The v1 verifier requires the cert claim's dep_graph to attest
		# every loaded dep.
		cert_dep_graph=(
			DepGraphEntry(
				package_id="sinklib",
				version="0.0.1",
				artifact_sha256=a_pub["artifact_sha256"],
				source_content_id=a_pub["sci"],
				author_kid=a_pub["kid"],
				cert_kid=a_pub["kid"],
				dep_kind="direct",
			),
		),
	)
	_write_trust()
	return pkg_root, trust_path


def test_generic_iface_pkg_topology_owned_and_widened(
	_abc_pkgs: tuple[Path, Path], tmp_path: Path
) -> None:
	pkg_root, trust_path = _abc_pkgs
	src = tmp_path / "main.drift"
	src.write_text(_APP_SOURCE, encoding="utf-8")
	out = tmp_path / "test_bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--package-root", str(pkg_root),
		"--dep", "sinklib@0.0.1",
		"--dep", "boxlib@0.0.1",
		"--trust-store", str(trust_path),
		str(src),
		"--entry", "main::main",
		"-o", str(out),
		"--json",
	]
	res = subprocess.run(
		cmd, capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
		timeout=sanitizer_timeout(300),
	)
	if res.stdout.strip():
		payload = json.loads(res.stdout)
		errs = [d["message"] for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
		assert payload.get("exit_code", res.returncode) == 0, f"consumer compile failed: {errs}"
	else:
		assert res.returncode == 0, f"consumer compile failed:\n{res.stderr[-1500:]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, (
		f"cross-package dispatch wrong (exit {run.returncode}: 1=owned-int, 2=owned-str, "
		f"3=widened-int, 4=widened-str); stderr:\n{run.stderr[-500:]}"
	)
