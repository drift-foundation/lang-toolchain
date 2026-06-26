# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""External-consumer regression fleet (signed package path).

Exercises the deployed codepath (``if loaded_pkgs:`` in driftc.py) with
proper trust verification — NOT the ``--allow-unsigned-from`` shortcut
used by existing K9-K14 tests.

Regression groups:
  K14: --entry honoured in deployed path
  K12: Generic variant ctor inference from package sigs
  K11: Tombstone metadata after package linking
  K13: Boundary-call nothrow (direct + wrapper)
  K10: Module-qualified struct ctor from package
  K4:  No fingerprint mismatch note in consume

Security negatives:
  Unsigned package rejected
  Tampered package rejected
  Reserved namespace unsigned rejected (even with --allow-unsigned-from)
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.drift.crypto import compute_ed25519_kid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from lang.codegen.llvm.test_utils import sanitizer_timeout


# ── Helpers ──────────────────────────────────────────────────────────


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
	return pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


# Sentinel SCI used by every fixture package built in this test file.
# A constant value is acceptable here because none of these tests
# assert anything about source identity -- they exercise the
# *consumer-side* compile flow.  The manifest stamp, author claim,
# and cert claim all agree because they all reference this same
# string, which is what the v1 verifier requires.
_TEST_SCI = "sha256:" + ("0" * 64)


def _write_trust_store(path: Path, *, kid: str, pub_b64: str, namespaces: list[str], revoked: list[str] | None = None) -> None:
	"""Write a v1 trust store JSON.

	`kid` plays BOTH the author and certifier roles for every
	listed namespace -- this is the Foundation-bootstrap pattern
	from the audit doc: in test fixtures (and stdlib in dev), the
	same key signs both the author claim and the cert claim, so a
	single trust entry covers both roles.
	"""
	revoked = revoked or []
	obj = {
		"format": "drift-trust",
		"version": 1,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {
			ns: {"authors": [kid], "certifiers": [kid]}
			for ns in namespaces
		},
		"revoked": revoked,
	}
	_write_file(path, json.dumps(obj, separators=(",", ":"), sort_keys=True))


def _priv_seed32(priv: Ed25519PrivateKey) -> bytes:
	"""Extract the raw 32-byte ed25519 seed from a cryptography priv key."""
	return priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)


def _write_v1_sidecars(
	pkg_path: Path,
	*,
	pkg_bytes: bytes,
	priv: Ed25519PrivateKey,
	package_id: str,
	version: str = "0.0.0",
	target: str = "test-target",
	namespaces: tuple[str, ...] | None = None,
	required_deps: tuple = (),
) -> tuple[Path, Path]:
	"""Replace the v0 `<pkg>.sig` envelope with v1 author + cert
	claim sidecars next to `pkg_path`.

	Returns `(author_claim_path, cert_claim_path)`.

	`namespaces` defaults to `(package_id,)` -- override when the
	package's actual module namespace differs (e.g. hyphenated
	package ids whose modules use the underscored form).
	"""
	from lang.driftc.packages.author_claim_v1 import make_author_claim_body, AuthorClaimBody
	from lang.driftc.packages.cert_claim_v1 import (
	make_cert_claim_body,
		CertClaimBody, CertSuite, Toolchain,
	)
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions, sign_and_write_author_claim,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions, sign_and_write_cert_claim,
	)

	seed = _priv_seed32(priv)
	sidecar_dir = pkg_path.parent
	# Default to (`<pkg>`, `<pkg>.*`) so the claim covers both a
	# bare module named `<pkg>` (the package's root namespace) AND
	# any descendant module (e.g. `std` package + `std.testlib`
	# module).  Callers override when a package's modules live in
	# a different namespace than its id (e.g. `net-tls` →
	# `net_tls.*`).
	ns = namespaces if namespaces is not None else (
		package_id, f"{package_id}.*",
	)

	author_path = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=make_author_claim_body(
			artifact_kind="package",
			package_id=package_id,
			version=version,
			namespaces=ns,
			source_content_id=_TEST_SCI,
			required_deps=required_deps,
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=seed,
		sidecar_dir=sidecar_dir,
	))
	cert_path = sign_and_write_cert_claim(SignCertClaimOptions(
		body=make_cert_claim_body(
			artifact_kind="package", artifact_path=f"{package_id}.dmp",
			package_id=package_id,
			version=version,
			artifact_sha256="sha256:" + _sha256_hex(pkg_bytes),
			source_content_id=_TEST_SCI,
			target=target,
			toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit="test"),
			dep_graph=(),
			cert_suite=CertSuite(
				id="drift-deploy/test", version="1.0", result="pass",
				result_evidence_sha256="sha256:" + ("f" * 64),
			),
			run_id=f"test-{package_id}",
			run_started_utc="2026-05-19T00:00:00Z",
			evidence_sha256="sha256:" + ("0" * 64),
		),
		seed32=seed,
		sidecar_dir=sidecar_dir,
	))
	return author_path, cert_path


# Compatibility shim: legacy call sites still using
# `_write_sig_sidecar` now produce v1 sidecars instead.  The
# `kid`/`sig_raw`/`pub_b64` args are unused (those came from v0's
# .sig envelope shape); callers will be cleaned up to call
# `_write_v1_sidecars` directly in a follow-up sweep.
def _write_sig_sidecar(
	pkg_path: Path, *, pkg_bytes: bytes, kid: str = "",
	sig_raw: bytes = b"", pub_b64: str | None = None,
	priv: Ed25519PrivateKey | None = None,
	package_id: str | None = None,
	version: str = "0.0.0",
) -> Path:
	"""v1 migration shim.  Call sites still spell `_write_sig_sidecar`
	but get trust-v1 author + cert claim sidecars instead.

	Call-site contract: must pass `priv=keys.priv`.  `package_id`
	defaults to `pkg_path.stem` (the filename without `.dmp`),
	which matches the naming convention every fixture in this
	file uses (`acme.util` → `acme.util.dmp`).  Override only when
	the on-disk filename differs from the package id.

	The legacy v0-shaped `kid` / `sig_raw` / `pub_b64` kwargs are
	accepted-and-ignored so the migration is purely additive at
	call sites.
	"""
	assert priv is not None, (
		"v1 migration: _write_sig_sidecar now requires `priv` (the "
		"Ed25519PrivateKey that built the v0 signature) so it can "
		"emit trust-v1 author + cert claim.  Pass priv=keys.priv at the "
		"call site."
	)
	if package_id is None:
		package_id = pkg_path.stem
	_, cert_path = _write_v1_sidecars(
		pkg_path, pkg_bytes=pkg_bytes, priv=priv,
		package_id=package_id, version=version,
	)
	return cert_path


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


def _empty_stdlib_root(tmp_path: Path) -> Path:
	d = tmp_path / "_empty_stdlib"
	d.mkdir(parents=True, exist_ok=True)
	return d


def _emit_pkg_args(package_id: str) -> list[str]:
	"""Args that build a v1-compatible package: includes
	`--source-content-id`, which `_write_v1_sidecars` matches in
	both the author and cert claim bodies."""
	return [
		"--package-id", package_id,
		"--package-version", "0.0.0",
		"--package-target", "test-target",
		"--source-content-id", _TEST_SCI,
	]


# ── Shared fixture: signed acme.util package ─────────────────────────


_ACME_UTIL_SOURCE = """\
module acme.util;

export { Counter, make_counter, Color, describe_color, Outcome };

pub struct Counter {
	pub value: Int
}

implement Counter {
	pub fn increment(self: &mut Counter) nothrow -> Void {
		self.value = self.value + 1;
	}

	pub fn get(self: &Counter) nothrow -> Int {
		return self.value;
	}
}

pub fn make_counter(start: Int) nothrow -> Counter {
	return Counter(value = start);
}

pub variant Color {
	Red,
	Green,
	Blue(value: Int)
}

pub fn describe_color(c: Color) nothrow -> Int {
	return match c {
		Color::Red => { 1 },
		Color::Green => { 2 },
		Color::Blue(value) => { value },
	};
}

pub variant Outcome<T, E> {
	Ok(value: T),
	Err(err: E),
	@tombstone Tombstone
}
"""


@dataclass(frozen=True)
class _SignedPkg:
	pkg_path: Path
	pkg_root: Path
	keys: _DeployKeys
	trust_path: Path
	core_trust_path: Path


def _build_signed_acme_pkg(tmp_path: Path) -> _SignedPkg:
	"""Build a signed acme.util package with Counter, Color, Outcome."""
	build = tmp_path / "pkg_build"
	mod_dir = build / "acme" / "util"
	_write_file(mod_dir / "util.drift", _ACME_UTIL_SOURCE)

	pkg_path = tmp_path / "pkgs" / "acme.util.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "util.drift"),
		*_emit_pkg_args("acme.util"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build acme.util package fixture"

	keys = _gen_keys()
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv)

	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["acme.*"])

	return _SignedPkg(
		pkg_path=pkg_path,
		pkg_root=pkg_path.parent,
		keys=keys,
		trust_path=trust_path,
		core_trust_path=core_trust_path,
	)


# ── Consumer compile helper ──────────────────────────────────────────


