# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Positive regression: implicit ConstShare duplication into a generic
struct field (`parse.Token<K>`), pinned at the HIR level — plus the
move-required fallback for non-ConstShare kinds.

Context
-------
A token kind that owns only Arc-backed / `Frozen` data (e.g. a `String`
payload) proves `std.core.shareable.ConstShare`.  When `std.core` is
visible, copying such a value into `parse.Token<type K>(kind = k)` WITHOUT
`move` is accepted: the type-checker's Phase-5 pass rewrites the bare
binding-read `k` into `k.const_share()`.  This is the documented ConstShare
contract (`stdlib/std/core/shareable.drift`: implicit duplication needs no
`move`), NOT a Copy-resolution defect.

A kind whose payload is a non-ConstShare type (e.g. `Array<String>`, a
mutable container) does NOT prove ConstShare, so the same bare copy requires
an explicit `move` — and that requirement is the SAME with or without
`std.core` (the ConstShare path simply never engages).

Why HIR-level assertions
------------------------
A test that only checks `rc == 0` for the ConstShare case would also pass if
a future change *mis*-classified `WKind` as `Copy` (a bug) — that would
accept the copy for the wrong reason and emit no `const_share()` rewrite.  So
these tests capture the post-typecheck HIR of `wtok` (the typechecker mutates
it in place) and assert the *mechanism*: the constructor argument is an
`HMethodCall(method_name="const_share", origin="implicit_const_share")` that
replaced the bare `HVar`, or — for the negative/`move` cases — that no such
rewrite exists.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from lang.driftc import type_checker as _tc_mod
from lang.driftc.driftc import main as driftc_main


_TMPL = """
module main;

import std.parse as parse;
import std.source as source;
{extra}import std.core.cmp as cmp;

variant WKind {{ Word({field}), End }}

implement WKind {{
	fn _rank(self: &WKind) nothrow -> Int {{
		match *self {{ WKind::Word(_) => {{ return 0; }}, WKind::End => {{ return 1; }} }}
	}}
}}
implement cmp.Equatable for WKind {{
	pub fn eq(self: &WKind, other: &WKind) nothrow -> Bool {{ return self._rank() == other._rank(); }}
}}
implement parse.TokenKind for WKind {{
	pub fn describe(self: &WKind) nothrow -> String {{
		match *self {{ WKind::Word(_) => {{ return "w"; }}, WKind::End => {{ return "e"; }} }}
	}}
}}

fn span0() nothrow -> source.SourceSpan {{
	val p = source.pos_zero();
	return source.SourceSpan(source_id = "t", start = p, end = p);
}}

fn wtok(k: WKind) nothrow -> parse.Token<WKind> {{
	return parse.Token<type WKind>(kind = {expr}, span = span0());
}}

fn main() nothrow -> Int {{ val t = wtok({make}); return 0; }}
""".lstrip()

_CORE = "import std.core as core;\n"
_COPY_DIAG = "is not Copy"


def _walk(node, fn) -> None:
	"""Depth-first walk over an HIR node tree (HExpr/HStmt objects and
	their list-valued attributes)."""
	if node is None:
		return
	fn(node)
	if isinstance(node, (list, tuple)):
		for x in node:
			_walk(x, fn)
		return
	d = getattr(node, "__dict__", None)
	if not d:
		return
	for v in d.values():
		if isinstance(v, (list, tuple)):
			for x in v:
				if hasattr(x, "__dict__") or isinstance(x, (list, tuple)):
					_walk(x, fn)
		elif hasattr(v, "__dict__") and type(v).__name__.startswith("H"):
			_walk(v, fn)


def _implicit_const_share_receivers(hir) -> list[str | None]:
	"""For each implicit `const_share()` rewrite in `hir`, the name of the
	`HVar` found in its receiver subtree (the receiver is typically an
	auto-borrow `HBorrow` of that var)."""
	found: list[str | None] = []

	def visit(n) -> None:
		if (
			type(n).__name__ == "HMethodCall"
			and getattr(n, "method_name", None) == "const_share"
			and getattr(n, "origin", None) == "implicit_const_share"
		):
			names: list[str] = []

			def collect(x) -> None:
				if type(x).__name__ == "HVar" and getattr(x, "name", None) is not None:
					names.append(x.name)

			_walk(getattr(n, "receiver", None), collect)
			found.append(names[0] if names else None)

	_walk(hir, visit)
	return found


def _bare_var_ctor_args(hir, var_name: str) -> list[str]:
	"""Slots where a bare `HVar(var_name)` is still a by-value constructor/
	call argument (i.e. NOT rewritten / moved)."""
	slots: list[str] = []

	def visit(n) -> None:
		if type(n).__name__ not in ("HCall", "HMethodCall", "HInvoke", "HStructLit"):
			return
		d = getattr(n, "__dict__", {})
		for key in ("args", "kwargs"):
			for a in d.get(key) or []:
				val = getattr(a, "value", a)
				if type(val).__name__ == "HVar" and getattr(val, "name", None) == var_name:
					slots.append(key)

	_walk(hir, visit)
	return slots


