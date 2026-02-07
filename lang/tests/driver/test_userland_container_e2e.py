# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _read_inst_index(path: Path) -> list[dict[str, object]]:
	if not path.exists():
		return []
	data = json.loads(path.read_text(encoding="utf-8"))
	assert isinstance(data, list)
	return data


def _emit_pkg_args(package_id: str) -> list[str]:
	return [
		"--package-id",
		package_id,
		"--package-version",
		"0.0.0",
		"--package-target",
		"test-target",
	]


def _inst_entries(entries: list[dict[str, object]], prefix: str) -> list[dict[str, object]]:
	return [e for e in entries if isinstance(e.get("key"), str) and str(e["key"]).startswith(prefix)]


def test_userland_vec_container_e2e(tmp_path: Path) -> None:
	vec_src = tmp_path / "acme" / "vec" / "lib.drift"
	app_src = tmp_path / "acme" / "app" / "main.drift"
	_write_file(
		vec_src,
		"""
module acme.vec

export { Vec, Show };

pub trait Show {
	fn show(self: &Self) -> Int
}

implement Show for Int {
	pub fn show(self: &Int) -> Int { return 1; }
}

pub struct Vec<T> require T is Show { pub value: T }

implement<T> Vec<T> {
	pub fn len(self: &Vec<T>) -> Int { return 1; }
	pub fn push(self: &mut Vec<T>, value: T) -> Void { return; }
}

implement<T> Show for Vec<T> {
	pub fn show(self: &Vec<T>) -> Int { return 1; }
}
""".lstrip(),
	)
	_write_file(
		app_src,
		"""
module acme.app

import acme.vec as vec;
use trait vec.Show;

export { run };

pub fn run() -> Int{
	var v: vec.Vec<Int> = vec.Vec<type Int>(value = 1);
	v.push(2);
	val a: Int = v.len();
	val b: Int = v.show();
	val c: Int = vec.Show::show(v);
	return a + b + c;
}
""".lstrip(),
	)
	idx_path = tmp_path / "inst.json"
	pkg_path = tmp_path / "app.dmp"
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			str(vec_src),
			str(app_src),
			*_emit_pkg_args("acme.app"),
			"--emit-package",
			str(pkg_path),
			"--emit-instantiation-index",
			str(idx_path),
		]
	)
	assert rc == 0
	entries = _read_inst_index(idx_path)
	len_matches = _inst_entries(entries, "acme.app:acme.vec::Vec<T>::len@")
	push_matches = _inst_entries(entries, "acme.app:acme.vec::Vec<T>::push@")
	show_matches = _inst_entries(entries, "acme.app:acme.vec::Vec<T>::Show::show@")
	assert len(len_matches) == 1
	assert len(push_matches) == 1
	assert len(show_matches) == 1
	assert "Int" in str(len_matches[0]["key"])
	assert "Int" in str(push_matches[0]["key"])
	assert "Int" in str(show_matches[0]["key"])