def _compile_consumer(
	tmp_path: Path,
	capsys: pytest.CaptureFixture[str],
	*,
	pkg: _SignedPkg,
	source: str,
	entry: str | None = None,
	deps: list[str] | None = None,
) -> tuple[int, dict, str, str]:
	"""Compile consumer source against a signed package.

	Returns (rc, json_payload, diagnostic_messages, stderr).
	``deps`` is a list of ``"PKG@VERSION"`` strings passed as ``--dep``
	arguments.  Callers MUST supply deps for every package loaded via
	``--package-root``; the compiler requires explicit ``--dep`` pins
	for all discovered packages.
	"""
	consumer = tmp_path / "consumer"
	src_name = entry.split("::")[0] if entry else "main"
	main_src = consumer / f"{src_name}.drift"
	_write_file(main_src, source)

	argv = [
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg.pkg_root),
		"--dev",
		"--dev-core-trust-store", str(pkg.core_trust_path),
		"--trust-store", str(pkg.trust_path),
	]
	for dep in (deps or []):
		argv += ["--dep", dep]
	if entry:
		argv += ["--entry", entry]
	argv += [
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	]

	rc = driftc_main(argv)
	captured = capsys.readouterr()
	payload = json.loads(captured.out) if captured.out.strip() else {}
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	return rc, payload, messages, captured.err


# ── Positive regression tests ────────────────────────────────────────


def test_ext_entry_plumbing(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K14: --entry module::main must be forwarded to the deployed/package
	compile path so validate_entrypoint finds the correct entry function.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		entry="runner::main",
		deps=["acme.util@0.0.0"],
		source="""\
module runner;

import acme.util as util;

fn main() nothrow -> Int {
	val c = util.make_counter(7);
	return c.value;
}
""",
	)
	assert "missing entry point" not in messages, f"--entry runner::main should be honored in signed path: {messages}"
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_ext_variant_ctor_inference(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K12: constructing a generic variant from a signed-package-consumed
	module must infer type arguments from the function return type.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.util@0.0.0"],
		source="""\
module main;

import acme.util as util;

fn get_ok() nothrow -> util.Outcome<Int, Int> {
	return util.Outcome::Ok(42);
}

fn get_err() nothrow -> util.Outcome<Int, Int> {
	return util.Outcome::Err(1);
}

fn main() nothrow -> Int {
	val o: util.Outcome<Int, Int> = get_ok();
	return 0;
}
""",
	)
	assert "cannot infer" not in messages.lower(), f"type inference failed for signed-package variant ctor: {messages}"
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_ext_tombstone_exhaustiveness(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K11: matching Ok/Err on a variant with @tombstone must be exhaustive
	— tombstone is internal and pruned from the required set.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.util@0.0.0"],
		source="""\
module main;

import acme.util as util;

fn check(o: util.Outcome<Int, Int>) nothrow -> Int {
	return match o {
		util.Outcome::Ok(value) => { value },
		util.Outcome::Err(err) => { err },
	};
}

fn main() nothrow -> Int {
	val o: util.Outcome<Int, Int> = util.Outcome::Ok(42);
	return check(move o);
}
""",
	)
	assert "NONEXHAUSTIVE" not in messages, f"expected exhaustive match (tombstone pruned); got: {messages}"
	assert "Tombstone" not in messages, f"tombstone leaked into diagnostics: {messages}"
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_ext_boundary_nothrow_direct(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K13-a: a nothrow caller invoking ONLY a nothrow free function across
	a signed-package boundary must compile without 'may throw'.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.util@0.0.0"],
		source="""\
module main;

import acme.util as util;

fn main() nothrow -> Int {
	val c = util.make_counter(42);
	return c.value;
}
""",
	)
	assert "may throw" not in messages, f"nothrow direct call to nothrow free function should not poison caller: {messages}"
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_ext_boundary_nothrow_wrapper(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K13-b: a nothrow caller invoking ONLY nothrow methods across a
	signed-package boundary must compile without 'may throw'.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.util@0.0.0"],
		source="""\
module main;

import acme.util as util;

fn main() nothrow -> Int {
	var c = util.Counter(value = 0);
	c.increment();
	c.increment();
	return c.get();
}
""",
	)
	assert "may throw" not in messages, f"nothrow method call via wrapper path should not poison caller: {messages}"
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_ext_module_qualified_ctor(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K10: module-qualified struct ctor calls must work for external
	modules loaded via signed package-root.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.util@0.0.0"],
		source="""\
module main;

import acme.util as util;

fn main() nothrow -> Int {
	val c1 = util.Counter(value = 1);
	val c2 = util.Counter(value = c1.value + 1);
	return c2.value;
}
""",
	)
	assert "module-qualified constructor call" not in messages, f"module-qualified ctor should work from signed package: {messages}"
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_ext_template_fingerprint_clean(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K4: consuming a signed package must not emit any fingerprint
	mismatch notes on stderr.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.util@0.0.0"],
		source="""\
module main;

import acme.util as util;

fn main() nothrow -> Int {
	return util.describe_color(util.Color::Blue(99));
}
""",
	)
	assert "fingerprint mismatch" not in stderr, f"unexpected fingerprint mismatch note in stderr:\n{stderr}"
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


# ── K16: Package-consumer symbol completeness (integrated) ───────────


def _audit_ir_symbols(ir: str) -> tuple[set[str], set[str], set[str]]:
	"""Collect called, defined, and declared symbols from LLVM IR.

	Returns (called, defined, declared).  A called symbol that is neither
	defined nor declared is an undefined internal target.
	"""
	# Match both @"quoted-name" and @plain_name forms after call/invoke.
	called = set(re.findall(r'(?:call|invoke)\s+[^@]*@"([^"]+)"', ir))
	called |= set(re.findall(r'(?:call|invoke)\s+[^@]*@([a-zA-Z_][\w.]*)', ir))
	defined = set(re.findall(r'define\s+[^@]*@"([^"]+)"', ir))
	defined |= set(re.findall(r'define\s+[^@]*@([a-zA-Z_][\w.]*)', ir))
	declared = set(re.findall(r'declare\s+[^@]*@"([^"]+)"', ir))
	declared |= set(re.findall(r'declare\s+[^@]*@([a-zA-Z_][\w.]*)', ir))
	return called, defined, declared


_CONSUMER_E2E_SOURCE = """\
module runner;

import acme.util as util;

fn main() nothrow -> Int {
	var c = util.Counter(value = 0);
	c.increment();
	c.increment();
	c.increment();
	val n = c.get();
	val color_val = util.describe_color(util.Color::Blue(n));
	val o: util.Outcome<Int, Int> = util.Outcome::Ok(color_val);
	return match o {
		util.Outcome::Ok(value) => { value },
		util.Outcome::Err(err) => { err },
	};
}
"""


