# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: Void-returning callback lambda crashes
throw_checks with a KeyError on the synthesized Void terminator
SSA value.

Surfaced by the SingularGateway app-team report against
0.31.94+abi14 (2026-05-17) -- blocks the entire `with_event_sink`
codepath, because event sinks attach via
`core.callback1(|ev: T| => { ... })` constructing
`Callback1<T, Void>`.  No app-side workaround.

**Minimal repro** (12 lines):

    module lambda_repro;
    import std.core as core;

    pub variant SingularEvent { Ping, Pong(tag: String) }

    pub fn main() nothrow -> Int {
        val sink: core.Callback1<SingularEvent, Void> =
            core.callback1(|ev: SingularEvent| => { val _ = 0; });
        return 0;
    }

**Pre-fix failure shape:**

    File ".../throw_checks.py", line 316, in enforce_fnresult_returns_typeaware
        ty = type_env.type_of_ssa_value(fn_id, term.value)
    KeyError: (FunctionId(module='lambda_repro',
                          name='__lambda_cb_main_0_0', ordinal=0), 't3')

**Root cause -- two bugs, defense-in-depth fix.**

*Bug 1 (root, structural).*  Hidden-lambda body lowering at
`lang/driftc/driftc.py` (~line 6625) for nothrow Void-returning
lambdas emitted `M.Return(value=<synth_void_value>)` where
`<synth_void_value>` was a fresh SSA value from `_void_value()`.
Regular Void-returning functions correctly emit
`M.Return(value=None)`.  The lambda path's malformed MIR violated
the LLVM lowering contract ("Void function must not return a
value (MIR bug)") AND left the synth value unkeyed in the SSA
type env -- which is what surfaced as the KeyError above.
Can-throw Void lambdas were unaffected because they need the
`Ok(Void)` carrier and the `ConstructResultOk` produces a typed
dest.

*Bug 2 (secondary, reinforcing).*  The production type-env
builder `Checker.build_type_env_from_ssa` at
`lang/driftc/checker/__init__.py` (~line 4995) gated terminator
return-type seeding on `if not fn_is_void:` -- unconditionally
skipping every Void function's terminator value.  Even if Bug 1
didn't exist, any Void terminator carrying a value would have
left the value unkeyed in the type env and surfaced the same
KeyError later.

For the reported shape, Bug 1 was the one actually firing; Bug 2
was a latent gap that would have surfaced on the next
Void-terminator path that ever appeared.

Note: there is also a separate test helper
`lang/driftc/core/types_env_impl.py::build_type_env_from_ssa`
with the same "nothrow second-pass missing" shape; it is NOT on
the production driftc path and is out of scope for this fix
(driftc routes through `Checker.build_type_env_from_ssa` in
`checker/__init__.py`).

**Fix shape (two layers).**
  - MIR layer (`driftc.py::6625`): nothrow Void lambdas emit
    `M.Return(value=None)` -- shape-identical to regular Void fns
    falling off the end.  Can-throw Void path kept verbatim, just
    routed through an explicit branch for readability.
  - Type-env layer (`checker/__init__.py::4995`): replaced the
    over-broad `if not fn_is_void:` guard with the precise gate
    `if fn_is_void and fn_is_can_throw: continue`.  Now nothrow
    Void seeds correctly; can-throw Void (incl.
    `declared_terminal_throws`) still skips because first-pass
    inference has already typed the terminator as
    `FnResult<Void, Error>` and re-seeding would clobber it.

No special-case for Void in `throw_checks` (rejected as
user-flagged anti-pattern -- would create an implicit invariant
at the lookup site).

Carriers (mirror the app-team report exactly):

  V1. The bare minimum the app team typed: empty `Callback1<T, Void>`
      lambda body.  No statements at all; implicit Void return.
  V2. App-team-shaped: same as V1 but body has `val _ = 0;`
      (the exact body in their commented-out workaround line).
  V3. Void lambda with conditional Void return (multiple terminators).
  V4. Callback0<Void> control -- same bug shape, arity 0.
  V5. Callback2<A, B, Void> control -- same bug shape, arity 2.

If this test ever flakes, the two load-bearing sites to check are
(in order of likelihood):
  1. `lang/driftc/driftc.py` (~line 6625): hidden-lambda body
     lowering must emit `M.Return(value=None)` for nothrow Void
     -- not `M.Return(value=<synth_void>)`.
  2. `lang/driftc/checker/__init__.py` (~line 4995): terminator
     return-type seeding must NOT be gated on `if not fn_is_void:`
     alone; the correct gate is `if fn_is_void and fn_is_can_throw`
     so nothrow Void terminators get typed (defense in depth, even
     when Bug 1 is fixed).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile_via_subprocess(
	tmp_path: Path,
	source: str,
) -> subprocess.CompletedProcess[str]:
	"""Compile via subprocess so an uncaught Python exception from
	driftc (e.g. the pre-fix `KeyError` from `throw_checks`) is
	visible as a non-zero exit + stderr traceback rather than
	bubbling up and aborting the pytest worker.

	`--test-build-only` is intentionally NOT passed: the throw_checks
	pass that contains the bug is skipped when `--test-build-only`
	is on, so the regression would be invisible.  We compile through
	to (attempted) link instead -- a separate link failure would
	be misleading here, but in practice the link step is reached
	only post-fix (pre-fix, the throw_checks pass throws first).
	"""
	main_path = tmp_path / "main.drift"
	_write_file(main_path, source)
	out_bin = tmp_path / "main_out"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--stdlib-root", str(ROOT / "stdlib"),
		str(main_path),
		"-o", str(out_bin),
	]
	return subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=120,
	)


