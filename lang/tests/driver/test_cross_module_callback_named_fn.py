# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Cross-module `core.callback{N}(other_mod.fn)` named-fn wrap.

Pre-0.31.71 the resolver overrode `can_throw = True` for every
`is_exported_entrypoint` / `is_extern` signature in
`_call_sig_for_fn_ref` (type_checker.py:3306-3307).  That reflected
the OK-wrap thunk's FnResult-based ABI but stomped the function's
user-declared `nothrow` bit on the resulting function-reference
TypeId.  When the fn ref flowed into `callback{N}(...)`, the
intrinsic resolver (call_resolver.py:5002) saw `fn_throws=True` on
a nothrow function and rejected the wrap as

    error: callback{N} requires a nothrow function [E-AUTO-...]

cascading into an Unknown type for the `callback{N}` result so
downstream calls (e.g. `add_route_group_middleware`) failed with
"no matching overload ... [3]" (3 = Unknown).

After fix: `_call_sig_for_fn_ref` returns the declared `can_throw`
unchanged.  The thunk machinery at the fn_ref construction site
(type_checker.py:3395-3400) still adapts the bare nothrow return
into FnResult — the user-facing nothrow shape is preserved.

Pins arity 1 / 2 / 3 of the cross-module wrap.  Same-module
references were never broken; the bug was specific to a named-fn
referenced via a module alias from a different module.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	root = stdlib_root()
	args = list(argv)
	if root:
		args += ["--stdlib-root", str(root)]
	args += ["--dev", "--json"]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile_two_module(
	tmp_path: Path,
	capsys: pytest.CaptureFixture[str],
	*,
	routes_source: str,
	main_source: str,
) -> tuple[int, dict]:
	mod_root = tmp_path / "mods"
	_write_file(mod_root / "routes" / "routes.drift", routes_source)
	_write_file(mod_root / "main" / "main.drift", main_source)
	out_bin = tmp_path / "bin"
	paths = sorted(mod_root.rglob("*.drift"))
	return _run_driftc_json(
		["-M", str(mod_root), *map(str, paths),
		 "--entry", "main::main", "-o", str(out_bin)],
		capsys,
	)


def test_callback1_cross_module_named_fn_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Minimal regression: `core.callback1(routes.my_fn)` over a
	cross-module exported nothrow function.  Pre-fix failed with
	'callback1 requires a nothrow function'.
	"""
	rc, payload = _compile_two_module(
		tmp_path, capsys,
		routes_source="""
module routes;

export { my_fn };

pub fn my_fn(a: Int) nothrow -> Int {
	return a + 1;
}
""".lstrip(),
		main_source="""
module main;

import std.core as core;
import routes as routes;

fn main() nothrow -> Int {
	val cb = core.callback1(routes.my_fn);
	return cb.call(41);
}
""".lstrip(),
	)
	assert rc == 0, payload


def test_callback2_cross_module_named_fn_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	rc, payload = _compile_two_module(
		tmp_path, capsys,
		routes_source="""
module routes;

export { my_fn };

pub fn my_fn(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
""".lstrip(),
		main_source="""
module main;

import std.core as core;
import routes as routes;

fn main() nothrow -> Int {
	val cb = core.callback2(routes.my_fn);
	return cb.call(20, 22);
}
""".lstrip(),
	)
	assert rc == 0, payload


def test_callback3_cross_module_named_fn_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	rc, payload = _compile_two_module(
		tmp_path, capsys,
		routes_source="""
module routes;

export { my_fn };

pub fn my_fn(a: Int, b: Int, c: Int) nothrow -> Int {
	return a + b + c;
}
""".lstrip(),
		main_source="""
module main;

import std.core as core;
import routes as routes;

fn main() nothrow -> Int {
	val cb = core.callback3(routes.my_fn);
	return cb.call(10, 12, 20);
}
""".lstrip(),
	)
	assert rc == 0, payload


def test_callback3_cross_module_with_ref_and_pub_error_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""App-team shape: 3-module dependency chain with refs + nested
	Callback2 + `Result<T, pub error E>`.  This is the structure the
	web-rest middleware reports its `auth_middleware(...)` against —
	the bug fires identically on the reduced shape because the
	trigger is the `is_exported_entrypoint` nothrow override, not
	anything in the type-args.
	"""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "errors" / "errors.drift",
		"""
module errors;

pub error RestError { tag: String }

export { RestError };
""".lstrip(),
	)
	_write_file(
		mod_root / "routes" / "routes.drift",
		"""
module routes;

import std.core as core;
import errors as errors;

export { auth_middleware };

pub fn auth_middleware(
	a: &Int,
	b: &mut Int,
	next: core.Callback2<&Int, &mut Int, core.Result<Int, errors.RestError>>
) nothrow -> core.Result<Int, errors.RestError> {
	return next.call(a, b);
}
""".lstrip(),
	)
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.core as core;
import errors as errors;
import routes as routes;

fn install(cb: core.Callback3<&Int, &mut Int, core.Callback2<&Int, &mut Int, core.Result<Int, errors.RestError>>, core.Result<Int, errors.RestError>>) nothrow -> Void {
	return;
}

fn main() nothrow -> Int {
	install(core.callback3(routes.auth_middleware));
	return 0;
}
""".lstrip(),
	)
	out_bin = tmp_path / "bin"
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(
		["-M", str(mod_root), *map(str, paths),
		 "--entry", "main::main", "-o", str(out_bin)],
		capsys,
	)
	assert rc == 0, payload


def test_callback3_throws_variant_still_rejects_nothrow_callback(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Sanity check: a throws-declared function should still be
	rejected by `callback3` (which requires nothrow).  The fix only
	closes the false-positive on declared-nothrow exported fns; it
	must not accidentally accept actually-throwing fns.
	"""
	rc, payload = _compile_two_module(
		tmp_path, capsys,
		routes_source="""
module routes;

pub error E { tag: String }

export { E, throwing_fn };

pub fn throwing_fn(a: Int, b: Int, c: Int) throws -> Int {
	throw E(tag = "always");
}
""".lstrip(),
		main_source="""
module main;

import std.core as core;
import routes as routes;

fn main() nothrow -> Int {
	val _ = core.callback3(routes.throwing_fn);
	return 0;
}
""".lstrip(),
	)
	assert rc != 0, payload
	msgs = [str(d.get("message", "")) for d in (payload.get("diagnostics") or [])]
	assert any("callback3 requires a nothrow function" in m for m in msgs), msgs
