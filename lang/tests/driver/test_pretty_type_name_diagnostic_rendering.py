# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pretty-printer regression: `_pretty_type_name` must render INTERFACE,
STRUCT, and VARIANT TypeIds with their *instance* type-args, not with
the wrong-shape `td.param_types` payload (which holds field types for
STRUCT, parameter types for FUNCTION, etc.).

Surfaced 2026-04-29 as a follow-up to the 0.31.30 fix for the
bookkeeper / web-rest middleware report.  The user-facing diagnostic
on a throws-mismatch in a bare-lambda Callback3 read

    lambda can throw but is expected to be nothrow for
    Fn(Ref<web.rest.request.Request<String, String, String,
       Array<String>, Array<String>, Array<String>, Array<String>,
       Array<String>, Array<String>, Int,
       std.json.JsonHandle<std.concurrent.Arc>>>,
       RefMut<web.rest.context.Context<...>>,
       std.core.Callback2)
    nothrow -> std.core.Result<...>

with two distinct rendering bugs collapsed into one message:

  R1. INTERFACE in FUNCTION param-type position rendered with no
      type-args at all (`std.core.Callback2` instead of
      `Callback2<&Req, &mut Ctx, Result<Resp, AppErr>>`).
  R2. STRUCT rendered with its field types in `<...>` instead of its
      instance type-args (`Request<String, String, String, ...>`
      where `Request` is non-generic and the visible "type-args" are
      actually the eleven field types).

Both stem from `lang/driftc/type_checker.py::_pretty_type_name` (lines
~301-323) reading `td.param_types` unconditionally as "type-args".
For STRUCT TypeDescriptors that's `field types`; for INTERFACE that's
typically empty (instance type-args live in `interface_instances`); for
VARIANT it can also disagree with the instance's `type_args`.

Cosmetic, no codegen impact, but actively misleading: the cascade in
the original Bug B report looked worse than reality and contributed to
the misdiagnosis.

Pin: render INTERFACE / STRUCT / VARIANT type-args from the matching
instance map; render REF / RAW_PTR / ARRAY / FUNCTION via
`td.param_types` (those still use it correctly).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> dict:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	argv = ["--stdlib-root", "stdlib", "--test-build-only", str(src)]
	_rc, payload = _run_driftc_json(argv, capsys)
	return payload


# Trigger the load-bearing diagnostic ("lambda can throw but is expected
# to be nothrow for Fn(...) nothrow -> ...") with an expected_type that
# embeds *both* a struct and a nested Callback iface in its parameter
# list.  The pretty-printer's INTERFACE-and-STRUCT branches both fire
# in this single rendering, so the test pins both bugs with one
# fixture.  The lambda body's `or_throw` injection is what guarantees
# the diagnostic fires; the precise cause of the throw is irrelevant
# to the rendering we're pinning.
_FIXTURE = """
module main;

import std.core as core;

struct Req { pub method: String }
struct Ctx { pub idx: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

// `maybe(...)` is throws-by-default → calling it inside a nothrow
// lambda triggers `or_throw` injection → throws-mismatch diagnostic.
fn maybe(n: Int) -> core.Result<Resp, AppErr> {
\treturn core.Result::Ok(Resp(status = n));
}

fn register(slot: &mut Array<core.Callback3<&Req, &mut Ctx,
\t        core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
\t        core.Result<Resp, AppErr>>>,
\t        cb: core.Callback3<&Req, &mut Ctx,
\t        core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
\t        core.Result<Resp, AppErr>>) nothrow -> Void {
\tslot.push(move cb);
\treturn core.void_value();
}

fn main() nothrow -> Int {
\tvar slot: Array<core.Callback3<&Req, &mut Ctx,
\t    core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>,
\t    core.Result<Resp, AppErr>>> = [];
\tregister(&mut slot, |req, ctx, next| => {
\t\tval r = maybe(req.idx);
\t\treturn next.call(req, ctx);
\t});
\treturn 0;
}
"""


def _throws_diagnostic(payload: dict) -> str:
	"""Pull the load-bearing 'lambda can throw but is expected to be
	nothrow for ...' message — the one that exercises the broken
	rendering."""
	for d in payload.get("diagnostics", []):
		if d.get("severity") != "error":
			continue
		msg = d.get("message", "")
		if "lambda can throw but is expected to be nothrow for" in msg:
			return msg
	pytest.fail(
		"expected the throws-mismatch diagnostic to fire; got: "
		+ "\n  - ".join(
			d.get("message", "") for d in payload.get("diagnostics", [])
			if d.get("severity") == "error"
		)
	)


