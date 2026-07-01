# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression: interprocedural borrowed-aggregate return
origin not enforced when the borrow is constructed via a method-call
auto-borrow of a local receiver.

Surfaced 2026-04-30 during Phase 1 of the exception-diagnostics-context
track (`work/exception-diagnostics-context/plan.md`) while validating
the borrow / lifetime hard gate for `containers.ReadOnlyMap<K, V, B>`.
The hard gate's central invariant — that a struct holding a `&T`
field cannot outlive its source — must hold across function
boundaries to be useful.

Pre-fix (0.31.40 and earlier): the MVP escape rule
(`borrowed aggregate return must derive from a reference parameter`)
caught explicit-borrow forms only:

    return view_of(&c);                 // rejected ✓
    return View<type T>(source = &c);   // rejected ✓

But the *method-call* form was incorrectly accepted (and produced a
runtime UAF, confirmed under valgrind):

    return c.view();                    // accepted (BUG)

**Root cause** (`type_checker.py:_borrowed_aggregate_origins`,
`:10465`): the function early-bailed on `HMethodCall` (`if not
isinstance(expr, H.HCall): return None`), so a method returning a
borrowed aggregate skipped the entire origin walk; the caller's
loop read `None` as "no constraint" and silently passed.

**Fix (0.31.41):** extend `_borrowed_aggregate_origins` to handle
`HMethodCall` by treating the method's receiver as the borrow
source.  `_return_origin`'s existing recursion through HMethodCall
to receiver chains correctly: receiver mapping to a ref-param →
origin returned; receiver mapping to a local → `None` → empty
origin set → caller emits the MVP-escape diagnostic.

**Tests in this file** pin both halves of the rule's full domain
(method-call AND explicit-borrow) plus the positive control
(method on ref-param).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[str]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d.get("message", "") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


_PRE = """
module main;

pub struct Container<T> {
	pub x: T
}

pub struct View<T> {
	source: &Container<T>
}

pub struct ViewMut<T> {
	source: &mut Container<T>
}

pub struct Holder<T> {
	pub inner: Container<T>
}

implement<T> Container<T> {
	pub fn view(self: &Container<T>) nothrow -> View<T> {
		return View<type T>(source = self);
	}

	pub fn view_mut(self: &mut Container<T>) nothrow -> ViewMut<T> {
		return ViewMut<type T>(source = self);
	}
}

implement<T> Holder<T> {
	pub fn inner_ref(self: &Holder<T>) nothrow -> &Container<T> {
		return &self.inner;
	}
}

pub fn view_of<T>(c: &Container<T>) nothrow -> View<T> {
	return View<type T>(source = c);
}
"""


# ── Primary regression: method-call on local receiver REJECTS (post-fix) ───