def _compile_capture(tmp_path: Path, *, field: str, expr: str, make: str, extra: str):
	"""Compile via the real `--test-build-only` pipeline and return
	(rc, error_diagnostics, wtok_hir) — where `wtok_hir` is the
	post-typecheck HIR block for `wtok` (mutated in place by the
	typechecker, so it reflects the Phase-5 ConstShare rewrite)."""
	captured: dict[str, object] = {}
	orig = _tc_mod.TypeChecker.check_function

	def _patched(self, fn_id, hir, *a, **k):
		res = orig(self, fn_id, hir, *a, **k)
		captured[str(fn_id)] = hir
		return res

	_tc_mod.TypeChecker.check_function = _patched
	try:
		src = tmp_path / "main.drift"
		src.write_text(_TMPL.format(field=field, expr=expr, make=make, extra=extra), encoding="utf-8")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
			rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
		out = buf.getvalue()
		payload = json.loads(out) if out.strip() else {}
	finally:
		_tc_mod.TypeChecker.check_function = orig

	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	wtok_hir = next((h for fid, h in captured.items() if "wtok" in fid), None)
	return rc, errs, wtok_hir


def _assert_noncopy_typecheck_error(errs: list[dict]) -> None:
	"""The non-Copy copy must be rejected at the typecheck phase with an
	error-severity diagnostic (not merely some nonzero rc)."""
	match = [
		e for e in errs
		if _COPY_DIAG in e.get("message", "")
		and e.get("phase") == "typecheck"
		and e.get("severity") == "error"
	]
	assert match, f"expected a typecheck-phase, error-severity non-Copy diagnostic; got {errs}"


# ── ConstShare-eligible kind (String payload): implicit duplication ──


def test_string_kind_implicit_const_share_rewrite_into_generic_field(tmp_path) -> None:
	"""`std.core` visible + bare `kind = k`: compiles, and the HIR shows the
	bare `k` replaced by an implicit `const_share()` call (the mechanism),
	with no bare `HVar(k)` left as a constructor argument."""
	rc, errs, hir = _compile_capture(tmp_path, field="s: String", expr="k", make='WKind::Word("x")', extra=_CORE)
	assert rc == 0, f"implicit ConstShare duplication should compile; errs={errs}"
	assert hir is not None, "failed to capture wtok HIR"
	receivers = _implicit_const_share_receivers(hir)
	assert "k" in receivers, (
		f"expected an implicit const_share() rewrite of `k` in wtok's HIR; "
		f"found receivers={receivers}.  (rc==0 alone could be satisfied by a "
		f"wrong Copy classification — this asserts the ConstShare mechanism.)"
	)
	assert _bare_var_ctor_args(hir, "k") == [], "bare HVar(k) ctor arg should have been rewritten"


def test_string_kind_explicit_move_has_no_const_share_rewrite(tmp_path) -> None:
	"""Explicit `move k` compiles and does NOT synthesize a `const_share()`
	rewrite (the user's escape hatch is honored)."""
	rc, errs, hir = _compile_capture(tmp_path, field="s: String", expr="move k", make='WKind::Word("x")', extra=_CORE)
	assert rc == 0, f"explicit move should compile; errs={errs}"
	assert hir is not None
	assert _implicit_const_share_receivers(hir) == [], "move must not trigger implicit const_share"


# ── Non-ConstShare kind (Array<String> payload): move required ──


def test_non_constshare_kind_requires_move_no_rewrite(tmp_path) -> None:
	"""`Array<String>` payload does not prove ConstShare: bare `kind = k` is
	rejected at typecheck (no const_share rewrite, bare HVar remains), and
	explicit `move` fixes it."""
	rc, errs, hir = _compile_capture(tmp_path, field="parts: Array<String>", expr="k", make="WKind::Word([])", extra=_CORE)
	assert rc == 1, f"non-ConstShare bare copy must fail with the controlled diagnostic exit (rc==1); got rc={rc}, errs={errs}"
	_assert_noncopy_typecheck_error(errs)
	assert hir is not None
	assert _implicit_const_share_receivers(hir) == [], "non-ConstShare must NOT get a const_share rewrite"
	assert _bare_var_ctor_args(hir, "k") == ["args"], "bare HVar(k) ctor arg should remain (unrewritten)"

	rc_mv, errs_mv, _ = _compile_capture(tmp_path, field="parts: Array<String>", expr="move k", make="WKind::Word([])", extra=_CORE)
	assert rc_mv == 0, f"non-ConstShare with explicit move should compile; errs={errs_mv}"


def test_non_constshare_move_requirement_independent_of_std_core(tmp_path) -> None:
	"""The move requirement for a non-ConstShare kind is identical with and
	without `std.core` — both reject bare `k` with the SAME typecheck-phase,
	error-severity non-Copy diagnostic.  Pins that there is no
	import-dependent Copy behavior; the differential only ever came from
	ConstShare proving (which needs the trait in scope)."""
	rc_with, errs_with, _ = _compile_capture(tmp_path, field="parts: Array<String>", expr="k", make="WKind::Word([])", extra=_CORE)
	rc_without, errs_without, _ = _compile_capture(tmp_path, field="parts: Array<String>", expr="k", make="WKind::Word([])", extra="")
	assert rc_with == 1 and rc_without == 1, (
		f"both must fail with the controlled diagnostic exit (rc==1); with={rc_with}, without={rc_without}"
	)
	_assert_noncopy_typecheck_error(errs_with)
	_assert_noncopy_typecheck_error(errs_without)
