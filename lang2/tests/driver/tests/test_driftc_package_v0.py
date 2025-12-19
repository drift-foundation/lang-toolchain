# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from lang2.driftc.driftc import main as driftc_main
from lang2.driftc.packages import dmir_pkg_v0
from lang2.driftc.packages.provider_v0 import discover_package_files
from lang2.driftc.packages.provider_v0 import load_package_v0
from lang2.driftc.packages.dmir_pkg_v0 import canonical_json_bytes, sha256_hex, write_dmir_pkg_v0
from lang2.driftc.packages.signature_v0 import compute_ed25519_kid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


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
module {module_id}

export {{ add }}

fn add(a: Int, b: Int) returns Int {{
	return a + b
}}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(module_dir / "lib.drift"), "--emit-package", str(pkg_path)]) == 0
	return pkg_path


def _emit_hidden_fn_pkg(tmp_path: Path, *, module_id: str = "acme.hidden") -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id}

export {{ add }}

fn add(a: Int, b: Int) returns Int {{
	return a + b
}}

fn hidden() returns Int {{
	return 1
}}
""".lstrip(),
	)
	pkg_path = tmp_path / "hidden.dmp"
	assert driftc_main(["-M", str(tmp_path), str(module_dir / "lib.drift"), "--emit-package", str(pkg_path)]) == 0
	return pkg_path


def _emit_point_type_only_pkg(tmp_path: Path, *, module_id: str = "acme.point") -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "point.drift",
		f"""
module {module_id}

export {{ Point }}

struct Point {{ x: Int, y: Int }}

fn make() returns Point {{
	return Point(x = 1, y = 2)
}}
""".lstrip(),
	)
	pkg_path = tmp_path / "point.dmp"
	assert driftc_main(["-M", str(tmp_path), str(module_dir / "point.drift"), "--emit-package", str(pkg_path)]) == 0
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
module {module_id}

export {{ Point, make }}

struct Point {{ x: Int, y: Int }}

fn make() returns Point {{
	return Point(x = 1, y = 0)
}}
""".lstrip(),
	)
	pkg_path = tmp_path / f"{module_id.replace('.', '_')}.dmp"
	assert driftc_main(["-M", str(tmp_path), str(module_dir / "point.drift"), "--emit-package", str(pkg_path)]) == 0
	return pkg_path


def _emit_optional_variant_pkg(
	tmp_path: Path,
	*,
	module_id: str = "acme.opt",
	extra_arm: bool = False,
	pkg_name: str | None = None,
) -> Path:
	"""
	Emit a package that exports a generic `variant Optional<T>` and a function
	that returns `Optional<Int>`.

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
module {module_id}

export {{ Optional, foo }}

variant Optional<T> {{
{arms}
}}

fn foo() returns Optional<Int> {{
	return Some(41)
}}
""".lstrip(),
	)
	pkg_path = tmp_path / (pkg_name or f"{module_id.replace('.', '_')}.dmp")
	assert driftc_main(["-M", str(tmp_path), str(module_dir / "opt.drift"), "--emit-package", str(pkg_path)]) == 0
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
	sidecar = Path(str(pkg_path) + ".sig")
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
	pub_raw = priv.public_key().public_bytes_raw()
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
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)

	out1 = tmp_path / "p1.dmp"
	out2 = tmp_path / "p2.dmp"

	argv_common = ["-M", str(tmp_path), str(tmp_path / "main.drift"), str(tmp_path / "lib" / "lib.drift"), "--emit-package"]
	assert driftc_main(argv_common + [str(out1)]) == 0
	assert driftc_main(argv_common + [str(out2)]) == 0

	assert out1.read_bytes() == out2.read_bytes()

	pkg = load_package_v0(out1)
	assert pkg.manifest["payload_kind"] == "provisional-dmir"
	assert pkg.manifest["payload_version"] == 0
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
module main

from acme.liba import add
from acme.optb import Optional
from acme.optb import foo

