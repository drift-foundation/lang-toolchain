# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: value-producing `match <rvalue> { Some(v) => move v, None => lit }`
must not leak the moved-out variant payload.

Filed 2026-05-22 from the bookkeeper bare-Config residual leak.  After the
0.32.7→0.32.8 Arc/VT teardown fix closed the registered-Arc-with-Destructible
class, bookkeeper still showed one definitely-lost block: 32 bytes from
`drift_string_from_cstr → drift_env_get → main`.  Reduced (by both the
PushCoin team in `work/bookkeeper-shutdown-hang/repro-bare-leak/` and
in-house via /tmp/mm.drift) to:

    val secret: String = match env.get("REPRO_VAR") {
        Optional::Some(v) => { move v },
        Optional::None    => { "default" }
    };
    return 0;

No struct, no Arc, no registry, no Destructible, no rest, no shutdown
machinery.  When the env var is set, the heap-allocated `Some` payload
String leaks; when unset (None arm taken), no leak.  Leak size scales
with payload length, confirming the buffer from `drift_string_from_cstr`
is the one not released.

Bisect findings pinned in the test bodies below — TL;DR:

  - inline-match value-producing on `env.get`: leaks
  - two-step `val opt = env.get; match opt`: clean
  - inline-match value-producing on a user fn returning `Optional<String>`
    (literal-clone payload): clean
  - statement-form `var s = "default"; match … { Some(v) => { s = move v }, None => {} }`:
    clean

So the bug is the value-producing match arm 0 (Some → move v) when the
match scrutinee is an rvalue Optional<String> sourced from `env.get`.
IR inspection (/tmp/mm.ll): arm 0 issues multiple `drift_string_retain`
calls on the moved-out payload with insufficient compensating releases,
and the rvalue variant temporary is never explicitly dropped — net
refcount stays > 0 at process exit.

Likely fix site: lowering/drop scheduling for match-arm result bindings
where the payload-move is the match-expression value (stage2 HIR→MIR
or string_arc.py drop-flag insertion for variant-payload move-out at
arm exit).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

# Shape A — LEAKS today.  Inline match on env.get, Some(v) tail-returns v
# as the match expression value bound to a typed `val`.  This is the
# bookkeeper `application_secret` pattern.
SHAPE_A_LEAKS = """\
module main;
import std.core as core;
import std.env as env;

pub fn main() nothrow -> Int {
\tval secret: String = match env.get("DRIFT_TEST_INLINE_MATCH_PROBE") {
\t\tOptional::Some(v) => { move v },
\t\tOptional::None    => { "default-value" }
\t};
\tif secret.byte_length() == 0 { return 1; }
\treturn 0;
}
"""

# Shape B — CLEAN today.  Two-step: bind the Optional to a local first,
# then match the local.  Same surface intent as Shape A but lifetimes
# are crisp because `opt` has a named owner.
SHAPE_B_CLEAN = """\
module main;
import std.core as core;
import std.env as env;

pub fn main() nothrow -> Int {
\tval opt = env.get("DRIFT_TEST_INLINE_MATCH_PROBE");
\tval secret: String = match opt {
\t\tOptional::Some(v) => { move v },
\t\tOptional::None    => { "default-value" }
\t};
\tif secret.byte_length() == 0 { return 1; }
\treturn 0;
}
"""

# Shape C — CLEAN today.  Statement-form conditional move: pre-bind a
# default, then conditionally overwrite from the match arm.  This is
# the bookkeeper `worker_id` pattern that does NOT leak.
SHAPE_C_CLEAN = """\
module main;
import std.core as core;
import std.env as env;

pub fn main() nothrow -> Int {
\tvar secret: String = "default-value";
\tmatch env.get("DRIFT_TEST_INLINE_MATCH_PROBE") {
\t\tOptional::Some(v) => { if v.byte_length() > 0 { secret = move v; } },
\t\tOptional::None => {}
\t}
\tif secret.byte_length() == 0 { return 1; }
\treturn 0;
}
"""

# Shape D — was failing before the fix (same path as Shape A).  Inline
# match on a user fn that returns Optional<String> with a *heap-allocated*
# payload (fmt.format_int produces a fresh refcounted String, not a
# static literal).  This is the non-env.get variant of the bug — proves
# the fix is type-shape-driven (Copy + runtime-drop payload in a
# value-producing inline match arm), NOT env.get-specific.
#
# An earlier version of this case used `"…".clone()` for the payload,
# but Drift's static-string optimization makes the literal-clone STATIC
# (no refcount ops, no leak surface).  That made the case a false
# positive control — it was clean BEFORE the fix because the path the
# fix touches was never exercised.  fmt.format_int produces a real
# allocated String with refcount=1, which IS what the fix protects.
SHAPE_D_USERFN_ALLOCATED = """\
module main;
import std.core as core;
import std.format as fmt;

fn _produce() nothrow -> Optional<String> {
\treturn Optional<type String>::Some(fmt.format_int(2147483648));
}

pub fn main() nothrow -> Int {
\tval secret: String = match _produce() {
\t\tOptional::Some(v) => { move v },
\t\tOptional::None    => { "default" }
\t};
\tif secret.byte_length() == 0 { return 1; }
\treturn 0;
}
"""

