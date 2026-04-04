# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.driftc import _abi_fingerprint, main as driftc_main
from lang.driftc.packages import dmir_pkg_v0
from lang.driftc.packages.provider_v0 import discover_package_files
from lang.driftc.packages.provider_v0 import load_package_v0
from lang.driftc.packages.dmir_pkg_v0 import canonical_json_bytes, sha256_hex, write_dmir_pkg_v0
from lang.driftc.packages.signature_v0 import compute_ed25519_kid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def _emit_pkg_args(package_id: str) -> list[str]:
	return [
		"--package-id",
		package_id,
		"--package-version",
		"0.0.0",
		"--package-target",
		"test-target",
	]


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _public_key_bytes(pub) -> bytes:
	if hasattr(pub, "public_bytes_raw"):
		return pub.public_bytes_raw()
	return pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def _patch_file_bytes(path: Path, offset: int, patch: bytes) -> None:
	data = path.read_bytes()
	if offset < 0 or offset + len(patch) > len(data):
		raise ValueError("patch out of range")
	new_data = data[:offset] + patch + data[offset + len(patch) :]
	path.write_bytes(new_data)


def _patch_pkg_header(path: Path, *, manifest_sha256: bytes | None = None, toc_sha256: bytes | None = None) -> None:
	header_bytes = path.read_bytes()[: dmir_pkg_v0.HEADER_SIZE_V0]
	(
		magic,
		version,
		flags,
		header_size,
		manifest_len,
		manifest_sha,
		toc_len,
		toc_entry_size,
		toc_sha,
		reserved,
	) = dmir_pkg_v0._HEADER_STRUCT.unpack(header_bytes)
	if manifest_sha256 is not None:
		manifest_sha = manifest_sha256
	if toc_sha256 is not None:
		toc_sha = toc_sha256
	new_header = dmir_pkg_v0._HEADER_STRUCT.pack(
		magic,
		version,
		flags,
		header_size,
		manifest_len,
		manifest_sha,
		toc_len,
		toc_entry_size,
		toc_sha,
		reserved,
	)
	_patch_file_bytes(path, 0, new_header)


def _patch_pkg_manifest_bytes_same_len(path: Path, patch_fn) -> None:
	"""
	Patch the manifest bytes in-place without changing `manifest_len`.

	This helper is intentionally strict: it requires the new bytes to be the same
	length as the old bytes so TOC offsets remain valid.
	"""
	header_bytes = path.read_bytes()[: dmir_pkg_v0.HEADER_SIZE_V0]
	(
		_magic,
		_version,
		_flags,
		_header_size,
		manifest_len,
		_manifest_sha,
		_toc_len,
		_toc_entry_size,
		_toc_sha,
		_reserved,
	) = dmir_pkg_v0._HEADER_STRUCT.unpack(header_bytes)
	manifest_off = dmir_pkg_v0.HEADER_SIZE_V0
	old = path.read_bytes()[manifest_off : manifest_off + int(manifest_len)]
	new = patch_fn(old)
	if len(new) != len(old):
		raise ValueError("manifest patch must not change length")
	_patch_file_bytes(path, manifest_off, new)
	_patch_pkg_header(path, manifest_sha256=dmir_pkg_v0.sha256_bytes(new))


def _b64(data: bytes) -> str:
	return base64.b64encode(data).decode("ascii")


def _sha256_hex(data: bytes) -> str:
	return sha256(data).hexdigest()


@dataclass(frozen=True)
class _SignedPkg:
	root: Path
	pkg_path: Path
	trust_path: Path
	kid: str
	pub_b64: str


def _emit_lib_pkg(tmp_path: Path, *, module_id: str = "acme.lib") -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id};

export {{ add }};

pub fn add(a: Int, b: Int) nothrow -> Int {{
	return a + b;
}}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "lib.drift"),
				*_emit_pkg_args(module_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_main_pkg(tmp_path: Path, *, module_id: str = "dep", package_id: str = "dep.main") -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "main.drift",
		f"""
module {module_id};

export {{ main }};

pub fn main() nothrow -> Int {{
	return 0;
}}
""".lstrip(),
	)
	pkg_path = tmp_path / "dep_main.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "main.drift"),
				*_emit_pkg_args(package_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_main_method_pkg(tmp_path: Path, *, module_id: str = "dep", package_id: str = "dep.method") -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id};

export {{ S }};

pub struct S {{ pub x: Int }}

implement S {{
	pub fn main(self: &S) nothrow -> Int {{
		return 0;
	}}
}}
""".lstrip(),
	)
	pkg_path = tmp_path / "dep_method.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "lib.drift"),
				*_emit_pkg_args(package_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_hidden_fn_pkg(tmp_path: Path, *, module_id: str = "acme.hidden") -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id};

export {{ add }};

pub fn add(a: Int, b: Int) nothrow -> Int {{
	return a + b;
}}

fn hidden() -> Int {{
	return 1;
}}
""".lstrip(),
	)
	pkg_path = tmp_path / "hidden.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "lib.drift"),
				*_emit_pkg_args(module_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_pub_hidden_fn_pkg(tmp_path: Path, *, module_id: str = "acme.hiddenpub") -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id};

export {{ add }};

pub fn add(a: Int, b: Int) nothrow -> Int {{
	return a + b;
}}

pub fn hidden() -> Int {{
	return 1;
}}
""".lstrip(),
	)
	pkg_path = tmp_path / "hiddenpub.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "lib.drift"),
				*_emit_pkg_args(module_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_star_reexport_pkg(
	tmp_path: Path,
	*,
	core_id: str = "acme.core",
	api_id: str = "acme.api",
	package_id: str = "acme",
) -> Path:
	core_dir = tmp_path.joinpath(*core_id.split("."))
	api_dir = tmp_path.joinpath(*api_id.split("."))
	_write_file(
		core_dir / "lib.drift",
		f"""
module {core_id};

export {{ add }};

pub fn add(a: Int, b: Int) nothrow -> Int {{
	return a + b;
}}
""".lstrip(),
	)
	_write_file(
		api_dir / "lib.drift",
		f"""
module {api_id};

export {{ {core_id}.* }};
""".lstrip(),
	)
	pkg_path = tmp_path / "star.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(core_dir / "lib.drift"),
				str(api_dir / "lib.drift"),
				*_emit_pkg_args(package_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_const_pkg(tmp_path: Path, *, module_id: str = "acme.consts") -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "consts.drift",
		f"""
module {module_id};

export {{ ANSWER }};

pub const ANSWER: Int = 42;
""".lstrip(),
	)
	pkg_path = tmp_path / "consts.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "consts.drift"),
				*_emit_pkg_args(module_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_point_type_only_pkg(tmp_path: Path, *, module_id: str = "acme.point") -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "point.drift",
		f"""
module {module_id};

export {{ Point }};

pub struct Point {{ pub x: Int, pub y: Int }}

fn make() -> Point {{
	return Point(x = 1, y = 2);
}}
""".lstrip(),
	)
	pkg_path = tmp_path / "point.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "point.drift"),
				*_emit_pkg_args(module_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_point_pkg(tmp_path: Path, *, module_id: str) -> Path:
	"""
	Emit a package that exports a `struct Point` and an exported constructor-like `make()`.

	This is used to validate module-scoped nominal type identity across multiple
	package-provided modules that share the same short type name.
	"""
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "point.drift",
		f"""
module {module_id};

export {{ Point, make }};

pub struct Point {{ pub x: Int, pub y: Int }}

pub fn make() nothrow -> Point {{
	return Point(x = 1, y = 0);
}}
""".lstrip(),
	)
	pkg_path = tmp_path / f"{module_id.replace('.', '_')}.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "point.drift"),
				*_emit_pkg_args(module_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_optional_variant_pkg(
	tmp_path: Path,
	*,
	module_id: str = "acme.opt",
	extra_arm: bool = False,
	pkg_name: str | None = None,
	package_id: str | None = None,
) -> Path:
	"""
	Emit a package that exports a generic `variant Maybe<T>` and a function
	that -> `Maybe<Int>`.

	This is used to validate package TypeTable linking for variants.
	"""
	module_dir = tmp_path.joinpath(*module_id.split("."))
	arms = (
		"""
	Some(value: T),
	None,
	Extra
""".lstrip()
		if extra_arm
		else """
	Some(value: T),
	None
""".lstrip()
	)
	_write_file(
		module_dir / "opt.drift",
		f"""
module {module_id};

export {{ Maybe, foo }};

pub variant Maybe<T> {{
{arms}
}}

pub fn foo() nothrow -> Maybe<Int> {{
	return Some(41);
}}
""".lstrip(),
	)
	pkg_path = tmp_path / (pkg_name or f"{module_id.replace('.', '_')}.dmp")
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "opt.drift"),
				*_emit_pkg_args(package_id or module_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_exception_pkg(tmp_path: Path, *, module_id: str = "acme.exc") -> Path:
	"""
Emit a package that exports an exception type (in the type namespace).

We include a dummy function so the module has at least one signature/MIR body and
is emitted into the package.
	"""
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "exc.drift",
		f"""
module {module_id};

export {{ Boom }};

pub exception Boom(a: Int, b: String);

fn dummy() nothrow -> Int {{
	return 0;
}}
""".lstrip(),
	)
	pkg_path = tmp_path / f"{module_id.replace('.', '_')}.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "exc.drift"),
				*_emit_pkg_args(module_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _write_trust_store(path: Path, *, kid: str, pub_b64: str, ns: str = "acme.*", revoked: list[str] | None = None) -> None:
	revoked = revoked or []
	obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {
			kid: {"algo": "ed25519", "pubkey": pub_b64},
		},
		"namespaces": {
			ns: [kid],
		},
		"revoked": revoked,
	}
	_write_file(path, json.dumps(obj, separators=(",", ":"), sort_keys=True))


def _write_sig_sidecar(
	pkg_path: Path,
	*,
	pkg_bytes: bytes,
	kid: str,
	sig_raw: bytes,
	pub_b64: str | None = None,
	package_sha256_override: str | None = None,
	extra_entries: list[dict] | None = None,
) -> Path:
	pkg_sha_hex = package_sha256_override or _sha256_hex(pkg_bytes)
	entry = {"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw)}
	if pub_b64 is not None:
		entry["pubkey"] = pub_b64
	sigs = [entry]
	if extra_entries:
		sigs.extend(extra_entries)
	sidecar = pkg_path.with_suffix(".sig")
	obj = {
		"format": "dmir-pkg-sig",
		"version": 0,
		"package_sha256": f"sha256:{pkg_sha_hex}",
		"signatures": sigs,
	}
	_write_file(sidecar, json.dumps(obj, separators=(",", ":"), sort_keys=True))
	return sidecar


def _make_signed_package(tmp_path: Path) -> _SignedPkg:
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	pkg_path = _emit_lib_pkg(tmp_path)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=kid, sig_raw=sig_raw)

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=kid, pub_b64=pub_b64)
	return _SignedPkg(root=tmp_path, pkg_path=pkg_path, trust_path=trust_path, kid=kid, pub_b64=pub_b64)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_emit_package_is_deterministic(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
	)

	out1 = tmp_path / "p1.dmp"
	out2 = tmp_path / "p2.dmp"

	argv_common = [
		"-M",
		str(tmp_path),
		str(tmp_path / "main.drift"),
		str(tmp_path / "lib" / "lib.drift"),
		*_emit_pkg_args("test.determinism"),
		"--emit-package",
	]
	assert driftc_main(argv_common + [str(out1)]) == 0
	assert driftc_main(argv_common + [str(out2)]) == 0

	assert out1.read_bytes() == out2.read_bytes()

	pkg = load_package_v0(out1)
	assert pkg.manifest["payload_kind"] == "provisional-dmir"
	assert pkg.manifest["payload_version"] == 2
	assert pkg.manifest["unstable_format"] is True


def test_emit_package_is_deterministic_with_permuted_package_roots(tmp_path: Path) -> None:
	"""
	Determinism guard: package root CLI ordering must not affect build output.

	This locks that `--package-root A --package-root B` yields identical package
	bytes as `--package-root B --package-root A` when inputs are the same.
	"""
	src_root = tmp_path / "src"
	pkgs_a = tmp_path / "pkgs_a"
	pkgs_b = tmp_path / "pkgs_b"
	src_root.mkdir(parents=True, exist_ok=True)
	pkgs_a.mkdir(parents=True, exist_ok=True)
	pkgs_b.mkdir(parents=True, exist_ok=True)

	# Build two packages under separate roots.
	_emit_lib_pkg(pkgs_a, module_id="acme.liba")
	_emit_optional_variant_pkg(pkgs_b, module_id="acme.optb")

	_write_file(
		src_root / "main.drift",
		"""
