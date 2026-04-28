# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG carrier: bare lambda passed to a concrete `CallbackN`
parameter, whose body returns `core.Result::Ok(Resp(...))` or
`core.Result::Err(AppErr(...))`, fails implicit-wrap with:

	error: lambda is declared nothrow but may throw
	error: callback{N} expects a function value

Surfaced by web-team report against 0.31.20 / 0.31.21 / 0.31.22.

**Root cause** (final, 2026-04-28): parser-side
`_resolve_types_in_expr` in `lang/driftc/parser/__init__.py` walks
the AST rewriting `module_alias` → `module_id` on `TypeExpr`
nodes — but had no `parser_ast.Lambda` case.  Type expressions
inside a lambda body kept their raw alias spelling.
`resolve_opaque_type` then took `module_id="core"` (the alias)
literally for `core.Result::Ok(...)`, found no nominal in module
`"core"`, returned `FORWARD_NOMINAL`, and
`resolve_qualified_member_call` returned `None`.  No `CallInfo`
was recorded for the variant ctor; `_lambda_can_throw` fell back
to the conservative may-throw default; the bare lambda was then
either flagged "may throw" against its own `nothrow` annotation
OR — when the lambda has no annotation — caused implicit-wrap to
dispatch to the throwing `callback_throw{N}`, which the concrete
nothrow `Callback{N}<...>` parameter rejected.

Fix: `_resolve_types_in_expr` recurses into Lambda bodies (params,
ret_type, body_expr, body_block).  See
`project_lambda_alias_resolution.md`.

The misleading `Resp<Int>` rendering some pre-fix diagnostics
showed is a separate, secondary pretty-printer bug at
`type_checker.py::_pretty_type_name` (renders STRUCT
`td.param_types` as type-args; for STRUCT those are field types,
not type args).  Not load-bearing for this fix; tracked
separately and verified by C5 below.

Carriers:

  C1. Bare lambda (no `nothrow` annotation — checker must INFER it
	  from the body) → `Callback2<Int, Int, Result<Resp, AppErr>>`,
	  body returns `core.Result::Ok(Resp(...))`.  This is the
	  exact shape from the web-team middleware report; the bug
	  was that nothrow inference silently failed because the
	  body's variant ctor never resolved.
  C2. Sibling with explicit `nothrow` and `Result::Err(...)`.  The
	  declared-nothrow path also fired pre-fix because the body
	  was incorrectly classified may-throw.
  C3. Callback3 — same shape at arity 3 (the actual web-rest
	  middleware shape), no `nothrow` annotation on the lambda.
  C4. Named-fn control — same body in a named nothrow fn works
	  cleanly.  Pinned so any future "this regressed broadly" is
	  caught here too.
  C5. Pretty-printer pin: non-generic `Resp` must NOT be rendered
	  as `Resp<Int>` (or any `Resp<...>`) in any diagnostic.  This
	  catches the secondary pretty-printer bug if it ever starts
	  firing on this path again (currently latent; no diagnostics
	  fire for C1 source post-fix).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile_with_stdlib(
	tmp_path: Path,
	capsys: pytest.CaptureFixture[str],
	source: str,
) -> tuple[int, dict]:
	main_path = tmp_path / "main.drift"
	_write_file(main_path, source)
	argv = ["--stdlib-root", "stdlib", "--test-build-only", str(main_path)]
	return _run_driftc_json(argv, capsys)


_C1_SOURCE = """
module main;

import std.core as core;

struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn register(slot: &mut Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>>,
            cb: core.Callback2<Int, Int, core.Result<Resp, AppErr>>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

fn main() nothrow -> Int {
    var slot: Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>> = [];
    register(&mut slot, |a: Int, b: Int| => {
        return core.Result::Ok(Resp(status = a + b));
    });
    return 0;
}
"""


_C2_SOURCE = """
module main;

import std.core as core;

struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn register(slot: &mut Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>>,
            cb: core.Callback2<Int, Int, core.Result<Resp, AppErr>>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

fn main() nothrow -> Int {
    var slot: Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>> = [];
    register(&mut slot, |a: Int, b: Int| nothrow => {
        return core.Result::Err(AppErr(code = a + b));
    });
    return 0;
}
"""


_C3_SOURCE = """
module main;

import std.core as core;

struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn register3(slot: &mut Array<core.Callback3<Int, Int, Int, core.Result<Resp, AppErr>>>,
             cb: core.Callback3<Int, Int, Int, core.Result<Resp, AppErr>>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

fn main() nothrow -> Int {
    var slot: Array<core.Callback3<Int, Int, Int, core.Result<Resp, AppErr>>> = [];
    register3(&mut slot, |a: Int, b: Int, c: Int| => {
        return core.Result::Ok(Resp(status = a + b + c));
    });
    return 0;
}
"""