# Shape E — same bug class, different variant.  Result<String, E>
# returned by a user fn, with Ok(v) binding the heap-allocated String
# payload via inline value-producing match.  If the materialization gate
# only saw `Optional` and missed `Result`, this would leak.  Pins the
# fix's type-shape generality (any variant with a Copy + runtime-drop
# payload, not just Optional).
SHAPE_E_RESULT_VARIANT = """\
module main;
import std.core as core;
import std.format as fmt;

pub error E { tag: String }

fn _produce() nothrow -> core.Result<String, E> {
\treturn core.Result::Ok(fmt.format_int(99999));
}

pub fn main() nothrow -> Int {
\tval value: String = match _produce() {
\t\tcore.Result::Ok(v)  => { move v },
\t\tcore.Result::Err(_) => { "fallback" }
\t};
\tif value.byte_length() == 0 { return 1; }
\treturn 0;
}
"""

# Shape F — multi-field ctor.  The Some-equivalent arm binds both an
# allocated String AND a non-drop Int field.  Pins that the
# materialization gate fires even when only one of several fields needs
# drop work (the loop's break-on-first-hit logic).
SHAPE_F_MULTIFIELD_CTOR = """\
module main;
import std.core as core;
import std.format as fmt;

pub variant Pair {
\tFilled(label: String, count: Int),
\tEmpty
}

fn _produce() nothrow -> Pair {
\treturn Pair::Filled(label = fmt.format_int(777), count = 42);
}

pub fn main() nothrow -> Int {
\tval label: String = match _produce() {
\t\tPair::Filled(l, c) => { val _ = c; move l },
\t\tPair::Empty        => { "none" }
\t};
\tif label.byte_length() == 0 { return 1; }
\treturn 0;
}
"""

# Shape G — discarded match result.  `val _ = match … { Some(v) => move v, None => lit }`.
# The binder is moved out and the entire match expression is then
# discarded.  Pins that the drop chain works even when the result is
# never named.  Same materialization gate must fire.
SHAPE_G_DISCARDED_RESULT = """\
module main;
import std.core as core;
import std.env as env;

pub fn main() nothrow -> Int {
\tval _ = match env.get("DRIFT_TEST_INLINE_MATCH_PROBE") {
\t\tOptional::Some(v) => { move v },
\t\tOptional::None    => { "default" }
\t};
\treturn 0;
}
"""


def _compile_and_valgrind(
	tmp_path: Path, source: str, *, label: str, env_value: str | None = None
) -> tuple[int, str]:
	"""Compile under raw stdlib and run under valgrind.  Returns
	(definitely_lost_bytes, valgrind_log_text)."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), f"[{label}] binary not produced"

	vg_log = tmp_path / f"valgrind_{label}.log"
	run_env = {"PATH": "/usr/bin:/bin"}
	if env_value is not None:
		run_env["DRIFT_TEST_INLINE_MATCH_PROBE"] = env_value
	subprocess.run(
		["valgrind", "--tool=memcheck", "--leak-check=full",
		 "--show-leak-kinds=definite,indirect",
		 "--errors-for-leak-kinds=definite,indirect",
		 "--error-exitcode=97",
		 f"--log-file={vg_log}",
		 str(out_bin)],
		capture_output=True, text=True, timeout=180, env=run_env,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	return definitely_lost, vg_output


def _assert_clean(lost: int, vg_log: str, *, label: str, payload: str | None) -> None:
	if lost != 0:
		raise AssertionError(
			f"[{label}] LANGUAGE_BUG: inline-match-rvalue payload-move leak — "
			f"{lost} bytes definitely lost (payload={payload!r}).\n"
			f"Repro lives at lang/tests/memcheck/test_inline_match_rvalue_string_payload_leak.py.\n"
			f"Bookkeeper-side workaround: rewrite as two-step (Shape B) OR "
			f"statement-form conditional move (Shape C).\n"
			f"Likely compiler fix site: stage2 HIR→MIR lowering of match-arm "
			f"result bindings where the variant payload is moved out as the "
			f"match expression value (the rvalue variant temporary is never "
			f"explicitly dropped, leaving an extra retain alive).\n\n"
			f"Valgrind log tail:\n{vg_log[-1500:]}"
		)


def test_inline_match_env_get_some_arm_no_leak(tmp_path: Path) -> None:
	"""Shape A — the leaking shape.  Pins the regression.

	When the env var is set, the heap-allocated `Some` payload from
	env.get must be released at the end of main.  Currently leaks.
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, SHAPE_A_LEAKS,
		label="shape_a_some_arm", env_value="hello-world-canary",
	)
	_assert_clean(lost, vg_log, label="shape_a_some_arm", payload="hello-world-canary")