module main;

import acme.liba as liba;
import acme.optb as optb;

fn main() nothrow -> Int{
	try {
		val x = liba.add(40, 2);
		val y: optb.Maybe<Int> = optb.foo();
		val z = match y {
			Some(v) => { v },
			default => { 0 },
		};
		return x + z;
	} catch {
		return 0;
	}
}
""".lstrip(),
	)

	out1 = tmp_path / "out_ab.dmp"
	out2 = tmp_path / "out_ba.dmp"

	argv_common = ["-M", str(src_root), str(src_root / "main.drift")]
	argv_ab = argv_common + [
		"--package-root",
		str(pkgs_a),
		"--package-root",
		str(pkgs_b),
		"--allow-unsigned-from",
		str(pkgs_a),
		"--allow-unsigned-from",
		str(pkgs_b),
		"--dep",
		"acme.liba@0.0.0",
		"--dep",
		"acme.optb@0.0.0",
	]
	argv_ba = argv_common + [
		"--package-root",
		str(pkgs_b),
		"--package-root",
		str(pkgs_a),
		"--allow-unsigned-from",
		str(pkgs_a),
		"--allow-unsigned-from",
		str(pkgs_b),
		"--dep",
		"acme.liba@0.0.0",
		"--dep",
		"acme.optb@0.0.0",
	]

	assert driftc_main(argv_ab + [*_emit_pkg_args("test.determinism"), "--emit-package", str(out1)]) == 0
	assert driftc_main(argv_ba + [*_emit_pkg_args("test.determinism"), "--emit-package", str(out2)]) == 0
	assert out1.read_bytes() == out2.read_bytes()


def test_emit_package_is_deterministic_with_changed_package_filenames(tmp_path: Path) -> None:
	"""
	Determinism guard: package discovery ordering (filename sorting / rglob order);
	must not affect build output.
	"""
	src_root = tmp_path / "src"
	pkgs = tmp_path / "pkgs"
	src_root.mkdir(parents=True, exist_ok=True)
	pkgs.mkdir(parents=True, exist_ok=True)

	_emit_lib_pkg(pkgs, module_id="acme.liba")
	_emit_optional_variant_pkg(pkgs, module_id="acme.optb")

	_write_file(
		src_root / "main.drift",
		"""
module main;

import acme.liba as liba;
import acme.optb as optb;

fn main() nothrow -> Int{
	try {
		val x = liba.add(40, 2);
		val y: optb.Maybe<Int> = optb.foo();
		val z = match y {
			Some(v) => { v },
			default => { 0 },
		};
		return x + z;
	} catch {
		return 0;
	}
}
""".lstrip(),
	)

	out1 = tmp_path / "out_before.dmp"
	out2 = tmp_path / "out_after.dmp"
	argv = [
		"-M",
		str(src_root),
		str(src_root / "main.drift"),
		"--package-root",
		str(pkgs),
		"--allow-unsigned-from",
		str(pkgs),
		"--dep",
		"acme.liba@0.0.0",
		"--dep",
		"acme.optb@0.0.0",
		*_emit_pkg_args("test.determinism"),
		"--emit-package",
	]

	assert driftc_main(argv + [str(out1)]) == 0

	# Rename package files to flip sorted discovery order.
	# The content is identical; only the filesystem ordering changes.
	(pkg1, pkg2) = (pkgs / "lib.dmp", pkgs / "acme_optb.dmp")
	assert pkg1.exists()
	assert pkg2.exists()
	pkg1.rename(pkgs / "z_lib.dmp")
	pkg2.rename(pkgs / "a_optb.dmp")

	assert driftc_main(argv + [str(out2)]) == 0
	assert out1.read_bytes() == out2.read_bytes()


def test_emit_package_is_deterministic_with_three_packages_and_derived_types(tmp_path: Path) -> None:
	"""
	Determinism stress test:
	- multiple single-module packages in one root (discovery order perturbations),
	- derived/instantiated types created in the consuming build (Maybe<geom.Point>),
	- output bytes must remain identical.
	"""
	src_root = tmp_path / "src"
	pkgs = tmp_path / "pkgs"
	src_root.mkdir(parents=True, exist_ok=True)
	pkgs.mkdir(parents=True, exist_ok=True)

	_emit_lib_pkg(pkgs, module_id="acme.liba")
	_emit_point_pkg(pkgs, module_id="acme.geom")
	_emit_optional_variant_pkg(pkgs, module_id="acme.opt")

	_write_file(
		src_root / "main.drift",
		"""
module main;

import acme.geom as g;

import acme.liba as liba;
import acme.opt as opt;

fn main() nothrow -> Int{
	try {
		val p: g.Point = g.make();
		val o: opt.Maybe<g.Point> = Some(p);
		val x = match o {
			Some(v) => { v.x },
			default => { 0 },
		};
		return (try liba.add(40, 2) catch { 0 }) + x;
	} catch {
		return 0;
	}
}
""".lstrip(),
	)

	out1 = tmp_path / "out_before.dmp"
	out2 = tmp_path / "out_after.dmp"
	argv = [
		"-M",
		str(src_root),
		str(src_root / "main.drift"),
		"--package-root",
		str(pkgs),
		"--allow-unsigned-from",
		str(pkgs),
		"--dep",
		"acme.liba@0.0.0",
		"--dep",
		"acme.geom@0.0.0",
		"--dep",
		"acme.opt@0.0.0",
		*_emit_pkg_args("test.determinism"),
		"--emit-package",
	]

	assert driftc_main(argv + [str(out1)]) == 0

	# Rename package files to change discovery order.
	(pkg1, pkg2, pkg3) = (pkgs / "lib.dmp", pkgs / "acme_geom.dmp", pkgs / "acme_opt.dmp")
	assert pkg1.exists()
	assert pkg2.exists()
	assert pkg3.exists()
	pkg1.rename(pkgs / "z_lib.dmp")
	pkg2.rename(pkgs / "m_geom.dmp")
	pkg3.rename(pkgs / "a_opt.dmp")

	assert driftc_main(argv + [str(out2)]) == 0
	assert out1.read_bytes() == out2.read_bytes()


def test_load_package_v0_round_trip(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
	)

	out = tmp_path / "p.dmp"
	assert driftc_main(
		[
			"-M",
			str(tmp_path),
			str(tmp_path / "main.drift"),
			str(tmp_path / "lib" / "lib.drift"),
			*_emit_pkg_args("test.roundtrip"),
			"--emit-package",
			str(out),
		]
	) == 0

	pkg = load_package_v0(out)
	assert pkg.manifest["payload_kind"] == "provisional-dmir"
	assert set(pkg.modules_by_id.keys()) >= {"lib", "main"}

	lib_iface = pkg.modules_by_id["lib"].interface
	assert lib_iface["module_id"] == "lib"
	assert "add" in lib_iface["exports"]["values"]


def test_load_package_rejects_bad_blob_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)

	# Load once to discover a concrete blob offset, then corrupt the blob bytes.
	pkg_ok = load_package_v0(pkg_path)
	assert pkg_ok.toc, "package should have at least one blob"
	blob = pkg_ok.toc[0]
	# Flip one byte at the start of the blob.
	orig = pkg_path.read_bytes()[blob.offset : blob.offset + 1]
	_patch_file_bytes(pkg_path, blob.offset, bytes([orig[0] ^ 0xFF]))

	with pytest.raises(ValueError, match="blob sha256 mismatch for"):
		load_package_v0(pkg_path)


def test_load_package_rejects_bad_manifest_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)

	_patch_pkg_header(pkg_path, manifest_sha256=b"\0" * 32)
	with pytest.raises(ValueError, match="manifest sha256 mismatch"):
		load_package_v0(pkg_path)


def test_load_package_rejects_bad_toc_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)

	_patch_pkg_header(pkg_path, toc_sha256=b"\0" * 32)
	with pytest.raises(ValueError, match="toc sha256 mismatch"):
		load_package_v0(pkg_path)


def test_load_package_rejects_duplicate_toc_blob_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)

	# Duplicate the first TOC entry's sha256 into the second entry.
	header_bytes = pkg_path.read_bytes()[: dmir_pkg_v0.HEADER_SIZE_V0]
	(
		_magic,
		_version,
		_flags,
		_header_size,
		manifest_len,
		_manifest_sha,
		toc_len,
		_toc_entry_size,
		_toc_sha,
		_reserved,
	) = dmir_pkg_v0._HEADER_STRUCT.unpack(header_bytes)
	assert toc_len >= 2
	toc_start = dmir_pkg_v0.HEADER_SIZE_V0 + int(manifest_len)
	first_entry_off = toc_start
	second_entry_off = toc_start + dmir_pkg_v0.TOC_ENTRY_SIZE_V0
	first_sha = pkg_path.read_bytes()[first_entry_off : first_entry_off + 32]
	_patch_file_bytes(pkg_path, second_entry_off, first_sha)

	# Update toc_sha256 in the header so we reach TOC parsing.
	toc_bytes = pkg_path.read_bytes()[toc_start : toc_start + int(toc_len) * dmir_pkg_v0.TOC_ENTRY_SIZE_V0]
	_patch_pkg_header(pkg_path, toc_sha256=dmir_pkg_v0.sha256_bytes(toc_bytes))

	with pytest.raises(ValueError, match="duplicate blob sha256 in toc"):
		load_package_v0(pkg_path)


def test_driftc_rejects_duplicate_module_id_across_packages(tmp_path: Path) -> None:
	# Create two packages that both provide module `lib`.
	for n in (1, 2):
		root = tmp_path / f"p{n}"
		_write_file(
			root / "lib" / "lib.drift",
			f"""
module lib;

export {{ add }};

pub fn add(a: Int, b: Int) nothrow -> Int {{
	return a + b + {n};
}}
""".lstrip(),
		)
		pkg = tmp_path / f"lib{n}.dmp"
		assert (
			driftc_main(
				[
					"-M",
					str(root),
					str(root / "lib" / "lib.drift"),
					*_emit_pkg_args(f"test.lib{n}"),
					"--emit-package",
					str(pkg),
				]
			)
			== 0
		)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"test.lib1@0.0.0",
			"--dep",
			"test.lib2@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0


def test_driftc_can_consume_package_with_additional_types_via_type_table_linking(tmp_path: Path) -> None:
	# Package defines an extra user type that is not present in the consuming build,
	# and exports it across the module boundary. The consumer should succeed via
	# link-time TypeTable unification + TypeId remapping.
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { S, make };

pub struct S { pub x: Int }

pub fn make() nothrow -> S {
	return S(x = 42);
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	try {
		val s: lib.S = lib.make();
		return s.x;
	} catch {
		return 0;
	}
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0


def test_driftc_rejects_dependency_main_entrypoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	_emit_main_pkg(tmp_path)
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"dep.main@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("illegal entrypoint 'main' in dependency package" in d.get("message", "") for d in diags)


def test_driftc_allows_dependency_method_named_main(tmp_path: Path) -> None:
	_emit_main_method_pkg(tmp_path)
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"dep.method@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0


def test_driftc_rejects_duplicate_module_ids_across_packages(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	pkgs_a = tmp_path / "pkgs_a"
	pkgs_b = tmp_path / "pkgs_b"
	pkgs_a.mkdir(parents=True, exist_ok=True)
	pkgs_b.mkdir(parents=True, exist_ok=True)

	module_id = "dup.mod"
	for root, pkg_id in ((pkgs_a, "pkg.a"), (pkgs_b, "pkg.b")):
		module_dir = root.joinpath(*module_id.split("."))
		_write_file(
			module_dir / "mod.drift",
			f"""