_C4_NAMED_FN_SOURCE = """
module main;

import std.core as core;

struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn handler(a: Int, b: Int) nothrow -> core.Result<Resp, AppErr> {
    return core.Result::Ok(Resp(status = a + b));
}

fn register(slot: &mut Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>>,
            cb: core.Callback2<Int, Int, core.Result<Resp, AppErr>>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

fn main() nothrow -> Int {
    var slot: Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>> = [];
    register(&mut slot, core.callback2(handler));
    return 0;
}
"""


def test_c1_callback2_lambda_returns_result_ok(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""C1 — irreducible: bare lambda WITHOUT `nothrow` annotation +
	`Callback2<Int, Int, Result<Resp, AppErr>>` + body returns
	`core.Result::Ok(Resp(...))`.  The checker must INFER the body
	is nothrow (Result-ctor is a value construction, not a throw)
	and accept the implicit-callback-wrap into the concrete nothrow
	`Callback2`.  This is the exact shape from the web-team
	middleware report — pinning inference, not just the annotated
	path."""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, _C1_SOURCE)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"bare lambda (no nothrow annotation) body returning "
		f"core.Result::Ok(Resp(...)) must compile against the "
		f"concrete nothrow Callback2 — checker must INFER nothrow "
		f"from the body; got rc={rc} "
		f"diagnostics={[d.get('message') for d in errors]}"
	)


def test_c2_callback2_lambda_returns_result_err(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""C2 — Err-arm sibling: explicit `nothrow` annotation +
	`Result::Err(AppErr(...))`.  Pre-fix, the declared-nothrow path
	fired the `lambda is declared nothrow but may throw` diagnostic
	because the body's variant-ctor never resolved (parser-side
	alias was kept literal; `resolve_qualified_member_call`
	returned None; `_lambda_can_throw` defaulted to may-throw).
	Post-fix this compiles cleanly.  Both Ok and Err arms exercise
	the same alias-resolution path; pinning both ensures arm-symmetric
	coverage."""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, _C2_SOURCE)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"bare lambda body returning core.Result::Err(AppErr(...)) "
		f"must compile against Callback2 nothrow; got rc={rc} "
		f"diagnostics={[d.get('message') for d in errors]}"
	)


def test_c3_callback3_lambda_returns_result_ok(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""C3 — arity 3 (the actual web-rest middleware shape), bare
	lambda WITHOUT `nothrow` annotation.  Same dispatch path
	through the central `_CALLBACK_ROWS` table extended in 0.31.21.
	Pins arity-coverage of the alias-resolution fix and inference
	on the concrete middleware shape."""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, _C3_SOURCE)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"arity 3 bare lambda (no nothrow annotation) + "
		f"Result::Ok(...) must compile; checker must INFER nothrow "
		f"from the body; got rc={rc} "
		f"diagnostics={[d.get('message') for d in errors]}"
	)


def test_c4_named_fn_control_already_works(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""C4 — control: same body lifted into a named nothrow fn +
	`core.callback2(handler)` already works pre-fix.  Pinned here
	so a future regression that breaks the named-fn path too is
	caught alongside the lambda fix.  The defect must be specific
	to the lambda body resolution path."""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, _C4_NAMED_FN_SOURCE)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"named-fn control must compile (it's the existing "
		f"workaround that proves the language supports the body); "
		f"got rc={rc} diagnostics={[d.get('message') for d in errors]}"
	)


def test_c5_no_spurious_type_args_on_resp(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""C5 — pretty-printer pin.  Non-generic `Resp` / `AppErr` must
	NEVER be rendered as `Resp<Int>` / `AppErr<Int>` (or any
	`<...>`) in any diagnostic.  This is a SECONDARY bug, distinct
	from the primary parser-alias-resolution fix that the rest of
	this file pins:
	`type_checker.py::_pretty_type_name` renders `td.param_types`
	as type-args unconditionally, but for STRUCT TypeDescriptors
	`param_types` holds field types (not type args), so a
	non-generic `struct Resp { status: Int }` ends up rendered
	`Resp<Int>`.

	Pre-fix this rendering appeared in the
	`lambda can throw but is expected to be nothrow for ...` line
	produced by the primary bug, which made the report look like
	"spurious type-arg synthesis on Resp" when it was actually two
	independent issues.  Post-fix the C1 source compiles cleanly
	(no diagnostics at all), so the assertion is currently
	vacuously satisfied — but it stays in place to catch the
	pretty-printer bug if it ever surfaces on a similar shape
	again, and to pin the fact that no fix should "paper over" by
	accepting `Resp<Int>` as a valid spelling of `Resp`.
	"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, _C1_SOURCE)
	all_msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	bad_renderings = [
		m for m in all_msgs
		if ("Resp<" in m and "Resp<>" not in m)
		or ("AppErr<" in m and "AppErr<>" not in m)
	]
	assert not bad_renderings, (
		f"non-generic Resp / AppErr must not be rendered with "
		f"type-arg syntax in any diagnostic (separate "
		f"`_pretty_type_name` STRUCT-vs-VARIANT bug; field types "
		f"are not type args).  Found: {bad_renderings}"
	)