def test_inline_match_env_get_none_arm_no_leak(tmp_path: Path) -> None:
	"""Shape A — None path control.  With env unset, no allocation is
	made by env.get → no leak surface.  Confirms the leak is specifically
	the Some-arm payload move, not generic env.get teardown."""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, SHAPE_A_LEAKS,
		label="shape_a_none_arm", env_value=None,
	)
	_assert_clean(lost, vg_log, label="shape_a_none_arm", payload=None)


def test_two_step_match_env_get_no_leak(tmp_path: Path) -> None:
	"""Shape B — two-step.  Bind env.get's Optional to a local first,
	then match the local.  Already clean; pins the working pattern as
	a positive control so a future regression that breaks Shape B
	(e.g., overly-aggressive payload-move pruning) fails loudly here."""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, SHAPE_B_CLEAN,
		label="shape_b_two_step", env_value="hello-world-canary",
	)
	_assert_clean(lost, vg_log, label="shape_b_two_step", payload="hello-world-canary")


def test_statement_form_conditional_move_no_leak(tmp_path: Path) -> None:
	"""Shape C — statement-form conditional move.  bookkeeper's
	worker_id / service_group pattern.  Already clean; positive
	control."""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, SHAPE_C_CLEAN,
		label="shape_c_conditional", env_value="hello-world-canary",
	)
	_assert_clean(lost, vg_log, label="shape_c_conditional", payload="hello-world-canary")


def test_inline_match_user_fn_allocated_payload_no_leak(tmp_path: Path) -> None:
	"""Shape D — same bug path as Shape A but the scrutinee is a user fn
	returning Optional<String>::Some(fmt.format_int(N)) instead of
	env.get.  The payload IS a real heap-allocated refcounted String
	(format_int's return value), not a static literal — so this case
	exercises the same Copy + runtime-drop materialization gate the
	fix touches.  Before the fix this leaks; after the fix it's clean.
	Proves the bug class is type-shape-driven, not env.get-specific."""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, SHAPE_D_USERFN_ALLOCATED,
		label="shape_d_user_fn_allocated", env_value=None,
	)
	_assert_clean(lost, vg_log, label="shape_d_user_fn_allocated", payload="format_int(2147483648)")


def test_inline_match_result_variant_no_leak(tmp_path: Path) -> None:
	"""Shape E — same bug class, different variant family.  Result<String, E>
	with Ok(v) binding the allocated String payload.  If the
	materialization gate only saw `Optional` (or had a hardcoded variant
	list), this would still leak.  Pins type-shape generality of the
	fix — `_needs_runtime_drop(f_ty)` is variant-agnostic."""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, SHAPE_E_RESULT_VARIANT,
		label="shape_e_result_ok", env_value=None,
	)
	_assert_clean(lost, vg_log, label="shape_e_result_ok", payload="format_int(99999)")


def test_inline_match_multifield_ctor_no_leak(tmp_path: Path) -> None:
	"""Shape F — multi-field ctor `Filled(label: String, count: Int)`.
	The arm binds both an allocated String AND a non-drop Int.  Pins
	that the materialization gate (a per-field loop with break-on-first
	-Copy+drop hit) correctly fires for the mixed-field ctor case."""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, SHAPE_F_MULTIFIELD_CTOR,
		label="shape_f_multifield", env_value=None,
	)
	_assert_clean(lost, vg_log, label="shape_f_multifield", payload="format_int(777)")


def test_inline_match_discarded_result_no_leak(tmp_path: Path) -> None:
	"""Shape G — discarded result.  `val _ = match … { Some(v) => move v,
	None => lit }`.  The binder is moved out and the entire match
	expression is then dropped without naming.  Pins that the
	materialized variant + the discarded binder both get scope-drop
	cleanup correctly even without a named result owner."""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, SHAPE_G_DISCARDED_RESULT,
		label="shape_g_discarded", env_value="discarded-canary-payload",
	)
	_assert_clean(lost, vg_log, label="shape_g_discarded", payload="discarded-canary-payload")