fn main() returns Int {
	val x = add(40, 2)
	val y: Optional<Int> = foo()
	val z = match y {
		Some(v) => { v }
		default => { 0 }
	}
	return x + z
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
	]

	assert driftc_main(argv_ab + ["--emit-package", str(out1)]) == 0
	assert driftc_main(argv_ba + ["--emit-package", str(out2)]) == 0
	assert out1.read_bytes() == out2.read_bytes()


def test_emit_package_is_deterministic_with_changed_package_filenames(tmp_path: Path) -> None:
	"""
	Determinism guard: package discovery ordering (filename sorting / rglob order)
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
module main

from acme.liba import add
from acme.optb import Optional
from acme.optb import foo

fn main() returns Int {
	val x = add(40, 2)
	val y: Optional<Int> = foo()
	val z = match y {
		Some(v) => { v }
		default => { 0 }
	}
	return x + z
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
	- derived/instantiated types created in the consuming build (Optional<geom.Point>),
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
module main

import acme.geom as g

from acme.liba import add
from acme.opt import Optional

fn main() returns Int {
	val p: g.Point = g.make()
	val o: Optional<g.Point> = Some(p)
	val x = match o {
		Some(v) => { v.x }
		default => { 0 }
	}
	return add(40, 2) + x
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
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)

	out = tmp_path / "p.dmp"
	assert driftc_main(
		["-M", str(tmp_path), str(tmp_path / "main.drift"), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(out)]
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
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg_path)]) == 0

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
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg_path)]) == 0

	_patch_pkg_header(pkg_path, manifest_sha256=b"\0" * 32)
	with pytest.raises(ValueError, match="manifest sha256 mismatch"):
		load_package_v0(pkg_path)


def test_load_package_rejects_bad_toc_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg_path)]) == 0

	_patch_pkg_header(pkg_path, toc_sha256=b"\0" * 32)
	with pytest.raises(ValueError, match="toc sha256 mismatch"):
		load_package_v0(pkg_path)


def test_load_package_rejects_duplicate_toc_blob_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg_path)]) == 0

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
module lib

export {{ add }}

fn add(a: Int, b: Int) returns Int {{
	return a + b + {n}
}}
""".lstrip(),
		)
		pkg = tmp_path / f"lib{n}.dmp"
		assert driftc_main(["-M", str(root), str(root / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
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
module lib

export { S, make }

struct S(x: Int)

fn make() returns S {
	return S(x = 42)
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import S
from lib import make

fn main() returns Int {
	val s: S = make()
	return s.x
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
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc == 0


def test_package_embedding_includes_only_call_graph_closure(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}

fn unused() returns Int {
	return 999
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
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
				str(tmp_path / "main.drift"),
				"--emit-ir",
				str(ir_path),
			]
		)
		== 0
	)
	ir = ir_path.read_text(encoding="utf-8")
	assert "lib::unused" not in ir
	# Exported functions are ABI boundary entrypoints: they are emitted as
	# `FnResult<ok, Error*>` wrappers, with a private `__impl` body that keeps
	# the internal calling convention.
	assert "define %FnResult_Int_Error @\"lib::add\"" in ir
	assert "define i64 @\"lib::add__impl\"" in ir
	assert "define i64 @lib::unused" not in ir


def test_discover_package_files_accepts_package_file_path(tmp_path: Path) -> None:
	pkg = tmp_path / "one.dmp"
	pkg.write_bytes(b"")
	assert discover_package_files([pkg]) == [pkg]


def test_driftc_rejects_unsigned_package_outside_allowlist(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	pkg = pkg_root / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)

	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkg_root),
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
module main

fn main() returns Int {
	return 0
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
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
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


def test_driftc_rejects_missing_sidecar_when_signatures_required(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	# Emit an unsigned package but do not write a `.sig` file.
	pkg_path = _emit_lib_pkg(tmp_path)
	trust_path = tmp_path / "trust.json"
	_write_trust_store(trust_path, kid="ed25519:dummy", pub_b64=_b64(b"\0" * 32))

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
	Path(str(signed.pkg_path) + ".sig").write_text("{", encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
	sidecar = Path(str(signed.pkg_path) + ".sig")
	obj = json.loads(sidecar.read_text(encoding="utf-8"))
	obj["signatures"][0]["sig"] = "!!!"
	sidecar.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
	sidecar = Path(str(signed.pkg_path) + ".sig")
	obj = json.loads(sidecar.read_text(encoding="utf-8"))
	obj["signatures"][0]["sig"] = _b64(b"\0" * 63)
	sidecar.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
	pub_raw = priv.public_key().public_bytes_raw()
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
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
	pub1 = priv1.public_key().public_bytes_raw()
	kid1 = compute_ed25519_kid(pub1)
	priv2 = Ed25519PrivateKey.generate()
	pub2 = priv2.public_key().public_bytes_raw()
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
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
	pub_raw = priv.public_key().public_bytes_raw()
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
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	other_priv = Ed25519PrivateKey.generate()
	other_pub = other_priv.public_key().public_bytes_raw()
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
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
	sidecar = Path(str(signed.pkg_path) + ".sig")
	obj = json.loads(sidecar.read_text(encoding="utf-8"))
	obj["signatures"][0]["pubkey"] = _b64(b"\0" * 31)
	sidecar.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
	sidecar = Path(str(signed.pkg_path) + ".sig")
	obj = json.loads(sidecar.read_text(encoding="utf-8"))
	obj["signatures"][0]["pubkey"] = "!!!"
	sidecar.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from acme.lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
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
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	pkg = pkg_root / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

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
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
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
module main

from acme.opt import Optional
from acme.opt import foo

fn main() returns Int {
	val x: Optional<Int> = foo()
	val y = match x {
		Some(v) => { v + 1 }
		None => { 0 }
	}
	return y
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
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(ir_path),
		]
	)
	assert rc == 0


def test_driftc_rejects_variant_schema_collision_between_source_and_package(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	# Package defines `variant Optional<T>` in module `acme.opt`, while source defines
	# a different `variant Optional<T>` in module `main`. With module-scoped nominal
	# type identity, these are distinct and must not collide.
	_emit_optional_variant_pkg(tmp_path)

	_write_file(
		tmp_path / "main.drift",
		"""
module main

// Collides by name with the package's `Optional<T>` schema.
variant Optional<T> {
	Some(value: T),
	None,
	Extra
}

fn main() returns Int {
	return 0
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
	_emit_optional_variant_pkg(tmp_path, module_id="acme.opt", pkg_name="opt_a.dmp")
	_emit_optional_variant_pkg(tmp_path, module_id="acme.opt", extra_arm=True, pkg_name="opt_b.dmp")

	_write_file(
		tmp_path / "main.drift",
		"""
module main

fn main() returns Int {
	return 0
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
module main

from acme.hidden import hidden

fn main() returns Int {
	return hidden()
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


def test_driftc_allows_import_of_exported_type_but_rejects_non_exported_value_from_package(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	_emit_point_type_only_pkg(tmp_path)

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from acme.point import Point
from acme.point import make

fn main() returns Int {
	val p: Point = make()
	return p.x
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
module main

import a.geom as ag
import b.geom as bg

fn main() returns Int {
	val p1: ag.Point = ag.make()
	val p2: bg.Point = bg.make()
	return p1.x + p2.x
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
	out_pkg = tmp_path / "badiface.dmp"
	write_dmir_pkg_v0(
		out_pkg,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
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
module main

from acme.badiface import add

fn main() returns Int {
	return add(40, 2)
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
	out_pkg = tmp_path / "badiface2.dmp"
	write_dmir_pkg_v0(
		out_pkg,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
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
module main

from acme.badiface2 import add

fn main() returns Int {
	return add(40, 2)
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
	out_pkg = tmp_path / "badiface3.dmp"
	write_dmir_pkg_v0(
		out_pkg,
		manifest_obj={
			"format": "dmir-pkg",
			"format_version": 0,
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
module main

from acme.badiface3 import add

fn main() returns Int {
	return add(40, 2)
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


def test_driftc_require_signatures_rejects_unsigned_packages(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	pkg = pkg_root / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
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
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0
