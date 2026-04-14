#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""K28 delta-reduction repro harness.

Mirrors lang/tests/driver/test_external_consumer.py::test_ext_cross_package_or_throw
as a standalone script so we can iterate one variable at a time.

Usage:
	.venv/bin/python work/k28-delta-reduction/repro.py [VARIANT]

Variants:
	baseline   — exact xfail fixture (expected: FAIL)
	v1         — baseline minus --stdlib-root on consumer
	v2         — v1 plus --target-word-bits 64
	v3         — v2 with producer built via drift deploy (TODO)
	v4         — v3 with web-style naming (or-throw-probe / probe) (TODO)
	v5         — v4 with web-style trust/staging (TODO)
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from lang.driftc.driftc import main as driftc_main  # noqa: E402
from lang.driftc.packages.signature_v0 import compute_ed25519_kid  # noqa: E402


# ── Helpers (lifted from test_external_consumer.py) ─────────────────


def _b64(data: bytes) -> str:
	return base64.b64encode(data).decode("ascii")


def _sha256_hex(data: bytes) -> str:
	return sha256(data).hexdigest()


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _public_key_bytes(pub) -> bytes:
	if hasattr(pub, "public_bytes_raw"):
		return pub.public_bytes_raw()
	return pub.public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)


def _write_trust_store(path: Path, *, kid: str, pub_b64: str, namespaces: list[str]) -> None:
	obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {ns: [kid] for ns in namespaces},
		"revoked": [],
	}
	_write_file(path, json.dumps(obj, separators=(",", ":"), sort_keys=True))


def _write_sig_sidecar(pkg_path: Path, *, pkg_bytes: bytes, kid: str, sig_raw: bytes, pub_b64: str) -> Path:
	pkg_sha_hex = _sha256_hex(pkg_bytes)
	entry: dict = {"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw), "pubkey": pub_b64}
	sidecar = pkg_path.with_suffix(".sig")
	obj = {
		"format": "dmir-pkg-sig",
		"version": 0,
		"package_sha256": f"sha256:{pkg_sha_hex}",
		"signatures": [entry],
	}
	_write_file(sidecar, json.dumps(obj, separators=(",", ":"), sort_keys=True))
	return sidecar


@dataclass(frozen=True)
class _DeployKeys:
	priv: Ed25519PrivateKey
	kid: str
	pub_b64: str


def _gen_keys() -> _DeployKeys:
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)
	return _DeployKeys(priv=priv, kid=kid, pub_b64=pub_b64)


def _emit_pkg_args(package_id: str) -> list[str]:
	return [
		"--package-id", package_id,
		"--package-version", "0.0.0",
		"--package-target", "test-target",
	]


# ── Producer source ─────────────────────────────────────────────────


_ACME_THROWER_SOURCE = """\
module acme.thrower;

import std.core as core;

export { ProducerError, make_err };

pub struct ProducerError {
	pub code: Int
}

implement core.Throw for ProducerError {
	pub fn throw_self(self: ProducerError) throws {
		throw std.err:ResultError(dv = DiagnosticValue::Int(self.code));
	}
}

pub fn make_err() nothrow -> core.Result<Int, ProducerError> {
	return core.Result::Err(ProducerError(code = 42));
}
"""


# Web-probe full rename: module name, package id, type names — all matched.
_PROBE_SOURCE = """\
module probe;

import std.core as core;

export { ProbeError, ProbeException, make_probe_ok, make_probe_err };

pub struct ProbeError { pub code: Int }
pub exception ProbeException(code: Int)

implement core.Throw for ProbeError {
	pub fn throw_self(var self: ProbeError) throws {
		throw ProbeException(code = self.code);
	}
}

pub fn make_probe_ok()    nothrow -> core.Result<Int, ProbeError> { return core.Result::Ok(42); }
pub fn make_probe_err()   nothrow -> core.Result<Int, ProbeError> { return core.Result::Err(ProbeError(code = 42)); }
"""


_PROBE_CONSUMER_SOURCE = """\
module main;

import probe as probe;

fn do_throw() throws -> Int {
	val r = probe.make_probe_err();
	return (move r).or_throw();
}

pub fn main() nothrow -> Int {
	return try do_throw() catch probe:ProbeException(_) {
		42
	} catch {
		98
	};
}
"""


_PROBE_CONSUMER_SOURCE_IMPORTCORE = """\
module main;

import std.core as core;
import probe as probe;

fn do_throw() throws -> Int {
	val r = probe.make_probe_err();
	return (move r).or_throw();
}

pub fn main() nothrow -> Int {
	return try do_throw() catch probe:ProbeException(_) {
		42
	} catch {
		98
	};
}
"""


