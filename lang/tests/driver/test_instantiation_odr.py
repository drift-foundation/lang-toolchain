# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.function_key import FunctionKey
from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import main as driftc_main
from lang.driftc.instantiation.key import build_instantiation_key, instantiation_key_str
from lang.driftc.packages.dmir_pkg_v0 import canonical_json_bytes, sha256_hex, write_dmir_pkg_v0
from lang.driftc.packages.provider_v1 import load_package_v1


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


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _read_inst_index(path: Path) -> list[dict[str, object]]:
	if not path.exists():
		return []
	data = json.loads(path.read_text(encoding="utf-8"))
	assert isinstance(data, list)
	return data


def _compile_ir_with_clang(ir_path: Path, bin_path: Path) -> None:
	clang = shutil.which("clang")
	if clang is None:
		raise RuntimeError("clang not available")
	res = subprocess.run(
		[clang, "-x", "ir", "-c", str(ir_path), "-o", str(bin_path)],
		check=False,
		capture_output=True,
		text=True,
	)
	if res.returncode != 0:
		raise RuntimeError(f"clang failed: {res.stderr}")


def _run_nm(path: Path) -> str:
	nm = shutil.which("llvm-nm") or shutil.which("nm")
	if nm is None:
		pytest.skip("nm not available")
	res = subprocess.run([nm, str(path)], check=False, capture_output=True, text=True)
	if res.returncode != 0:
		raise RuntimeError(f"nm failed: {res.stderr}")
	return res.stdout


def _defined_nm_symbols(output: str) -> list[str]:
	symbols: list[str] = []
	for line in output.splitlines():
		parts = line.split()
		if len(parts) < 2:
			continue
		sym = parts[-1]
		kind = parts[-2]
		if kind == "U":
			continue
		symbols.append(sym)
	return symbols


def _emit_generic_pkg(tmp_path: Path, *, module_id: str, pkg_name: str, require: str | None = None) -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	require_clause = f" require {require}" if require else ""
	body = "return move x"
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id};

export {{ id, Show }};
pub trait Show {{
	fn show(self: Self) -> Int;
}}
pub fn id<T>(var x: T) nothrow -> T{require_clause} {{
	{body};
}}
""".lstrip(),
	)
	pkg_path = tmp_path / f"{pkg_name}.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "lib.drift"),
				*_emit_pkg_args(pkg_name),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_box_pkg(tmp_path: Path, *, module_id: str, pkg_name: str) -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id};

export {{ Box, make }};

pub struct Box<T> {{ value: T }}

pub fn make() -> Box<Int> {{
	return Box<type Int>(1);
}}

implement<T> Box<T> {{
	pub fn tag(self: Box<T>) -> Int {{
		return 1;
	}}
}}
""".lstrip(),
	)
	pkg_path = tmp_path / f"{pkg_name}.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "lib.drift"),
				*_emit_pkg_args(pkg_name),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_box_get_pkg(tmp_path: Path, *, module_id: str, pkg_name: str) -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id};

export {{ Box, make }};

pub struct Box<T> {{ value: T }}

pub fn make() -> Box<Int> {{
	return Box<type Int>(1);
}}

implement<T> Box<T> {{
	pub fn get(self: Box<T>) -> Int {{
		return 1;
	}}
}}
""".lstrip(),
	)
	pkg_path = tmp_path / f"{pkg_name}.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "lib.drift"),
				*_emit_pkg_args(pkg_name),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_box_id_pkg(tmp_path: Path, *, module_id: str, pkg_name: str) -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id};

export {{ Box, make }};

pub struct Box<T> {{ value: T }}

pub fn make() -> Box<Int> {{
	return Box<type Int>(1);
}}

implement<T> Box<T> {{
	pub fn id<U>(self: &Box<T>, var value: U) -> U {{
		return move value;
	}}
}}
""".lstrip(),
	)
	pkg_path = tmp_path / f"{pkg_name}.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "lib.drift"),
				*_emit_pkg_args(pkg_name),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _emit_box_show_pkg(tmp_path: Path, *, module_id: str, pkg_name: str) -> Path:
	module_dir = tmp_path.joinpath(*module_id.split("."))
	_write_file(
		module_dir / "lib.drift",
		f"""
module {module_id};

export {{ Box, make, Show, Debug }};

pub trait Debug {{
	fn debug(self: Self) -> Int
}}

pub trait Show {{
	fn show(self: Self) -> Int
}}

pub struct Box<T> {{ value: T }}

implement Debug for Int {{
	pub fn debug(self: Int) -> Int {{ return self; }}
}}

implement<T> Show for Box<T> require T is Debug {{
	pub fn show(self: Box<T>) -> Int {{ return 7; }}
}}

pub fn make() -> Box<Int> {{
	return Box<type Int>(1);
}}
""".lstrip(),
	)
	pkg_path = tmp_path / f"{pkg_name}.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(module_dir / "lib.drift"),
				*_emit_pkg_args(pkg_name),
				"--emit-package",
				str(pkg_path),
			]
		)
		== 0
	)
	return pkg_path


