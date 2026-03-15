# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import main as driftc_main
from lang.driftc.packages.provider_v0 import load_package_v0


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_package_includes_generic_templates(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib;

fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

import std.core as core;

export { id, need };

pub fn id<T>(x: T) nothrow -> T {
	return x;
}

pub fn need<T>(x: T) nothrow -> Int require T is core.Copy {
	return 1;
}

struct Box<T> { value: T }

implement<T> Box<T> {
	pub fn get<U>(self: &Box<T>, value: U) -> U {
		return value;
	}
}

pub trait Show {
	fn show(self: &Self) -> Int;
}

implement<T> Show for Box<T> {
	pub fn show(self: &Box<T>) -> Int {
		return 1;
	}
}
""".lstrip(),
	)

	out = tmp_path / "p.dmp"
	argv = [
		"-M",
		str(tmp_path),
		str(tmp_path / "main.drift"),
		str(tmp_path / "lib" / "lib.drift"),
		"--package-id",
		"test.pkg",
		"--package-version",
		"0.0.0",
		"--package-target",
		"test",
		"--emit-package",
		str(out),
	]
	assert driftc_main(argv) == 0

	pkg = load_package_v0(out)
	lib_payload = pkg.modules_by_id["lib"].payload
	templates = lib_payload.get("generic_templates")
	assert isinstance(templates, list)
	entry = next(e for e in templates if isinstance(e, dict) and e.get("fn_symbol") == "lib::id")
	assert entry.get("ir_kind") == "TemplateHIR-v1"
	template_id = entry.get("template_id")
	assert isinstance(template_id, dict)
	assert template_id.get("package") == "test.pkg"
	assert template_id.get("module") == "lib"
	assert template_id.get("name") == "id"
	assert isinstance(template_id.get("fingerprint"), str)
	assert "require" in entry
	ir = entry.get("ir")
	assert isinstance(ir, dict)
	assert ir.get("_type") == "HBlock"
	sig = entry.get("signature")
	assert isinstance(sig, dict)
	assert sig.get("type_params") == ["T"]
	assert entry.get("generic_param_layout") == [{"scope": "fn", "index": 0}]
	param_types = sig.get("param_types")
	assert isinstance(param_types, list)
	assert param_types[0].get("param") == "T"
	ret = sig.get("return_type")
	assert isinstance(ret, dict)
	assert ret.get("param") == "T"
	need = next(e for e in templates if isinstance(e, dict) and e.get("fn_symbol") == "lib::need")
	assert isinstance(need.get("require"), dict)
	inherent = next(e for e in templates if isinstance(e, dict) and e.get("fn_symbol") == "lib::Box<T>::get")
	inherent_sig = inherent.get("signature")
	assert isinstance(inherent_sig, dict)
	assert inherent_sig.get("is_method") is True
	assert inherent_sig.get("impl_type_params") == ["T"]
	assert inherent_sig.get("type_params") == ["U"]
	assert inherent.get("generic_param_layout") == [{"scope": "impl", "index": 0}, {"scope": "fn", "index": 0}]
	trait_method = next(
		e for e in templates if isinstance(e, dict) and e.get("fn_symbol") == "lib::Box<T>::Show::show"
	)
	trait_sig = trait_method.get("signature")
	assert isinstance(trait_sig, dict)
	assert trait_sig.get("is_method") is True
	assert trait_sig.get("impl_type_params") == ["T"]
	assert trait_method.get("generic_param_layout") == [{"scope": "impl", "index": 0}]
