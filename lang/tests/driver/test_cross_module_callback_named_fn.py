# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Cross-module `core.callback{N}(other_mod.fn)` named-fn wrap.

Pre-0.31.72 the resolver let the fn-reference TypeId carry the
OK-wrap thunk's `can_throw=True` (set in `_call_sig_for_fn_ref`
at type_checker.py:3317 for every `is_exported_entrypoint` /
`is_extern` signature so direct-call sites see the FnResult ABI).
That same TypeId flowed into the `callback{N}` intrinsic at
`call_resolver.py`, where the `fn_throws` check rejected the
wrap as

    error: callback{N} requires a nothrow function [E-AUTO-...]

cascading into an Unknown type for the `callback{N}` result so
downstream calls (e.g. `add_route_group_middleware`) failed with
"no matching overload ... [3]" (3 = Unknown).

After fix (0.31.72): the type-checker's `can_throw = True`
override is unchanged — direct-call semantics through the
OK-wrap thunk are preserved (pinned by
`lang/tests/codegen/e2e/fnptr_cross_module_wrapper`).  The
`callback{N}` resolver detects the wrapped entry in
`fnptr_consts_by_node_id` (kind=`THUNK_OK_WRAP`), looks up the
underlying function via `ctx.find_thunk_spec_by_id`, and
rewrites the side-table entry in place to point at the impl
with the declared (unwrapped) signature.  The fn_throws check
then passes, and `_apply_fnptr_consts` installs an HFnPtrConst
against the original function so HIR→MIR's `ConstructIface`
targets the impl rather than `__thunk_ok_wrap::<callee>`.

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

pub fn main() nothrow -> Int {
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

pub fn main() nothrow -> Int {
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

pub fn main() nothrow -> Int {
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

pub fn main() nothrow -> Int {
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


def test_two_cross_module_callbacks_chained_dispatches_correctly(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Multi-mw chain regression: two cross-module exported nothrow
	`Callback3` middlewares stored simultaneously and dispatched
	through a `next.call(...)` chain.

	Pre-0.31.72 the resolver's rewrite of the wrapped fn-ref used
	`has_wrapper=False`, which resolved the symbol to the bare
	exported boundary wrapper (FnResult-returning) instead of the
	`__impl` bare-R body.  The Callback3's vtable indirected
	through a FnResult-shaped pointer where Fn{N}.call expected a
	bare R — observed as a SIGSEGV inside the next-link dispatch
	(bookkeeper's `auth_middleware` → `__lambda_cb_dispatch_0_1`
	crash trace).

	After fix (`has_wrapper=True`): the fn-ref resolves to
	`<base>__impl`, matching the ABI that Fn{N}.call indirects
	through.  Chain runs to completion: mw1.call(1, 2, next=...) →
	next is a lambda that calls mw2.call(...) → mw2 returns
	Ok(42) → exit 42.
	"""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "routes" / "routes.drift",
		"""
module routes;

import std.core as core;

pub error E { tag: String }

export { E, mw1, mw2 };

pub fn mw1(a: Int, b: Int, next: core.Callback2<Int, Int, core.Result<Int, E>>) nothrow -> core.Result<Int, E> {
	return next.call(a, b);
}

pub fn mw2(a: Int, b: Int, next: core.Callback2<Int, Int, core.Result<Int, E>>) nothrow -> core.Result<Int, E> {
	val _a = a;
	val _b = b;
	val _next = move next;
	return core.Result<Int, E>::Ok(42);
}
""".lstrip(),
	)
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.core as core;
import routes as routes;

fn terminal(a: Int, b: Int) nothrow -> core.Result<Int, routes.E> {
	val _ = a;
	val _ = b;
	return core.Result<Int, routes.E>::Err(routes.E(tag = "should-not-reach"));
}

pub fn main() nothrow -> Int {
	val cb1 = core.callback3(routes.mw1);
	val cb2 = core.callback3(routes.mw2);
	val cb_term = core.callback2(terminal);
	match cb1.call(1, 2, core.callback2(|a: Int, b: Int| captures(move cb2, move cb_term) => {
		return cb2.call(a, b, move cb_term);
	})) {
		core.Result::Ok(v) => { return v; },
		core.Result::Err(_) => { return 99; }
	}
}
""".lstrip(),
	)
	import subprocess as _sub
	import sys as _sys
	from lang.codegen.llvm.test_utils import sanitizer_timeout
	root_path = Path(__file__).resolve().parents[3]
	paths = sorted(mod_root.rglob("*.drift"))
	out_bin = tmp_path / "bin"
	cmd = [
		_sys.executable, "-m", "lang.driftc.driftc", "--dev",
		"--stdlib-root", str(stdlib_root() or (root_path / "stdlib")),
		"-M", str(mod_root), *map(str, paths),
		"--entry", "main::main", "-o", str(out_bin),
	]
	res = _sub.run(cmd, cwd=root_path, capture_output=True, text=True, timeout=sanitizer_timeout(60))
	assert res.returncode == 0, f"compile failed: {res.stderr[:1500]}"
	run = _sub.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	assert run.returncode == 42, (
		f"chain dispatch returned exit={run.returncode} (expected 42 from mw2.Ok); "
		f"139=SIGSEGV (pre-fix symbol mismatch).  stderr: {run.stderr[:200]}"
	)


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

pub fn main() nothrow -> Int {
	val _ = core.callback3(routes.throwing_fn);
	return 0;
}
""".lstrip(),
	)
	assert rc != 0, payload
	msgs = [str(d.get("message", "")) for d in (payload.get("diagnostics") or [])]
	assert any("callback3 requires a nothrow function" in m for m in msgs), msgs
