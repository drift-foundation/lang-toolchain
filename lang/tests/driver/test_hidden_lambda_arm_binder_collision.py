# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression: hidden-lambda capture ID allocation collides
with persisted match-arm binder IDs inside the lambda body.

Reported by the PushCoin bookkeeper team (filed 2026-05-27 at
`pushcoin/work/drift-0.33.4-mir-lowering-regression.md`).  Symptom:
0.33.4 ICEs on bookkeeper source that 0.33.3 compiled cleanly, with:

    error: internal: MIR lowering contract failure
    (unknown struct field reached MIR lowering (checker bug))
    [E-AUTO-faa62b04]

The instrumented form of the same assertion narrows it to:

    field='status' subj_ty=<Arc-TypeId> struct='Arc' module='std.core.arc'
    known_fields=[] fn='__lambda_cb_*_*'

Failure shape: a hidden lambda (extracted from `core.callback0(...)` /
`rest.add_middleware(...)` / etc.) explicitly captures one or more
outer locals via `captures(share x, ...)` AND the lambda body opens a
`match` whose arm binder is later used for field access.  Pre-fix the
arm-binder HVar inside the arm body gets its binding-id rewritten by
the hidden-lambda capture-id remap because the remap allocator's
`max_existing` walk does NOT inspect `HMatchArm.binder_ids` (a bare
`list[int]` introduced in 0.33.4 for persistent match-arm binder
identity).  Capture IDs are then issued starting from a too-low
`max_existing + 1`, collide with the arm-binder IDs already stamped
on body HVars by `lower_match`, and the subsequent `_remap_ids` pass
rewrites those HVars to point at the capture's env-slot type — which
for the typical `captures(share arc_local)` shape is
`std.core.arc.Arc<T>`, a struct with no `status` / `<arm-binder>`
field surface.  MIR lowering then trips the "unknown struct field"
contract assertion.

Fix lives in
`lang/driftc/driftc.py::_scan_binding_ids`: the walker explicitly
inspects `HMatchArm.binder_ids` and bumps `max_existing` for each
persisted ID it finds, so capture IDs are always issued in a range
disjoint from arm-binder IDs.