_V1_EMPTY_BODY_SOURCE = """
module main;

import std.core as core;

pub variant SingularEvent { Ping, Pong(tag: String) }

pub fn main() nothrow -> Int {
\tval sink: core.Callback1<SingularEvent, Void> =
\t\tcore.callback1(|ev: SingularEvent| => { });
\treturn 0;
}
"""


_V2_APP_TEAM_BODY_SOURCE = """
module main;

import std.core as core;

pub variant SingularEvent { Ping, Pong(tag: String) }

pub fn main() nothrow -> Int {
\tval sink: core.Callback1<SingularEvent, Void> =
\t\tcore.callback1(|ev: SingularEvent| => { val _ = 0; });
\treturn 0;
}
"""


_V3_CONDITIONAL_VOID_SOURCE = """
module main;

import std.core as core;

pub variant SingularEvent { Ping, Pong(tag: String) }

pub fn main() nothrow -> Int {
\tval sink: core.Callback1<SingularEvent, Void> =
\t\tcore.callback1(|ev: SingularEvent| => {
\t\t\tval x: Int = 42;
\t\t\tif x > 0 { return; }
\t\t});
\treturn 0;
}
"""


_V4_CALLBACK0_VOID_SOURCE = """
module main;

import std.core as core;

pub fn main() nothrow -> Int {
\tval sink: core.Callback0<Void> =
\t\tcore.callback0(| | => { val _ = 0; });
\treturn 0;
}
"""


_V5_CALLBACK2_VOID_SOURCE = """
module main;

import std.core as core;

pub fn main() nothrow -> Int {
\tval sink: core.Callback2<Int, Int, Void> =
\t\tcore.callback2(|a: Int, b: Int| => { val _ = a + b; });
\treturn 0;
}
"""


@pytest.mark.parametrize(
	"label, source",
	[
		("V1_empty_body", _V1_EMPTY_BODY_SOURCE),
		("V2_app_team_body", _V2_APP_TEAM_BODY_SOURCE),
		("V3_conditional_void", _V3_CONDITIONAL_VOID_SOURCE),
		("V4_callback0_void", _V4_CALLBACK0_VOID_SOURCE),
		("V5_callback2_void", _V5_CALLBACK2_VOID_SOURCE),
	],
)
def test_void_callback_lambda_compiles_without_throw_check_keyerror(
	tmp_path: Path,
	label: str,
	source: str,
) -> None:
	"""Void-returning callback lambdas must compile cleanly.

	Pre-fix, every carrier here crashed driftc with

	    KeyError: (FunctionId(module='main',
	                          name='__lambda_cb_main_0_0',
	                          ordinal=0), 't3')

	raised out of `throw_checks::enforce_fnresult_returns_typeaware`
	(`stage4/throw_checks.py:316`) at the
	`type_env.type_of_ssa_value(fn_id, term.value)` lookup against
	the synthesized lambda's terminator SSA value.

	Pre-fix carrier signal: stderr contains both `KeyError` and
	`__lambda_cb_main_` -- those two strings together pin the
	failure shape to this bug specifically (rather than some
	unrelated `KeyError` elsewhere in driftc).

	Post-fix expectation:
	  - driftc exits 0 (compile succeeds end to end);
	  - stderr does not carry the pre-fix `KeyError` signature.
	"""
	res = _compile_via_subprocess(tmp_path, source)
	stderr = res.stderr or ""

	pre_fix_signature = (
		"KeyError" in stderr
		and "__lambda_cb_main_" in stderr
		and "throw_checks" in stderr
	)
	assert not pre_fix_signature, (
		f"[{label}] driftc crashed with the pre-fix throw_checks "
		f"KeyError signature on a Void-returning callback lambda.  "
		f"Two sites can produce this failure (check in order):\n"
		f"  1. `lang/driftc/driftc.py` (~line 6625): hidden-lambda "
		f"body lowering for nothrow Void lambdas must emit "
		f"`M.Return(value=None)` -- not "
		f"`M.Return(value=<synth_void_value>)`.  Regular Void fns "
		f"already do this; the lambda path must match.\n"
		f"  2. `lang/driftc/checker/__init__.py` (~line 4995, in "
		f"`Checker.build_type_env_from_ssa`): terminator return-type "
		f"seeding must NOT be gated on `if not fn_is_void:` alone.  "
		f"The correct gate is `if fn_is_void and fn_is_can_throw: "
		f"continue` so nothrow Void terminators get typed (defense "
		f"in depth even when #1 is fixed; protects against any new "
		f"path that synthesizes a Void terminator value).\n\n"
		f"STDERR (tail):\n{stderr[-2000:]}"
	)
	assert res.returncode == 0, (
		f"[{label}] driftc exited {res.returncode} on a "
		f"Void-returning callback lambda, but the failure is NOT the "
		f"known pre-fix throw_checks KeyError shape -- something else "
		f"is wrong:\n\nSTDERR (tail):\n{stderr[-2000:]}"
	)