module {module_id};

export {{ add }};

pub fn add(a: Int, b: Int) nothrow -> Int {{
	return a + b;
}}
""".lstrip(),
		)
		pkg_path = root / f"{pkg_id}.dmp"
		assert (
			driftc_main(
				[
					"-M",
					str(root),
					str(module_dir / "mod.drift"),
					*_emit_pkg_args(pkg_id),
					"--emit-package",
					str(pkg_path),
				]
			)
			== 0
		)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkgs_a),
			"--package-root",
			str(pkgs_b),
			"--allow-unsigned-from",
			str(pkgs_a),
			"--allow-unsigned-from",
			str(pkgs_b),
			"--dep",
			"pkg.a@0.0.0",
			"--dep",
			"pkg.b@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("provided by multiple packages" in d.get("message", "") for d in diags)


def test_driftc_rejects_unsigned_reserved_namespace_package(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
	)
	for module_id in ("std.evil", "lang.evil", "drift.evil"):
		pkg_root = tmp_path / f"pkgs_{module_id.replace('.', '_')}"
		pkg_root.mkdir(parents=True, exist_ok=True)
		module_dir = pkg_root.joinpath(*module_id.split("."))
		_write_file(
			module_dir / "evil.drift",
			f"""
module {module_id};

export {{ add }};

pub fn add(a: Int, b: Int) nothrow -> Int {{
	return a + b;
}}
""".lstrip(),
		)
		pkg_path = pkg_root / "evil.dmp"
		assert (
			driftc_main(
				[
					"--dev",
					"-M",
					str(pkg_root),
					str(module_dir / "evil.drift"),
					*_emit_pkg_args(module_id),
					"--emit-package",
					str(pkg_path),
				]
			)
			== 0
		)

		rc, payload = _run_driftc_json(
			[
				"-M",
				str(tmp_path),
				"--package-root",
				str(pkg_root),
				"--allow-unsigned-from",
				str(pkg_root),
				"--dep",
				"std.evil@0.0.0",
				"--dep",
				"lang.evil@0.0.0",
				"--dep",
				"drift.evil@0.0.0",
				str(tmp_path / "main.drift"),
				"--emit-ir",
				str(tmp_path / "out.ll"),
			],
			capsys,
		)
		assert rc != 0
		diags = payload.get("diagnostics", [])
		assert any("reserved module namespace" in d.get("message", "") for d in diags)


def test_driftc_reserved_namespace_requires_core_trust_keys(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	home = tmp_path / "home"
	monkeypatch.setenv("HOME", str(home))
	user_trust_path = home / ".config" / "drift" / "trust.json"
	user_trust_path.parent.mkdir(parents=True, exist_ok=True)
	core_trust_path = tmp_path / "core_trust.json"

	core_priv = Ed25519PrivateKey.generate()
	core_pub_raw = _public_key_bytes(core_priv.public_key())
	core_kid = compute_ed25519_kid(core_pub_raw)
	core_pub_b64 = _b64(core_pub_raw)

	user_priv = Ed25519PrivateKey.generate()
	user_pub_raw = _public_key_bytes(user_priv.public_key())
	user_kid = compute_ed25519_kid(user_pub_raw)
	user_pub_b64 = _b64(user_pub_raw)

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=core_kid, pub_b64=core_pub_b64, ns="std.*")
	_write_trust_store(user_trust_path, kid=user_kid, pub_b64=user_pub_b64, ns="std.*")
	_write_trust_store(core_trust_path, kid=core_kid, pub_b64=core_pub_b64, ns="std.*")

	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	module_id = "std.evil"
	module_dir = pkg_root.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "evil.drift",
		f"""
module {module_id};

export {{ add }};

pub fn add(a: Int, b: Int) nothrow -> Int {{
	return a + b;
}}
""".lstrip(),
	)
	pkg_path = pkg_root / "evil.dmp"
	assert (
		driftc_main(
			[
				"--dev",
				"-M",
				str(pkg_root),
				str(module_dir / "evil.drift"),
				*_emit_pkg_args(module_id),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
	)

	pkg_bytes = pkg_path.read_bytes()
	user_sig = user_priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=user_kid, sig_raw=user_sig)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkg_root),
			"--allow-unsigned-from",
			str(pkg_root),
			"--dep",
			"std.evil@0.0.0",
			"--dev",
			"--dev-core-trust-store",
			str(core_trust_path),
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("package signatures are not trusted for module" in d.get("message", "") for d in diags)

	core_sig = core_priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=core_kid, sig_raw=core_sig)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkg_root),
			"--allow-unsigned-from",
			str(pkg_root),
			"--dep",
			"std.evil@0.0.0",
			"--dev",
			"--dev-core-trust-store",
			str(core_trust_path),
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc == 0
	assert payload.get("diagnostics") == []


def test_driftc_dev_core_trust_requires_dev_flag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	core_trust_path = tmp_path / "core_trust.json"
	core_priv = Ed25519PrivateKey.generate()
	core_pub_raw = _public_key_bytes(core_priv.public_key())
	core_kid = compute_ed25519_kid(core_pub_raw)
	core_pub_b64 = _b64(core_pub_raw)
	_write_trust_store(core_trust_path, kid=core_kid, pub_b64=core_pub_b64, ns="std.*")

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dev-core-trust-store",
			str(core_trust_path),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("--dev-core-trust-store requires --dev" in d.get("message", "") for d in diags)


def test_package_embedding_includes_only_call_graph_closure(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}

fn unused() nothrow -> Int {
	return 999;
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	ir_path = tmp_path / "out.ll"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				"--package-root",
				str(tmp_path),
				"--allow-unsigned-from",
				str(tmp_path),
				"--dep",
				"lib@0.0.0",
				str(tmp_path / "main.drift"),
				"--emit-ir",
				str(ir_path),
			]
		)
		== 0
	)
	ir = ir_path.read_text(encoding="utf-8")
	assert "lib::unused" not in ir
	# Option B: package functions compiled from HIR use direct calling
	# convention — no wrapper/impl split, no FnResult boundary wrapping.
	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	assert f"define {word_ty} @\"lib::add\"" in ir
	assert "lib::add__impl" not in ir
	assert "lib::unused" not in ir


def test_discover_package_files_accepts_package_file_path(tmp_path: Path) -> None:
	pkg = tmp_path / "one.dmp"
	pkg.write_bytes(b"")
	assert discover_package_files([pkg]) == [pkg]


def test_discover_package_files_follows_symlinked_dirs(tmp_path: Path) -> None:
	"""
	Regression: package discovery must follow symlinked directories.

	drift deploy constructs staged/build package roots using symlinks.
	Path.rglob() does not follow symlinks, so packages reachable only
	through symlinked directories were invisible to the compiler.
	"""
	# Real package location (e.g. ~/opt/drift/libs/web-jwt/0.1.0/).
	real_dir = tmp_path / "real" / "web-jwt" / "0.1.0"
	real_dir.mkdir(parents=True)
	dmp = real_dir / "web-jwt.dmp"
	dmp.write_bytes(b"fake-dmp")

	# Staged package root with symlink (as drift deploy creates).
	staged_root = tmp_path / "staged"
	staged_root.mkdir()
	(staged_root / "web-jwt").symlink_to(tmp_path / "real" / "web-jwt")

	# Discovery must find the .dmp through the symlink.
	found = discover_package_files([staged_root])
	assert len(found) == 1, f"expected 1 package, found {len(found)}: {found}"
	assert found[0].name == "web-jwt.dmp"


def test_package_struct_from_submodule_roundtrips(tmp_path: Path) -> None:
	"""
	Regression: struct types declared in sub-modules must survive package
	serialization/deserialization. If the struct_schema references a type
	but the STRUCT TypeDef is missing from the package type table, the
	consumer fails with 'missing STRUCT TypeDef in package type table'.
	"""
	# Build library package with struct in a sub-module.
	lib_root = tmp_path / "lib_src"
	# --- Package A: errors library with struct in sub-module ---
	err_root = tmp_path / "err_src"
	_write_file(
		err_root / "web" / "jwt" / "errors" / "errors.drift",
		"""
module web.jwt.errors;

export { JwtConfigError };

pub struct JwtConfigError {
	pub code: Int,
}
""".lstrip(),
	)
	err_pkg = tmp_path / "pkg" / "web-jwt-errors" / "0.1.0" / "web-jwt-errors.dmp"
	err_pkg.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"-M", str(err_root),
		str(err_root / "web" / "jwt" / "errors" / "errors.drift"),
		"--package-id", "web-jwt-errors",
		"--package-version", "0.1.0",
		"--package-target", "test",
		"--emit-package", str(err_pkg),
	])
	assert rc == 0, "errors package build should succeed"

	# --- Package B: jwt library that depends on errors package ---
	lib_root = tmp_path / "lib_src"
	_write_file(
		lib_root / "web" / "jwt" / "jwt.drift",
		"""
module web.jwt;

import web.jwt.errors as errors;

export { make_error };

pub fn make_error() nothrow -> errors.JwtConfigError {
	return errors.JwtConfigError(code = 42);
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkg"
	jwt_pkg = pkg_root / "web-jwt" / "0.1.0" / "web-jwt.dmp"
	jwt_pkg.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"-M", str(lib_root),
		str(lib_root / "web" / "jwt" / "jwt.drift"),
		"--package-root", str(pkg_root),
		"--allow-unsigned-from", str(pkg_root),
		"--dep", "web-jwt-errors@0.1.0",
		"--package-id", "web-jwt",
		"--package-version", "0.1.0",
		"--package-target", "test",
		"--emit-package", str(jwt_pkg),
	])
	assert rc == 0, "jwt package build (consuming errors pkg) should succeed"

	# --- Consumer: imports web.jwt (which re-exports errors struct) ---
	consumer_root = tmp_path / "consumer_src"
	_write_file(
		consumer_root / "main.drift",
		"""
module main;

import web.jwt as jwt;

fn main() nothrow -> Int {
	val e = jwt.make_error();
	return e.code;
}
""".lstrip(),
	)
	rc = driftc_main([
		"-M", str(consumer_root),
		str(consumer_root / "main.drift"),
		"--package-root", str(pkg_root),
		"--allow-unsigned-from", str(pkg_root),
		"--dep", "web-jwt@0.1.0",
		"--dep", "web-jwt-errors@0.1.0",
		"--test-build-only",
	])
	assert rc == 0, (
		"consumer build should succeed — struct types from dependency "
		"sub-modules must be present in package type table"
	)


def test_driftc_rejects_unsigned_package_outside_allowlist(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	pkg = pkg_root / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)

	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkg_root),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0


