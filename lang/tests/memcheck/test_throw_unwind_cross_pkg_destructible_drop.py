# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG #102 (continued): cross-package variant.

The in-package case from `test_throw_unwind_destructible_drop.py` is
now clean after commit 07048d9b.  Singular 0.32.12 still leaks at the
identical stack — but `var lease: managed.LeasedConn` consumes a
Destructible declared in a DIFFERENT package (the mariadb-rpc pool /
managed package).  This regression isolates the cross-package shape
so we can verify whether the destructible-recognition path matches
the in-package one.

Hypothesis (precedent: LANGUAGE_BUG #97): when the type of an owned
local is a Destructible defined in another package,
`_type_is_destructible(ty)` may return False on the consumer side
(stale type-table snapshot, missing trait-impl import, etc.).  If so,
`_register_drop_local` skips the local entirely — it never enters
`_scope_stack` — and the LANGUAGE_BUG #102 cleanup hook has nothing
to drop on the throw-unwind edge.

Test layout:
  - Build a package `pkglease` defining a Destructible `Lease` with
    observable `destroy()` (prints "LEASE_DESTROYED") and a heap-
    allocated `Array<Byte>` payload (so a missed drop is visible to
    valgrind, not just to stdout).
  - Consumer imports `pkglease` and replays the singular shape:
    `var lease = pkglease.acquire()` inside `try { ... throw E1 ... }
    catch E2(_) {}` with the typed catch deliberately not matching.

Pass criteria are identical to the in-package test: stdout contains
"LEASE_DESTROYED" AND valgrind reports 0 leaks.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


LIB_SOURCE = """\
module pkglease;

import std.core as core;
import std.console as console;

export { acquire, Lease };

pub struct Lease {
\tpub label: String,
\tpub payload: Array<Byte>,
}

implement core.Destructible for Lease {
\tpub fn destroy(var self: Lease) nothrow -> Void {
\t\tconsole.print("LEASE_DESTROYED\\n");
\t\treturn;
\t}
}

fn _make_payload() nothrow -> Array<Byte> {
\tvar a: Array<Byte> = [];
\tvar i = 0;
\twhile i < 64 {
\t\ta.push(cast<Byte>(i));
\t\ti = i + 1;
\t}
\treturn move a;
}

pub fn acquire() nothrow -> Lease {
\treturn Lease(
\t\tlabel = "ACQUIRED_PAYLOAD_FROM_PACKAGE",
\t\tpayload = _make_payload()
\t);
}
"""

CONSUMER_SOURCE = """\
module consumer;

import std.core as core;
import pkglease;

pub error CaughtKind {}
pub error UncaughtKind { code: Int }

// Shape mirrors singular.gateway::SingularImpl::complete with a
// cross-package Destructible local.
fn inner(code: Int) -> Int {
\ttry {
\t\tvar lease = pkglease.acquire();
\t\tif code == 1 {
\t\t\treturn 1;
\t\t} else if code == 2 {
\t\t\treturn 2;
\t\t} else {
\t\t\tthrow UncaughtKind(code = code);
\t\t}
\t} catch CaughtKind(_e) {
\t}
\treturn 0;
}

pub fn main() nothrow -> Int {
\ttry {
\t\tval _r = inner(99);
\t} catch {
\t}
\treturn 0;
}
"""


def _build_signed_package(tmp_path: Path) -> tuple[Path, Path]:
	"""Build, sign, and set up the pkglease package. Returns (pkg_root, trust_path)."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid

	lib_dir = tmp_path / "lib_src"
	lib_dir.mkdir()
	(lib_dir / "pkglease.drift").write_text(LIB_SOURCE)

	from lang.tests.driver.pkg_test_helpers import emit_v1_sidecars_inline
	_TEST_SCI = "sha256:" + ("0" * 64)
	pkg_path = tmp_path / "pkglease.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "-M", str(lib_dir), str(lib_dir / "pkglease.drift"),
		 "--stdlib-root", str(stdlib_root()),
		 "--target-word-bits", "64",
		 "--package-id", "pkglease", "--package-version", "0.1.0",
		 "--package-target", "test-target",
		 "--source-content-id", _TEST_SCI,
		 "--emit-package", str(pkg_path), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"lib build failed: {res.stderr[:400]}"

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	pub_raw = pub.public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	pkg_root = tmp_path / "libs" / "pkglease" / "0.1.0"
	pkg_root.mkdir(parents=True)
	shutil.copy2(str(pkg_path), str(pkg_root / "pkglease.dmp"))
	emit_v1_sidecars_inline(
		pkg_root / "pkglease.dmp",
		package_id="pkglease", package_version="0.1.0",
		priv=priv, namespaces=["pkglease.*"],
	)
	(tmp_path / "trust.json").write_text(json.dumps({
		"format": "drift-trust", "version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"pkglease.*": {"authors": [kid], "certifiers": [kid]}},
		"revoked": [],
	}))
	return tmp_path / "libs", tmp_path / "trust.json"


def test_cross_pkg_destructible_throw_unwind_no_leak(tmp_path: Path) -> None:
	"""Cross-package Destructible local must drop on throw-unwind."""
	assert shutil.which("valgrind") is not None, "valgrind required"
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
		 "--package-root", str(pkg_root), "--dep", "pkglease@0.1.0",
		 "--trust-store", str(trust_path),
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:1200]}"
	assert out_bin.exists(), "binary not produced"

	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		["valgrind", "--tool=memcheck", "--leak-check=full",
		 "--show-leak-kinds=definite,indirect",
		 "--errors-for-leak-kinds=definite,indirect",
		 "--error-exitcode=97",
		 f"--log-file={vg_log}",
		 str(out_bin)],
		capture_output=True, text=True, timeout=60,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	indir_match = re.search(r"indirectly lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	indirectly_lost = int(indir_match.group(1).replace(",", "")) if indir_match else 0

	# Independent of memcheck: assert Destructible::destroy() actually
	# ran along the throw-unwind path.  If the consumer side fails to
	# recognise the cross-package destructible, this print is absent
	# even when (for unrelated reasons) memcheck shows no leak.
	assert "LEASE_DESTROYED" in vg.stdout, (
		f"Cross-package Destructible::destroy() did not run on the "
		f"throw-unwind path.\n"
		f"stdout: {vg.stdout!r}\nstderr: {vg.stderr[-300:]!r}\n"
		f"definitely lost: {definitely_lost} bytes; "
		f"indirectly lost: {indirectly_lost} bytes"
	)
	assert vg.returncode != 97, (
		f"Valgrind detected leaks on cross-package Destructible "
		f"throw-unwind.\n"
		f"definitely lost: {definitely_lost} bytes; "
		f"indirectly lost: {indirectly_lost} bytes\n"
		f"valgrind log (tail):\n{vg_output[-1200:]}"
	)
	assert definitely_lost == 0, f"definitely lost: {definitely_lost} bytes"
	assert indirectly_lost == 0, f"indirectly lost: {indirectly_lost} bytes"