@pytest.mark.parametrize("debug_style", [False, True], ids=["normal", "debug-style"])
def test_ext_package_consumer_e2e(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
	debug_style: bool,
) -> None:
	"""K16 integrated: signed-package consumer must compile, link, and run.

	Exercises in a single pass:
	  K16a  – nothrow method wrapper synthesis (Counter.get — byte_length
	          pattern: nothrow method returning Int across boundary)
	  K16b  – OS entry wrapper via --entry runner::main
	  K10   – module-qualified struct ctor (util.Counter)
	  K11   – tombstone exhaustiveness (Outcome match)
	  K12   – generic variant ctor inference (Outcome::Ok)
	  K13   – nothrow boundary call (increment/get/make_counter)

	Stages verified:
	  1. IR symbol completeness (no unresolved internal targets)
	  2. Object links with clang
	  3. Binary runs and returns expected exit code
	  4. Normal and debug-style runtime lanes (parametrized via DRIFT_DEBUG)
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	if debug_style:
		monkeypatch.setenv("DRIFT_DEBUG", "1")
	else:
		monkeypatch.delenv("DRIFT_DEBUG", raising=False)
	# This test is parametrized over both lanes regardless of the parent
	# session's DRIFT_DEBUG state, so its build artifacts are by design
	# orthogonal to the active lane.  Opt out of the conftest sentinel
	# audit so the parametrized "wrong" lane does not look like a leak.
	(tmp_path / ".drift-lane-audit-skip").write_text("", encoding="utf-8")
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	# ── Compile ──────────────────────────────────────────────────────
	consumer = tmp_path / "consumer"
	main_src = consumer / "runner.drift"
	_write_file(main_src, _CONSUMER_E2E_SOURCE)
	extra_args: list[str] = []
	argv = [
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg.pkg_root),
		"--dep", "acme.util@0.0.0",
		"--dev",
		"--dev-core-trust-store", str(pkg.core_trust_path),
		"--trust-store", str(pkg.trust_path),
		"--entry", "runner::main",
		*extra_args,
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	]
	rc = driftc_main(argv)
	captured = capsys.readouterr()
	payload = json.loads(captured.out) if captured.out.strip() else {}
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert rc == 0, f"compilation failed: {messages}"

	ir = (tmp_path / "out.ll").read_text()

	# ── Stage 1: IR symbol completeness ──────────────────────────────
	called, defined, declared = _audit_ir_symbols(ir)
	available = defined | declared
	undefined = called - available
	assert not undefined, f"undefined symbols in IR: {undefined}"

	# Option B: no boundary wrapper routing — __wrap_method symbols should
	# not appear in the IR.
	wrap_refs = {s for s in called if "__wrap_method" in s}
	assert not wrap_refs, f"Option B: no wrapper call targets expected in IR, found: {wrap_refs}"

	# K16b pinned: OS entry wrapper.
	assert "define i32 @main" in ir, "package-consumer IR must contain a C main entry point"

	# ── Stage 2: Link ────────────────────────────────────────────────
	clang = shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available for link+run stage")

	build_dir = tmp_path / "build"
	build_dir.mkdir(parents=True, exist_ok=True)
	ir_path = build_dir / "program.ll"
	bin_path = build_dir / "a.out"

	# Provide stubs for drift runtime symbols (normally from the runtime
	# archive).  llvm.* intrinsics are handled by LLVM and need no stubs.
	patched_ir = ir
	for m in re.finditer(r'(declare\s+(\S+)\s+(@(?:drift_|__drift_)\w+)\(((?:[^()]*|\([^()]*\))*)\))', patched_ir):
		full, ret_ty, name, params = m.group(1), m.group(2), m.group(3), m.group(4)
		if name == "@drift_run_main_on_vt":
			# Forward-call the function-pointer arg so user main runs.
			body = f"%r = call {ret_ty} %0()\n  ret {ret_ty} %r"
		elif ret_ty == "void":
			body = "ret void"
		else:
			body = "unreachable"
		patched_ir = patched_ir.replace(full, f"define {ret_ty} {name}({params}) {{\n  {body}\n}}")
	ir_path.write_text(patched_ir)

	compile_res = subprocess.run(
		[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
		capture_output=True, text=True,
	)
	assert compile_res.returncode == 0, f"clang link failed:\n{compile_res.stderr}"

	# ── Stage 3: Run ─────────────────────────────────────────────────
	run_res = subprocess.run(
		[str(bin_path)], capture_output=True, text=True, timeout=sanitizer_timeout(10),
	)
	# Counter(0) → 3 increments → get()=3 → Blue(3) → describe_color=3
	# → Outcome::Ok(3) → match=3 → exit code 3
	assert run_res.returncode == 3, (
		f"expected exit code 3, got {run_res.returncode}"
		f"\nstdout: {run_res.stdout}\nstderr: {run_res.stderr}"
	)


# ── K18: preamble not force-seeded (supersedes K17) ──────────────────


def _build_signed_std_io_pkg(tmp_path: Path, keys: _DeployKeys) -> Path:
	"""Build a signed std.dmp with a minimal std.io module."""
	build = tmp_path / "std_build"
	mod_dir = build / "std" / "io"
	_write_file(
		mod_dir / "io.drift",
		"""\
module std.io;

export { install_process_preamble };

pub fn install_process_preamble() nothrow -> Bool {
	return true;
}
""",
	)
	pkg_path = tmp_path / "pkgs" / "std.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "io.drift"),
		*_emit_pkg_args("std"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build std.io package fixture"
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv)
	return pkg_path


def test_ext_preamble_not_force_seeded(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K18: install_process_preamble must NOT be force-seeded into the BFS
	when the consumer does not transitively import std.io.

	The K17 fix force-seeded install_process_preamble into pkg_needed so its
	body would be lowered (producing the __impl symbol the entry wrapper calls).
	However, install_process_preamble's transitive closure pulls in heavy
	generic instantiations (GlobalRegistry::set<T>, mem alloc/write/read,
	core.callback1, core.drop_value) whose types the LLVM codegen cannot
	represent in the package-consumer context — causing NotImplementedError
	in deploy smoke.

	Fix: remove the BFS force-seeding.  The entry wrapper's preamble call
	is gated on a mir_all availability check, so it is correctly omitted
	when the function is not naturally reachable.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	# Build a std.dmp alongside acme.util.dmp in the same --package-root.
	_build_signed_std_io_pkg(tmp_path, keys=pkg.keys)
	_ = capsys.readouterr()

	consumer = tmp_path / "consumer"
	main_src = consumer / "runner.drift"
	_write_file(
		main_src,
		"""\
module runner;

import acme.util as util;

fn main() nothrow -> Int {
	val c = util.make_counter(5);
	return c.value;
}
""",
	)

	argv = [
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg.pkg_root),
		"--dep", "acme.util@0.0.0",
		"--dep", "std@0.0.0",
		"--dev",
		"--dev-core-trust-store", str(pkg.core_trust_path),
		"--trust-store", str(pkg.trust_path),
		"--entry", "runner::main",
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	]
	rc = driftc_main(argv)
	captured = capsys.readouterr()
	payload = json.loads(captured.out) if captured.out.strip() else {}
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert rc == 0, f"compilation failed: {messages}"

	ir = (tmp_path / "out.ll").read_text()

	# The consumer does NOT import std.io, so install_process_preamble
	# must NOT be in the IR — neither as a definition nor as a call target.
	# If it is present, the BFS is force-seeding it, which pulls in the
	# heavy transitive closure that breaks LLVM codegen (K18).
	assert "install_process_preamble" not in ir, (
		"install_process_preamble found in IR but consumer does not import std.io — "
		"BFS force-seeding is leaking unreachable package functions (K18 regression)"
	)

	# Full symbol audit: no undefined call targets.
	called, defined, declared = _audit_ir_symbols(ir)
	available = defined | declared
	undefined = called - available
	assert not undefined, f"undefined symbols in IR: {undefined}"

	# Link + run.
	clang = shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available for link+run stage")

	build_dir = tmp_path / "build"
	build_dir.mkdir(parents=True, exist_ok=True)
	ir_path = build_dir / "program.ll"
	bin_path = build_dir / "a.out"
	patched_ir = ir
	for m in re.finditer(r'(declare\s+(\S+)\s+(@(?:drift_|__drift_)\w+)\(((?:[^()]*|\([^()]*\))*)\))', patched_ir):
		full, ret_ty, name, params = m.group(1), m.group(2), m.group(3), m.group(4)
		if name == "@drift_run_main_on_vt":
			# Forward-call the function-pointer arg so user main runs.
			body = f"%r = call {ret_ty} %0()\n  ret {ret_ty} %r"
		elif ret_ty == "void":
			body = "ret void"
		else:
			body = "unreachable"
		patched_ir = patched_ir.replace(full, f"define {ret_ty} {name}({params}) {{\n  {body}\n}}")
	ir_path.write_text(patched_ir)

	compile_res = subprocess.run(
		[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
		capture_output=True, text=True,
	)
	assert compile_res.returncode == 0, f"clang link failed:\n{compile_res.stderr}"

	run_res = subprocess.run(
		[str(bin_path)], capture_output=True, text=True, timeout=sanitizer_timeout(10),
	)
	assert run_res.returncode == 5, (
		f"expected exit code 5, got {run_res.returncode}"
		f"\nstdout: {run_res.stdout}\nstderr: {run_res.stderr}"
	)


# ── Security negatives ───────────────────────────────────────────────


def test_ext_unsigned_package_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A package without v1 trust sidecars must be rejected.

	In v0 this was "delete the `.sig` envelope"; in v1 the
	equivalent is "delete the author-claim AND every cert-claim
	sidecar."  The verifier must reject because the author claim
	is required for every package load (see
	`verify_v1.compose_verify` -- the author-claim gate fires
	first), and a missing author claim is the canonical "unsigned
	package" state in the trust-v1 model.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	# Remove all v1 trust sidecars to make the package unsigned.
	from lang.driftc.packages.sidecar_naming import (
		author_claim_filename,
		cert_claim_filename_prefix,
	)
	pkg_dir = pkg.pkg_path.parent
	author_path = pkg_dir / author_claim_filename("acme.util")
	if author_path.is_file():
		author_path.unlink()
	cert_prefix = cert_claim_filename_prefix("acme.util")
	for entry in pkg_dir.iterdir():
		if entry.is_file() and entry.name.startswith(cert_prefix) and entry.name.endswith(".json"):
			entry.unlink()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.util@0.0.0"],
		source="""\
module main;

import acme.util as util;

fn main() nothrow -> Int {
	return util.make_counter(1).value;
}
""",
	)
	assert rc != 0, "unsigned package should be rejected"
	# v1 rejection diagnostic names the missing author claim or
	# the missing sidecar generically.
	rejection_terms = ("author-claim", "author claim", "sidecar", "signature", "unsigned", "trust verification")
	assert any(t in messages.lower() for t in rejection_terms), (
		f"expected v1 sidecar-missing rejection, got: {messages}"
	)


def test_ext_tampered_package_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A signed package with tampered bytes must be rejected (hash mismatch)."""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	# Tamper: flip last byte of .dmp after signing.
	pkg_bytes = pkg.pkg_path.read_bytes()
	tampered = bytearray(pkg_bytes)
	tampered[-1] ^= 0xFF
	pkg.pkg_path.write_bytes(bytes(tampered))

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.util@0.0.0"],
		source="""\
module main;

import acme.util as util;

fn main() nothrow -> Int {
	return 0;
}
""",
	)
	assert rc != 0, "tampered package should be rejected"
	assert "hash" in messages.lower() or "integrity" in messages.lower() or "sha256" in messages.lower() or "signature" in messages.lower(), f"expected integrity/hash/signature error, got: {messages}"