# Probe-shape variant: own exception, var self, no std.err:ResultError or
# DiagnosticValue references.
_ACME_THROWER_SOURCE_PROBESHAPE = """\
module acme.thrower;

import std.core as core;

export { ProducerError, ProducerException, make_err };

pub struct ProducerError {
	pub code: Int
}

pub exception ProducerException(code: Int)

implement core.Throw for ProducerError {
	pub fn throw_self(var self: ProducerError) throws {
		throw ProducerException(code = self.code);
	}
}

pub fn make_err() nothrow -> core.Result<Int, ProducerError> {
	return core.Result::Err(ProducerError(code = 42));
}
"""


_CONSUMER_SOURCE = """\
module main;

import acme.thrower as thrower;

fn do_throw() throws -> Int {
	val r = thrower.make_err();
	return (move r).or_throw();
}

pub fn main() nothrow -> Int {
	return try do_throw() catch std.err:ResultError(_) {
		42
	} catch {
		98
	};
}
"""


_CONSUMER_SOURCE_CHAINED = """\
module main;

import acme.thrower as thrower;

fn do_throw() throws -> Int {
	return thrower.make_err().or_throw();
}

pub fn main() nothrow -> Int {
	return try do_throw() catch std.err:ResultError(_) {
		42
	} catch {
		98
	};
}
"""


# Import std.core directly in consumer (probe of the visible-modules hypothesis).
_CONSUMER_SOURCE_IMPORTCORE = """\
module main;

import std.core as core;
import acme.thrower as thrower;

fn do_throw() throws -> Int {
	val r = thrower.make_err();
	return (move r).or_throw();
}

pub fn main() nothrow -> Int {
	return try do_throw() catch std.err:ResultError(_) {
		42
	} catch {
		98
	};
}
"""


def _build_signed_probe_pkg(
	tmp_path: Path, keys: _DeployKeys,
	*, dev: bool = True, no_M: bool = False, no_stdlib_root: bool = False,
) -> Path:
	"""Build the web-probe-shaped package (or-throw-probe / module probe)."""
	build = tmp_path / "probe_build"
	mod_dir = build  # single-segment module: file lives directly under -M root
	_write_file(mod_dir / "lib.drift", _PROBE_SOURCE)

	stdlib_dir = REPO_ROOT / "stdlib"

	pkg_path = tmp_path / "pkgs" / "or-throw-probe.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	argv = []
	if dev:
		argv.append("--dev")
	if not no_M:
		argv += ["-M", str(build)]
	if not no_stdlib_root:
		argv += ["--stdlib-root", str(stdlib_dir)]
	argv += [
		str(mod_dir / "lib.drift"),
		"--package-id", "or-throw-probe",
		"--package-version", "0.0.1",
		"--package-target", "test-target",
		"--emit-package", str(pkg_path),
	]
	rc = driftc_main(argv)
	assert rc == 0, f"failed to build probe pkg; argv={argv}"

	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(
		pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid,
		sig_raw=sig_raw, pub_b64=keys.pub_b64,
	)
	return pkg_path


def _build_signed_thrower_pkg(
	tmp_path: Path, keys: _DeployKeys,
	*, dev: bool = True, no_M: bool = False, no_stdlib_root: bool = False,
	source: str = _ACME_THROWER_SOURCE,
) -> Path:
	build = tmp_path / "thrower_build"
	mod_dir = build / "acme" / "thrower"
	_write_file(mod_dir / "thrower.drift", source)

	stdlib_dir = REPO_ROOT / "stdlib"

	pkg_path = tmp_path / "pkgs" / "acme.thrower.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	argv = []
	if dev:
		argv.append("--dev")
	if not no_M:
		argv += ["-M", str(build)]
	if not no_stdlib_root:
		argv += ["--stdlib-root", str(stdlib_dir)]
	argv += [
		str(mod_dir / "thrower.drift"),
		*_emit_pkg_args("acme.thrower"),
		"--emit-package", str(pkg_path),
	]
	rc = driftc_main(argv)
	assert rc == 0, f"failed to build acme.thrower package fixture; argv={argv}"

	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(
		pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid,
		sig_raw=sig_raw, pub_b64=keys.pub_b64,
	)
	return pkg_path


# ── Variant runners ─────────────────────────────────────────────────


