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
	sig_path = pkg.pkg_path.with_suffix(".sig")
	sig_path.unlink()

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
	parity checks (same 5 checks as DRIFT_DEBUG=convergence_parity).

	If this test fails, the local and package-consumer codepaths have diverged.
	"""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	monkeypatch.setenv("DRIFT_DEBUG", '{"convergence_parity": true}')
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
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
		_write_sig_sidecar(util_pkg_path, pkg_bytes=util_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
		_write_sig_sidecar(other_pkg_path, pkg_bytes=other_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
		_write_sig_sidecar(util_pkg_path, pkg_bytes=util_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
		_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
		_write_sig_sidecar(local_pkg_path, pkg_bytes=local_bytes, kid=keys.kid, sig_raw=sig_raw, pub_b64=keys.pub_b64)

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