def test_method_call_returns_view_of_local_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""LANGUAGE_BUG fix regression (0.31.41).

	`c.view()` where `c` is a local `Container<T>` and the result is
	returned must be rejected by the MVP escape rule — the receiver
	auto-borrow is equivalent to an explicit `&c` of a local, which
	the rule already rejects in the explicit-borrow forms.

	Pre-0.31.41 (BUG): rc == 0 (incorrectly accepted).  Runtime UAF
	confirmed under valgrind.
	Post-0.31.41 (fix): rc != 0 with the
	`borrowed aggregate return must derive from a reference
	parameter` diagnostic.
	"""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn make_dangling() nothrow -> View<Int> {
\tvar c = Container<type Int>(x = 99);
\treturn c.view();
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc != 0, (
		f"method-call auto-borrow of local receiver returning a "
		f"borrowed aggregate must be rejected (was the BUG carrier "
		f"pre-0.31.41); errs={errs}"
	)
	assert any("borrowed aggregate return" in m or "MVP escape rule" in m for m in errs), (
		f"expected MVP-escape diagnostic; got: {errs}"
	)


# ── Positive control: method on ref-param ACCEPTS ─────────────────


def test_method_call_returns_view_of_ref_param_accepts(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Positive control.  When the receiver is a `&Container<T>`
	parameter (not a local), `c.view()` returning `View<T>` is the
	legitimate borrowed-aggregate-from-ref-param shape — must
	accept.  Pinned to ensure the fix doesn't over-reject the
	correct case."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn ok_through_ref_param(c: &Container<Int>) nothrow -> View<Int> {
\treturn c.view();
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc == 0, f"method-call on ref-param receiver must accept; errs={errs}"
	assert not errs, f"unexpected diagnostics on positive control: {errs}"


# ── Controls: explicit-borrow forms keep rejecting ────────────────


def test_explicit_borrow_return_of_local_correctly_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Control: free-function form `view_of(&local)` returning a
	borrowed-aggregate must keep rejecting (the explicit-borrow path
	the rule already handles).  Pinned so the fix doesn't
	accidentally weaken this.
	"""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn make_dangling_via_free_fn() nothrow -> View<Int> {
\tvar c = Container<type Int>(x = 99);
\treturn view_of(&c);
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc != 0, "MVP escape rule must reject return of explicit-borrow-of-local"
	assert any("borrowed aggregate return" in m or "MVP escape rule" in m for m in errs), (
		f"expected MVP-escape diagnostic; got: {errs}"
	)


def test_explicit_field_init_borrow_of_local_correctly_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Control: direct-field-init form `View(source = &local)` must
	keep rejecting.  Same reason as the free-function control — pins
	the rule's existing coverage."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn make_dangling_via_field_init() nothrow -> View<Int> {
\tvar c = Container<type Int>(x = 99);
\treturn View<type Int>(source = &c);
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc != 0, "MVP escape rule must reject direct field-init of borrowed-aggregate from local"
	assert any("borrowed aggregate return" in m or "MVP escape rule" in m for m in errs), (
		f"expected MVP-escape diagnostic; got: {errs}"
	)


# ── &mut receiver — same rule applies ─────────────────────────────


def test_mut_method_call_returns_view_of_local_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""`&mut` form of the primary regression.  `c.view_mut()` on a
	local `c` returns a `ViewMut<T>` that holds `&mut Container<T>`;
	the auto-`&mut`-borrow of the local receiver must be rejected.

	Either the bare MVP-escape diagnostic OR the more specific
	`mutable references must derive from an &mut parameter` arm is
	an acceptable rejection — both correctly reject the program."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn dangling_mut() nothrow -> ViewMut<Int> {
\tvar c = Container<type Int>(x = 99);
\treturn c.view_mut();
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc != 0, (
		f"`&mut` method-call auto-borrow of local receiver returning "
		f"a borrowed aggregate must be rejected; errs={errs}"
	)
	assert any(
		"borrowed aggregate return" in m
		or "MVP escape rule" in m
		or "mutable references" in m
		or "&mut parameter" in m
		for m in errs
	), f"expected MVP-escape or &mut-origin diagnostic; got: {errs}"


# ── Chained method receiver — recursion must trace to ultimate origin ──


def test_chained_method_call_with_local_root_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Chained method calls: `a.inner_ref().view()` where `a` is a
	local `Holder<T>`, `a.inner_ref()` returns `&Container<T>`
	(borrowed from `a`), and `view()` returns `View<T>` borrowed from
	that `&Container<T>`.  The ultimate borrow origin is local `a`
	→ must reject.

	Validates that `_return_origin`'s existing HMethodCall recursion
	through the receiver correctly traces back to the local."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn dangling_chain() nothrow -> View<Int> {
\tvar a = Holder<type Int>(inner = Container<type Int>(x = 99));
\treturn a.inner_ref().view();
}
pub fn main() nothrow -> Int { return 0; }
""")
	assert rc != 0, (
		f"chained method receiver tracing to local must be rejected; "
		f"errs={errs}"
	)
	assert any("borrowed aggregate return" in m or "MVP escape rule" in m for m in errs), (
		f"expected MVP-escape diagnostic; got: {errs}"
	)