def _setup_consumer(
	tmp_path: Path, *, producer_dev: bool = True, consumer_source: str = _CONSUMER_SOURCE,
	producer_no_M: bool = False, producer_no_stdlib_root: bool = False,
	producer_source: str = _ACME_THROWER_SOURCE,
) -> tuple[Path, Path, Path, Path, _DeployKeys]:
	keys = _gen_keys()
	thrower_pkg = _build_signed_thrower_pkg(
		tmp_path, keys, dev=producer_dev,
		no_M=producer_no_M, no_stdlib_root=producer_no_stdlib_root,
		source=producer_source,
	)

	stdlib_dir = REPO_ROOT / "stdlib"
	core_trust = tmp_path / "core_trust.json"
	_write_trust_store(
		core_trust, kid=keys.kid, pub_b64=keys.pub_b64,
		namespaces=["std.*", "lang.*", "drift.*"],
	)
	trust = tmp_path / "trust.json"
	_write_trust_store(
		trust, kid=keys.kid, pub_b64=keys.pub_b64,
		namespaces=["acme.*"],
	)

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(main_src, consumer_source)

	return main_src, thrower_pkg, core_trust, trust, keys


def _capture_compile(argv: list[str]) -> tuple[int, str, str]:
	"""Run driftc_main capturing stdout/stderr."""
	import io
	import contextlib

	out_buf = io.StringIO()
	err_buf = io.StringIO()
	rc = -1
	try:
		with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
			rc = driftc_main(argv)
	except SystemExit as e:
		rc = int(e.code) if isinstance(e.code, int) else 1
	except Exception as e:
		err_buf.write(f"\n[exception] {type(e).__name__}: {e}\n")
		import traceback
		traceback.print_exc(file=err_buf)
	return rc, out_buf.getvalue(), err_buf.getvalue()


def _summarize(label: str, rc: int, stdout: str, stderr: str) -> None:
	print(f"\n=== {label} ===")
	print(f"rc={rc}")
	# Try to parse JSON diagnostics
	try:
		payload = json.loads(stdout) if stdout.strip() else {}
		diags = payload.get("diagnostics", [])
		if diags:
			print(f"diagnostics ({len(diags)}):")
			for d in diags:
				msg = d.get("message", "")
				sev = d.get("severity", "?")
				print(f"  [{sev}] {msg}")
		else:
			tail = stdout.strip()
			if tail:
				print(f"stdout (no diagnostics): {tail[:300]}")
	except json.JSONDecodeError:
		print(f"stdout (non-JSON): {stdout[:500]}")
	if stderr.strip():
		print(f"stderr:\n{stderr}")