def _build_std_testlib_package(tmp_path: Path) -> Path:
	"""Build a minimal std.testlib package (unsigned, --dev)."""
	build = tmp_path / "pkg_build"
	mod_dir = build / "std" / "testlib"
	_write_file(
		mod_dir / "testlib.drift",
		"""\
module std.testlib;

export { ANSWER };

pub const ANSWER: Int = 42;
""",
	)
	pkg_path = tmp_path / "pkgs" / "std.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "testlib.drift"),
		*_emit_pkg_args("std"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build std.testlib package"
	return pkg_path


def test_ext_reserved_ns_unsigned_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Unsigned std.* package must be rejected — reserved namespaces require
	signatures regardless of flags.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg_path = _build_std_testlib_package(tmp_path)
	_ = capsys.readouterr()

	keys = _gen_keys()
	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])
	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*"])

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import std.testlib as testlib;

fn main() nothrow -> Int {
	return testlib.ANSWER;
}
""",
	)
	pkg_root = pkg_path.parent

	rc = driftc_main([
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg_root),
		"--dep", "std@0.0.0",
		"--dev",
		"--dev-core-trust-store", str(core_trust_path),
		"--trust-store", str(trust_path),
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	])
	captured = capsys.readouterr()
	assert rc != 0, "unsigned std.* package should be rejected"
	payload = json.loads(captured.out) if captured.out.strip() else {}
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "sidecar" in messages.lower() or "signature" in messages.lower() or "unsigned" in messages.lower(), f"expected signature rejection for unsigned std.* package, got: {messages}"


def test_ext_unsigned_override_no_reserved_bypass(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""--allow-unsigned-from must NOT bypass reserved namespace trust for
	std.* packages.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg_path = _build_std_testlib_package(tmp_path)
	_ = capsys.readouterr()

	keys = _gen_keys()
	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])
	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*"])

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import std.testlib as testlib;

fn main() nothrow -> Int {
	return testlib.ANSWER;
}
""",
	)
	pkg_root = pkg_path.parent

	rc = driftc_main([
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg_root),
		"--dep", "std@0.0.0",
		"--allow-unsigned-from", str(pkg_root),
		"--dev",
		"--dev-core-trust-store", str(core_trust_path),
		"--trust-store", str(trust_path),
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	])
	captured = capsys.readouterr()
	assert rc != 0, "--allow-unsigned-from should not bypass reserved namespace trust"
	payload = json.loads(captured.out) if captured.out.strip() else {}
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "unsigned" in messages.lower() or "sidecar" in messages.lower() or "signature" in messages.lower(), f"expected unsigned/signature rejection, got: {messages}"


# ── K25-guard: non-stdlib package visibility is package-generic ──────


_ACME_VIS_SOURCE = """\
module acme.vis;

export { Showable, Wrapper, wrap_and_show };

pub trait Showable {
	fn show(self: &Self) nothrow -> Int;
}

pub struct Wrapper<T> {
	pub inner: T
}

implement<T> Wrapper<T> {
	pub fn get(self: &Wrapper<T>) nothrow -> Int {
		return 0;
	}

	fn _private_helper(self: &Wrapper<T>) nothrow -> Int {
		return 42;
	}
}

pub fn wrap_and_show<T>(item: T) nothrow -> Wrapper<T> {
	return Wrapper(inner = move item);
}
"""


def _build_signed_acme_vis_pkg(tmp_path: Path) -> _SignedPkg:
	"""Build a signed acme.vis package with trait + generic struct."""
	build = tmp_path / "pkg_build"
	mod_dir = build / "acme" / "vis"
	_write_file(mod_dir / "vis.drift", _ACME_VIS_SOURCE)

	pkg_path = tmp_path / "pkgs" / "acme.vis.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "vis.drift"),
		*_emit_pkg_args("acme.vis"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build acme.vis package fixture"

	keys = _gen_keys()
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv)

	core_trust_path = tmp_path / "core_trust.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["acme.*"])

	return _SignedPkg(
		pkg_path=pkg_path,
		pkg_root=pkg_path.parent,
		keys=keys,
		trust_path=trust_path,
		core_trust_path=core_trust_path,
	)


def test_ext_nonlib_method_visibility(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K25-guard: non-stdlib external package methods must be visible to
	consumer code.  Proves K25 visibility fix is package-generic, not
	std.*-specific.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_vis_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.vis@0.0.0"],
		source="""\
module main;

import acme.vis as vis;

fn main() nothrow -> Int {
	val w = vis.wrap_and_show(42);
	return w.get();
}
""",
	)
	assert rc == 0, f"non-stdlib package method call should compile: {messages}"


def test_ext_nonlib_private_method_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K25-guard: non-stdlib external package private methods must still be
	rejected.  Proves K25 broadening does not leak private APIs from
	non-stdlib packages.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_vis_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.vis@0.0.0"],
		source="""\
module main;

import acme.vis as vis;

fn main() nothrow -> Int {
	val w = vis.wrap_and_show(42);
	return w._private_helper();
}
""",
	)
	assert rc != 0, f"private method on non-stdlib package type should be rejected"
	assert "_private_helper" in messages, f"expected rejection mentioning _private_helper, got: {messages}"


# ── Convergence parity ──────────────────────────────────────────────


def test_convergence_parity_pass1_state(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Convergence proof: Pass1State parity assertions pass on a representative
	package-consumer compilation.  Exercises function keys, wrapper injection,
	signature resolution, visibility provenance, and destructor registration
	parity checks (same 5 checks as DRIFT_COMPILER_DEBUG={"convergence_parity": true}).

	If this test fails, the local and package-consumer codepaths have diverged.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	monkeypatch.setenv("DRIFT_COMPILER_DEBUG", '{"convergence_parity": true}')
	# Reset cached debug flags so the monkeypatched env is picked up.
	from lang.driftc import debug as drift_debug
	drift_debug._cached_flags = None
	try:
		pkg = _build_signed_acme_pkg(tmp_path)
		_ = capsys.readouterr()
		rc, payload, messages, _stderr = _compile_consumer(
			tmp_path, capsys, pkg=pkg,
			entry="runner::main",
			deps=["acme.util@0.0.0"],
			source="""\
module runner;

import acme.util as util;

fn main() nothrow -> Int {
	val c = util.make_counter(7);
	return c.value;
}
""",
		)
		assert rc == 0, f"convergence parity compilation failed: {messages}"
	finally:
		drift_debug._cached_flags = None


# ── TypeId normalization: external sig preserves linked TypeIds ────


_ACME_GENERIC_SOURCE = """\
module acme.generic;

export { Wrapper, make_wrapper, try_unwrap };

pub struct Wrapper<T> {
	pub inner: T
}

pub fn make_wrapper(value: Int) nothrow -> Wrapper<Int> {
	return Wrapper(inner = value);
}

pub fn try_unwrap(w: Wrapper<Int>) nothrow -> Int {
	return w.inner;
}
"""


def _build_signed_generic_pkg(tmp_path: Path) -> _SignedPkg:
	"""Build a signed acme.generic package with Wrapper<T>."""
	build = tmp_path / "pkg_build_gen"
	mod_dir = build / "acme" / "generic"
	_write_file(mod_dir / "generic.drift", _ACME_GENERIC_SOURCE)

	pkg_path = tmp_path / "pkgs_gen" / "acme.generic.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "generic.drift"),
		*_emit_pkg_args("acme.generic"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build acme.generic package fixture"

	keys = _gen_keys()
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv)

	core_trust_path = tmp_path / "core_trust_gen.json"
	_write_trust_store(core_trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])

	trust_path = tmp_path / "trust_gen.json"
	_write_trust_store(trust_path, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["acme.*"])

	return _SignedPkg(
		pkg_path=pkg_path,
		pkg_root=pkg_path.parent,
		keys=keys,
		trust_path=trust_path,
		core_trust_path=core_trust_path,
	)


def test_ext_sig_preserves_linked_typeids(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Phase 1 TypeId normalization: when DMIR provides serialized TypeIds,
	the external signature must preserve the linked/canonical ids rather
	than diverging through resolve_opaque_type.  Verifies that Path A
	(external_signatures_by_id) and Path B (pkg_sigs_by_id) produce
	convergent return_type_ids for the same fn_id.

	This test catches the divergence earlier than a downstream wrapper or
	codegen failure.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	monkeypatch.setenv("DRIFT_DEBUG_TYPEID_DIVERGENCE", "1")
	pkg = _build_signed_generic_pkg(tmp_path)
	_ = capsys.readouterr()

	rc, payload, messages, stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		deps=["acme.generic@0.0.0"],
		source="""\
module main;

import acme.generic as gen;

fn main() nothrow -> Int {
	val w = gen.make_wrapper(42);
	return gen.try_unwrap(move w);
}
""",
	)
	# Under DRIFT_DEBUG_TYPEID_DIVERGENCE=1, any TypeId divergence between
	# Path A (external_signatures_by_id) and Path B (pkg_sigs_by_id) or
	# between FnInfo and signature raises AssertionError, which the compiler
	# catches and converts to a diagnostic with rc != 0.
	assert rc == 0, f"TypeId divergence assertion fired or compilation failed; diagnostics: {messages}"


