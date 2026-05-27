# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression: a binder introduced by a match-arm pattern
must be capturable by a closure defined in the arm body, with the
binder's identity (binding_id) preserved across the alpha-rename
that `lower_match` applies to make arm binders function-scope
unique.

Reported by the PushCoin bookkeeper team (filed 2026-05-27 at
`pushcoin/work/drift-match-arm-capture-bug.md`).  Minimal shape:

    match move opt {
        Some(worker_tx) => {
            val cb = core.callback0(| | captures(move worker_tx) ... => {
                ... worker_tx ...
            });
            ...
        },
        None => { ... }
    }

Pre-fix the compiler emits `unknown name 'worker_tx'` at the body's
reference, even though `worker_tx` is the arm binder used in
`captures(move worker_tx)`.

Two stacked structural gaps caused the failure; the accepted fix
closes both:

  1.  `_rename_expr` in `lang/driftc/stage1/ast_to_hir.py` had no
      `HLambda` branch, so explicit_captures.name and body HVars
      inside a lambda were not alpha-renamed when the enclosing
      arm's binder was rewritten to `__match_binder_<N>_<source>`.

  2.  Match-arm binders had no first-class HIR binding identity.
      `HMatchArm` now carries `binder_ids: list[BindingId]` parallel
      to `binders`.  `lower_match` (and the `for`-desugaring at
      `_visit_stmt_ForStmt`) push a scope and allocate the binder
      IDs BEFORE lowering the arm block, so nested lambdas pick up
      the correct `binding_id` for captures, body references, and
      the synthesized `share_value`.  The type-checker reuses those
      persisted IDs on arm entry rather than allocating fresh ones.
      HIR reconstructors (`borrow_materialize`, `place_canonicalize`,
      `_rename_expr`'s HMatchExpr rebuild) preserve `binder_ids`,
      and the binding-id max scans in `type_checker.py` and
      `stage1/normalize.py` include `arm.binder_ids` so the
      type-checker's local-id counter cannot collide with a
      persisted arm binder.

The test gate below pins the primary repro, the `share` capture
shape (which exercises lower-time `share_value` synthesis against
the persisted binder), the `for`-loop desugared shape (untyped and
typed iter binder), positive lambda-param/local shadowing controls,
and a negative uncaptured-outer-binding control.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


_SOURCE = """\
module main;
import std.core as core;

pub struct Box {
\tpub v: Int
}

pub fn main() nothrow -> Int {
\tval opt: Optional<Box> = Optional<type Box>::Some(Box(v = 7));
\tmatch move opt {
\t\tSome(worker_tx) => {
\t\t\tval cb: core.Callback0<Int> = core.callback0(
\t\t\t\t| | captures(move worker_tx) nothrow => {
\t\t\t\t\treturn worker_tx.v;
\t\t\t\t}
\t\t\t);
\t\t\treturn cb.call();
\t\t},
\t\tNone => { return 1; },
\t\tdefault => { return 2; }
\t}
}
"""


def _compile(tmp_path: Path) -> tuple[int, str]:
	src = tmp_path / "main.drift"
	src.write_text(_SOURCE)
	out = tmp_path / "repro"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	return res.returncode, res.stderr


def _compile_source(tmp_path: Path, source: str, *, stem: str = "main") -> tuple[int, str, Path]:
	src = tmp_path / f"{stem}.drift"
	src.write_text(source)
	out = tmp_path / f"{stem}_repro"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	return res.returncode, res.stderr, out


def test_match_arm_binder_capturable_by_inner_lambda(tmp_path: Path) -> None:
	"""Pre-fix: this compile fails with
	`unknown name 'worker_tx'` at the lambda body's reference.
	Post-fix: compile succeeds and the binary returns 7."""
	rc, stderr = _compile(tmp_path)
	assert rc == 0, (
		f"compile failed (rc={rc}) — match-arm binder not visible "
		f"to inner lambda's captures/body.\n"
		f"stderr: {stderr[:800]}"
	)
	out = tmp_path / "repro"
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 7, (
		f"binary returned {run.returncode}, expected 7 (the captured "
		f"Box.v value).\nstderr: {run.stderr[-400:]}"
	)


_SHARE_CAPTURE_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

struct Box {
\tv: Int
}

implement Box {
\tpub fn read(self: &Box) nothrow -> Int { return self.v; }
}

fn main() nothrow -> Int {
\tval app = conc.arc(Box(v = 11));
\tval opt = Optional<type conc.Arc<Box>>::Some(app);
\tmatch opt {
\t\tSome(b) => {
\t\t\tval cb: core.Callback0<Int> = core.callback0(| | captures(share b) nothrow => {
\t\t\t\tval inner = b.get();
\t\t\t\treturn inner.read();
\t\t\t});
\t\t\treturn cb.call();
\t\t},
\t\tNone => { return 1; },
\t\tdefault => { return 2; }
\t}
}
"""


def test_match_arm_binder_share_capture(tmp_path: Path) -> None:
	"""`captures(share arm_binder)` must resolve to the persistent
	arm binder ID at lower time so the synthesized share_value sees
	the same binding identity that arm-body HVars see."""
	rc, stderr, out = _compile_source(tmp_path, _SHARE_CAPTURE_SOURCE, stem="share")
	assert rc == 0, f"share-capture compile failed: rc={rc}\n{stderr[:800]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 11, (
		f"share-capture binary returned {run.returncode}, expected 11."
	)


_FOR_BINDER_CAPTURE_SOURCE = """\
module main;
import std.core as core;

pub fn main() nothrow -> Int {
\tval xs = [3, 4, 5];
\tvar total: Int = 0;
\tfor item in xs {
\t\tval cb: core.Callback0<Int> = core.callback0(
\t\t\t| | captures(copy item) nothrow => {
\t\t\t\treturn *item;
\t\t\t}
\t\t);
\t\ttotal = total + cb.call();
\t}
\treturn total;
}
"""


def test_for_binder_capturable_by_inner_lambda(tmp_path: Path) -> None:
	"""`for item in xs { val cb = callback0(|| captures(move item) ...) }`
	must work — the `for` desugaring synthesizes a `Some(item)`
	HMatchArm and so must apply the same persistent binder_ids
	discipline as user-written match arms."""
	rc, stderr, out = _compile_source(tmp_path, _FOR_BINDER_CAPTURE_SOURCE, stem="for_capture")
	assert rc == 0, f"for-binder-capture compile failed: rc={rc}\n{stderr[:800]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 12, (
		f"for-binder-capture returned {run.returncode}, expected 12 (3+4+5)."
	)


_FOR_TYPED_BINDER_CAPTURE_SOURCE = """\
module main;
import std.core as core;

pub fn main() nothrow -> Int {
\tval xs = [3, 4, 5];
\tvar total: Int = 0;
\tfor Int item in xs {
\t\tval cb: core.Callback0<Int> = core.callback0(
\t\t\t| | captures(copy item) nothrow => {
\t\t\t\treturn item;
\t\t\t}
\t\t);
\t\ttotal = total + cb.call();
\t}
\treturn total;
}
"""


def test_typed_for_binder_capturable_by_inner_lambda(tmp_path: Path) -> None:
	"""`for item: T in xs { val cb = callback0(|| captures(...) ...) }`
	exercises the typed-iter branch in `_visit_stmt_ForStmt`, which
	allocates BOTH a synthetic `__for_item_N` HMatchArm binder ID
	AND a user-visible `item` decl-let binding ID inside the arm
	scope.  Inner lambdas reference the user-visible name and must
	resolve to the decl-let's binding — not the synthetic binder
	and not a stale outer-scope ID.  Verifies the typed-iter
	identity wiring."""
	rc, stderr, out = _compile_source(tmp_path, _FOR_TYPED_BINDER_CAPTURE_SOURCE, stem="for_typed_capture")
	assert rc == 0, f"typed-for-binder-capture compile failed: rc={rc}\n{stderr[:800]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 12, (
		f"typed-for-binder-capture returned {run.returncode}, expected 12 (3+4+5)."
	)


_LAMBDA_PARAM_SHADOW_SOURCE = """\
module main;
import std.core as core;

pub fn main() nothrow -> Int {
\tval opt: Optional<Int> = Optional<type Int>::Some(7);
\tmatch opt {
\t\tSome(v) => {
\t\t\tval cb: core.Callback1<Int, Int> = core.callback1(
\t\t\t\t| v: Int | captures() nothrow => {
\t\t\t\t\treturn v + 100;
\t\t\t\t}
\t\t\t);
\t\t\treturn cb.call(1);
\t\t},
\t\tNone => { return 1; },
\t\tdefault => { return 2; }
\t}
}
"""


def test_lambda_param_shadows_match_arm_binder(tmp_path: Path) -> None:
	"""Lambda parameter `v` must shadow the enclosing arm binder `v`
	inside the lambda body.  The lambda body's `v` refers to the
	lambda parameter (value 1), not the arm binder (value 7).
	Result: 1 + 100 = 101."""
	rc, stderr, out = _compile_source(tmp_path, _LAMBDA_PARAM_SHADOW_SOURCE, stem="param_shadow")
	assert rc == 0, f"param-shadow compile failed: rc={rc}\n{stderr[:800]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 101, (
		f"param-shadow returned {run.returncode}, expected 101."
	)


_LOCAL_SHADOW_SOURCE = """\
module main;

pub fn main() nothrow -> Int {
\tval opt: Optional<Int> = Optional<type Int>::Some(7);
\tmatch opt {
\t\tSome(v) => {
\t\t\tval v: Int = 99;
\t\t\treturn v;
\t\t},
\t\tNone => { return 1; },
\t\tdefault => { return 2; }
\t}
}
"""


def test_arm_body_local_shadows_arm_binder(tmp_path: Path) -> None:
	"""A `val v = ...` inside the arm body must shadow the arm
	binder `v` so the final `return v` produces the local's value
	(99), not the binder's (7).  Validates that allocating a
	persistent binder ID does not freeze the binder name against
	later same-name redeclarations inside the arm scope."""
	rc, stderr, out = _compile_source(tmp_path, _LOCAL_SHADOW_SOURCE, stem="local_shadow")
	assert rc == 0, f"local-shadow compile failed: rc={rc}\n{stderr[:800]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 99, (
		f"local-shadow returned {run.returncode}, expected 99."
	)


_UNCAPTURED_OUTER_SOURCE = """\
module main;
import std.core as core;

pub fn main() nothrow -> Int {
\tval outside: Int = 42;
\tval opt: Optional<Int> = Optional<type Int>::Some(7);
\tmatch opt {
\t\tSome(v) => {
\t\t\tval cb: core.Callback0<Int> = core.callback0(
\t\t\t\t| | captures() nothrow => {
\t\t\t\t\treturn outside;
\t\t\t\t}
\t\t\t);
\t\t\treturn cb.call();
\t\t},
\t\tNone => { return 1; },
\t\tdefault => { return 2; }
\t}
}
"""


_SHARE_CAPTURE_NON_SHARE_SOURCE = """\
module main;
import std.core as core;

pub struct Box {
\tpub v: Int
}

pub fn main() nothrow -> Int {
\tval opt: Optional<Box> = Optional<type Box>::Some(Box(v = 7));
\tmatch opt {
\t\tSome(b) => {
\t\t\tval cb: core.Callback0<Int> = core.callback0(
\t\t\t\t| | captures(share b) nothrow => {
\t\t\t\t\treturn b.v;
\t\t\t\t}
\t\t\t);
\t\t\treturn cb.call();
\t\t},
\t\tNone => { return 1; },
\t\tdefault => { return 2; }
\t}
}
"""


def test_match_arm_share_capture_diagnostic_hygiene(tmp_path: Path) -> None:
	"""Diagnostic-hygiene regression: `captures(share <arm_binder>)`
	on a Copy / non-Share type must emit
	`E-CAPTURE-SHARE-NOT-SHARE` *spelling the source binder name*,
	never the internal `__match_binder_<N>_<source>` form that
	`lower_match` synthesizes.  The diagnostic is routed through
	`user_facing_binding_name` (see
	`lang/driftc/checker/__init__.py:36` and
	`lang/driftc/stage1/hir_nodes.py` HExplicitCapture contract).
	The error message must reference 'b' (the source binder) and
	must NOT contain `__match_binder_`.
	"""
	rc, stderr, _ = _compile_source(tmp_path, _SHARE_CAPTURE_NON_SHARE_SOURCE, stem="share_neg")
	assert rc != 0, (
		"share-capture on a Copy/non-Share type unexpectedly compiled — "
		"E-CAPTURE-SHARE-NOT-SHARE has regressed.\n"
		f"stderr: {stderr[:400]}"
	)
	assert "__match_binder_" not in stderr, (
		"diagnostic leaks internal `__match_binder_<N>_<source>` name — "
		"route capture-name through `user_facing_binding_name`.\n"
		f"stderr: {stderr[:600]}"
	)
	assert "E-CAPTURE-SHARE-NOT-SHARE" in stderr, (
		f"expected E-CAPTURE-SHARE-NOT-SHARE diagnostic.\nstderr: {stderr[:600]}"
	)
	# Source binder spelling should appear in the suggested-capture hint.
	assert " b)" in stderr or "`b`" in stderr or "'b'" in stderr, (
		f"diagnostic does not mention the source binder name 'b'.\n"
		f"stderr: {stderr[:600]}"
	)


def test_uncaptured_outer_binding_rejected(tmp_path: Path) -> None:
	"""Negative control: a lambda with an empty `captures()` clause
	must NOT silently see an outer binding (`outside`) — the
	captures list is the closed-over set.  If the compiler accepts
	this it has regressed the capture-list-is-authoritative
	guarantee."""
	rc, stderr, out = _compile_source(tmp_path, _UNCAPTURED_OUTER_SOURCE, stem="uncaptured")
	assert rc != 0, (
		"uncaptured outer binding compiled — capture-list authority "
		"regressed.\n"
		f"stderr: {stderr[:400]}"
	)
