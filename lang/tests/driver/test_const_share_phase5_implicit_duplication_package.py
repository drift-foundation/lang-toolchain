# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 5 implicit ConstShare duplication — package roundtrip.

Producer publishes a synthesized-ConstShare struct (Phase 1 shape:
`Holder { handle: core.ConstArc<String> }`).  Consumer imports the
package, instantiates `lib.Holder`, and exercises implicit
duplication at the major value-flow sites:

  - `val b = a`             (HLet.value)
  - `takes_owned(a)`        (HCall.args[i])
  - `return a`              (HReturn.value)

The walker that installs the wrap calls runs on the consumer side
during typecheck of the consumer's body; for the rewrite to fire,
it must:

  - resolve `Holder`'s type to a non-Copy concrete TypeId,
  - prove `Holder is shareable.ConstShare` against the
    consumer-side trait world (which loaded the producer's
    synthesized impl from the .dmp's `impl_headers`),
  - dispatch `const_share` against the producer's synthesized
    method (whose fn_id and HIR body came from the .dmp's
    `signatures` + `hir_funcs`).

Pinning this case closes the same risk class Phase 3 / Phase 4
package roundtrips closed for the EXPLICIT `.const_share()` form:
package-imported synthesized ConstShare types must participate in
implicit duplication just like locally-declared types.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


LIB_SOURCE = """\
module sharedlib;

import std.core as core;

export { Holder, make_holder };

pub struct Holder {
\tpub handle: core.ConstArc<String>
}

pub fn make_holder(s: String) nothrow -> Holder {
\treturn Holder(handle = core.const_arc<type String>(move s));
}
"""


CONSUMER_SOURCE = """\
module main;

import std.core as core;
import sharedlib as lib;

fn takes_owned(h: lib.Holder) nothrow -> Int {
\treturn 0;
}

fn dup(h: lib.Holder) nothrow -> lib.Holder {
\treturn h;
}

fn main() nothrow -> Int {
\tval a = lib.make_holder("hi");
\tval b = a;
\tval n = takes_owned(a);
\tval c = dup(a);
\treturn n;
}
"""


def _b64(data: bytes) -> str:
	import base64
	return base64.b64encode(data).decode("ascii")


def _publish_signed_pkg(
	lib_dir: Path,
	*,
	src_files: list[Path],
	package_id: str,
	package_version: str,
	namespace_glob: str,
	dest_pkg_root: Path,
	dest_trust_path: Path,
) -> None:
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.drift.crypto import compute_ed25519_kid

	pkg_path = lib_dir / f"{package_id}.dmp"
	rc = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc",
			"--dev",
			"-M", str(lib_dir),
			"--stdlib-root", str(stdlib_root()),
			*[str(p) for p in src_files],
			"--package-id", package_id,
			"--package-version", package_version,
			"--package-target", "drift-dev",
			"--emit-package", str(pkg_path),
			"--json",
		],
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	assert rc.returncode == 0, f"lib '{package_id}' build failed:\n{rc.stdout}\n---\n{rc.stderr[:1000]}"

	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	sidecar = {
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw), "pubkey": pub_b64}],
	}
	sig_path = pkg_path.with_suffix(".sig")
	sig_path.write_text(json.dumps(sidecar, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	trust = {
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {namespace_glob: [kid]},
		"revoked": [],
	}
	dest_trust_path.write_text(json.dumps(trust, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	dest_dir = dest_pkg_root / package_id / package_version
	dest_dir.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(pkg_path), str(dest_dir / f"{package_id}.dmp"))
	shutil.copy2(str(sig_path), str(dest_dir / f"{package_id}.sig"))


@pytest.fixture(scope="module")
def _built_sharedlib(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	base = tmp_path_factory.mktemp("cs_phase5_pkg")
	lib_dir = base / "lib"
	lib_dir.mkdir(parents=True, exist_ok=True)
	(lib_dir / "sharedlib.drift").write_text(LIB_SOURCE, encoding="utf-8")

	pkg_root = base / "pkg_root"
	trust_path = base / "trust.json"
	_publish_signed_pkg(
		lib_dir,
		src_files=[lib_dir / "sharedlib.drift"],
		package_id="sharedlib",
		package_version="1.0.0",
		namespace_glob="sharedlib.*",
		dest_pkg_root=pkg_root,
		dest_trust_path=trust_path,
	)
	return pkg_root, trust_path


def _compile_consumer(
	source: str,
	*,
	pkg_root: Path,
	trust_path: Path,
	tmp_path: Path,
	dep: str = "sharedlib@1.0.0",
) -> tuple[int, list[str]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "a.out"

	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--package-root", str(pkg_root),
		"--dep", dep,
		"--trust-store", str(trust_path),
		str(src),
		"-o", str(out),
		"--json",
		"--test-build-only",
	]
	rc = subprocess.run(
		cmd,
		capture_output=True, text=True, cwd=str(ROOT),
		env={**os.environ, "PYTHONPATH": str(ROOT)},
	)
	if not rc.stdout.strip():
		return rc.returncode, [rc.stderr[:2000]]
	result = json.loads(rc.stdout)
	msgs = [d["message"] for d in result.get("diagnostics", []) if d.get("severity") == "error"]
	return result.get("exit_code", rc.returncode), msgs


def test_phase5_implicit_duplication_works_for_packaged_synth_const_share(
	_built_sharedlib: tuple[Path, Path], tmp_path: Path,
) -> None:
	"""Pin: implicit ConstShare duplication fires on the consumer
	side for a package-imported synthesized ConstShare struct.
	The walker resolves `lib.Holder` to a non-Copy concrete
	TypeId and proves ConstShare against the package-loaded
	trait world; method dispatch routes to the producer's
	synthesized `Holder::ConstShare::const_share`."""
	pkg_root, trust_path = _built_sharedlib
	exit_code, msgs = _compile_consumer(
		CONSUMER_SOURCE,
		pkg_root=pkg_root, trust_path=trust_path, tmp_path=tmp_path,
	)
	if exit_code != 0:
		pytest.fail(
			f"package-imported synthesized ConstShare type must "
			f"participate in implicit duplication on the consumer.  "
			f"exit_code={exit_code} diagnostics:\n" + "\n".join(msgs)
		)