def test_driftc_missing_explicit_trust_store_is_reported_as_diagnostic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn main() nothrow -> Int{
	return 0;
}
""".lstrip(),
	)
	missing = tmp_path / "nope-trust.json"
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--trust-store",
			str(missing),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
			"--json",
		]
	)
	assert rc != 0
	out = capsys.readouterr().out
	obj = json.loads(out)
	assert obj["exit_code"] == 1
	assert obj["diagnostics"][0]["phase"] == "package"
	assert "trust store not found" in obj["diagnostics"][0]["message"]


def test_driftc_accepts_signed_package_when_required(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	signed = _make_signed_package(tmp_path)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)

	ir_path = tmp_path / "out.ll"
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(signed.trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(ir_path),
		],
		capsys,
	)
	assert rc == 0
	assert payload.get("exit_code") == 0
	assert payload.get("diagnostics") == []


def test_driftc_rejects_signature_missing_module_in_strict_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	pkg_path = _emit_lib_pkg(tmp_path, module_id="acme.badmod")
	pkg = load_package_v0(pkg_path)
	mod = pkg.modules_by_id["acme.badmod"]

	iface_obj = dict(mod.interface)
	payload_obj = dict(mod.payload)

	def _strip_sig_module(obj: dict) -> dict[str, dict]:
		sigs = dict(obj.get("signatures") or {})
		add_key = "acme.badmod::add"
		sd = dict(sigs.get(add_key) or {})
		sd.pop("module", None)
		sd["name"] = "add"
		return {"add": sd}

	payload_obj["signatures"] = _strip_sig_module(payload_obj)
	iface_obj["signatures"] = {}

	iface_exports = dict(iface_obj.get("exports") or {})
	iface_exports["values"] = []
	iface_obj["exports"] = iface_exports
	payload_exports = dict(payload_obj.get("exports") or {})
	payload_exports["values"] = []
	payload_obj["exports"] = payload_exports

	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)
	write_dmir_pkg_v0(
		pkg_path,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": "acme.badmod",
			"package_version": "0.0.0",
			"target": "test-target",
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"abi_fingerprint": _abi_fingerprint("test-target", word_bits=host_word_bits()),
			"modules": [
				{
					"module_id": "acme.badmod",
					"exports": iface_obj.get("exports", {}),
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			],
			"blobs": {
				f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
				f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
			},
		},
		blobs={iface_sha: iface_bytes, payload_sha: payload_bytes},
		blob_types={iface_sha: 2, payload_sha: 1},
		blob_names={iface_sha: "iface:acme.badmod", payload_sha: "dmir:acme.badmod"},
	)

	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=kid, sig_raw=sig_raw)

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=kid, pub_b64=pub_b64)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.badmod as badmod;

fn main() nothrow -> Int{
	return 0;
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.badmod@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "missing module" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_missing_sidecar_when_signatures_required(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	# Emit an unsigned package but do not write a `.sig` file.
	pkg_path = _emit_lib_pkg(tmp_path)
	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid="ed25519:dummy", pub_b64=_b64(b"\0" * 32))

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "missing signature sidecar" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_malformed_signature_sidecar_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	signed = _make_signed_package(tmp_path)
	signed.pkg_path.with_suffix(".sig").write_text("{", encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(signed.trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "invalid JSON" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_sidecar_package_sha_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	signed = _make_signed_package(tmp_path)
	pkg_bytes = signed.pkg_path.read_bytes()
	bad_sha = "0" * 64
	_write_sig_sidecar(signed.pkg_path, pkg_bytes=pkg_bytes, kid=signed.kid, sig_raw=b"\0" * 64, package_sha256_override=bad_sha)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(signed.trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "package_sha256 mismatch" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_manifest_tamper_when_signatures_required(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	signed = _make_signed_package(tmp_path)

	def patch_manifest(old: bytes) -> bytes:
		needle = b"\"package_version\":\"0.0.0\""
		if needle not in old:
			raise ValueError("expected package_version in manifest")
		return old.replace(needle, b"\"package_version\":\"0.0.1\"")

	_patch_pkg_manifest_bytes_same_len(signed.pkg_path, patch_manifest)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(signed.trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "package_sha256 mismatch" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_sidecar_invalid_base64(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	signed = _make_signed_package(tmp_path)
	sidecar = signed.pkg_path.with_suffix(".sig")
	obj = json.loads(sidecar.read_text(encoding="utf-8"))
	obj["signatures"][0]["sig"] = "!!!"
	sidecar.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(signed.trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "invalid base64" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_sidecar_wrong_sig_length(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	signed = _make_signed_package(tmp_path)
	sidecar = signed.pkg_path.with_suffix(".sig")
	obj = json.loads(sidecar.read_text(encoding="utf-8"))
	obj["signatures"][0]["sig"] = _b64(b"\0" * 63)
	sidecar.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(signed.trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "signature must be 64 bytes" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_signed_package_when_kid_revoked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)
	pkg_path = _emit_lib_pkg(tmp_path)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=kid, sig_raw=sig_raw)

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=kid, pub_b64=pub_b64, revoked=[kid])

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	msg = str(payload["diagnostics"][0]["message"])
	assert ("no valid signatures" in msg) or ("revoked" in msg.lower())


def test_driftc_accepts_if_any_signature_entry_is_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	pkg_path = _emit_lib_pkg(tmp_path)
	pkg_bytes = pkg_path.read_bytes()

	# Two keys: first signature is invalid, second is valid. Both are trusted.
	priv1 = Ed25519PrivateKey.generate()
	pub1 = _public_key_bytes(priv1.public_key())
	kid1 = compute_ed25519_kid(pub1)
	priv2 = Ed25519PrivateKey.generate()
	pub2 = _public_key_bytes(priv2.public_key())
	kid2 = compute_ed25519_kid(pub2)

	invalid_sig = b"\0" * 64
	valid_sig = priv2.sign(pkg_bytes)

	# Sidecar includes both signatures (no pubkey needed; trust store provides it).
	_write_sig_sidecar(
		pkg_path,
		pkg_bytes=pkg_bytes,
		kid=kid1,
		sig_raw=invalid_sig,
		extra_entries=[{"algo": "ed25519", "kid": kid2, "sig": _b64(valid_sig)}],
	)

	trust_path = tmp_path / "trust.json"
	obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {
			kid1: {"algo": "ed25519", "pubkey": _b64(pub1)},
			kid2: {"algo": "ed25519", "pubkey": _b64(pub2)},
		},
		"namespaces": {
			"acme.*": [kid1, kid2],
		},
		"revoked": [],
	}
	_write_file(trust_path, json.dumps(obj, separators=(",", ":"), sort_keys=True))

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc == 0
	assert payload["exit_code"] == 0
	assert payload["diagnostics"] == []


def test_driftc_rejects_valid_signature_when_kid_not_trusted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	# Signed package exists, but the trust store does not contain the kid/pubkey.
	# driftc must not TOFU from sidecar pubkey bytes.
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	pkg_path = _emit_lib_pkg(tmp_path)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=kid, sig_raw=sig_raw, pub_b64=pub_b64)

	# Trust store does not contain the key (keys table empty), even though it
	# claims the namespace would allow it.
	trust_path = tmp_path / "trust.json"
	obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {},
		"namespaces": {"acme.*": [kid]},
		"revoked": [],
	}
	_write_file(trust_path, json.dumps(obj, separators=(",", ":"), sort_keys=True))

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "no valid signatures" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_valid_signature_when_namespace_disallows_kid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	# Signed package exists and kid is in trust store, but namespace allowlist
	# does not include the kid.
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	other_priv = Ed25519PrivateKey.generate()
	other_pub = _public_key_bytes(other_priv.public_key())
	other_kid = compute_ed25519_kid(other_pub)

	pkg_path = _emit_lib_pkg(tmp_path)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=kid, sig_raw=sig_raw)

	trust_path = tmp_path / "trust.json"
	obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {
			kid: {"algo": "ed25519", "pubkey": pub_b64},
			other_kid: {"algo": "ed25519", "pubkey": _b64(other_pub)},
		},
		# Allow only the other key for the namespace.
		"namespaces": {"acme.*": [other_kid]},
		"revoked": [],
	}
	_write_file(trust_path, json.dumps(obj, separators=(",", ":"), sort_keys=True))

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "not trusted for module" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_sidecar_wrong_pubkey_length(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	signed = _make_signed_package(tmp_path)
	sidecar = signed.pkg_path.with_suffix(".sig")
	obj = json.loads(sidecar.read_text(encoding="utf-8"))
	obj["signatures"][0]["pubkey"] = _b64(b"\0" * 31)
	sidecar.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(signed.trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "pubkey must be 32 bytes" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_sidecar_invalid_pubkey_base64(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	signed = _make_signed_package(tmp_path)
	sidecar = signed.pkg_path.with_suffix(".sig")
	obj = json.loads(sidecar.read_text(encoding="utf-8"))
	obj["signatures"][0]["pubkey"] = "!!!"
	sidecar.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(signed.trust_path),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "invalid base64" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_unsigned_package_without_manifest_marker(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	pkg = pkg_root / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	# Remove the "unsigned": true marker without changing manifest length.
	def patch_manifest(old: bytes) -> bytes:
		needle = b"\"unsigned\":true"
		if needle not in old:
			raise ValueError("expected unsigned marker in manifest")
		return old.replace(needle, b"\"unsigned\":null")

	_patch_pkg_manifest_bytes_same_len(pkg, patch_manifest)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkg_root),
			"--allow-unsigned-from",
			str(pkg_root),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0


def test_driftc_can_consume_package_exporting_generic_variant_optional(tmp_path: Path) -> None:
	# Package exports a generic variant and a function returning an instantiation.
	_emit_optional_variant_pkg(tmp_path)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.opt as opt;

fn main() nothrow -> Int{
	try {
		val x: opt.Maybe<Int> = opt.foo();
		val y = match x {
			Some(v) => { v + 1 },
			None => { 0 },
		};
		return y;
	} catch {
		return 0;
	}
}
""".lstrip(),
	)

	ir_path = tmp_path / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.opt@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(ir_path),
		]
	)
	assert rc == 0


