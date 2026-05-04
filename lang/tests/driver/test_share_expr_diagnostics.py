# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Checker-level diagnostics for the expression-form `share x` introduced
in 0.31.20.  Symmetric with `captures(share x)` at lambda capture sites
(see `test_share_capture_diagnostics.py`); same Share-trait constraint,
mirrored "Copy → use copy x" / "non-Share → use move x" diagnostic
shape, dedicated `E-SHARE-EXPR-NOT-SHARE` code so telemetry can
distinguish call-site share from capture-site share.

Carriers cover the spec from the 0.31.20 feature request:

  P1. `consume(share app)` for `Arc<T>` + use of `app` after.
  P2. `_serve(share app, port)` + catch-arm uses `app` (bookkeeper shape).
  P3. Left-to-right evaluation order: `share app` is not pre-hoisted.
  P4. **Borrow-survives-throws invariant** (critical per the revised
      spec): a reference taken before the try (`val r = x.get();`) is
      VALID in the catch arm of `try { f(share x); } where f throws`.
      `share` is a refcount bump on the owner, not a mutation of the
      binding, so references into `*x` stay pointing at live memory
      through both the call and the unwind path.  This is the second
      ergonomic gain over `move x` — the bookkeeper case can drop the
      redundant `.get()` after the share, not just the named clone.
  P5. No-double-share: `share x.clone()` (or any explicit
      Share-trait method call) inside `share` doesn't compound — the
      grammar restricts subjects to NAMEs, so this surfaces as the
      same N6 "subject must be a local binding" diagnostic, not as
      silent double-refcount.
  N4. `share s` for `String` fails (String is not Share).
  N5. `share i` for `Int` fails with the Copy-spirit "use copy x" hint.
  N6. Non-NAME subject (`share f()`, `share x.field`, ...) emits a clear
      "share x: subject must be a local binding" diagnostic, NOT the
      pre-feature parser-level "expected comma between match arms"
      misleading error.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main

ROOT = Path(__file__).resolve().parents[3]


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


# ── Positive carriers (P1, P2, P3) ─────────────────────────────────────