ABI implication: this is a compiler defect that breaks valid existing
ABI-14 source compilation; per the Drift ABI policy
(`doc/design/drift-lang-abi.md` §"When to bump", §"Stable ABI
Artifact Rule") the fix lands in 0.33.4-followup without an ABI
bump.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


_SHARE_CAPTURE_RESULT_MATCH_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

pub struct Response {
\tpub status: Int
}

pub struct App {
\tpub name: String
}

implement App {
\tpub fn handle(self: &App) nothrow -> core.Result<Response, Int> {
\t\tval r = Response(status = 7);
\t\treturn core.Result::Ok(r);
\t}
}

pub fn main() nothrow -> Int {
\tval app = conc.arc(App(name = "bk"));
\tval cb: core.Callback0<Int> = core.callback0(| | captures(share app) nothrow => {
\t\tval a = app.get();
\t\tval result = a.handle();
\t\tmatch &result {
\t\t\tcore.Result::Ok(resp) => { return resp.status; },
\t\t\tcore.Result::Err(_) => { return -1; }
\t\t}
\t});
\treturn cb.call();
}
"""


def _compile_and_run(tmp_path: Path, source: str, *, stem: str) -> tuple[int, str, int | None]:
	src = tmp_path / f"{stem}.drift"
	src.write_text(source)
	out = tmp_path / f"{stem}_repro"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	if res.returncode != 0:
		return res.returncode, res.stderr, None
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	return res.returncode, res.stderr, run.returncode


def test_hidden_lambda_share_capture_with_arm_binder_field_access(tmp_path: Path) -> None:
	"""A callback-lifted hidden lambda that captures an `Arc<App>` via
	`share` and matches on a `Result<Response, _>`, then accesses
	`resp.status` in the Ok arm.

	Pre-fix: compile fails with
	`MIR lowering contract failure (unknown struct field ...)` —
	the hidden-lambda capture-id remap collided with the persistent
	arm-binder id stamped on `resp`'s HVar.

	Post-fix: compile succeeds and the binary returns 7
	(the Response.status value)."""
	rc, stderr, exit_code = _compile_and_run(
		tmp_path,
		_SHARE_CAPTURE_RESULT_MATCH_SOURCE,
		stem="bk_repro",
	)
	assert rc == 0, (
		f"hidden-lambda + arm-binder regression: compile failed (rc={rc}).\n"
		f"stderr: {stderr[:800]}"
	)
	assert exit_code == 7, (
		f"binary returned {exit_code}, expected 7 (Response.status).\n"
		f"stderr: {stderr[-400:]}"
	)


_CAPTURE_USED_INSIDE_ARM_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

pub struct Config {
\tpub bias: Int
}

pub fn main() nothrow -> Int {
\tval cfg = conc.arc(Config(bias = 100));
\tval opt: Optional<Int> = Optional<type Int>::Some(7);
\tval cb: core.Callback0<Int> = core.callback0(| | captures(share cfg, move opt) nothrow => {
\t\tmatch opt {
\t\t\tOptional::Some(v) => {
\t\t\t\tval c = cfg.get();
\t\t\t\treturn c.bias + v;
\t\t\t},
\t\t\tOptional::None => { return -1; }
\t\t}
\t});
\treturn cb.call();
}
"""


def test_hidden_lambda_capture_used_inside_arm_body(tmp_path: Path) -> None:
	"""Pins the second walker gap: a hidden lambda capture USED
	INSIDE a match arm body (the `cfg.get()` call inside the
	`Some(v) => { ... }` arm).  Pre-fix the capture-id remap in
	`_remap_ids` never descends into `arm.block`, so the
	captured `cfg`'s HVar inside the arm body keeps its outer-fn
	binding id and the hidden function's local-id space has no
	entry for it.  Builds 107 = 100 (Config.bias) + 7 (Some
	binder)."""
	rc, stderr, exit_code = _compile_and_run(
		tmp_path,
		_CAPTURE_USED_INSIDE_ARM_SOURCE,
		stem="capture_in_arm",
	)
	assert rc == 0, (
		f"capture-used-inside-arm regression: compile failed (rc={rc}).\n"
		f"stderr: {stderr[:800]}"
	)
	assert exit_code == 107, (
		f"binary returned {exit_code}, expected 107 (100 + 7).\n"
		f"stderr: {stderr[-400:]}"
	)


_CAPTURE_NAME_COLLIDES_WITH_ARM_LOCAL_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

pub struct Config {
\tpub bias: Int
}

pub fn main() nothrow -> Int {
\tval inner = conc.arc(Config(bias = 100));
\tval opt: Optional<Int> = Optional<type Int>::Some(7);
\tval cb: core.Callback0<Int> = core.callback0(| | captures(share inner, move opt) nothrow => {
\t\tmatch opt {
\t\t\tOptional::Some(v) => {
\t\t\t\tval inner: Int = v;
\t\t\t\treturn inner;
\t\t\t},
\t\t\tOptional::None => { return -1; }
\t\t}
\t});
\treturn cb.call();
}
"""


def test_hidden_lambda_arm_body_local_visible_to_collision_check(tmp_path: Path) -> None:
	"""Pins the `_collect_local_names` arm-body traversal: an
	explicit capture is named `inner` AND a `val inner` is
	declared inside the `Some(v) => { ... }` arm body.

	Pre-walker-fix `_collect_local_names` never descended into
	`arm.block`, so the arm-body `val inner` was invisible.  The
	subsequent capture-name-vs-local collision check at
	`lang/driftc/driftc.py::E_CAPTURE_NAME_COLLIDES_WITH_LOCAL`
	saw `local_names` = {param names, body-top-level lets} which
	did not include the arm-body `inner` — so a collision that
	should have fired stayed silent and the compiler kept going,
	eventually mistyping the body and producing some downstream
	error.

	Post-walker-fix the collision is correctly observed and the
	compile fails with `E_CAPTURE_NAME_COLLIDES_WITH_LOCAL`
	naming `inner`.  This is the diagnostic-positive form that
	pins arm-body locals being visible to the local-name
	collector — there is no way to silently no-op past this
	check once it actually sees the name."""
	rc, stderr, _ = _compile_and_run(
		tmp_path,
		_CAPTURE_NAME_COLLIDES_WITH_ARM_LOCAL_SOURCE,
		stem="arm_local_collide",
	)
	assert rc != 0, (
		"capture-name-vs-arm-body-local collision NOT detected — "
		"`_collect_local_names` is not descending into `arm.block`.\n"
		f"stderr: {stderr[:600]}"
	)
	assert "E_CAPTURE_NAME_COLLIDES_WITH_LOCAL" in stderr or "collides with a local binding" in stderr, (
		f"expected E_CAPTURE_NAME_COLLIDES_WITH_LOCAL diagnostic naming "
		f"'inner'.\nstderr: {stderr[:600]}"
	)
	assert "'inner'" in stderr or '"inner"' in stderr or "inner" in stderr, (
		f"diagnostic does not mention the colliding name 'inner'.\n"
		f"stderr: {stderr[:600]}"
	)