def run_variant(name: str) -> tuple[int, str, str]:
	tmp_root = Path(tempfile.mkdtemp(prefix=f"k28_{name}_"))
	# Isolate HOME for trust resolution
	old_home = os.environ.get("HOME")
	os.environ["HOME"] = str(tmp_root / "home")
	try:
		producer_dev = name not in ("v2c-producer-nondev", "v2d-both-nondev", "v3", "v3a", "v3b")
		producer_no_M = name in ("v3", "v3a", "v3b")
		producer_no_stdlib_root = name in ("v3", "v3a", "v3b")
		if name == "v2e-chained":
			consumer_source = _CONSUMER_SOURCE_CHAINED
		elif name == "v2g-importcore":
			consumer_source = _CONSUMER_SOURCE_IMPORTCORE
		else:
			consumer_source = _CONSUMER_SOURCE
		producer_source = _ACME_THROWER_SOURCE_PROBESHAPE if name in ("v2f-probeshape",) else _ACME_THROWER_SOURCE

		# v4/v4-importcore special-cases: build the web-probe-named package + matching consumer.
		if name in ("v4", "v4-importcore"):
			keys = _gen_keys()
			probe_pkg = _build_signed_probe_pkg(tmp_root, keys, dev=False, no_M=True, no_stdlib_root=True)
			stdlib_dir = REPO_ROOT / "stdlib"
			nondev_trust = tmp_root / "trust_nondev.json"
			_write_trust_store(
				nondev_trust, kid=keys.kid, pub_b64=keys.pub_b64,
				namespaces=["probe", "std.*", "lang.*", "drift.*"],
			)
			consumer_dir = tmp_root / "consumer"
			main_src = consumer_dir / "main.drift"
			_write_file(
				main_src,
				_PROBE_CONSUMER_SOURCE_IMPORTCORE if name == "v4-importcore" else _PROBE_CONSUMER_SOURCE,
			)
			out_bin = tmp_root / "out.bin"
			argv = [
				"--target-word-bits", "64",
				"--package-root", str(probe_pkg.parent),
				"--dep", "or-throw-probe@0.0.1",
				"--trust-store", str(nondev_trust),
				"--entry", "main::main",
				str(main_src),
				"-o", str(out_bin),
				"--json",
			]
			return _capture_compile(argv)

		main_src, thrower_pkg, core_trust, trust, keys = _setup_consumer(
			tmp_root, producer_dev=producer_dev, consumer_source=consumer_source,
			producer_no_M=producer_no_M, producer_no_stdlib_root=producer_no_stdlib_root,
			producer_source=producer_source,
		)
		stdlib_dir = REPO_ROOT / "stdlib"
		ir_path = tmp_root / "out.ll"

		base_argv = [
			"-M", str(main_src.parent),
			"--package-root", str(thrower_pkg.parent),
			"--dep", "acme.thrower@0.0.0",
			"--dev",
			"--dev-core-trust-store", str(core_trust),
			"--trust-store", str(trust),
			"--entry", "main::main",
			str(main_src),
			"--emit-ir", str(ir_path),
			"--json",
		]
		# Non-dev consumer requires acme.* in the trust store but no
		# core-trust override; rebuild the trust store with both namespaces.
		nondev_trust = tmp_root / "trust_nondev.json"
		_write_trust_store(
			nondev_trust, kid=keys.kid, pub_b64=keys.pub_b64,
			namespaces=["acme.*", "std.*", "lang.*", "drift.*"],
		)
		nondev_argv = [
			"-M", str(main_src.parent),
			"--package-root", str(thrower_pkg.parent),
			"--dep", "acme.thrower@0.0.0",
			"--trust-store", str(nondev_trust),
			"--entry", "main::main",
			str(main_src),
			"--emit-ir", str(ir_path),
			"--json",
		]

		if name == "baseline":
			argv = ["--stdlib-root", str(stdlib_dir), *base_argv]
		elif name == "v1":
			argv = list(base_argv)
		elif name == "v2":
			argv = [*base_argv, "--target-word-bits", "64"]
		elif name == "v2b-nondev":
			# Strip --dev/--dev-core-trust-store from consumer (web-team mode)
			argv = list(nondev_argv)
		elif name == "v2c-producer-nondev":
			# Producer built without --dev; consumer keeps --dev.
			argv = ["--stdlib-root", str(stdlib_dir), *base_argv]
		elif name == "v2d-both-nondev":
			# Producer and consumer both without --dev.
			argv = list(nondev_argv)
		elif name == "v2f-probeshape":
			# Producer rewritten to web-probe shape (own exception, no
			# std.err:ResultError, no DiagnosticValue) — keeps xfail-style flags.
			argv = ["--stdlib-root", str(stdlib_dir), *base_argv]
		elif name == "v2e-chained":
			# Chained call form (no local binding); xfail-style flags.
			argv = ["--stdlib-root", str(stdlib_dir), *base_argv]
		elif name == "v2g-importcore":
			# Consumer adds `import std.core as core;` — tests visible-modules hypothesis.
			argv = ["--stdlib-root", str(stdlib_dir), *base_argv]
		elif name == "v3a":
			# Producer built drift-deploy-style (no -M, no --dev, no --stdlib-root,
			# uses positional source path only); consumer keeps xfail-style flags.
			argv = ["--stdlib-root", str(stdlib_dir), *base_argv]
		elif name == "v3b":
			# v3a + consumer drops -M (positional source only).
			argv = [
				"--stdlib-root", str(stdlib_dir),
				"--package-root", str(thrower_pkg.parent),
				"--dep", "acme.thrower@0.0.0",
				"--dev",
				"--dev-core-trust-store", str(core_trust),
				"--trust-store", str(trust),
				"--entry", "main::main",
				str(main_src),
				"--emit-ir", str(ir_path),
				"--json",
			]
		elif name == "v3":
			# Full drift-deploy-style consumer: no -M, no --dev,
			# nondev trust store, --target-word-bits 64, -o instead of --emit-ir.
			out_bin = tmp_root / "out.bin"
			argv = [
				"--target-word-bits", "64",
				"--package-root", str(thrower_pkg.parent),
				"--dep", "acme.thrower@0.0.0",
				"--trust-store", str(nondev_trust),
				"--entry", "main::main",
				str(main_src),
				"-o", str(out_bin),
				"--json",
			]
		else:
			print(f"unknown variant: {name}")
			return -1, "", ""

		return _capture_compile(argv)
	finally:
		if old_home is not None:
			os.environ["HOME"] = old_home
		# Keep tmp dir around for inspection
		print(f"(tmp: {tmp_root})")


def main() -> int:
	variants = sys.argv[1:] or ["baseline"]
	for v in variants:
		rc, stdout, stderr = run_variant(v)
		_summarize(f"variant {v}", rc, stdout, stderr)
	return 0


if __name__ == "__main__":
	sys.exit(main())