def _strip_generic_templates(pkg_path: Path, *, module_id: str) -> None:
	pkg = load_package_v1(pkg_path)
	mod = pkg.modules_by_id[module_id]
	iface_obj = dict(mod.interface)
	payload_obj = dict(mod.payload)
	payload_obj["generic_templates"] = []

	iface_bytes = canonical_json_bytes(iface_obj)
	payload_bytes = canonical_json_bytes(payload_obj)
	iface_sha = sha256_hex(iface_bytes)
	payload_sha = sha256_hex(payload_bytes)

	manifest_obj = dict(pkg.manifest)
	manifest_obj["modules"] = [
		{
			"module_id": module_id,
			"exports": iface_obj.get("exports", {}),
			"interface_blob": f"sha256:{iface_sha}",
			"payload_blob": f"sha256:{payload_sha}",
		}
	]
	manifest_obj["blobs"] = {
		f"sha256:{iface_sha}": {"type": "exports", "length": len(iface_bytes)},
		f"sha256:{payload_sha}": {"type": "dmir", "length": len(payload_bytes)},
	}
	write_dmir_pkg_v0(
		pkg_path,
		manifest_obj=manifest_obj,
		blobs={iface_sha: iface_bytes, payload_sha: payload_bytes},
		blob_types={iface_sha: 2, payload_sha: 1},
		blob_names={iface_sha: f"iface:{module_id}", payload_sha: f"dmir:{module_id}"},
	)


