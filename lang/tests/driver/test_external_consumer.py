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
from lang.driftc.packages.signature_v0 import compute_ed25519_kid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


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


def _write_trust_store(path: Path, *, kid: str, pub_b64: str, namespaces: list[str], revoked: list[str] | None = None) -> None:
	revoked = revoked or []
	obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {ns: [kid] for ns in namespaces},
		"revoked": revoked,
	}
	_write_file(path, json.dumps(obj, separators=(",", ":"), sort_keys=True))


def _write_sig_sidecar(pkg_path: Path, *, pkg_bytes: bytes, kid: str, sig_raw: bytes, pub_b64: str | None = None) -> Path:
	pkg_sha_hex = _sha256_hex(pkg_bytes)
	entry: dict = {"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw)}
	if pub_b64 is not None:
		entry["pubkey"] = pub_b64
	sidecar = Path(str(pkg_path) + ".sig")
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


def _empty_stdlib_root(tmp_path: Path) -> Path:
	d = tmp_path / "_empty_stdlib"
	d.mkdir(parents=True, exist_ok=True)
	return d


def _emit_pkg_args(package_id: str) -> list[str]:
	return [
		"--package-id", package_id,
		"--package-version", "0.0.0",
		"--package-target", "test-target",
	]


# ── Shared fixture: signed acme.util package ─────────────────────────


_ACME_UTIL_SOURCE = """\
module acme.util

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
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
) -> tuple[int, dict, str, str]:
	"""Compile consumer source against a signed package.

	Returns (rc, json_payload, diagnostic_messages, stderr).
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
		source="""\
module runner

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
		source="""\
module main

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
		source="""\
module main

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
		source="""\
module main

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
		source="""\
module main

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
		source="""\
module main

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
		source="""\
module main

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
module runner

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


@pytest.mark.parametrize("optimized", [False, True], ids=["debug", "optimized"])
def test_ext_package_consumer_e2e(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
	optimized: bool,
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
	  4. Debug and optimized modes (parametrized)
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	# ── Compile ──────────────────────────────────────────────────────
	consumer = tmp_path / "consumer"
	main_src = consumer / "runner.drift"
	_write_file(main_src, _CONSUMER_E2E_SOURCE)
	extra_args = ["--optimized"] if optimized else []
	argv = [
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg.pkg_root),
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

	# K16a pinned: wrapper symbols specifically must be *defined*.
	wrap_refs = {s for s in called if "__wrap_method" in s}
	wrap_defs = {s for s in defined if "__wrap_method" in s}
	assert wrap_refs, "expected wrapper call targets in IR (nothrow method boundary)"
	assert not (wrap_refs - wrap_defs), f"undefined wrapper symbols in IR: {wrap_refs - wrap_defs}"

	# K16b pinned: OS entry wrapper.
	assert "define i32 @main" in ir, "package-consumer IR must contain a C main entry point"

	# ── Stage 2: Link ────────────────────────────────────────────────
	clang = shutil.which("clang-15") or shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available for link+run stage")

	build_dir = tmp_path / "build"
	build_dir.mkdir(parents=True, exist_ok=True)
	ir_path = build_dir / "program.ll"
	bin_path = build_dir / "a.out"

	# Provide stubs for drift runtime symbols (normally from the runtime
	# archive).  llvm.* intrinsics are handled by LLVM and need no stubs.
	patched_ir = ir
	for m in re.finditer(r'(declare\s+(\S+)\s+(@(?:drift_|__drift_)\w+)\(([^)]*)\))', patched_ir):
		full, ret_ty, name, params = m.group(1), m.group(2), m.group(3), m.group(4)
		body = "ret void" if ret_ty == "void" else "unreachable"
		patched_ir = patched_ir.replace(full, f"define {ret_ty} {name}({params}) {{\n  {body}\n}}")
	ir_path.write_text(patched_ir)

	compile_res = subprocess.run(
		[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
		capture_output=True, text=True,
	)
	assert compile_res.returncode == 0, f"clang link failed:\n{compile_res.stderr}"

	# ── Stage 3: Run ─────────────────────────────────────────────────
	run_res = subprocess.run(
		[str(bin_path)], capture_output=True, text=True, timeout=10,
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
module std.io

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
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)
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
module runner

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
	clang = shutil.which("clang-15") or shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available for link+run stage")

	build_dir = tmp_path / "build"
	build_dir.mkdir(parents=True, exist_ok=True)
	ir_path = build_dir / "program.ll"
	bin_path = build_dir / "a.out"
	patched_ir = ir
	for m in re.finditer(r'(declare\s+(\S+)\s+(@(?:drift_|__drift_)\w+)\(([^)]*)\))', patched_ir):
		full, ret_ty, name, params = m.group(1), m.group(2), m.group(3), m.group(4)
		body = "ret void" if ret_ty == "void" else "unreachable"
		patched_ir = patched_ir.replace(full, f"define {ret_ty} {name}({params}) {{\n  {body}\n}}")
	ir_path.write_text(patched_ir)

	compile_res = subprocess.run(
		[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
		capture_output=True, text=True,
	)
	assert compile_res.returncode == 0, f"clang link failed:\n{compile_res.stderr}"

	run_res = subprocess.run(
		[str(bin_path)], capture_output=True, text=True, timeout=10,
	)
	assert run_res.returncode == 5, (
		f"expected exit code 5, got {run_res.returncode}"
		f"\nstdout: {run_res.stdout}\nstderr: {run_res.stderr}"
	)


# ── Security negatives ───────────────────────────────────────────────


def test_ext_unsigned_package_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""An unsigned .dmp without sidecar must be rejected via signed path."""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	pkg = _build_signed_acme_pkg(tmp_path)
	_ = capsys.readouterr()

	# Remove .sig sidecar to make package unsigned.
	sig_path = Path(str(pkg.pkg_path) + ".sig")
	sig_path.unlink()

	rc, payload, messages, _stderr = _compile_consumer(
		tmp_path, capsys, pkg=pkg,
		source="""\
module main

import acme.util as util;

fn main() nothrow -> Int {
	return util.make_counter(1).value;
}
""",
	)
	assert rc != 0, "unsigned package should be rejected"
	assert "sidecar" in messages.lower() or "signature" in messages.lower() or "unsigned" in messages.lower(), f"expected sidecar/signature rejection, got: {messages}"


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
		source="""\
module main

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
module std.testlib

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
module main

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
module main

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
module acme.vis

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
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
		source="""\
module main

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
		source="""\
module main

import acme.vis as vis;

fn main() nothrow -> Int {
	val w = vis.wrap_and_show(42);
	return w._private_helper();
}
""",
	)
	assert rc != 0, f"private method on non-stdlib package type should be rejected"
	assert "_private_helper" in messages, f"expected rejection mentioning _private_helper, got: {messages}"