def test_driftc_rejects_variant_schema_collision_between_source_and_package(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	# Package defines `variant Maybe<T>` in module `acme.opt`, while source defines
	# a different `variant Maybe<T>` in module `main`. With module-scoped nominal
	# type identity, these are distinct and must not collide.
	_emit_optional_variant_pkg(tmp_path)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

// Collides by name with the package's `Maybe<T>` schema.
variant Maybe<T> {
	Some(value: T),
	None,
	Extra
}

fn main() nothrow -> Int{
	return 0;
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.opt@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc == 0
	assert payload["exit_code"] == 0
	assert payload["diagnostics"] == []


def test_driftc_rejects_variant_schema_collision_between_packages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	# The same module id must not be provided by multiple packages.
	_emit_optional_variant_pkg(tmp_path, module_id="acme.opt", pkg_name="opt_a.dmp", package_id="test.opt_a")
	_emit_optional_variant_pkg(
		tmp_path, module_id="acme.opt", extra_arm=True, pkg_name="opt_b.dmp", package_id="test.opt_b"
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn main() nothrow -> Int{
	return 0;
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"test.opt_a@0.0.0",
			"--dep",
			"test.opt_b@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "provided by multiple packages" in payload["diagnostics"][0]["message"]
	assert "acme.opt" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_import_of_non_exported_value_from_package(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	_emit_hidden_fn_pkg(tmp_path)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.hidden as hidden;

fn main() nothrow -> Int{
	return hidden.hidden();
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.hidden@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "parser"
	assert "does not export symbol 'hidden'" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_import_of_pub_but_not_exported_value_from_package(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	_emit_pub_hidden_fn_pkg(tmp_path)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.hiddenpub as hidden;

fn main() nothrow -> Int{
	return hidden.hidden();
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.hiddenpub@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "parser"
	assert "does not export symbol 'hidden'" in payload["diagnostics"][0]["message"]


def test_driftc_can_consume_package_with_export_star(tmp_path: Path) -> None:
	pkg = _emit_star_reexport_pkg(tmp_path)
	pkg_root = pkg.parent

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.api as api;

fn main() nothrow -> Int{
	return try api.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				"--package-root",
				str(pkg_root),
				"--allow-unsigned-from",
				str(pkg_root),
				"--dep",
				"acme@0.0.0",
				str(tmp_path / "main.drift"),
				"--emit-ir",
				str(tmp_path / "out.ll"),
			]
		)
		== 0
	)


def test_driftc_allows_import_of_exported_const_from_package(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	_emit_const_pkg(tmp_path)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.consts as consts;

fn main() nothrow -> Int{
	return consts.ANSWER;
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.consts@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc == 0
	assert payload["exit_code"] == 0
	assert payload["diagnostics"] == []


def test_driftc_allows_import_of_exported_type_but_rejects_non_exported_value_from_package(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	_emit_point_type_only_pkg(tmp_path)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.point as point;

fn main() nothrow -> Int{
	val p: point.Point = point.make();
	return p.x;
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.point@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "parser"
	assert "does not export symbol 'make'" in payload["diagnostics"][0]["message"]


def test_driftc_allows_two_modules_with_same_struct_name_from_packages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	_emit_point_pkg(tmp_path, module_id="a.geom")
	_emit_point_pkg(tmp_path, module_id="b.geom")

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import a.geom as ag;
import b.geom as bg;

fn main() nothrow -> Int{
	try {
		val p1: ag.Point = ag.make();
		val p2: bg.Point = bg.make();
		return p1.x + p2.x;
	} catch {
		return 0;
	}
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"a.geom@0.0.0",
			"--dep",
			"b.geom@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc == 0
	assert payload["exit_code"] == 0


def test_driftc_rejects_package_with_exported_value_missing_entrypoint_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""
	ABI-boundary invariant: exported values must correspond to entrypoint signatures.

	This constructs a malformed package where the interface exports `add`, but the
	payload signature for `add` is not marked as an exported entrypoint.
	"""
	pkg_path = _emit_lib_pkg(tmp_path, module_id="acme.badiface")
	pkg = load_package_v0(pkg_path)
	mod = pkg.modules_by_id["acme.badiface"]

	iface_obj = dict(mod.interface)
	payload_obj = dict(mod.payload)

	sigs = dict(payload_obj.get("signatures") or {})
	add_key = "acme.badiface::add"
	sd = dict(sigs.get(add_key) or {})
	sd["is_exported_entrypoint"] = False
	sigs[add_key] = sd
	payload_obj["signatures"] = sigs
	# Keep interface and payload signatures consistent; the interface table is
	# strict and must match the payload.
	iface_sigs = dict(iface_obj.get("signatures") or {})
	iface_sd = dict(iface_sigs.get(add_key) or {})
	iface_sd["is_exported_entrypoint"] = False
	iface_sigs[add_key] = iface_sd
	iface_obj["signatures"] = iface_sigs

	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)
	out_pkg = pkg_path
	write_dmir_pkg_v0(
		out_pkg,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": "acme.badiface",
			"package_version": "0.0.0",
			"target": "test-target",
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"modules": [
				{
					"module_id": "acme.badiface",
					"exports": iface_obj.get("exports", {}),
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			],
			"blobs": {
				f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
				f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
			},
		},
		blobs={iface_sha: iface_bytes, payload_sha: payload_bytes},
		blob_types={iface_sha: 2, payload_sha: 1},
		blob_names={iface_sha: "iface:acme.badiface", payload_sha: "dmir:acme.badiface"},
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.badiface as badiface;

fn main() nothrow -> Int{
	return try badiface.add(40, 2) catch { 0 };
}
""".lstrip(),
	)

	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.badiface@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "exported value 'add' is missing exported entrypoint signature metadata" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_package_with_exported_value_missing_interface_signature(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""
	Interface table tightening: exported values must have interface signature entries.
	"""
	pkg_path = _emit_lib_pkg(tmp_path, module_id="acme.badiface2")
	pkg = load_package_v0(pkg_path)
	mod = pkg.modules_by_id["acme.badiface2"]

	iface_obj = dict(mod.interface)
	payload_obj = dict(mod.payload)

	add_key = "acme.badiface2::add"
	iface_sigs = dict(iface_obj.get("signatures") or {})
	iface_sigs.pop(add_key, None)
	iface_obj["signatures"] = iface_sigs

	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)
	out_pkg = pkg_path
	write_dmir_pkg_v0(
		out_pkg,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": "acme.badiface2",
			"package_version": "0.0.0",
			"target": "test-target",
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"modules": [
				{
					"module_id": "acme.badiface2",
					"exports": iface_obj.get("exports", {}),
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			],
			"blobs": {
				f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
				f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
			},
		},
		blobs={iface_sha: iface_bytes, payload_sha: payload_bytes},
		blob_types={iface_sha: 2, payload_sha: 1},
		blob_names={iface_sha: "iface:acme.badiface2", payload_sha: "dmir:acme.badiface2"},
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.badiface2 as badiface2;

fn main() nothrow -> Int{
	return try badiface2.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.badiface2@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "missing interface signature metadata" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_package_with_exports_mismatch_between_interface_and_payload(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""
	Interface table tightening: interface exports must match payload exports exactly.
	"""
	pkg_path = _emit_lib_pkg(tmp_path, module_id="acme.badiface3")
	pkg = load_package_v0(pkg_path)
	mod = pkg.modules_by_id["acme.badiface3"]

	iface_obj = dict(mod.interface)
	payload_obj = dict(mod.payload)

	# Remove an exported value from the interface, leaving payload unchanged.
	exports = dict(iface_obj.get("exports") or {})
	values = list(exports.get("values") or [])
	values = [v for v in values if v != "add"]
	exports["values"] = values
	iface_obj["exports"] = exports
	# Also keep interface signature table consistent with its exports.
	iface_sigs = dict(iface_obj.get("signatures") or {})
	iface_sigs.pop("acme.badiface3::add", None)
	iface_obj["signatures"] = iface_sigs

	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)
	out_pkg = pkg_path
	write_dmir_pkg_v0(
		out_pkg,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": "acme.badiface3",
			"package_version": "0.0.0",
			"target": "test-target",
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"modules": [
				{
					"module_id": "acme.badiface3",
					"exports": iface_obj.get("exports", {}),
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			],
			"blobs": {
				f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
				f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
			},
		},
		blobs={iface_sha: iface_bytes, payload_sha: payload_bytes},
		blob_types={iface_sha: 2, payload_sha: 1},
		blob_names={iface_sha: "iface:acme.badiface3", payload_sha: "dmir:acme.badiface3"},
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.badiface3 as badiface3;

fn main() nothrow -> Int{
	return try badiface3.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.badiface3@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "interface exports do not match payload exports" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_package_with_exported_exception_missing_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""
Interface completeness: exported exceptions must have interface schema entries.
	"""
	pkg_path = _emit_exception_pkg(tmp_path, module_id="acme.badexc")
	pkg = load_package_v0(pkg_path)
	mod = pkg.modules_by_id["acme.badexc"]

	iface_obj = dict(mod.interface)
	payload_obj = dict(mod.payload)

	iface_exc = dict(iface_obj.get("exception_schemas") or {})
	# Remove the exported exception schema entry.
	iface_exc.pop("acme.badexc:Boom", None)
	iface_obj["exception_schemas"] = iface_exc

	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)
	write_dmir_pkg_v0(
		pkg_path,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": "acme.badexc",
			"package_version": "0.0.0",
			"target": "test-target",
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"modules": [
				{
					"module_id": "acme.badexc",
					"exports": iface_obj.get("exports", {}),
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			],
			"blobs": {
				f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
				f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
			},
		},
		blobs={iface_sha: iface_bytes, payload_sha: payload_bytes},
		blob_types={iface_sha: 2, payload_sha: 1},
		blob_names={iface_sha: "iface:acme.badexc", payload_sha: "dmir:acme.badexc"},
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.badexc as badexc;

fn main() nothrow -> Int{
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.badexc@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "missing interface schema" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_package_with_exported_variant_missing_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""
Interface completeness: exported variants must have interface schema entries.
	"""
	pkg_path = _emit_optional_variant_pkg(tmp_path, module_id="acme.badvar")
	pkg = load_package_v0(pkg_path)
	mod = pkg.modules_by_id["acme.badvar"]

	iface_obj = dict(mod.interface)
	payload_obj = dict(mod.payload)

	iface_var = dict(iface_obj.get("variant_schemas") or {})
	iface_var.pop("Maybe", None)
	iface_obj["variant_schemas"] = iface_var

	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)
	write_dmir_pkg_v0(
		pkg_path,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": "acme.badvar",
			"package_version": "0.0.0",
			"target": "test-target",
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"modules": [
				{
					"module_id": "acme.badvar",
					"exports": iface_obj.get("exports", {}),
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			],
			"blobs": {
				f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
				f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
			},
		},
		blobs={iface_sha: iface_bytes, payload_sha: payload_bytes},
		blob_types={iface_sha: 2, payload_sha: 1},
		blob_names={iface_sha: "iface:acme.badvar", payload_sha: "dmir:acme.badvar"},
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.badvar as badvar;

fn main() nothrow -> Int{
	val o: badvar.Maybe<Int> = None;
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"acme.badvar@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "missing interface schema" in payload["diagnostics"][0]["message"]


def test_driftc_rejects_package_exporting_method_value(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""
MVP guardrail: exported methods are forbidden.
	"""
	_write_file(
		tmp_path / "m" / "lib.drift",
		"""
module m;

export { Point };

pub struct Point { pub x: Int }

pub implement Point {
	fn move_by(self: &mut Point, dx: Int) -> Void {
		self->x += dx;
	}
}

fn dummy() nothrow -> Int { return 0; }
""".lstrip(),
	)
	pkg_path = tmp_path / "m.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "m" / "lib.drift"),
				*_emit_pkg_args("m"),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	pkg = load_package_v0(pkg_path)
	mod = pkg.modules_by_id["m"]
	iface_obj = dict(mod.interface)
	payload_obj = dict(mod.payload)

	# Identify the method symbol key in the payload signatures.
	payload_sigs = dict(payload_obj.get("signatures") or {})
	method_sym = None
	for k, sd in payload_sigs.items():
		if isinstance(sd, dict) and sd.get("is_method") and "move_by" in k:
			method_sym = str(k)
			break
	assert method_sym is not None
	local_name = method_sym.split("::", 1)[1]

	# Malform the package: export the method as a value.
	exports = dict(payload_obj.get("exports") or {})
	values = list(exports.get("values") or [])
	if local_name not in values:
		values.append(local_name)
	exports["values"] = values
	payload_obj["exports"] = exports

	# Mark the method signature as an exported entrypoint and mirror it into the
	# interface signature table so all other checks pass.
	sd = dict(payload_sigs[method_sym])
	sd["is_exported_entrypoint"] = True
	payload_sigs[method_sym] = sd
	payload_obj["signatures"] = payload_sigs

	iface_exports = dict(iface_obj.get("exports") or {})
	iface_values = list(iface_exports.get("values") or [])
	if local_name not in iface_values:
		iface_values.append(local_name)
	iface_exports["values"] = iface_values
	iface_obj["exports"] = iface_exports

	iface_sigs = dict(iface_obj.get("signatures") or {})
	iface_sigs[method_sym] = sd
	iface_obj["signatures"] = iface_sigs

	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)
	write_dmir_pkg_v0(
		pkg_path,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": "m",
			"package_version": "0.0.0",
			"target": "test-target",
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"modules": [
				{
					"module_id": "m",
					"exports": iface_obj.get("exports", {}),
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			],
			"blobs": {
				f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
				f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
			},
		},
		blobs={iface_sha: iface_bytes, payload_sha: payload_bytes},
		blob_types={iface_sha: 2, payload_sha: 1},
		blob_names={iface_sha: "iface:m", payload_sha: "dmir:m"},
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn main() nothrow -> Int{ return 0 }
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"m@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	assert payload["exit_code"] == 1
	assert payload["diagnostics"][0]["phase"] == "package"
	assert "must not be a method" in payload["diagnostics"][0]["message"]

def test_driftc_can_consume_package_with_extern_c_declarations(tmp_path: Path) -> None:
	"""Package containing extern C declarations must not crash consumer codegen."""
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { wrapper };

extern "C" fn abs(x: Int32) nothrow -> Int32;

pub fn wrapper(x: Int32) nothrow -> Int32 {
	return unsafe { abs(x) };
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				"--allow-unsafe",
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int {
	val _r = lib.wrapper(cast<Int32>(42));
	return 0;
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0
	# Extern C declarations must use bare C symbol names, not module-qualified.
	ir_text = (tmp_path / "out.ll").read_text()
	assert "declare i32 @abs(i32)" in ir_text, (
		"expected bare @abs declare in consumer IR; "
		f"got mangled name instead:\n{[l for l in ir_text.splitlines() if 'abs' in l]}"
	)
	assert "lib::abs" not in ir_text, (
		"consumer IR must not contain module-qualified extern C symbol 'lib::abs'"
	)


def test_driftc_can_consume_package_exporting_int32_uint32(tmp_path: Path) -> None:
	"""Package exports functions using Int32/Uint32 scalars; consumer imports and calls them."""
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { identity32, identityu32 };

pub fn identity32(a: Int32) nothrow -> Int32 {
	return a;
}

pub fn identityu32(a: Uint32) nothrow -> Uint32 {
	return a;
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int {
	val r = lib.identity32(cast<Int32>(37));
	val u = lib.identityu32(cast<Uint32>(13));
	return cast<Int>(r) + cast<Int>(u);
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0


def test_driftc_can_consume_package_exporting_uint64_byte(tmp_path: Path) -> None:
	"""Package exports functions using Uint64/Byte scalars; consumer imports and calls them."""
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { identity_u64, identity_byte };

pub fn identity_u64(a: Uint64) nothrow -> Uint64 {
	return a;
}

pub fn identity_byte(a: Byte) nothrow -> Byte {
	return a;
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int {
	val u = lib.identity_u64(cast<Uint64>(42));
	val b = lib.identity_byte(cast<Byte>(7));
	return cast<Int>(u) + cast<Int>(b);
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0


def test_driftc_require_signatures_rejects_unsigned_packages(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	pkg = pkg_root / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	return try lib.add(40, 2) catch { 0 };
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkg_root),
			"--allow-unsigned-from",
			str(pkg_root),
			"--dep",
			"lib@0.0.0",
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0


def test_driftc_multi_package_impl_id_no_collision(tmp_path: Path) -> None:
	"""Consuming two packages that each have implement blocks must not crash.

	Regression: package-local impl_ids are sequential-from-zero within each
	package.  When two packages are loaded, their impl_ids can collide in the
	global id_registry if the consumer passes them through as preferred ids.
	The fix: let the registry assign fresh global ids instead of forcing
	package-local values.
	"""
	# --- Package A: struct + implement ---
	_write_file(
		tmp_path / "pkg_a" / "lib.drift",
		"""
module pkg_a.lib;

export { make_a };

struct FooA {
    x: Int;
}

implement FooA {
    fn get_x(self: &FooA) nothrow -> Int {
        return self.x;
    }
}

pub fn make_a() nothrow -> Int {
    val f = FooA(x = 1);
    return f.get_x();
}
""".lstrip(),
	)
	pkg_a = tmp_path / "pkgs" / "pkg_a.dmp"
	pkg_a.parent.mkdir(parents=True, exist_ok=True)
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "pkg_a" / "lib.drift"),
				*_emit_pkg_args("pkg-a"),
				"--emit-package",
				str(pkg_a),
			]
		)
		== 0
	)

	# --- Package B: struct + implement ---
	_write_file(
		tmp_path / "pkg_b" / "lib.drift",
		"""
module pkg_b.lib;

export { make_b };

struct FooB {
    y: Int;
}

implement FooB {
    fn get_y(self: &FooB) nothrow -> Int {
        return self.y;
    }
}

pub fn make_b() nothrow -> Int {
    val f = FooB(y = 2);
    return f.get_y();
}
""".lstrip(),
	)
	pkg_b = tmp_path / "pkgs" / "pkg_b.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "pkg_b" / "lib.drift"),
				*_emit_pkg_args("pkg-b"),
				"--emit-package",
				str(pkg_b),
			]
		)
		== 0
	)

	# --- Consumer importing both packages ---
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import pkg_a.lib as a;
import pkg_b.lib as b;

fn main() nothrow -> Int {
    return a.make_a() + b.make_b();
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path / "pkgs"),
			"--allow-unsigned-from",
			str(tmp_path / "pkgs"),
			"--dep",
			"pkg-a@0.0.0",
			"--dep",
			"pkg-b@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0, "multi-package consumption with implement blocks must not crash"


def test_driftc_package_rawptr_field_with_destructible(tmp_path: Path) -> None:
	"""Package struct with RawPtr<T> field + Destructible impl must not crash consumer.

	Regression: _eval_generic_type_expr handled Ptr (internal name from std.mem)
	but not RawPtr (user-facing alias).  When the struct schema's GenericTypeExpr
	stores "RawPtr", the field type degrades to Unknown, crashing codegen when
	the Destructible::destroy method body references the struct.
	"""
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

import std.core as core;

export { Wrapper };

pub struct Wrapper {
    pub handle: RawPtr<Byte>;
}

implement core.Destructible for Wrapper {
    pub fn destroy(var self: Wrapper) nothrow -> Void {}
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				"--allow-unsafe",
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import std.core as core;
import lib as lib;

fn main() nothrow -> Int {
    return 0;
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0, "RawPtr<T> field + Destructible impl must not crash package consumer"


def test_driftc_package_stdlib_method_call_wrapper(tmp_path: Path) -> None:
	"""Package body calling a stdlib method must link in the consumer.

	Regression: package code references __wrap_method stubs for stdlib methods
	(e.g., String::byte_length).  The consumer must synthesize those wrapper
	stubs AND include the underlying stdlib method body in the IR.
	"""
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

import std.core as core;

export { string_len };

pub fn string_len(s: String) nothrow -> Int {
    return s.byte_length();
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import std.core as core;
import lib as lib;

fn main() nothrow -> Int {
    return lib.string_len("hello");
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0, "package calling stdlib method must link in consumer"


def test_driftc_package_destroy_body_stdlib_method_wrapper(tmp_path: Path) -> None:
	"""Destroy body calling stdlib method must link in the consumer.

	Regression: wrapper stubs referenced from a Destructible::destroy body
	are seeded AFTER the main BFS loop (via the destroy type graph).  A
	final wrapper-discovery pass must scan post-BFS pkg_needed functions
	so their __wrap_method references are included.
	"""
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

import std.core as core;

export { Wrapper };

pub struct Wrapper {
    pub name: String;
}

implement core.Destructible for Wrapper {
    pub fn destroy(var self: Wrapper) nothrow -> Void {
        val _len = self.name.byte_length();
    }
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import std.core as core;
import lib as lib;

fn main() nothrow -> Int {
    return 0;
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0, "destroy body calling stdlib method must link in consumer"


def test_driftc_package_destroy_body_transitive_pkg_target(tmp_path: Path) -> None:
	"""Destroy body calling package helper that itself calls stdlib method.

	Regression: the final post-BFS reachability pass must compute transitive
	closure.  If a destroy body calls a package-internal helper, and that
	helper calls a stdlib method via a __wrap_method stub, both the helper
	AND the wrapper target must be pulled in transitively.
	"""
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

import std.core as core;

export { Wrapper };

pub struct Wrapper {
    pub name: String;
}

fn name_len(s: String) nothrow -> Int {
    return s.byte_length();
}

implement core.Destructible for Wrapper {
    pub fn destroy(var self: Wrapper) nothrow -> Void {
        val _len = name_len(self.name);
    }
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				*_emit_pkg_args("lib"),
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import std.core as core;
import lib as lib;

fn main() nothrow -> Int {
    return 0;
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			"--dep",
			"lib@0.0.0",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0, "destroy body transitive package target must link in consumer"


# ---------------------------------------------------------------------------
# Native dependency metadata tests (0.27.47-dev)
# ---------------------------------------------------------------------------


def test_native_deps_manifest_roundtrip(tmp_path: Path) -> None:
	"""Build a package with --native-link-lib, load it, verify native_deps in manifest."""
	pkg_path = tmp_path / "lib.dmp"
	module_dir = tmp_path / "acme" / "lib"
	_write_file(
		module_dir / "lib.drift",
		"module acme.lib;\n\nexport { add };\n\npub fn add(a: Int, b: Int) nothrow -> Int {\n\treturn a + b;\n}\n",
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			str(module_dir / "lib.drift"),
			*_emit_pkg_args("acme.lib"),
			"--native-link-lib",
			"ssl",
			"--native-link-lib",
			"crypto",
			"--emit-package",
			str(pkg_path),
		]
	)
	assert rc == 0, "emit-package with --native-link-lib should succeed"
	loaded = load_package_v0(pkg_path)
	assert len(loaded.native_deps) == 2
	assert loaded.native_deps[0].lib == "ssl"
	assert loaded.native_deps[1].lib == "crypto"
	# Verify manifest structure
	nd = loaded.manifest.get("native_deps")
	assert isinstance(nd, dict)
	assert nd["schema_version"] == 1
	assert len(nd["link_libs"]) == 2


def test_native_deps_absent_is_empty(tmp_path: Path) -> None:
	"""Load a package without native_deps — defaults to empty list."""
	pkg_path = _emit_lib_pkg(tmp_path)
	loaded = load_package_v0(pkg_path)
	assert loaded.native_deps == []


def test_package_deps_manifest_roundtrip(tmp_path: Path) -> None:
	"""Build a package with --package-dep, load it, verify package_deps in manifest."""
	pkg_path = tmp_path / "lib.dmp"
	module_dir = tmp_path / "acme" / "lib"
	_write_file(
		module_dir / "lib.drift",
		"module acme.lib;\n\nexport { add };\n\npub fn add(a: Int, b: Int) nothrow -> Int {\n\treturn a + b;\n}\n",
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			str(module_dir / "lib.drift"),
			*_emit_pkg_args("acme.lib"),
			"--package-dep",
			"net.tls=^0.3.0",
			"--package-dep",
			"acme.crypto=~1.0.0",
			"--emit-package",
			str(pkg_path),
		]
	)
	assert rc == 0, "emit-package with --package-dep should succeed"
	loaded = load_package_v0(pkg_path)
	assert len(loaded.package_deps) == 2
	assert loaded.package_deps[0].name == "net.tls"
	assert loaded.package_deps[0].version == "^0.3.0"
	assert loaded.package_deps[1].name == "acme.crypto"
	assert loaded.package_deps[1].version == "~1.0.0"
	# Verify manifest structure
	pd = loaded.manifest.get("package_deps")
	assert isinstance(pd, list)
	assert len(pd) == 2


# ── Consumer auto-link behavior tests ────────────────────────────────────────
# These test the link-time behavior when consuming packages that declare
# native_deps.  They require subprocess + clang (skipped if unavailable).

import shutil
import subprocess
import sys
import tempfile

_ROOT = Path(__file__).resolve().parents[3]
_STDLIB = _ROOT / "stdlib"
_CONSUMER_BUILD = Path("build/tests/pkg_consumer")


def _emit_lib_pkg_with_native_deps(
	tmp_path: Path,
	native_libs: list[str],
	*,
	module_id: str = "acme.lib",
) -> Path:
	"""Emit a .dmp package that declares native_deps with given link_libs."""
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"module {module_id};\n\nexport {{ add }};\n\npub fn add(a: Int, b: Int) nothrow -> Int {{\n\treturn a + b;\n}}\n",
	)
	pkg_path = tmp_path / "lib.dmp"
	args = [
		"-M",
		str(tmp_path),
		str(module_dir / "lib.drift"),
		*_emit_pkg_args(module_id),
		"--emit-package",
		str(pkg_path),
	]
	for lib in native_libs:
		args.extend(["--native-link-lib", lib])
	rc = driftc_main(args)
	assert rc == 0, f"emit-package with native deps should succeed (rc={rc})"
	return pkg_path


def _run_consumer_link(
	tmp_path: Path,
	pkg_dir: Path,
	*,
	extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
	"""
	Compile a consumer program that imports from a package, triggering the link path.

	Returns the CompletedProcess so callers can inspect stderr for the link command.
	The link may fail (e.g. missing native lib) — that's expected for some tests.
	"""
	clang = shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available")

	_CONSUMER_BUILD.mkdir(parents=True, exist_ok=True)
	work = Path(tempfile.mkdtemp(prefix="pkg-consumer-", dir=_CONSUMER_BUILD))

	_write_file(
		work / "main.drift",
		"module main;\n\nimport acme.lib as lib;\n\nfn main() nothrow -> Int {\n\treturn lib.add(1, 2);\n}\n",
	)

	bin_path = work / "a.out"
	cmd = [
		sys.executable,
		"-m", "lang.driftc",
		"--stdlib-root", str(_STDLIB),
		"--dev",
		"--package-root", str(pkg_dir),
		"--allow-unsigned-from", str(pkg_dir),
		"--dep",
		"acme.lib@0.0.0",
		"-o", str(bin_path),
		str(work / "main.drift"),
	]
	if extra_args:
		cmd.extend(extra_args)

	env = {"PYTHONPATH": ".", "PATH": subprocess.check_output(["bash", "-lc", "echo $PATH"], text=True).strip()}
	return subprocess.run(cmd, capture_output=True, text=True, cwd=str(_ROOT), env=env)


def _extract_link_line(stderr: str) -> str | None:
	"""Extract the '[driftc] link:' line from stderr."""
	for line in stderr.splitlines():
		if line.startswith("[driftc] link:"):
			return line
	return None


class TestConsumerAutoLink:
	"""Consumer-side tests for native_deps auto-link, opt-out, and diagnostics."""

	def test_auto_link_appends_native_libs(self, tmp_path: Path) -> None:
		"""Consumer auto-link appends -l<lib> from package native_deps to link command."""
		pkgs = tmp_path / "pkgs"
		pkgs.mkdir()
		_emit_lib_pkg_with_native_deps(pkgs, ["testdummylib_a", "testdummylib_b"])

		res = _run_consumer_link(tmp_path, pkgs)
		link_line = _extract_link_line(res.stderr)
		assert link_line is not None, f"expected [driftc] link: line in stderr:\n{res.stderr}"
		assert "-ltestdummylib_a" in link_line, f"expected -ltestdummylib_a in link line: {link_line}"
		assert "-ltestdummylib_b" in link_line, f"expected -ltestdummylib_b in link line: {link_line}"

	def test_no_package_native_deps_suppresses_auto_link(self, tmp_path: Path) -> None:
		"""--no-package-native-deps suppresses auto-link of package-declared native libs."""
		pkgs = tmp_path / "pkgs"
		pkgs.mkdir()
		_emit_lib_pkg_with_native_deps(pkgs, ["testdummylib_a", "testdummylib_b"])

		res = _run_consumer_link(tmp_path, pkgs, extra_args=["--no-package-native-deps"])
		link_line = _extract_link_line(res.stderr)
		assert link_line is not None, f"expected [driftc] link: line in stderr:\n{res.stderr}"
		assert "-ltestdummylib_a" not in link_line, f"did not expect -ltestdummylib_a in link line: {link_line}"
		assert "-ltestdummylib_b" not in link_line, f"did not expect -ltestdummylib_b in link line: {link_line}"

	def test_diagnostic_enrichment_on_link_failure(self, tmp_path: Path) -> None:
		"""Link failure with package-declared native lib produces diagnostic hint identifying the source package."""
		pkgs = tmp_path / "pkgs"
		pkgs.mkdir()
		_emit_lib_pkg_with_native_deps(pkgs, ["nonexistent_drift_test_lib_xyz"])

		res = _run_consumer_link(tmp_path, pkgs)
		# Link should fail because the library doesn't exist.
		assert res.returncode != 0, "expected link failure for nonexistent library"
		# The diagnostic hint should identify the package and library.
		assert "nonexistent_drift_test_lib_xyz" in res.stderr, f"expected library name in stderr:\n{res.stderr}"
		assert "acme.lib" in res.stderr, f"expected package id 'acme.lib' in diagnostic hint:\n{res.stderr}"


def test_package_deps_absent_is_empty(tmp_path: Path) -> None:
	"""Load a package without package_deps — defaults to empty list."""
	pkg_path = _emit_lib_pkg(tmp_path)
	loaded = load_package_v0(pkg_path)
	assert loaded.package_deps == []


def test_package_dep_bad_format_rejected(tmp_path: Path) -> None:
	"""--package-dep without = separator is rejected."""
	module_dir = tmp_path / "acme" / "lib"
	_write_file(
		module_dir / "lib.drift",
		"module acme.lib;\n\nexport { add };\n\npub fn add(a: Int, b: Int) nothrow -> Int {\n\treturn a + b;\n}\n",
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			str(module_dir / "lib.drift"),
			*_emit_pkg_args("acme.lib"),
			"--package-dep",
			"net.tls",
			"--emit-package",
			str(tmp_path / "lib.dmp"),
		]
	)
	assert rc == 1, "--package-dep without =VERSION should fail"


def test_native_deps_and_package_deps_combined(tmp_path: Path) -> None:
	"""Build a package with both native and package deps."""
	pkg_path = tmp_path / "lib.dmp"
	module_dir = tmp_path / "acme" / "lib"
	_write_file(
		module_dir / "lib.drift",
		"module acme.lib;\n\nexport { add };\n\npub fn add(a: Int, b: Int) nothrow -> Int {\n\treturn a + b;\n}\n",
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			str(module_dir / "lib.drift"),
			*_emit_pkg_args("acme.lib"),
			"--native-link-lib",
			"ssl",
			"--package-dep",
			"net.tls=^0.3.0",
			"--emit-package",
			str(pkg_path),
		]
	)
	assert rc == 0
	loaded = load_package_v0(pkg_path)
	assert len(loaded.native_deps) == 1
	assert loaded.native_deps[0].lib == "ssl"
	assert len(loaded.package_deps) == 1
	assert loaded.package_deps[0].name == "net.tls"


# ── Version selection tests ──────────────────────────────────────────────────


def _emit_versioned_pkg(
	src_root: Path,
	pkg_out: Path,
	*,
	module_id: str = "acme.lib",
	version: str = "0.0.0",
) -> Path:
	"""Emit a .dmp package with a specific version."""
	module_dir = src_root.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"module {module_id};\n\nexport {{ add }};\n\npub fn add(a: Int, b: Int) nothrow -> Int {{\n\treturn a + b;\n}}\n",
	)
	pkg_out.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main(
		[
			"-M",
			str(src_root),
			str(module_dir / "lib.drift"),
			"--package-id",
			module_id,
			"--package-version",
			version,
			"--package-target",
			"test-target",
			"--emit-package",
			str(pkg_out),
		]
	)
	assert rc == 0, f"emit-package for {module_id}@{version} should succeed (rc={rc})"
	return pkg_out


def test_package_version_pin_selects(tmp_path: Path) -> None:
	"""--dep net.tls@0.3.0 loads exactly that version from a multi-version root."""
	pkgs = tmp_path / "pkgs"
	src1 = tmp_path / "src1"
	src2 = tmp_path / "src2"
	_emit_versioned_pkg(src1, pkgs / "tls_020.dmp", module_id="net.tls", version="0.2.0")
	_emit_versioned_pkg(src2, pkgs / "tls_030.dmp", module_id="net.tls", version="0.3.0")

	# With pin, should succeed (emit-ir to avoid needing clang).
	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport net.tls as tls;\n\nfn main() nothrow -> Int {\n\treturn tls.add(1, 2);\n}\n",
	)
	ir_out = tmp_path / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--dep",
			"net.tls@0.3.0",
			"--emit-ir",
			str(ir_out),
		]
	)
	assert rc == 0, "version-pinned consumer should compile successfully"
	assert ir_out.exists()


def test_package_version_missing_fails(tmp_path: Path) -> None:
	"""--dep for nonexistent version produces a clear error."""
	pkgs = tmp_path / "pkgs"
	src = tmp_path / "src"
	_emit_versioned_pkg(src, pkgs / "tls_030.dmp", module_id="net.tls", version="0.3.0")

	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport net.tls as tls;\n\nfn main() nothrow -> Int {\n\treturn tls.add(1, 2);\n}\n",
	)
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--dep",
			"net.tls@0.4.0",
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 1, "pinning to nonexistent version should fail"


def test_package_version_ambiguous_without_dep_fails(tmp_path: Path) -> None:
	"""--package-root without --dep is rejected (even with packages present)."""
	pkgs = tmp_path / "pkgs"
	src1 = tmp_path / "src1"
	src2 = tmp_path / "src2"
	_emit_versioned_pkg(src1, pkgs / "tls_020.dmp", module_id="net.tls", version="0.2.0")
	_emit_versioned_pkg(src2, pkgs / "tls_030.dmp", module_id="net.tls", version="0.3.0")

	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport net.tls as tls;\n\nfn main() nothrow -> Int {\n\treturn tls.add(1, 2);\n}\n",
	)
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 1, "--package-root without --dep should fail"


def test_package_version_single_with_dep(tmp_path: Path) -> None:
	"""Single version present, --dep pins it — loads correctly."""
	pkgs = tmp_path / "pkgs"
	src = tmp_path / "src"
	_emit_versioned_pkg(src, pkgs / "tls.dmp", module_id="net.tls", version="0.3.0")

	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport net.tls as tls;\n\nfn main() nothrow -> Int {\n\treturn tls.add(1, 2);\n}\n",
	)
	ir_out = tmp_path / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--dep",
			"net.tls@0.3.0",
			"--emit-ir",
			str(ir_out),
		]
	)
	assert rc == 0, "single-version package with --dep should load"
	assert ir_out.exists()


def test_dep_malformed_rejected(tmp_path: Path) -> None:
	"""--dep without @VERSION is rejected."""
	pkgs = tmp_path / "pkgs"
	src = tmp_path / "src"
	_emit_versioned_pkg(src, pkgs / "tls.dmp", module_id="net.tls", version="0.3.0")

	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport net.tls as tls;\n\nfn main() nothrow -> Int {\n\treturn tls.add(1, 2);\n}\n",
	)
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--dep",
			"net.tls",
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 1, "--dep without @VERSION should fail"


def test_dep_duplicate_rejected(tmp_path: Path) -> None:
	"""--dep specified twice for the same package is rejected."""
	pkgs = tmp_path / "pkgs"
	src = tmp_path / "src"
	_emit_versioned_pkg(src, pkgs / "tls.dmp", module_id="net.tls", version="0.3.0")

	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport net.tls as tls;\n\nfn main() nothrow -> Int {\n\treturn tls.add(1, 2);\n}\n",
	)
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--dep",
			"net.tls@0.3.0",
			"--dep",
			"net.tls@0.2.0",
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 1, "duplicate --dep for same package should fail"


# ── Raw .dmp / compressed .zdmp consumption regression ─────────────


def test_signed_dmp_can_be_consumed_via_package_root(tmp_path: Path) -> None:
	"""Regression: raw .dmp emitted by --emit-package, signed, consumed.

	TLS team reported ZstdError when consuming uncompressed .dmp packages
	on 0.27.69.  This test pins that the raw .dmp → sign → consume path
	works end-to-end.
	"""
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	# Emit a signed .dmp package.
	pkg_path = _emit_lib_pkg(tmp_path)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=kid, sig_raw=sig_raw)

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=kid, pub_b64=pub_b64)

	# Consumer source.
	consumer_dir = tmp_path / "consumer"
	_write_file(
		consumer_dir / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int {
	return try lib.add(1, 2) catch { 0 };
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(consumer_dir),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(consumer_dir / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0, "signed .dmp consumption must succeed"


def test_signed_zdmp_can_be_consumed_via_package_root(tmp_path: Path) -> None:
	"""Regression: .dmp compressed to .zdmp (deploy pipeline), signed, consumed.

	Exercises the .zdmp decompression path in load_package_v0_with_policy.
	"""
	from lang.driftc.packages.zdmp import compress_to_zdmp

	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	# Emit a .dmp, then compress to .zdmp (mimicking drift deploy).
	dmp_path = _emit_lib_pkg(tmp_path)
	raw_bytes = dmp_path.read_bytes()
	zdmp_bytes = compress_to_zdmp(raw_bytes)

	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	zdmp_path = pkg_root / "lib.zdmp"
	zdmp_path.write_bytes(zdmp_bytes)

	# Sign covers uncompressed bytes (matching deploy pipeline).
	sig_raw = priv.sign(raw_bytes)
	_write_sig_sidecar(zdmp_path, pkg_bytes=raw_bytes, kid=kid, sig_raw=sig_raw)

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=kid, pub_b64=pub_b64)

	# Consumer source.
	consumer_dir = tmp_path / "consumer"
	_write_file(
		consumer_dir / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int {
	return try lib.add(1, 2) catch { 0 };
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(consumer_dir),
			"--package-root",
			str(pkg_root),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(consumer_dir / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0, "signed .zdmp consumption must succeed"


def test_dmp_not_shadowed_by_stale_zdmp_in_same_dir(tmp_path: Path) -> None:
	"""Regression: stale .zdmp in the same directory silently shadows valid .dmp.

	discover_package_files dedup prefers .zdmp over .dmp when both share
	a stem.  If the .zdmp is stale/corrupt, the loader falls back to the
	.dmp sibling instead of crashing with ZstdError.
	"""
	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	# Emit a valid signed .dmp.
	pkg_path = _emit_lib_pkg(tmp_path)
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	_write_sig_sidecar(pkg_path, pkg_bytes=pkg_bytes, kid=kid, sig_raw=sig_raw)

	# Place a stale/corrupt .zdmp with the same stem alongside the .dmp.
	stale_zdmp = pkg_path.with_suffix(".zdmp")
	stale_zdmp.write_bytes(b"NOT VALID ZSTD DATA")

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=kid, pub_b64=pub_b64)

	# Consumer source.
	consumer_dir = tmp_path / "consumer"
	_write_file(
		consumer_dir / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int {
	return try lib.add(1, 2) catch { 0 };
}
""".lstrip(),
	)
	# This should succeed: the valid .dmp should be consumable even when
	# a stale .zdmp exists alongside it.
	rc = driftc_main(
		[
			"-M",
			str(consumer_dir),
			"--package-root",
			str(tmp_path),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(consumer_dir / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0, "valid .dmp must not be shadowed by corrupt .zdmp"


def test_valid_zdmp_not_shadowed_by_stale_dmp_in_same_dir(tmp_path: Path) -> None:
	"""Mirror regression: valid published .zdmp must not be ignored when a stale .dmp coexists.

	The dedup prefers .zdmp.  A stale .dmp with the same stem must not
	override the published .zdmp.
	"""
	from lang.driftc.packages.zdmp import compress_to_zdmp

	priv = Ed25519PrivateKey.generate()
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	# Emit a .dmp, compress to .zdmp, sign the uncompressed bytes.
	dmp_path = _emit_lib_pkg(tmp_path)
	raw_bytes = dmp_path.read_bytes()
	zdmp_bytes = compress_to_zdmp(raw_bytes)

	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	zdmp_path = pkg_root / "lib.zdmp"
	zdmp_path.write_bytes(zdmp_bytes)

	sig_raw = priv.sign(raw_bytes)
	_write_sig_sidecar(zdmp_path, pkg_bytes=raw_bytes, kid=kid, sig_raw=sig_raw)

	# Place a stale/corrupt .dmp with the same stem alongside the .zdmp.
	stale_dmp = pkg_root / "lib.dmp"
	stale_dmp.write_bytes(b"NOT A VALID DMIR PACKAGE")

	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid=kid, pub_b64=pub_b64)

	# Consumer source.
	consumer_dir = tmp_path / "consumer"
	_write_file(
		consumer_dir / "main.drift",
		"""
module main;

import acme.lib as lib;

fn main() nothrow -> Int {
	return try lib.add(1, 2) catch { 0 };
}
""".lstrip(),
	)
	# Valid .zdmp must be consumed; stale .dmp must not shadow it.
	rc = driftc_main(
		[
			"-M",
			str(consumer_dir),
			"--package-root",
			str(pkg_root),
			"--dep",
			"acme.lib@0.0.0",
			"--trust-store",
			str(trust_path),
			"--require-signatures",
			str(consumer_dir / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0, "valid .zdmp must not be shadowed by stale .dmp"


# ── --dep as package-loading allowlist ──────────────────────────────


def _emit_versioned_multi_module_pkg(
	src_root: Path,
	pkg_out: Path,
	*,
	package_id: str,
	module_ids: list[str],
	version: str = "0.0.0",
) -> Path:
	"""Emit a .dmp package with multiple modules."""
	for mid in module_ids:
		module_dir = src_root.joinpath(*mid.split("."))
		_write_file(
			module_dir / "lib.drift",
			f"module {mid};\n\nexport {{ add }};\n\npub fn add(a: Int, b: Int) nothrow -> Int {{\n\treturn a + b;\n}}\n",
		)
	pkg_out.parent.mkdir(parents=True, exist_ok=True)
	source_files = []
	for mid in module_ids:
		module_dir = src_root.joinpath(*mid.split("."))
		source_files.append(str(module_dir / "lib.drift"))
	rc = driftc_main(
		[
			"-M",
			str(src_root),
			*source_files,
			"--package-id",
			package_id,
			"--package-version",
			version,
			"--package-target",
			"test-target",
			"--emit-package",
			str(pkg_out),
		]
	)
	assert rc == 0, f"emit-package for {package_id}@{version} should succeed (rc={rc})"
	return pkg_out


def test_dep_allowlist_ignores_unrelated_packages(tmp_path: Path) -> None:
	"""--dep should be an allowlist: only listed packages are loaded from
	--package-root.  Unrelated packages in the same root must be ignored.

	Regression: driftc loaded every package under --package-root, causing
	module ID collisions when the root contained a deployed copy of the
	package being compiled from source.
	"""
	pkgs = tmp_path / "pkgs"

	# Package A: deployed web-client containing web.client.cookie module.
	# This is the SAME module id as our source — loading it causes a collision.
	src_a = tmp_path / "src_a"
	_emit_versioned_multi_module_pkg(
		src_a, pkgs / "web_client.dmp",
		package_id="web-client", module_ids=["web.client.cookie"],
		version="0.2.0",
	)

	# Package B: the intended dependency.
	src_b = tmp_path / "src_b"
	_emit_versioned_pkg(src_b, pkgs / "net_tls.dmp", module_id="net.tls", version="0.3.3")

	# Package C: unrelated, also in the root.
	src_c = tmp_path / "src_c"
	_emit_versioned_pkg(src_c, pkgs / "web_jwt.dmp", module_id="web.jwt", version="0.2.0")

	# Compile a main program that uses web.client.cookie as local source
	# and depends on net.tls from the package root.
	# The package root also contains a deployed web-client with the SAME
	# module id — without --dep allowlisting, the compiler loads it and
	# hits a module ID collision.
	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "web" / "client" / "cookie.drift",
		"module web.client.cookie;\n\nimport net.tls as tls;\n\nexport { wrap };\n\npub fn wrap(x: Int) nothrow -> Int {\n\treturn tls.add(x, 1);\n}\n",
	)
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport web.client.cookie as cookie;\n\nfn main() nothrow -> Int {\n\treturn cookie.wrap(5);\n}\n",
	)
	ir_out = tmp_path / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			str(consumer_src / "web" / "client" / "cookie.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--dep",
			"net.tls@0.3.3",
			"--emit-ir",
			str(ir_out),
		]
	)
	assert rc == 0, (
		"--dep net.tls@0.3.3 should load only net.tls; "
		"web-client and web.jwt in the root must be ignored"
	)
	assert ir_out.exists()
	assert ir_out.exists()


def test_dep_allowlist_self_package_with_package_id(tmp_path: Path) -> None:
	"""When compiling with --package-id, the deployed copy is excluded by
	both --dep allowlist AND self-exclusion.  Belt-and-suspenders."""
	pkgs = tmp_path / "pkgs"

	# Deploy web-client 0.2.0 with module web.client.cookie to shared root.
	src_deployed = tmp_path / "src_deployed"
	_emit_versioned_multi_module_pkg(
		src_deployed, pkgs / "web_client.dmp",
		package_id="web-client", module_ids=["web.client.cookie"],
		version="0.2.0",
	)

	# Also deploy the intended dependency.
	src_dep = tmp_path / "src_dep"
	_emit_versioned_pkg(src_dep, pkgs / "net_tls.dmp", module_id="net.tls", version="0.3.3")

	# Compile web.client.cookie from source with --package-id web-client.
	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "web" / "client" / "cookie.drift",
		"module web.client.cookie;\n\nimport net.tls as tls;\n\nexport { wrap };\n\npub fn wrap(x: Int) nothrow -> Int {\n\treturn tls.add(x, 1);\n}\n",
	)
	pkg_out = tmp_path / "out.dmp"
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "web" / "client" / "cookie.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--dep",
			"net.tls@0.3.3",
			"--package-id",
			"web-client",
			"--package-version",
			"0.3.0",
			"--package-target",
			"test-target",
			"--emit-package",
			str(pkg_out),
		]
	)
	assert rc == 0, (
		"deployed web-client in package root must not collide "
		"with web.client.cookie source being compiled"
	)


def test_dep_allowlist_multi_version_still_pins(tmp_path: Path) -> None:
	"""When --dep is present, multi-version packages for a requested dep
	still require exact version selection."""
	pkgs = tmp_path / "pkgs"
	src1 = tmp_path / "src1"
	src2 = tmp_path / "src2"
	_emit_versioned_pkg(src1, pkgs / "tls_020.dmp", module_id="net.tls", version="0.2.0")
	_emit_versioned_pkg(src2, pkgs / "tls_030.dmp", module_id="net.tls", version="0.3.0")

	# Also place an unrelated package that should be ignored.
	src_unrelated = tmp_path / "src_unrelated"
	_emit_versioned_pkg(src_unrelated, pkgs / "web_jwt.dmp", module_id="web.jwt", version="0.1.0")

	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport net.tls as tls;\n\nfn main() nothrow -> Int {\n\treturn tls.add(1, 2);\n}\n",
	)
	ir_out = tmp_path / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--dep",
			"net.tls@0.3.0",
			"--emit-ir",
			str(ir_out),
		]
	)
	assert rc == 0, "version-pinned dep with unrelated packages in root should succeed"
	assert ir_out.exists()


def test_package_root_without_dep_is_rejected(tmp_path: Path) -> None:
	"""--package-root without --dep is an error — explicit deps required."""
	pkgs = tmp_path / "pkgs"
	src1 = tmp_path / "src1"
	_emit_versioned_pkg(src1, pkgs / "tls.dmp", module_id="net.tls", version="0.3.0")

	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport net.tls as tls;\n\nfn main() nothrow -> Int {\n\treturn tls.add(1, 2);\n}\n",
	)
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 1, "--package-root without --dep should be rejected"


def test_dep_allowlist_malformed_unrelated_package_ignored(tmp_path: Path) -> None:
	"""Malformed/untrusted unrelated packages under the root do not affect
	the build when not listed in --dep.  The compiler should never attempt
	to load, decompress, or trust-verify them."""
	pkgs = tmp_path / "pkgs"
	pkgs.mkdir(parents=True, exist_ok=True)

	# Good dependency.
	src_dep = tmp_path / "src_dep"
	_emit_versioned_pkg(src_dep, pkgs / "net_tls.dmp", module_id="net.tls", version="0.3.3")

	# Garbage file that looks like a package but is corrupt.
	(pkgs / "corrupted.dmp").write_bytes(b"NOT A VALID DMIR PACKAGE AT ALL")
	# Another garbage .zdmp.
	(pkgs / "broken.zdmp").write_bytes(b"\x00\x00\x00garbage zstd frame")

	consumer_src = tmp_path / "consumer"
	_write_file(
		consumer_src / "main.drift",
		"module main;\n\nimport net.tls as tls;\n\nfn main() nothrow -> Int {\n\treturn tls.add(1, 2);\n}\n",
	)
	ir_out = tmp_path / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(consumer_src),
			str(consumer_src / "main.drift"),
			"--package-root",
			str(pkgs),
			"--allow-unsigned-from",
			str(pkgs),
			"--dep",
			"net.tls@0.3.3",
			"--emit-ir",
			str(ir_out),
		]
	)
	assert rc == 0, (
		"malformed unrelated packages in root must not affect "
		"the build when not listed in --dep"
	)
	assert ir_out.exists()
