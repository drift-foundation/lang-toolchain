# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: implicit projected move-capture into a boxed callback.

Reported from a downstream team via a staged toolchain (driftc 0.33.68/
abi19): a struct field (e.g. `p.execute`, an owned `core.CallbackThrow1`)
passed BY VALUE to a callee inside a `core.callback0`/`callbackN`-boxed
lambda, with NO explicit `captures(...)` clause naming the field, silently
compiled — but miscompiled. `capture_discovery.py`'s implicit-capture
inference defaults a plain field READ to a MOVE-kind capture for boxed-
callback lambdas (`capture_as_move`), and MIR lowering's projected-capture
branch (hir_to_mir.py `cap.key.proj`) only knows how to copy-READ the
projection into the closure env, not move-and-zero-back the source field.
The source struct's own drop later re-drops the same (already captured)
field -> heap-use-after-free, confirmed via `--sanitize=address,undefined`:
`heap-use-after-free ... in drift_string_release`, freed by a callback env
drop thunk, on a String field moved into a boxed callback constructed
inside ANOTHER boxed callback's body, then spawned onto a VirtualThread and
invoked there. The existing rejection ("lambda move captures of
projections are not supported yet") only checked `use.move` — set only by
an explicit `move` expression — so it never fired for this
`capture_as_move`-defaulted path.

Fix is an intentionally CONSERVATIVE, blanket rejection in
`capture_discovery.py` (stage1): every MOVE-kind capture of a projected
place is rejected, regardless of whether the field's type happens to be
Copy — including a safe case like `p.count: Int`
(`test_copy_typed_projected_field_also_currently_rejected` below). A
type-aware variant (downgrade to a plain COPY capture when the field is
Copy) was prototyped and reverted: it requires the lambda-body prologue
(`hir_to_mir.py` `_emit_lambda_capture_prologue`) to bind a projected
capture key as a distinct body-visible binding, which it currently cannot
do at all (it binds purely by root local id/name) — a real lowering
feature, not a checker-side enum flip. Deferred as a separate follow-up;
see `work/callback-env-uaf-ref-args/projected-copy-captures-followup.md`.
`hir_to_mir.py`'s `cap.key.proj` branches assert if a MOVE+projected
capture ever reaches lowering unrejected, as a defense-in-depth backstop.

The safe, still-supported pattern (used by e.g. mariadb-client's own
in-repo idiom) is to explicitly extract the field via `std.mem.replace`
into a standalone local FIRST, then `captures(move <that local>)` — see
`test_projected_move_capture_via_mem_replace_still_compiles` below.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

# BAD: `p.execute` (a struct field) is passed BY VALUE to `run_execute`
# inside a `core.callback0(| | => {...})` lambda with NO explicit
# `captures(...)` clause. Must be REJECTED at compile time, not silently
# miscompiled into a use-after-free.
_IMPLICIT_PROJECTED_MOVE_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

struct Prepared {
\texecute: core.CallbackThrow1<Bool, String>,
}

fn _execute(fields: &String, is_reclaim: Bool) throws -> String {
\tif is_reclaim {
\t\treturn "reclaim:" + *fields;
\t}
\treturn "run:" + *fields;
}

fn run_execute(var e: core.CallbackThrow1<Bool, String>) throws -> String {
\treturn e.call(false);
}

fn driver_handle(prepare: core.CallbackThrow2<Int, String, Prepared>, tk: Int, payload: String) throws -> String {
\tvar p = prepare.call(tk, payload);
\tvar vt = conc.spawn<type String>(core.callback0(| | => {
\t\treturn try run_execute(p.execute) catch { "spawn-err" };
\t}));
\tmatch vt.join() {
\t\tOk(v) => { return v; },
\t\tErr(_) => { return "join-err"; },
\t\tdefault => { return "join-default"; }
\t}
}

pub fn main() nothrow -> Int {
\tval prepare: core.CallbackThrow2<Int, String, Prepared> = core.callback_throw2(| tk, pl | -> Prepared => {
\t\tval fields = "payload:" + pl;
\t\tval execute: core.CallbackThrow1<Bool, String> = core.callback_throw1(| is_reclaim | captures(move fields) -> String => _execute(&fields, is_reclaim));
\t\treturn Prepared(execute = move execute);
\t});
\tval result = try driver_handle(prepare, 1, "hello") catch { "caught" };
\tif result == "run:payload:hello" {
\t\treturn 0;
\t}
\treturn 1;
}
"""

# GOOD control: identical shape, but `execute` is extracted from `Prepared`
# via `std.mem.replace` into a standalone local FIRST, then explicitly
# `captures(move execute)`'d — the documented, supported "move a field out
# of an owned struct" idiom. Must still compile and run correctly.
_MEM_REPLACE_EXTRACTED_MOVE_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;
import std.mem as mem;

struct Prepared {
\texecute: core.CallbackThrow1<Bool, String>,
}

fn _execute(fields: &String, is_reclaim: Bool) throws -> String {
\tif is_reclaim {
\t\treturn "reclaim:" + *fields;
\t}
\treturn "run:" + *fields;
}

fn _dummy_execute(is_reclaim: Bool) throws -> String {
\treturn "";
}

fn driver_handle(prepare: core.CallbackThrow2<Int, String, Prepared>, tk: Int, payload: String) throws -> String {
\tvar p = prepare.call(tk, payload);
\tvar execute = mem.replace(&mut p.execute, core.callback_throw1(_dummy_execute));
\tvar vt = conc.spawn<type String>(core.callback0(| | captures(move execute) => {
\t\treturn try execute.call(false) catch { "spawn-err" };
\t}));
\tmatch vt.join() {
\t\tOk(v) => { return v; },
\t\tErr(_) => { return "join-err"; },
\t\tdefault => { return "join-default"; }
\t}
}

pub fn main() nothrow -> Int {
\tval prepare: core.CallbackThrow2<Int, String, Prepared> = core.callback_throw2(| tk, pl | -> Prepared => {
\t\tval fields = "payload:" + pl;
\t\tval execute: core.CallbackThrow1<Bool, String> = core.callback_throw1(| is_reclaim | captures(move fields) -> String => _execute(&fields, is_reclaim));
\t\treturn Prepared(execute = move execute);
\t});
\tval result = try driver_handle(prepare, 1, "hello") catch { "caught" };
\tif result == "run:payload:hello" {
\t\treturn 0;
\t}
\treturn 1;
}
"""


# Still rejected for now (deferred follow-up, not a bug): `p.count` (a Copy
# `Int` field) is read implicitly — no explicit `captures(...)`, no `move`
# keyword — inside a `core.callback0(...)` body. The blanket rejection in
# capture_discovery.py doesn't distinguish this from the unsafe non-Copy
# `execute` case above; it's intentionally conservative until the lowering
# feature described in the module docstring lands.
_COPY_FIELD_IMPLICIT_CAPTURE_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

struct Prepared {
\tcount: Int,
}

fn driver_handle(prepare: core.CallbackThrow2<Int, String, Prepared>, tk: Int, payload: String) throws -> Int {
\tvar p = prepare.call(tk, payload);
\tvar vt = conc.spawn<type Int>(core.callback0(| | => {
\t\treturn p.count + 1;
\t}));
\tmatch vt.join() {
\t\tOk(v) => { return v; },
\t\tErr(_) => { return -1; },
\t\tdefault => { return -2; }
\t}
}

pub fn main() nothrow -> Int {
\tval prepare: core.CallbackThrow2<Int, String, Prepared> = core.callback_throw2(| tk, pl | -> Prepared => {
\t\treturn Prepared(count = tk);
\t});
\tval result = try driver_handle(prepare, 41, "hello") catch { -3 };
\tif result == 42 {
\t\treturn 0;
\t}
\treturn 1;
}
"""


def _compile(tmp_path: Path, source: str, name: str = "test_bin") -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)


def test_implicit_projected_move_capture_into_boxed_callback_rejected(tmp_path: Path) -> None:
	"""A struct field passed by value into a callee inside a boxed-callback
	lambda, with no explicit `captures(...)`, must fail to compile — not
	silently miscompile into a use-after-free."""
	res = _compile(tmp_path, _IMPLICIT_PROJECTED_MOVE_SOURCE)
	assert res.returncode != 0, "expected a compile error, but it compiled successfully"
	assert "lambda move captures of projections are not supported yet" in res.stderr, (
		f"expected the projected-move-capture rejection diagnostic, got:\n{res.stderr[-1000:]}"
	)


def test_projected_move_capture_via_mem_replace_still_compiles(tmp_path: Path) -> None:
	"""The documented-safe alternative (std.mem.replace to extract the field
	into a standalone local, then explicit captures(move <local>)) must
	still compile and run correctly — this fix must not regress it."""
	tmp_path_bin = tmp_path
	res = _compile(tmp_path_bin, _MEM_REPLACE_EXTRACTED_MOVE_SOURCE)
	assert res.returncode == 0, f"compile failed: {res.stderr[-1000:]}"
	out = tmp_path_bin / "test_bin"
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-500:]}"


def test_copy_typed_projected_field_also_currently_rejected(tmp_path: Path) -> None:
	"""Locks in the current, intentional scope: a Copy-typed field (`Int`)
	read implicitly inside a boxed-callback lambda is ALSO rejected right
	now, same as the non-Copy case — the blanket rejection doesn't (yet)
	distinguish them. This is expected to change once the deferred
	lowering follow-up lands (see the module docstring); when it does,
	update this test rather than silently losing coverage of the scope
	decision."""
	res = _compile(tmp_path, _COPY_FIELD_IMPLICIT_CAPTURE_SOURCE)
	assert res.returncode != 0, (
		"expected the projected-move-capture rejection (Copy-projected capture "
		"support is deferred, not yet implemented) — if this now compiles, the "
		"deferred follow-up may have landed; update this test's expectations "
		"instead of deleting it"
	)
	assert "lambda move captures of projections are not supported yet" in res.stderr, (
		f"expected the projected-move-capture rejection diagnostic, got:\n{res.stderr[-1000:]}"
	)