def test_share_expr_arc_call_arg_keeps_caller_owner(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P1 — `consume(share app)` for `Arc<T>`: `app` remains LIVE after
	the call so the body below can dereference it."""
	source = """
module main;

import std.core as core;
import std.concurrent as conc;

struct App { tag: Int }

fn consume(a: conc.Arc<App>) nothrow -> Int {
	val r = a.get();
	return r.tag;
}

fn main() nothrow -> Int {
	val app = conc.arc(App(tag = 7));
	val first = consume(share app);
	val r = app.get();
	return first + r.tag;
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc == 0, (
		f"`consume(share app)` for Arc must compile and leave `app` LIVE "
		f"for the trailing `app.get()`. Diagnostics: "
		f"{[d.get('message') for d in payload.get('diagnostics', [])]}"
	)


def test_share_expr_bookkeeper_serve_catch_shape(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P2 — bookkeeper-motivated shape: `_serve(share app, port)` plus a
	catch arm that still references `app`.  The exact pattern that
	motivated the 0.31.20 feature request.  Eliminates the named
	`var log_app = app.clone();` keepalive that the pre-feature
	workaround required."""
	source = """
module main;

import std.core as core;
import std.concurrent as conc;

error Boom { message: String }
struct App { tag: Int }

fn _serve(a: conc.Arc<App>, port: Int) -> Int {
	throw Boom(message = "startup failed");
}

fn main() nothrow -> Int {
	val app = conc.arc(App(tag = 7));
	val port = 8080;
	try {
		val _ = _serve(share app, port);
		return 0;
	} catch e {
		val r = app.get();
		return 100 + r.tag;
	}
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc == 0, (
		f"bookkeeper-shaped `_serve(share app, port)` + catch arm using "
		f"`app` must compile cleanly. Diagnostics: "
		f"{[d.get('message') for d in payload.get('diagnostics', [])]}"
	)


def test_share_expr_left_to_right_eval_no_pre_hoist(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P3 — argument evaluation order: `share app` in the middle arg
	position must lower in place, not pre-hoist before earlier args.
	Compile-only check — the lowering at
	`stage1/ast_to_hir.py::_visit_expr_Share` produces an HCall in
	the source position, which `normalize.py` and the type checker
	walk in argument order; no pre-hoisting is introduced.

	The carrier exercises the shape "earlier-arg, share x, later-arg"
	to cover the surface where pre-hoisting would have surfaced as
	an out-of-order evaluation in the first place.  Runtime ordering
	is enforced by Drift's standard left-to-right argument
	evaluation rule and is not unique to share-expr.
	"""
	source = """
module main;

import std.concurrent as conc;

struct App { tag: Int }

fn side_effect_a() nothrow -> Int { return 10; }
fn side_effect_c() nothrow -> Int { return 100; }

fn take(first: Int, a: conc.Arc<App>, third: Int) nothrow -> Int {
	val r = a.get();
	return first + r.tag + third;
}

fn main() nothrow -> Int {
	val app = conc.arc(App(tag = 5));
	val total = take(side_effect_a(), share app, side_effect_c());
	if total != 115 { return -1; }
	return 0;
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc == 0, (
		f"left-to-right argument evaluation must hold across `share x`. "
		f"Diagnostics: {[d.get('message') for d in payload.get('diagnostics', [])]}"
	)


def test_share_expr_borrow_into_x_survives_throwing_call(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P4 — **borrow-survives-throws invariant** (critical per the
	0.31.20 revised spec).  A reference taken before the try
	(`val r = x.get();`) must remain VALID inside the catch arm of
	`try { f(share x); } where f throws`.

	Lowering must NOT mutate `x` (no MoveOut, no tombstone, no
	transient zero-write).  `share x` is a refcount bump on the
	owner, leaving `*x` and any outstanding borrows untouched
	through both the normal-return path and the unwind path.

	The bookkeeper-motivated source for this carrier is exactly the
	revised-spec example:

		val app_ref = app.get();
		try {
			_serve(share app, port);
			return 0;
		} catch e {
			app_ref.tag;  // must be valid; no re-.get() needed
			return 1;
		}
	"""
	source = """
module main;

import std.core as core;
import std.concurrent as conc;

error Boom { message: String }
struct App { tag: Int }

fn _serve(a: conc.Arc<App>, port: Int) -> Int {
	throw Boom(message = "startup failed");
}

fn main() nothrow -> Int {
	val app = conc.arc(App(tag = 42));
	val port = 8080;
	val app_ref = app.get();
	try {
		val _ = _serve(share app, port);
		return 0;
	} catch e {
		// app_ref must be VALID here — share x is a refcount bump,
		// NOT a move/mutation of the binding.  No re-.get() needed.
		return 100 + app_ref.tag;
	}
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc == 0, (
		f"borrow into shared owner must survive a throwing call's "
		f"unwind path. If this fails with a borrow-checker diagnostic, "
		f"the lowering is mutating the binding (move/tombstone/etc.) "
		f"instead of just bumping the refcount. Diagnostics: "
		f"{[d.get('message') for d in payload.get('diagnostics', [])]}"
	)


# ── Negative carriers (N4, N5, N6) ─────────────────────────────────────


def test_share_expr_of_string_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""N4 — `share s` for `String` must fail.

	String is classified `Copy` in Drift's type system (its
	internal representation is refcount-bumped under the hood, so
	"copying" a String is cheap).  Therefore the diagnostic must
	identify it as `Copy`, not `Share`, and suggest `copy s` —
	matching the existing capture-form behavior at
	`test_share_capture_diagnostics.py::test_share_capture_of_copy_type_suggests_copy`.

	The point of the carrier is the same regardless of which branch
	fires (Copy vs non-Share-non-Copy): `share s` is rejected with
	a clear, actionable diagnostic and not silently coerced."""
	source = """
module main;

fn consume(s: String) nothrow -> Int {
	return s.byte_length();
}

fn main() nothrow -> Int {
	val s = "hello";
	return consume(share s);
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc != 0, "compile should reject `share s` for String"
	diags = [d for d in payload.get("diagnostics", []) if "E-SHARE-EXPR" in d.get("code", "")]
	# Exactly one focused share-expr diagnostic — guards against the
	# pre-fix duplication where retry-typing emitted the same
	# diagnostic twice.
	assert len(diags) == 1, (
		f"expected exactly one E-SHARE-EXPR diagnostic, got {len(diags)}: "
		f"{[d.get('message') for d in diags]}"
	)
	d = diags[0]
	assert d.get("code") == "E-SHARE-EXPR-NOT-SHARE", d
	# String is Copy in Drift (refcount-bump shared-owner internally).
	assert "is `Copy`, not `Share`" in d.get("message", ""), d
	assert "copy s" in d.get("message", ""), d
	# Real source span — not file:'<source>', line:null, column:null.
	assert d.get("file") and d.get("file") != "<source>", d
	assert isinstance(d.get("line"), int) and d["line"] > 0, d
	assert isinstance(d.get("column"), int) and d["column"] > 0, d


def test_share_expr_of_copy_type_suggests_copy(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""N5 — `share i` for `Int` (Copy) must fail with the Copy-spirit
	"use copy x" hint (parallel to capture form's
	`E-CAPTURE-SHARE-NOT-SHARE` Copy branch).  `share` is reserved for
	genuine aliasing; value-spirit duplication uses `copy x`."""
	source = """
module main;

fn consume(n: Int) nothrow -> Int {
	return n + 1;
}

fn main() nothrow -> Int {
	val i: Int = 5;
	return consume(share i);
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc != 0, "compile should reject `share i` for Copy type"
	diags = [d for d in payload.get("diagnostics", []) if "E-SHARE-EXPR" in d.get("code", "")]
	assert len(diags) == 1, (
		f"expected exactly one E-SHARE-EXPR diagnostic, got {len(diags)}: "
		f"{[d.get('message') for d in diags]}"
	)
	d = diags[0]
	assert d.get("code") == "E-SHARE-EXPR-NOT-SHARE", d
	assert "is `Copy`, not `Share`" in d.get("message", ""), d
	assert "copy i" in d.get("message", ""), d
	assert d.get("file") and d.get("file") != "<source>", d
	assert isinstance(d.get("line"), int) and d["line"] > 0, d
	assert isinstance(d.get("column"), int) and d["column"] > 0, d


def test_share_expr_non_name_subject_clear_diagnostic(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""N6 — `share <non-NAME>` (e.g. `share f()`, `share x.field`) must
	emit a clear "share x: subject must be a local binding" checker
	diagnostic, NOT the pre-feature parser error
	`E_EXPECTED_COMMA_BETWEEN_MATCH_ARMS` that initially surfaced when
	the parser treated `share` as a NAME identifier in expression
	position.

	This is a deliberate v1 restriction parallel to the capture form's
	`SHARE NAME` rule — only direct local bindings are shareable.
	If a user wants `share x.field` they should bind `val a = x.field;
	share a;` first.
	"""
	source = """
module main;

import std.concurrent as conc;

struct App { tag: Int }

fn make_app() nothrow -> conc.Arc<App> {
	return conc.arc(App(tag = 1));
}

fn consume(a: conc.Arc<App>) nothrow -> Int {
	return a.get().tag;
}

fn main() nothrow -> Int {
	return consume(share make_app());
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc != 0, "compile should reject `share <non-NAME>`"
	all_msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	# Must NOT be the misleading pre-feature parser error.
	assert not any("E_EXPECTED_COMMA_BETWEEN_MATCH_ARMS" in m for m in all_msgs), (
		f"diagnostic must not be the misleading parser error; got: {all_msgs}"
	)
	assert not any("Unexpected token" in m for m in all_msgs), (
		f"diagnostic must not be a raw parser 'Unexpected token' error; got: {all_msgs}"
	)
	# Exactly one focused share-expr diagnostic, with real span and the
	# SUBJECT-NOT-LOCAL code.
	diags = [d for d in payload.get("diagnostics", []) if "E-SHARE-EXPR" in d.get("code", "")]
	assert len(diags) == 1, (
		f"expected exactly one E-SHARE-EXPR diagnostic, got {len(diags)}: "
		f"{[d.get('message') for d in diags]}"
	)
	d = diags[0]
	assert d.get("code") == "E-SHARE-EXPR-SUBJECT-NOT-LOCAL", d
	assert d.get("file") and d.get("file") != "<source>", d
	assert isinstance(d.get("line"), int) and d["line"] > 0, d
	assert isinstance(d.get("column"), int) and d["column"] > 0, d
	# Sanity: message is the clear checker-level form.
	assert any(
		"share" in m.lower() and ("local binding" in m.lower() or "must be a name" in m.lower() or "must be a local" in m.lower())
		for m in all_msgs
	), (
		f"expected a clear `share x: subject must be a local binding` "
		f"diagnostic; got: {all_msgs}"
	)


# ── Runtime carriers ───────────────────────────────────────────────────


def _compile_and_run(tmp_path: Path, source: str, *, name: str = "share_eval_order") -> int:
	"""Compile a Drift source under raw stdlib and run it.  Returns
	the binary's exit code.  Fails the test if compile fails."""
	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / name
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[:1500]}"
	assert out_bin.exists()
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=60)
	return run.returncode


def test_share_expr_runtime_eval_order_no_pre_hoist(tmp_path: Path) -> None:
	"""**P3-runtime** — pin that `share x` actually evaluates IN PLACE
	at runtime, not pre-hoisted before earlier args.  Compiles a
	user-defined `Tracker` whose `Share::share` records its
	execution position into a shared counter via a positional-encoding
	scheme:

	    counter ← (counter * 10) + tag

	Sequence (1, 2, 3) yields counter == 123.  In `take(record(1),
	share tracker, record(3))` we expect the share-tick to land in
	position 2, producing counter == 123.  Any pre-hoisting of
	`Share::share(&tracker)` before arg #0 would land share-tick
	first (counter goes 0 → 2 → 21 → 213 — observably wrong).

	Runtime carrier so that the eval-order guarantee is observed,
	not just relied on as a property of the lowering shape.
	"""
	# Note: this source uses `arc.clone()` instead of
	# `Share::share(&arc)` for cloning the Arc<AtomicInt> field
	# inside Tracker's Share impl.  Both lower to the same
	# refcount-bump operation; `Share::share` would require an
	# unrelated grammar relaxation (`qualified_member` post-`::`
	# accepts NAME only — SHARE/MOVE/COPY are keywords now and
	# `Share::share` literal does not parse).  That's a pre-existing
	# inconsistency vs `ident: NAME|MOVE|COPY|SHARE`, out of scope
	# for the share-expr feature.
	source = """\
module main;

import std.core.shareable as shareable;
import std.concurrent as conc;
import std.sync as sync;

struct Tracker {
	tag: Int,
	counter: conc.Arc<sync.AtomicInt>
}

implement shareable.Share for Tracker {
	pub fn share(self: &Tracker) nothrow -> Tracker {
		val a = self.counter.get();
		val cur = a.load(sync.MemoryOrder::Relaxed());
		a.store(cur * 10 + self.tag, sync.MemoryOrder::Relaxed());
		return Tracker(tag = self.tag, counter = self.counter.clone());
	}
}

fn record(a: &sync.AtomicInt, tag: Int) nothrow -> Int {
	val cur = a.load(sync.MemoryOrder::Relaxed());
	a.store(cur * 10 + tag, sync.MemoryOrder::Relaxed());
	return tag;
}

fn take(first: Int, t: Tracker, third: Int) nothrow -> Int {
	return first * 100 + third;
}

pub fn main() nothrow -> Int {
	val counter = conc.arc(sync.atomic_int(0));
	val tracker = Tracker(tag = 2, counter = counter.clone());
	val a = counter.get();
	val total = take(record(a, 1), share tracker, record(a, 3));
	val final = a.load(sync.MemoryOrder::Relaxed());
	// Expected: tick sequence 1, 2 (share), 3 → counter == 123.
	if final != 123 { return 50 + (final - 100); }
	if total != 103 { return 60; }
	return 0;
}
"""
	rc = _compile_and_run(tmp_path, source)
	assert rc == 0, (
		f"share-expr eval-order failed: exit={rc}.\n"
		f"Expected counter == 123 (tick order: arg#0=1, share=2, arg#2=3)\n"
		f"  exit 50..59 → counter had wrong value (50 + delta from 100)\n"
		f"  exit 60     → take()'s arithmetic was wrong (args mis-ordered upstream)\n"
		f"If `share x` were pre-hoisted before arg #0, the counter would land "
		f"at 213 or similar; the test's exit-code arithmetic exposes which "
		f"order actually fired."
	)