def test_instantiation_index_marks_comdat_linkonce(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

fn id<T>(var x: T) nothrow -> T { return move x; }

fn main() nothrow -> Int{
	return id<type Int>(1);
}
""".lstrip(),
	)
	ir_path = tmp_path / "out.ll"
	idx_path = tmp_path / "inst.json"
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(ir_path),
			"--emit-instantiation-index",
			str(idx_path),
		]
	)
	assert rc == 0
	entries = _read_inst_index(idx_path)
	assert entries
	for entry in entries:
		assert entry.get("linkage") == "linkonce_odr"
		assert entry.get("comdat") is True


def test_instantiation_missing_template_reports_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	pkg_path = _emit_generic_pkg(tmp_path, module_id="acme.gen", pkg_name="acme.gen")
	_strip_generic_templates(pkg_path, module_id="acme.gen")

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.gen as gen;

fn main() nothrow -> Int{
	return try gen.id<type Int>(1) catch { 0 };
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
			"acme.gen@0.0.0",
			"--allow-unsigned-from",
			str(tmp_path),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any(d.get("severity") == "error" for d in diags)
	codes = [d.get("code") for d in diags]
	assert "E_MISSING_TEMPLATE_BODY" in codes


def test_instantiation_constraint_failure_reports_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	_emit_generic_pkg(tmp_path, module_id="acme.req", pkg_name="acme.req", require="T is Show")

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import acme.req as req;

struct Bad { value: Int }

fn main() nothrow -> Int{
	try {
		req.id<type Bad>(Bad(1));
		return 0;
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
			"--dep",
			"acme.req@0.0.0",
			"--allow-unsigned-from",
			str(tmp_path),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any(d.get("severity") == "error" for d in diags)
	codes = [d.get("code") for d in diags]
	assert "E_REQUIREMENT_NOT_SATISFIED" in codes


@pytest.mark.heavy
def test_instantiation_dedup_across_packages(tmp_path: Path) -> None:
	common_root = tmp_path / "common"
	common_pkg = _emit_generic_pkg(common_root, module_id="acme.common", pkg_name="acme.common")

	# Package A
	a_root = tmp_path / "a"
	a_mod_dir = a_root / "acme" / "a"
	a_src = a_mod_dir / "lib.drift"
	_write_file(
		a_src,
		"""
module acme.a;

import acme.common as common;
export { a };

pub fn a() -> Int {
	return common.id<type Int>(1);
}
""".lstrip(),
	)
	a_pkg = a_root / "a.dmp"
	a_idx = a_root / "inst.json"
	assert (
		driftc_main(
			[
				"-M",
				str(a_root),
				"--package-root",
				str(common_root),
				"--dep",
				"acme.common@0.0.0",
				"--allow-unsigned-from",
				str(common_root),
				str(a_src),
				*_emit_pkg_args("acme.a"),
				"--emit-package",
				str(a_pkg),
				"--emit-instantiation-index",
				str(a_idx),
			]
		)
		== 0
	)

	# Package B
	b_root = tmp_path / "b"
	b_mod_dir = b_root / "acme" / "b"
	b_src = b_mod_dir / "lib.drift"
	_write_file(
		b_src,
		"""
module acme.b;

import acme.common as common;
export { b };

pub fn b() -> Int {
	return common.id<type Int>(2);
}
""".lstrip(),
	)
	b_pkg = b_root / "b.dmp"
	b_idx = b_root / "inst.json"
	assert (
		driftc_main(
			[
				"-M",
				str(b_root),
				"--package-root",
				str(common_root),
				"--dep",
				"acme.common@0.0.0",
				"--allow-unsigned-from",
				str(common_root),
				str(b_src),
				*_emit_pkg_args("acme.b"),
				"--emit-package",
				str(b_pkg),
				"--emit-instantiation-index",
				str(b_idx),
			]
		)
		== 0
	)

	target_prefix = "acme.common:acme.common::id@"
	a_entries = [e for e in _read_inst_index(a_idx) if isinstance(e.get("key"), str) and str(e["key"]).startswith(target_prefix)]
	b_entries = [e for e in _read_inst_index(b_idx) if isinstance(e.get("key"), str) and str(e["key"]).startswith(target_prefix)]
	assert a_entries and b_entries
	assert a_entries[0]["symbol"] == b_entries[0]["symbol"]
	inst_symbol = str(a_entries[0]["symbol"])

	# Root program links A + B.
	root = tmp_path / "root"
	root_src = root / "main.drift"
	_write_file(
		root_src,
		"""
module main;

import acme.a as a;
import acme.b as b;

fn main() nothrow -> Int{
	return try (a.a() + b.b()) catch { 0 };
}
""".lstrip(),
	)
	ir_path = root / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(root),
			"--package-root",
			str(common_root),
			"--package-root",
			str(a_root),
			"--package-root",
			str(b_root),
			"--dep",
			"acme.common@0.0.0",
			"--dep",
			"acme.a@0.0.0",
			"--dep",
			"acme.b@0.0.0",
			"--allow-unsigned-from",
			str(common_root),
			"--allow-unsigned-from",
			str(a_root),
			"--allow-unsigned-from",
			str(b_root),
			str(root_src),
			"--emit-ir",
			str(ir_path),
		]
	)
	assert rc == 0
	bin_path = root / "out.bin"
	_compile_ir_with_clang(ir_path, bin_path)
	nm_out = _run_nm(bin_path)
	defined = _defined_nm_symbols(nm_out)
	matches = [sym for sym in defined if sym == inst_symbol]
	assert len(matches) == 1


def test_generic_function_infers_type_args_across_modules(tmp_path: Path) -> None:
	lib_root = tmp_path / "libpkg"
	_emit_generic_pkg(lib_root, module_id="acme.lib", pkg_name="acme.lib")

	user_root = tmp_path / "userpkg"
	user_mod_a = user_root / "acme" / "user_a" / "lib.drift"
	user_mod_b = user_root / "acme" / "user_b" / "lib.drift"
	_write_file(
		user_mod_a,
		"""
module acme.user_a;

import acme.lib as lib;
export { run_a };

pub fn run_a() -> Int {
	return lib.id(1);
}
""".lstrip(),
	)
	_write_file(
		user_mod_b,
		"""
module acme.user_b;

import acme.lib as lib;
export { run_b };

pub fn run_b() -> Int {
	return lib.id(2);
}
""".lstrip(),
	)
	user_pkg = user_root / "user.dmp"
	idx_path = user_root / "inst.json"
	assert (
		driftc_main(
			[
				"-M",
				str(user_root),
				"--package-root",
				str(lib_root),
				"--dep",
				"acme.lib@0.0.0",
				"--allow-unsigned-from",
				str(lib_root),
				str(user_mod_a),
				str(user_mod_b),
				*_emit_pkg_args("acme.user"),
				"--emit-package",
				str(user_pkg),
				"--emit-instantiation-index",
				str(idx_path),
			]
		)
		== 0
	)
	entries = _read_inst_index(idx_path)
	prefix = "acme.lib:acme.lib::id@"
	matches = [e for e in entries if isinstance(e.get("key"), str) and str(e["key"]).startswith(prefix)]
	assert matches
	assert len(matches) == 1
	assert "Int" in str(matches[0]["key"])


@pytest.mark.heavy
def test_impl_generic_method_instantiated_across_packages(tmp_path: Path) -> None:
	box_root = tmp_path / "boxpkg"
	_emit_box_pkg(box_root, module_id="acme.box", pkg_name="acme.box")

	user_root = tmp_path / "userpkg"
	user_mod_dir = user_root / "acme" / "user"
	user_src = user_mod_dir / "main.drift"
	_write_file(
		user_src,
		"""
module acme.user;

import acme.box as box;
export { run };

pub fn run() -> Int {
	val b: box.Box<Int> = box.make();
	return b.tag();
}
""".lstrip(),
	)
	user_pkg = user_root / "user.dmp"
	idx_path = user_root / "inst.json"
	assert (
		driftc_main(
			[
				"-M",
				str(user_root),
				"--package-root",
				str(box_root),
				"--dep",
				"acme.box@0.0.0",
				"--allow-unsigned-from",
				str(box_root),
				str(user_src),
				*_emit_pkg_args("acme.user"),
				"--emit-package",
				str(user_pkg),
				"--emit-instantiation-index",
				str(idx_path),
			]
		)
		== 0
	)
	entries = _read_inst_index(idx_path)
	assert entries
	prefix = "acme.box:acme.box::Box<T>::tag@"
	assert any(isinstance(e.get("key"), str) and str(e["key"]).startswith(prefix) for e in entries)


def test_impl_generic_method_infers_type_args_across_packages(tmp_path: Path) -> None:
	box_root = tmp_path / "boxpkg"
	_emit_box_get_pkg(box_root, module_id="acme.box", pkg_name="acme.box")

	user_root = tmp_path / "userpkg"
	user_mod_dir = user_root / "acme" / "user"
	user_src = user_mod_dir / "main.drift"
	_write_file(
		user_src,
		"""
module acme.user;

import acme.box as box;
export { run };

pub fn run() -> Int {
	val b: box.Box<Int> = box.make();
	return b.get();
}
""".lstrip(),
	)
	user_pkg = user_root / "user.dmp"
	idx_path = user_root / "inst.json"
	assert (
		driftc_main(
			[
				"-M",
				str(user_root),
				"--package-root",
				str(box_root),
				"--dep",
				"acme.box@0.0.0",
				"--allow-unsigned-from",
				str(box_root),
				str(user_src),
				*_emit_pkg_args("acme.user"),
				"--emit-package",
				str(user_pkg),
				"--emit-instantiation-index",
				str(idx_path),
			]
		)
		== 0
	)
	entries = _read_inst_index(idx_path)
	prefix = "acme.box:acme.box::Box<T>::get@"
	matches = [e for e in entries if isinstance(e.get("key"), str) and str(e["key"]).startswith(prefix)]
	assert len(matches) == 1
	assert "Int" in str(matches[0]["key"])


def test_impl_method_type_params_instantiated_across_packages(tmp_path: Path) -> None:
	box_root = tmp_path / "boxpkg"
	_emit_box_id_pkg(box_root, module_id="acme.box", pkg_name="acme.box")

	user_root = tmp_path / "userpkg"
	user_mod_a = user_root / "acme" / "user_a" / "lib.drift"
	user_mod_b = user_root / "acme" / "user_b" / "lib.drift"
	_write_file(
		user_mod_a,
		"""
module acme.user_a;

import acme.box as box;
export { run_a };

pub fn run_a() -> Int {
	val b: box.Box<Int> = box.make();
	return b.id(1);
}
""".lstrip(),
	)
	_write_file(
		user_mod_b,
		"""
module acme.user_b;

import acme.box as box;
export { run_b };

pub fn run_b() -> String {
	val b: box.Box<Int> = box.make();
	return b.id("x");
}
""".lstrip(),
	)
	user_pkg = user_root / "user.dmp"
	idx_path = user_root / "inst.json"
	assert (
		driftc_main(
			[
				"-M",
				str(user_root),
				"--package-root",
				str(box_root),
				"--dep",
				"acme.box@0.0.0",
				"--allow-unsigned-from",
				str(box_root),
				str(user_mod_a),
				str(user_mod_b),
				*_emit_pkg_args("acme.user"),
				"--emit-package",
				str(user_pkg),
				"--emit-instantiation-index",
				str(idx_path),
			]
		)
		== 0
	)
	entries = _read_inst_index(idx_path)
	prefix = "acme.box:acme.box::Box<T>::id@"
	keys = [
		str(e.get("key"))
		for e in entries
		if isinstance(e.get("key"), str) and str(e["key"]).startswith(prefix)
	]
	assert keys
	assert len(keys) == len(set(keys))
	assert any("|Int,Int|" in key for key in keys)
	assert any("|Int,String|" in key for key in keys)


def test_trait_generic_method_instantiated_across_packages(tmp_path: Path) -> None:
	box_root = tmp_path / "boxpkg"
	_emit_box_show_pkg(box_root, module_id="acme.box", pkg_name="acme.box")

	user_root = tmp_path / "userpkg"
	user_mod_dir = user_root / "acme" / "user"
	user_src = user_mod_dir / "main.drift"
	_write_file(
		user_src,
		"""
module acme.user;

import acme.box as box;
export { run };
use trait box.Show;

pub fn run() -> Int {
	val b: box.Box<Int> = box.make();
	return b.show();
}
""".lstrip(),
	)
	user_pkg = user_root / "user.dmp"
	idx_path = user_root / "inst.json"
	assert (
		driftc_main(
			[
				"-M",
				str(user_root),
				"--package-root",
				str(box_root),
				"--dep",
				"acme.box@0.0.0",
				"--allow-unsigned-from",
				str(box_root),
				str(user_src),
				*_emit_pkg_args("acme.user"),
				"--emit-package",
				str(user_pkg),
				"--emit-instantiation-index",
				str(idx_path),
			]
		)
		== 0
	)
	entries = _read_inst_index(idx_path)
	assert entries
	prefix = "acme.box:acme.box::Box<T>::Show::show@"
	assert any(isinstance(e.get("key"), str) and str(e["key"]).startswith(prefix) for e in entries)


def test_trait_generic_method_dedup_across_modules_in_package(tmp_path: Path) -> None:
	box_root = tmp_path / "boxpkg"
	_emit_box_show_pkg(box_root, module_id="acme.box", pkg_name="acme.box")

	user_root = tmp_path / "userpkg"
	user_mod_a = user_root / "acme" / "user_a" / "lib.drift"
	user_mod_b = user_root / "acme" / "user_b" / "lib.drift"
	_write_file(
		user_mod_a,
		"""
module acme.user_a;

import acme.box as box;
export { run_a };
use trait box.Show;

pub fn run_a() -> Int {
	val b: box.Box<Int> = box.make();
	return b.show();
}
""".lstrip(),
	)
	_write_file(
		user_mod_b,
		"""
module acme.user_b;

import acme.box as box;
export { run_b };
use trait box.Show;

pub fn run_b() -> Int {
	val b: box.Box<Int> = box.make();
	return b.show();
}
""".lstrip(),
	)
	user_pkg = user_root / "user.dmp"
	idx_path = user_root / "inst.json"
	assert (
		driftc_main(
			[
				"-M",
				str(user_root),
				"--package-root",
				str(box_root),
				"--dep",
				"acme.box@0.0.0",
				"--allow-unsigned-from",
				str(box_root),
				str(user_mod_a),
				str(user_mod_b),
				*_emit_pkg_args("acme.user"),
				"--emit-package",
				str(user_pkg),
				"--emit-instantiation-index",
				str(idx_path),
			]
		)
		== 0
	)
	entries = _read_inst_index(idx_path)
	prefix = "acme.box:acme.box::Box<T>::Show::show@"
	matches = [e for e in entries if isinstance(e.get("key"), str) and str(e["key"]).startswith(prefix)]
	assert matches
	assert len(matches) == 1


def test_instantiation_key_includes_abi_flags() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_key = FunctionKey(package_id="test", module_path="main", name="id", decl_fingerprint="fp")
	key_a = build_instantiation_key(fn_key, (int_ty,), type_table=table, can_throw=True)
	key_b = build_instantiation_key(fn_key, (int_ty,), type_table=table, can_throw=False)
	assert instantiation_key_str(key_a) != instantiation_key_str(key_b)