def test_interface_in_function_param_renders_type_args(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""R1.  An INTERFACE TypeId in FUNCTION param-type position must
	render with its instance type-args.  In the throws diagnostic,
	the third lambda parameter is a `Callback2<&Req, &mut Ctx,
	Result<Resp, AppErr>>`; pre-fix it rendered as bare
	`std.core.Callback2`."""
	payload = _compile(tmp_path, capsys, _FIXTURE)
	msg = _throws_diagnostic(payload)
	# Pre-fix, the message contained ", std.core.Callback2)" or
	# ", Callback2)" with no `<...>` after.  Post-fix, it must render
	# the full instance with all three type-args in order, including
	# the nested Result inner type.
	assert "Callback2<Ref<Req>, RefMut<Ctx>, std.core.Result<Resp, AppErr>>" in msg, (
		f"INTERFACE in FUNCTION param-type position must render with "
		f"its full instance type-args (Ref<Req>, RefMut<Ctx>, "
		f"Result<Resp, AppErr>) in order; got message:\n  {msg}"
	)
	assert ", std.core.Callback2)" not in msg and ", Callback2)" not in msg, (
		f"bare `Callback2` (no type-args) must not appear as a fn "
		f"param-type rendering; got:\n  {msg}"
	)


def test_struct_does_not_render_field_types_as_type_args(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""R2.  A non-generic STRUCT must not be rendered with its field
	types in `<...>`.  The struct's instance type-args list is empty
	(no type params); the renderer must not fall through to
	`td.param_types`, which for STRUCT holds field types.  Pre-fix:
	`Resp` rendered as `Resp<Int>` (because field 0 is `status: Int`),
	and `AppErr` as `AppErr<Int>`."""
	payload = _compile(tmp_path, capsys, _FIXTURE)
	msg = _throws_diagnostic(payload)
	# `Resp` and `AppErr` are non-generic; any `<...>` after them is
	# the rendering bug.
	assert "Resp<" not in msg, (
		f"non-generic struct `Resp` must not render with `<...>`; "
		f"struct field types are not type-args. Got message:\n  {msg}"
	)
	assert "AppErr<" not in msg, (
		f"non-generic struct `AppErr` must not render with `<...>`; "
		f"struct field types are not type-args. Got message:\n  {msg}"
	)
	assert "Req<" not in msg, (
		f"non-generic struct `Req` must not render with `<...>`; "
		f"got message:\n  {msg}"
	)
	assert "Ctx<" not in msg, (
		f"non-generic struct `Ctx` must not render with `<...>`; "
		f"got message:\n  {msg}"
	)


def _extract_balanced(msg: str, prefix: str) -> str | None:
	"""Return the substring `prefix<...>` from `msg` with balanced
	angle brackets, or None if `prefix<` not found.  Lets the variant
	pin assert *what's inside* the Result<...> rendering rather than
	just that the names appear somewhere in the message."""
	idx = msg.find(prefix + "<")
	if idx < 0:
		return None
	start = idx + len(prefix)
	depth = 0
	for i in range(start, len(msg)):
		ch = msg[i]
		if ch == "<":
			depth += 1
		elif ch == ">":
			depth -= 1
			if depth == 0:
				return msg[idx:i + 1]
	return None


def test_variant_renders_with_instance_type_args(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Generic VARIANT must render with its instance type-args.
	Tight pin: extract the `Result<...>` rendering by balanced-bracket
	scan and assert *both* `Resp` and `AppErr` appear inside that
	rendering specifically — not somewhere else in the message.  Also
	pin order (Ok arm first, Err arm second) by spelling the literal
	`Result<Resp, AppErr>` substring."""
	payload = _compile(tmp_path, capsys, _FIXTURE)
	msg = _throws_diagnostic(payload)
	# Order + literal-args pin: cheap and specific.
	assert "Result<Resp, AppErr>" in msg, (
		f"variant `Result<Resp, AppErr>` must render with both type-"
		f"args in order (Ok arm first, Err arm second); got:\n  {msg}"
	)
	# Independent pin via balanced extraction — guards against the
	# literal substring drifting (e.g. extra whitespace) while still
	# proving Resp / AppErr are *inside* the Result<...> rendering.
	rendered = _extract_balanced(msg, "Result")
	assert rendered is not None, (
		f"expected a `Result<...>` rendering in the diagnostic; "
		f"got:\n  {msg}"
	)
	inner = rendered[len("Result<"):-1]
	assert "Resp" in inner and "AppErr" in inner, (
		f"`Resp` and `AppErr` must appear inside the Result<...> "
		f"rendering, not elsewhere in the diagnostic; extracted "
		f"`{rendered}` from:\n  {msg}"
	)