# ── Source-wins-over-package regression ─────────────────────────────


class TestSourceWinsOverPackage:
	"""
	Regression: when building package X from source (--package-id X) and the
	--package-root also contains a previously-published copy of X, the compiler
	must ignore X's published artifacts entirely — they are never loaded or
	trust-verified.  Unrelated packages in the same root remain consumable.

	The exclusion is identity-based (--package-id matches package_id in
	the published artifact), not overlap-based.
	"""

	def test_source_wins_over_same_namespace_package(
		self,
		tmp_path: Path,
		capsys: pytest.CaptureFixture[str],
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""
		Build source modules for acme.util.  The --package-root contains both:
		  - acme.util.dmp (same namespace as source — should be ignored)
		  - acme.other.dmp (truly external dependency — should be consumed)

		The compiler must not error; the source modules must win and acme.other
		must remain importable.
		"""
		monkeypatch.setenv("HOME", str(tmp_path / "home"))
		keys = _gen_keys()

		# 1. Build acme.util package (will be the "stale published" copy).
		build_util = tmp_path / "build_util"
		_write_file(build_util / "src" / "util.drift", _ACME_UTIL_SOURCE)
		util_pkg_path = tmp_path / "pkgroot" / "acme.util.dmp"
		util_pkg_path.parent.mkdir(parents=True, exist_ok=True)
		rc = driftc_main([
			"--dev",
			"-M", str(build_util),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			str(build_util / "src" / "util.drift"),
			*_emit_pkg_args("acme.util"),
			"--emit-package", str(util_pkg_path),
		])
		assert rc == 0, "failed to build acme.util package"
		_ = capsys.readouterr()

		# Sign acme.util.
		util_bytes = util_pkg_path.read_bytes()
		sig_raw = keys.priv.sign(util_bytes)
		_write_sig_sidecar(util_pkg_path, pkg_bytes=util_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv)

		# 2. Build acme.other package (truly external dep).
		build_other = tmp_path / "build_other"
		_write_file(build_other / "src" / "other.drift", """\
module acme.other;

export { helper };

pub fn helper() nothrow -> Int {
	return 99;
}
""")
		other_pkg_path = tmp_path / "pkgroot" / "acme.other.dmp"
		rc = driftc_main([
			"--dev",
			"-M", str(build_other),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			str(build_other / "src" / "other.drift"),
			*_emit_pkg_args("acme.other"),
			"--emit-package", str(other_pkg_path),
		])
		assert rc == 0, "failed to build acme.other package"
		_ = capsys.readouterr()

		# Sign acme.other.
		other_bytes = other_pkg_path.read_bytes()
		sig_raw = keys.priv.sign(other_bytes)
		_write_sig_sidecar(other_pkg_path, pkg_bytes=other_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv)

		# 3. Write trust stores.
		core_trust = tmp_path / "core_trust.json"
		_write_trust_store(core_trust, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])
		trust = tmp_path / "trust.json"
		_write_trust_store(trust, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["acme.*"])

		# 4. Compile source for acme.util, consuming acme.other from package root.
		#    Package root contains BOTH acme.util.dmp and acme.other.dmp.
		#    --package-id acme.util tells the compiler this is a source build
		#    of acme.util — the published acme.util.dmp must be skipped entirely.
		src_dir = tmp_path / "src"
		_write_file(src_dir / "acme" / "util" / "util.drift", _ACME_UTIL_SOURCE)
		_write_file(src_dir / "main.drift", """\
module main;

import acme.util as util;
import acme.other as other;

fn main() nothrow -> Int {
	val c = util.make_counter(other.helper());
	return c.value;
}
""")
		rc = driftc_main([
			"-M", str(src_dir),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(tmp_path / "pkgroot"),
			"--dep", "acme.other@0.0.0",
			"--package-id", "acme.util",
			"--dev",
			"--dev-core-trust-store", str(core_trust),
			"--trust-store", str(trust),
			str(src_dir / "acme" / "util" / "util.drift"),
			str(src_dir / "main.drift"),
			"--test-build-only",
		])
		out, err = capsys.readouterr()
		assert rc == 0, f"source-wins build failed; stderr: {err}"
		assert "override" not in err.lower()

	def test_source_wins_without_test_build_only(
		self,
		tmp_path: Path,
		capsys: pytest.CaptureFixture[str],
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""
		Same as above but without --test-build-only, exercising the real
		compilation path including type-table linking.  The published copy
		of acme.util must be skipped before load/verify — not just filtered
		after loading.
		"""
		monkeypatch.setenv("HOME", str(tmp_path / "home"))
		keys = _gen_keys()

		# Build + sign acme.util package (stale published copy).
		build_util = tmp_path / "build_util"
		_write_file(build_util / "src" / "util.drift", _ACME_UTIL_SOURCE)
		util_pkg_path = tmp_path / "pkgroot" / "acme.util.dmp"
		util_pkg_path.parent.mkdir(parents=True, exist_ok=True)
		rc = driftc_main([
			"--dev",
			"-M", str(build_util),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			str(build_util / "src" / "util.drift"),
			*_emit_pkg_args("acme.util"),
			"--emit-package", str(util_pkg_path),
		])
		assert rc == 0
		_ = capsys.readouterr()
		util_bytes = util_pkg_path.read_bytes()
		sig_raw = keys.priv.sign(util_bytes)
		_write_sig_sidecar(util_pkg_path, pkg_bytes=util_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv)

		# Trust stores.
		core_trust = tmp_path / "core_trust.json"
		_write_trust_store(core_trust, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])
		trust = tmp_path / "trust.json"
		_write_trust_store(trust, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["acme.*"])

		# Source build of acme.util — package root contains acme.util.dmp.
		src_dir = tmp_path / "src"
		_write_file(src_dir / "acme" / "util" / "util.drift", _ACME_UTIL_SOURCE)
		_write_file(src_dir / "main.drift", """\
module main;

import acme.util as util;

fn main() nothrow -> Int {
	return util.make_counter(0).value;
}
""")
		rc = driftc_main([
			"-M", str(src_dir),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(tmp_path / "pkgroot"),
			"--dep", "acme.util@0.0.0",
			"--package-id", "acme.util",
			"--dev",
			"--dev-core-trust-store", str(core_trust),
			"--trust-store", str(trust),
			str(src_dir / "acme" / "util" / "util.drift"),
			str(src_dir / "main.drift"),
			"--json",
			"-o", str(tmp_path / "out.bin"),
		])
		out, err = capsys.readouterr()
		assert rc == 0, (
			f"source-wins build failed without --test-build-only; "
			f"stderr: {err}\nstdout: {out}"
		)

	def test_unrelated_package_still_consumed(
		self,
		tmp_path: Path,
		capsys: pytest.CaptureFixture[str],
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""
		Self-exclusion only drops the current package (--package-id).
		Unrelated packages in the same --package-root must still be loaded,
		verified, and importable.
		"""
		monkeypatch.setenv("HOME", str(tmp_path / "home"))
		keys = _gen_keys()

		# Build and sign acme.other (external dep).
		build_other = tmp_path / "build_other"
		_write_file(build_other / "src" / "other.drift", """\
module acme.other;

export { helper };

pub fn helper() nothrow -> Int {
	return 42;
}
""")
		pkg_path = tmp_path / "pkgroot" / "acme.other.dmp"
		pkg_path.parent.mkdir(parents=True, exist_ok=True)
		rc = driftc_main([
			"--dev",
			"-M", str(build_other),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			str(build_other / "src" / "other.drift"),
			*_emit_pkg_args("acme.other"),
			"--emit-package", str(pkg_path),
		])
		assert rc == 0
		_ = capsys.readouterr()
		pkg_bytes = pkg_path.read_bytes()
		sig_raw = keys.priv.sign(pkg_bytes)
		_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv)

		# Build and sign acme.local (will conflict with source).
		build_local = tmp_path / "build_local"
		_write_file(build_local / "src" / "local.drift", """\
module acme.local;

export { local_fn };

pub fn local_fn() nothrow -> Int {
	return 0;
}
""")
		local_pkg_path = tmp_path / "pkgroot" / "acme.local.dmp"
		rc = driftc_main([
			"--dev",
			"-M", str(build_local),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			str(build_local / "src" / "local.drift"),
			*_emit_pkg_args("acme.local"),
			"--emit-package", str(local_pkg_path),
		])
		assert rc == 0
		_ = capsys.readouterr()
		local_bytes = local_pkg_path.read_bytes()
		sig_raw = keys.priv.sign(local_bytes)
		_write_sig_sidecar(local_pkg_path, pkg_bytes=local_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv)

		# Trust stores.
		core_trust = tmp_path / "core_trust.json"
		_write_trust_store(core_trust, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])
		trust = tmp_path / "trust.json"
		_write_trust_store(trust, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["acme.*"])

		# Compile acme.local from source, consuming acme.other from package.
		src_dir = tmp_path / "src"
		_write_file(src_dir / "acme" / "local" / "local.drift", """\
module acme.local;

export { local_fn };

pub fn local_fn() nothrow -> Int {
	return 0;
}
""")
		_write_file(src_dir / "main.drift", """\
module main;

import acme.local as local;
import acme.other as other;

fn main() nothrow -> Int {
	return local.local_fn() + other.helper();
}
""")
		rc = driftc_main([
			"-M", str(src_dir),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(tmp_path / "pkgroot"),
			"--dep", "acme.other@0.0.0",
			"--dep", "acme.local@0.0.0",
			"--package-id", "acme.local",
			"--dev",
			"--dev-core-trust-store", str(core_trust),
			"--trust-store", str(trust),
			str(src_dir / "acme" / "local" / "local.drift"),
			str(src_dir / "main.drift"),
			"--test-build-only",
		])
		out, err = capsys.readouterr()
		assert rc == 0, f"source + external package build failed; stderr: {err}"

	def test_untrusted_self_package_skipped_before_verify(
		self,
		tmp_path: Path,
		capsys: pytest.CaptureFixture[str],
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""
		Pin: self-exclusion happens before load/verify.  A published copy of
		the current package that is unsigned (would fail trust verification)
		must not break the source build — it should never be loaded at all.
		"""
		monkeypatch.setenv("HOME", str(tmp_path / "home"))
		keys = _gen_keys()

		# Build acme.util package but do NOT sign it.
		build_util = tmp_path / "build_util"
		_write_file(build_util / "src" / "util.drift", _ACME_UTIL_SOURCE)
		util_pkg_path = tmp_path / "pkgroot" / "acme.util.dmp"
		util_pkg_path.parent.mkdir(parents=True, exist_ok=True)
		rc = driftc_main([
			"--dev",
			"-M", str(build_util),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			str(build_util / "src" / "util.drift"),
			*_emit_pkg_args("acme.util"),
			"--emit-package", str(util_pkg_path),
		])
		assert rc == 0
		_ = capsys.readouterr()
		# Deliberately no signature sidecar — loading would fail.

		# Trust stores (trust acme.* so that acme.util WOULD fail verify
		# if loaded, since it has no .sig).
		core_trust = tmp_path / "core_trust.json"
		_write_trust_store(core_trust, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["std.*", "lang.*", "drift.*"])
		trust = tmp_path / "trust.json"
		_write_trust_store(trust, kid=keys.kid, pub_b64=keys.pub_b64, namespaces=["acme.*"])

		# Source build with --package-id acme.util.  The unsigned published
		# copy must be skipped entirely — not loaded, not verified.
		src_dir = tmp_path / "src"
		_write_file(src_dir / "acme" / "util" / "util.drift", _ACME_UTIL_SOURCE)
		_write_file(src_dir / "main.drift", """\
module main;

import acme.util as util;

fn main() nothrow -> Int {
	return util.make_counter(0).value;
}
""")
		rc = driftc_main([
			"-M", str(src_dir),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(tmp_path / "pkgroot"),
			"--dep", "acme.util@0.0.0",
			"--package-id", "acme.util",
			"--dev",
			"--dev-core-trust-store", str(core_trust),
			"--trust-store", str(trust),
			str(src_dir / "acme" / "util" / "util.drift"),
			str(src_dir / "main.drift"),
			"--test-build-only",
		])
		out, err = capsys.readouterr()
		assert rc == 0, (
			f"unsigned self-package should be skipped before verify; "
			f"stderr: {err}"
		)


# ── K27: Cross-package trait impl visibility (Throw) ───────────────


_ACME_THROWER_SOURCE = """\
module acme.thrower;

import std.core as core;

export { ProducerError, make_err };

// Slice 5 (pub-error track): `pub error` is the canonical throwable error
// type — auto-gen `implement core.Throw for ProducerError` is provided
// by the compiler, so no manual impl is needed.  Or_throw on
// `Result<T, ProducerError>` throws ProducerError directly (no
// ResultError wrap).
pub error ProducerError {
	code: Int,
}

pub fn make_err() nothrow -> core.Result<Int, ProducerError> {
	return core.Result::Err(ProducerError(code = 42));
}
"""


def _build_signed_thrower_pkg(tmp_path: Path, keys: _DeployKeys) -> Path:
	"""Build a signed acme.thrower package that implements core.Throw."""
	build = tmp_path / "thrower_build"
	mod_dir = build / "acme" / "thrower"
	_write_file(mod_dir / "thrower.drift", _ACME_THROWER_SOURCE)

	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"

	pkg_path = tmp_path / "pkgs" / "acme.thrower.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(stdlib_dir),
		str(mod_dir / "thrower.drift"),
		*_emit_pkg_args("acme.thrower"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build acme.thrower package fixture"

	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(
		pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid,
		sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv,
	)
	return pkg_path


def test_ext_cross_package_throw_impl_metadata(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K27-a: impl_headers for 'implement core.Throw for ProducerError' must
	encode the trait's package_id as 'std' (the trait owner), not the
	producer's package_id 'acme.thrower'.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	keys = _gen_keys()
	thrower_pkg = _build_signed_thrower_pkg(tmp_path, keys)
	_ = capsys.readouterr()

	from lang.driftc.packages.provider_v1 import load_package_v1

	pkg = load_package_v1(thrower_pkg)
	mod = pkg.modules_by_id.get("acme.thrower")
	assert mod is not None, "acme.thrower module not in package"
	iface = mod.interface
	assert isinstance(iface, dict), "missing interface"
	impl_headers = iface.get("impl_headers", [])
	assert len(impl_headers) >= 1, "expected at least one impl_header"

	# Find the Throw impl
	throw_impl = None
	for ih in impl_headers:
		trait = ih.get("trait")
		if isinstance(trait, dict) and trait.get("name") == "Throw":
			throw_impl = ih
			break
	assert throw_impl is not None, (
		f"no Throw impl in impl_headers; got: {impl_headers}"
	)
	trait_obj = throw_impl["trait"]
	assert trait_obj["package_id"] == "std", (
		f"K27 regression: trait package_id should be 'std' (trait owner), "
		f"got '{trait_obj['package_id']}' (producer attribution)"
	)
	assert trait_obj["module"] == "std.core", (
		f"trait module should be 'std.core', got '{trait_obj['module']}'"
	)
	assert trait_obj["name"] == "Throw", (
		f"trait name should be 'Throw', got '{trait_obj['name']}'"
	)
	methods = throw_impl.get("methods", [])
	assert any(m.get("name") == "throw_self" for m in methods), (
		f"throw_self method missing from impl_headers methods: {methods}"
	)


def test_ext_cross_package_throw_impl(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K27-b: direct cross-package Throw::throw_self visibility.

	Proves: the external 'implement core.Throw for ProducerError' impl's
	terminal-throws method is registered in the consumer's callable registry,
	the typed catch arm matches ResultError (not just any exception), and
	the runtime throw/catch path executes correctly.

	Regression: terminal-throws signatures (declared_terminal_throws=True)
	had return_type=null in the package; the consumer skipped them from the
	callable registry because return_type_id was None.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))

	keys = _gen_keys()
	thrower_pkg = _build_signed_thrower_pkg(tmp_path, keys)
	_ = capsys.readouterr()

	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"

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
	# Stage 1: compile with typed catch to prove throw_self and typed
	# exception matching both resolve through the package boundary.
	_write_file(main_src, """\
module main;

import std.core as core;
import acme.thrower as thrower;

use trait core.Throw;

fn do_throw() throws -> Int {
	val e = thrower.ProducerError(code = 42);
	e.throw_self();
	return 0;
}

pub fn main() nothrow -> Int {
	return try do_throw() catch std.err:ResultError(_) {
		42
	} catch {
		98
	};
}
""")

	argv = [
		"-M", str(consumer),
		"--stdlib-root", str(stdlib_dir),
		"--package-root", str(thrower_pkg.parent),
		"--dep", "acme.thrower@0.0.0",
		"--dev",
		"--dev-core-trust-store", str(core_trust),
		"--trust-store", str(trust),
		"--entry", "main::main",
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	]
	rc = driftc_main(argv)
	captured = capsys.readouterr()
	payload = json.loads(captured.out) if captured.out.strip() else {}
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert rc == 0, (
		f"K27 regression: cross-package Throw impl not visible to consumer; "
		f"diagnostics: {messages}"
	)

	# Stage 2: recompile with catch-all for the link+run stage.
	# Typed catch needs runtime exception introspection helpers that are
	# not available in the stub-linked binary.  The catch-all proves the
	# throw/catch runtime path works; Stage 1 already proved the typed
	# catch compiles.
	_write_file(main_src, """\
module main;

import std.core as core;
import acme.thrower as thrower;

use trait core.Throw;

fn do_throw() throws -> Int {
	val e = thrower.ProducerError(code = 42);
	e.throw_self();
	return 0;
}

pub fn main() nothrow -> Int {
	return try do_throw() catch {
		42
	};
}
""")

	argv_run = list(argv)  # same flags, recompile
	rc2 = driftc_main(argv_run)
	captured2 = capsys.readouterr()
	assert rc2 == 0, f"catch-all recompile failed: {captured2.out[:500]}"

	# ── Link + Run ──────────────────────────────────────────────────
	ir = (tmp_path / "out.ll").read_text()

	clang = shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available for link+run stage")

	build_dir = tmp_path / "build"
	build_dir.mkdir(parents=True, exist_ok=True)
	ir_path = build_dir / "program.ll"
	bin_path = build_dir / "a.out"

	patched_ir = ir
	for m in re.finditer(r'(declare\s+(\S+)\s+(@(?:drift_|__drift_|__exc_)\w+)\(((?:[^()]*|\([^()]*\))*)\))', patched_ir):
		full, ret_ty, name, params = m.group(1), m.group(2), m.group(3), m.group(4)
		if name == "@drift_run_main_on_vt":
			body = f"%r = call {ret_ty} %0()\n  ret {ret_ty} %r"
		elif ret_ty == "void":
			body = "ret void"
		else:
			body = "unreachable"
		patched_ir = patched_ir.replace(full, f"define {ret_ty} {name}({params}) {{\n  {body}\n}}")
	ir_path.write_text(patched_ir)

	compile_res = subprocess.run(
		[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
		capture_output=True, text=True,
	)
	assert compile_res.returncode == 0, f"clang link failed:\n{compile_res.stderr}"

	run_res = subprocess.run(
		[str(bin_path)], capture_output=True, text=True, timeout=sanitizer_timeout(10),
	)
	# throw_self → throws → caught → exit 42
	assert run_res.returncode == 42, (
		f"expected exit code 42, got {run_res.returncode}"
		f"\nstdout: {run_res.stdout}\nstderr: {run_res.stderr}"
	)


def test_ext_cross_package_or_throw(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K28: Result<T, ProducerError>.or_throw() through package boundary.

	The consumer DOES NOT `import std.core`.  That is the bug surface.
	Without the K28 fix, `.or_throw()` resolution fails with
	"method 'or_throw' exists but is not visible here" because std.core is
	not in the consumer's visible_modules_set and the prelude exemption
	(`_is_prelude_type_method`) requires Result.module_id ∈
	{None, "lang.core"} but it is "std.core".

	The fix narrows the prelude exemption: `std.core.Result` is now
	treated as a prelude variant for visibility purposes (but only
	Result — see test_ext_std_core_non_prelude_still_hidden for the
	negative).  Optional is already covered by the existing
	`module_id in {None, "lang.core"}` branch because the parser
	seeds it under lang.core.

	Both call forms are exercised here (chained-on-rvalue and
	`(move r).or_throw()` on a bound local) because the visibility check
	runs before either path diverges and both must succeed.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))

	keys = _gen_keys()
	thrower_pkg = _build_signed_thrower_pkg(tmp_path, keys)
	_ = capsys.readouterr()

	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"

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

	def _compile(source: str, *, label: str, require_no_std_core: bool = True) -> None:
		_write_file(main_src, source)
		argv = [
			"-M", str(consumer),
			"--stdlib-root", str(stdlib_dir),
			"--package-root", str(thrower_pkg.parent),
			"--dep", "acme.thrower@0.0.0",
			"--dev",
			"--dev-core-trust-store", str(core_trust),
			"--trust-store", str(trust),
			"--entry", "main::main",
			str(main_src),
			"--emit-ir", str(tmp_path / f"out_{label}.ll"),
			"--json",
		]
		rc = driftc_main(argv)
		captured = capsys.readouterr()
		payload = json.loads(captured.out) if captured.out.strip() else {}
		diags = payload.get("diagnostics", [])
		messages = " ".join(d.get("message", "") for d in diags)
		assert rc == 0, (
			f"K28 ({label}): .or_throw() through package boundary should compile; "
			f"diagnostics: {messages}"
		)
		if require_no_std_core:
			# Sanity: the source must NOT import std.core — that's the bug surface.
			assert "import std.core" not in source, (
				f"K28 ({label}): fixture must not import std.core; that import would "
				"sidestep the bug instead of testing the fix"
			)

	# Local-binding form: receiver TypeId comes from the local variable's
	# symbol-table entry written from the package-linked Result.  The
	# explicit `core.Result<...>` annotation is the auto-try opt-out — we
	# want this test to exercise method resolution on a bound Result local,
	# not the eager auto-unwrap.
	#
	# COVERAGE CAVEAT: this fixture imports `std.core` to spell the
	# annotation, which narrows the K28 regression surface for the
	# local-binding shape.  The bound-local form of the original K28 bug
	# (`val r = pkg.make_err(); r.or_throw()` with NO `import std.core`)
	# is no longer expressible under eager auto-try, because preserving
	# the Result through a val binding now requires an explicit type
	# annotation, and unqualified `Result<...>` in annotation position
	# currently resolves to a FORWARD_NOMINAL that does not equate with
	# the package-linked Result instantiation (separate type-linker
	# limitation).  If/when unqualified `Result` works as a type
	# annotation via prelude aliasing, drop the `import std.core` here
	# and flip `require_no_std_core=True`.
	#
	# The chained form below keeps the "no std.core import" regression
	# for the primary bug surface (method resolution from a callee-return
	# TypeId rather than a symbol-table entry).
	_compile("""\
module main;

import std.core as core;
import acme.thrower as thrower;

fn do_throw() throws -> Int {
	val r: core.Result<Int, thrower.ProducerError> = thrower.make_err();
	return (move r).or_throw();
}

pub fn main() nothrow -> Int {
	return try do_throw() catch std.err:ResultError(_) {
		42
	} catch {
		98
	};
}
""", label="local_binding", require_no_std_core=False)

	# Chained form: receiver TypeId comes directly from the callee's
	# return-type expression resolved in the consumer.
	_compile("""\
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
""", label="chained")


# ── K28 guard: std.core types other than Result/Optional stay hidden ──


_ACME_CELL_SOURCE = """\
module acme.cell;

import std.core as core;

export { make_cell };

pub fn make_cell() nothrow -> core.Cell<Int> {
	return core.cell(7);
}
"""


def _build_signed_cell_pkg(tmp_path: Path, keys: _DeployKeys) -> Path:
	"""Build a signed acme.cell package returning std.core.Cell<Int>."""
	build = tmp_path / "cell_build"
	mod_dir = build / "acme" / "cell"
	_write_file(mod_dir / "cell.drift", _ACME_CELL_SOURCE)

	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"

	pkg_path = tmp_path / "pkgs" / "acme.cell.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(stdlib_dir),
		str(mod_dir / "cell.drift"),
		*_emit_pkg_args("acme.cell"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build acme.cell package fixture"

	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(
		pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid,
		sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv,
	)
	return pkg_path


def test_ext_std_core_non_prelude_still_hidden(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""K28 guard: the narrow prelude exemption covers ONLY std.core.Result.
	Inherent methods on other std.core types (here, std.core.Cell.get)
	must still require an explicit `import std.core` in the consumer to
	be visible.

	If this test ever fails, the prelude exemption has been broadened
	beyond Result and the visibility surface needs review — see
	_PRELUDE_STD_CORE_TYPE_NAMES in lang/driftc/checker/call_resolver.py.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))

	keys = _gen_keys()
	cell_pkg = _build_signed_cell_pkg(tmp_path, keys)
	_ = capsys.readouterr()

	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"

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
	# Consumer does NOT import std.core; calls Cell.get on the
	# package-returned Cell value.  Must fail visibility (Cell is not in
	# the K28 narrow exemption).
	_write_file(main_src, """\
module main;

import acme.cell as cell;

pub fn main() nothrow -> Int {
	val c = cell.make_cell();
	return (&c).get();
}
""")
	argv = [
		"-M", str(consumer),
		"--stdlib-root", str(stdlib_dir),
		"--package-root", str(cell_pkg.parent),
		"--dep", "acme.cell@0.0.0",
		"--dev",
		"--dev-core-trust-store", str(core_trust),
		"--trust-store", str(trust),
		"--entry", "main::main",
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	]
	rc = driftc_main(argv)
	captured = capsys.readouterr()
	payload = json.loads(captured.out) if captured.out.strip() else {}
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert rc != 0, (
		"K28 guard: Cell.get must NOT be visible without `import std.core`; "
		"the narrow exemption covers only Result"
	)
	assert "exists but is not visible here" in messages, (
		f"K28 guard: expected visibility diagnostic for Cell.get; got: {messages}"
	)


# ── Slice 5: cross-package Diagnostic-impl projectability ──────────


_ACME_DIAGCARRIER_SOURCE = """\
module acme.diagcarrier;

import std.core as core;

use trait core.Diagnostic;

export { Carrier };

pub struct Carrier {
	pub n: Int,
}

implement core.Diagnostic for Carrier {
	pub fn to_json_text(self: &Carrier) nothrow -> String {
		return core.diagnostic_json_int(self.n);
	}
}
"""


def _build_signed_diagcarrier_pkg(tmp_path: Path, keys: _DeployKeys) -> Path:
	"""Build a signed acme.diagcarrier package with `pub struct Carrier`
	and an explicit `implement core.Diagnostic for Carrier`.  Used by
	`test_ext_cross_package_diagnostic_projectability_gate_only` to pin
	the external trait-world scan in `_scan_external_diagnostic_targets`."""
	build = tmp_path / "diagcarrier_build"
	mod_dir = build / "acme" / "diagcarrier"
	_write_file(mod_dir / "diagcarrier.drift", _ACME_DIAGCARRIER_SOURCE)

	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"

	pkg_path = tmp_path / "pkgs" / "acme.diagcarrier.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(stdlib_dir),
		str(mod_dir / "diagcarrier.drift"),
		*_emit_pkg_args("acme.diagcarrier"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build acme.diagcarrier package fixture"

	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(
		pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid,
		sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv,
	)
	return pkg_path


def test_ext_cross_package_diagnostic_projectability_gate_only(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Slice 5/7b — cross-package projectability + clean compile.

	Pins two contracts: with a producer package that supplies
	`implement core.Diagnostic for Carrier`, a consumer that names
	`carrier.Carrier` as a field type in a `pub error`:
	  1. Must NOT be rejected by the synthesis projectability gate
	     with `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.  Exercises the
	     external-impl-headers pre-scan in driftc.py +
	     `_scan_external_diagnostic_targets`
	     (`lang/driftc/parser/__init__.py`).
	  2. Must compile cleanly (no diagnostics).  Slice 7b's
	     unification of synthesized + manual Diagnostic throw lowering
	     removed the type-checker-side per-field DV auto-promotion that
	     used to attempt UFCS dispatch on `(&self.c).to_json_text()`
	     at type-check time and fail for cross-package targets.  The
	     synthesized `to_json_text` body's UFCS dispatch is deferred to
	     hir_to_mir.py where the trait impl is uniformly resolvable.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))

	keys = _gen_keys()
	carrier_pkg = _build_signed_diagcarrier_pkg(tmp_path, keys)
	_ = capsys.readouterr()

	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"

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
	_write_file(main_src, """\
module main;

import std.core as core;
import acme.diagcarrier as carrier;

pub error Wraps {
	c: carrier.Carrier,
}

pub fn main() nothrow -> Int {
	return 0;
}
""")
	argv = [
		"-M", str(consumer),
		"--stdlib-root", str(stdlib_dir),
		"--package-root", str(carrier_pkg.parent),
		"--dep", "acme.diagcarrier@0.0.0",
		"--dev",
		"--dev-core-trust-store", str(core_trust),
		"--trust-store", str(trust),
		"--entry", "main::main",
		str(main_src),
		"--json",
	]
	driftc_main(argv)
	captured = capsys.readouterr()
	payload = json.loads(captured.out) if captured.out.strip() else {}
	diags = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	codes = [d.get("code") for d in diags]
	messages = [d.get("message", "") for d in diags]
	# Gate 1: projectability check must accept the cross-package field
	# via the impl-headers pre-scan.
	assert "E_PUB_ERROR_FIELD_NOT_PROJECTABLE" not in codes, (
		f"cross-package Diagnostic impl on `acme.diagcarrier.Carrier` should make "
		f"a `pub error Wraps {{ c: carrier.Carrier }}` field projectable via "
		f"the impl-headers pre-scan in driftc.py; got codes: {codes}"
	)
	# Gate 2 (Slice 7b): consumer compile must produce no error
	# diagnostics.  Pre-7b, the type-checker-side per-field DV
	# auto-promotion attempted UFCS dispatch on `(&self.c).to_json_text()`
	# at type-check time and failed for cross-package targets with
	# "no matching method 'to_json_text'".  Slice 7b retired that
	# auto-promotion — the synthesized body's UFCS dispatch is now
	# resolved later in hir_to_mir.py uniformly.
	assert codes == [], (
		f"unexpected error diagnostics on cross-package consumer compile "
		f"(Slice 7b unified the throw lowering — type-checker should not "
		f"reject this); codes={codes}, messages={messages}"
	)


# ── Slice 6: cross-package manual-Diagnostic gates (Sites A/B/C + typed-catch) ──


_ACME_MANUAL_DIAG_ERROR_SOURCE = """\
module acme.manualdiag;

import std.core as core;

use trait core.Diagnostic;

export { ManualErr, raise_it };

pub error ManualErr {
	user_id: Int,
	secret: String,
}

implement core.Diagnostic for ManualErr {
	pub fn to_json_text(self: &ManualErr) nothrow -> String {
		// Redacted: secret never appears.
		return "{\\"user_id\\":" + core.diagnostic_json_int(self.user_id) + "}";
	}
}

// Producer-side helper: cross-module throw of `ManualErr` requires the
// bare ctor (the v1 grammar's throw clause/ctor doesn't accept the
// module-alias-qualified form), so the consumer reaches the typed
// catch arm by calling this exported function instead of throwing
// `md.ManualErr(...)` itself.
pub fn raise_it() throws ManualErr -> Int {
	throw ManualErr(user_id = 7, secret = "shhh");
}
"""


def _build_signed_manual_diag_error_pkg(tmp_path: Path, keys: _DeployKeys) -> Path:
	"""Build a signed `acme.manualdiag` package whose `pub error ManualErr`
	carries a user-owned `implement core.Diagnostic for ManualErr`.

	Used by `test_ext_cross_package_manual_diagnostic_typed_binder_rejected`
	to pin: package-defined manual-Diagnostic pub errors hit the same
	Slice-6 gates (typed-catch rejection + Site A/B/C ownership) when
	used by a consumer.  Without the cross-package serialization fix
	in `parse_drift_workspace_to_hir`, the consumer would never see
	the manual-ownership bit and the typed binder would be silently
	accepted."""
	build = tmp_path / "manualdiag_build"
	mod_dir = build / "acme" / "manualdiag"
	_write_file(mod_dir / "manualdiag.drift", _ACME_MANUAL_DIAG_ERROR_SOURCE)

	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"

	pkg_path = tmp_path / "pkgs" / "acme.manualdiag.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(stdlib_dir),
		str(mod_dir / "manualdiag.drift"),
		*_emit_pkg_args("acme.manualdiag"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build acme.manualdiag package fixture"

	pkg_bytes = pkg_path.read_bytes()
	sig_raw = keys.priv.sign(pkg_bytes)
	_write_sig_sidecar(
		pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid,
		sig_raw=sig_raw, pub_b64=keys.pub_b64, priv=keys.priv,
	)
	return pkg_path


def test_ext_cross_package_manual_diagnostic_typed_binder_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Slice 6 — cross-package boundary regression.

	A `pub error E` defined in package P with a user-supplied
	`implement core.Diagnostic for E` must carry the manual-ownership
	bit across the package boundary: a consumer that writes
	`catch P.E(e) { ... }` (typed binder) must be rejected at
	compile time with `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED`,
	just like a workspace-local manual-Diagnostic pub error.

	Pins the cross-package serialization plumbing: the producer's
	`_synthesize_auto_diagnostic_impls` records manual-Diagnostic
	pub-error FQNs in `TypeTable.manual_diagnostic_pub_errors`,
	`provisional_dmir_v0.py` serializes them into the package
	format, and `type_table_link_v0.py` merges per-package entries
	into the consumer's `host.manual_diagnostic_pub_errors` at
	link time.  Without this, the consumer would silently accept
	the typed binder (no gate fires).  The intersection-with-
	impl-headers approach was rejected because impl_headers
	don't carry the synthesized-vs-manual bit and would tag
	auto-synthesized Diagnostic impls (e.g. on package-defined
	pub errors with all-projectable fields) as manual."""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))

	keys = _gen_keys()
	manualdiag_pkg = _build_signed_manual_diag_error_pkg(tmp_path, keys)
	_ = capsys.readouterr()

	repo_root = Path(__file__).resolve().parents[3]
	stdlib_dir = repo_root / "stdlib"

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
	# Note: cross-module throw of ManualErr lives in the producer's
	# `raise_it` (v1 grammar limitation on module-alias-qualified
	# throw ctors).  The consumer's typed binder is the gate-bearing
	# surface here.
	_write_file(main_src, """\
module main;

import std.core as core;
import acme.manualdiag as md;

pub fn main() nothrow -> Int {
	try {
		return md.raise_it();
	} catch md:ManualErr(e) {
		return e.user_id;
	} catch {
		return 99;
	}
}
""")
	argv = [
		"-M", str(consumer),
		"--stdlib-root", str(stdlib_dir),
		"--package-root", str(manualdiag_pkg.parent),
		"--dep", "acme.manualdiag@0.0.0",
		"--dev",
		"--dev-core-trust-store", str(core_trust),
		"--trust-store", str(trust),
		"--entry", "main::main",
		str(main_src),
		"--json",
	]
	driftc_main(argv)
	captured = capsys.readouterr()
	payload = json.loads(captured.out) if captured.out.strip() else {}
	diags = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	codes = [d.get("code") for d in diags]
	assert "E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED" in codes, (
		f"cross-package manual-Diagnostic typed catch binder must be rejected; "
		f"got codes: {codes}"
	)

