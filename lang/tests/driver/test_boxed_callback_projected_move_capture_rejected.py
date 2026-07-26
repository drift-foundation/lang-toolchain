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

0.33.69 shipped an intentionally CONSERVATIVE, blanket rejection in
`capture_discovery.py` (stage1): every MOVE-kind capture of a projected
place was rejected, regardless of whether the field's type happened to be
Copy — including a safe case like `p.count: Int`. That blanket rule
required real lowering work first (the lambda-body prologue,
`hir_to_mir.py` `_emit_lambda_capture_prologue`, bound captures purely by
root local id/name and had no support for a field-projection capture as
a distinct body-visible binding), tracked in
`work/callback-env-uaf-ref-args/projected-copy-captures-followup.md`.

That lowering work landed in the `fix/projected-capture-lowering` branch
(prologue skips materializing a body-visible local for any projected
capture; env construction/loads route non-bitcopy values through
`_copy_if_ref_alias`; see
`work/callback-env-uaf-ref-args/research-copy-projected-captures.md`).
`capture_discovery.py` now downgrades a MOVE-kind projected capture to a
plain COPY read when a typed caller (`borrow_checker_pass.py`, via
`_type_of_place`) confirms the field is Copy AND bitcopy —
`test_copy_typed_projected_field_now_compiles_and_runs` below (an `Int`
field). A non-Copy field (`execute` above) is still rejected: there is no
lowering support for move-and-zero-back a projected place, only for
Copy-typed reads. `hir_to_mir.py`'s `cap.key.proj` MOVE-branches still
assert if a non-Copy MOVE+projected capture ever reaches lowering
unrejected, as a defense-in-depth backstop.

**Narrowed to bitcopy types (0.33.70 review finding).** A Copy-BUT-NOT-
bitcopy field — a `String`, or a Copy struct/variant containing one —
downgraded the same way produced a CONFIRMED heap-use-after-free under
ASAN for the struct/variant case (`Tag(label: String)` marked
`implement core.Copy for Tag {}`, captured as `p.tag`): the boxed-callback
COPY-kind env-construction branch's retain/copy of the field does not
survive intact once the field's value flows out of the callback and both
the source struct and the callback env are later dropped. The plain
`String` field case does not reproduce today, but only because a
separate, independent pass (ownership normalization) happens to provide
incidental coverage for that one type — not something this lowering path
can rely on, and not true for the struct/variant case. So the downgrade
is restricted to Copy-AND-BITCOPY fields (`type_table.is_bitcopy`, which
have no refcount to double-own in the first place). String Scope A
(doc/history.md) then lifted the 0.33.70 bitcopy-only
narrowing: Copy-but-NON-bitcopy fields — `String`, or a Copy struct
CONTAINING one (`Tag { label: String }`) — are accepted too, because the
root cause of the narrowing (the COPY-kind capture-slot read returning an
UNMARKED shallow view of the env's field, double-released once it crossed
a by-value boundary) is fixed by the central alias-marking contract
(`hir_to_mir._mark_ref_alias_if_non_bitcopy`). See
`test_copy_typed_non_bitcopy_string_field_compiles_and_runs` and the ASAN
proof `test_copy_typed_non_bitcopy_struct_field_runs_clean_asan` below.
Non-Copy projected MOVE captures remain rejected, as does the
`--emit-package` projected-capture path.

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


# `p.count` (a Copy `Int` field) is read implicitly — no explicit
# `captures(...)`, no `move` keyword — inside a `core.callback0(...)` body.
# Now downgraded to a COPY capture and compiles/runs correctly (see the
# module docstring for the lowering work that enabled this).
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


# `p.point` (a `Point` STRUCT field — `implement core.Copy for Point {}`,
# composed entirely of `Int` fields, hence bitcopy per
# `types_core.py::TypeTable.is_bitcopy`'s transitive struct case) read
# implicitly inside a `core.callback0(...)` body. This is deliberately NOT
# a scalar, to lock in that the Copy-AND-bitcopy downgrade covers a bitcopy
# STRUCT too, not only scalars like the `Int` case above — see the module
# docstring.
_COPY_FIELD_BITCOPY_STRUCT_IMPLICIT_CAPTURE_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

struct Point(x: Int, y: Int);
implement core.Copy for Point {}

struct Prepared {
\tpoint: Point,
}

fn driver_handle(prepare: core.CallbackThrow2<Int, String, Prepared>, tk: Int, payload: String) throws -> Point {
\tvar p = prepare.call(tk, payload);
\tvar vt = conc.spawn<type Point>(core.callback0(| | => {
\t\treturn p.point;
\t}));
\tmatch vt.join() {
\t\tOk(v) => { return v; },
\t\tErr(_) => { return Point(x = -1, y = -1); },
\t\tdefault => { return Point(x = -2, y = -2); }
\t}
}

pub fn main() nothrow -> Int {
\tval prepare: core.CallbackThrow2<Int, String, Prepared> = core.callback_throw2(| tk, pl | -> Prepared => {
\t\treturn Prepared(point = Point(x = tk, y = tk + 1));
\t});
\tval result = try driver_handle(prepare, 41, "hello") catch { Point(x = -3, y = -3) };
\tif result.x == 41 {
\t\tif result.y == 42 {
\t\t\treturn 0;
\t\t}
\t}
\treturn 1;
}
"""


# `p.name` (a `String` field — Copy per project policy, but NOT bitcopy)
# read implicitly inside a `core.callback0(...)` body. Copy-but-non-bitcopy
# projected fields are rejected (narrowed scope, 0.33.70 review finding —
# see the module docstring): the boxed-callback COPY-kind env-construction
# branch's retain/copy of a non-bitcopy field does not survive intact once
# the field's value flows out of the callback and both the source struct
# and the callback env are later dropped (CONFIRMED as a real
# heap-use-after-free for the struct/variant case below; `String` itself
# happens not to reproduce, but only via an unrelated independent pass —
# not something this lowering path can rely on, so it stays conservative
# for every non-bitcopy Copy type, String included).
_COPY_FIELD_STRING_IMPLICIT_CAPTURE_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

struct Prepared {
\tname: String,
}

fn driver_handle(prepare: core.CallbackThrow2<Int, String, Prepared>, tk: Int, payload: String) throws -> String {
\tvar p = prepare.call(tk, payload);
\tvar vt = conc.spawn<type String>(core.callback0(| | => {
\t\treturn p.name;
\t}));
\tmatch vt.join() {
\t\tOk(v) => { return v; },
\t\tErr(_) => { return "join-err"; },
\t\tdefault => { return "join-default"; }
\t}
}

pub fn main() nothrow -> Int {
\tval prepare: core.CallbackThrow2<Int, String, Prepared> = core.callback_throw2(| tk, pl | -> Prepared => {
\t\treturn Prepared(name = "hello-" + pl);
\t});
\tval result = try driver_handle(prepare, 1, "world") catch { "caught" };
\tif result == "hello-world" {
\t\treturn 0;
\t}
\treturn 1;
}
"""

# `p.tag` (a `Tag` struct field, `implement core.Copy for Tag {}`,
# containing a `String`) read implicitly inside a `core.callback0(...)`
# body. THIS SHAPE PRODUCED A CONFIRMED heap-use-after-free (`memcmp` on a
# freed `String` buffer inside `drift_string_eq`, comparing the returned
# `Tag.label`) when the Copy-downgrade was not restricted to bitcopy types
# — found during 0.33.70 review, before the narrowing landed. Locks in the
# rejection so this exact shape can never silently regress back to
# miscompiling.
_COPY_FIELD_STRUCT_CONTAINING_STRING_IMPLICIT_CAPTURE_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

struct Tag(label: String);
implement core.Copy for Tag {}

struct Prepared {
\ttag: Tag,
}

fn driver_handle(prepare: core.CallbackThrow2<Int, String, Prepared>, tk: Int, payload: String) throws -> Tag {
\tvar p = prepare.call(tk, payload);
\tvar vt = conc.spawn<type Tag>(core.callback0(| | => {
\t\treturn p.tag;
\t}));
\tmatch vt.join() {
\t\tOk(v) => { return v; },
\t\tErr(_) => { return Tag(label = "join-err"); },
\t\tdefault => { return Tag(label = "join-default"); }
\t}
}

pub fn main() nothrow -> Int {
\tval prepare: core.CallbackThrow2<Int, String, Prepared> = core.callback_throw2(| tk, pl | -> Prepared => {
\t\treturn Prepared(tag = Tag(label = "hello-" + pl));
\t});
\tval result = try driver_handle(prepare, 1, "world") catch { Tag(label = "caught") };
\tif result.label == "hello-world" {
\t\treturn 0;
\t}
\treturn 1;
}
"""


def _compile(tmp_path: Path, source: str, name: str = "test_bin", *, sanitize: str | None = None) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	cmd = [sys.executable, "-m", "lang.driftc.driftc", "--dev",
	 "--stdlib-root", str(ROOT / "stdlib")]
	if sanitize:
		cmd += [f"--sanitize={sanitize}"]
	cmd += [str(src), "--entry", "main::main", "-o", str(out)]
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120))


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


def test_copy_typed_projected_field_now_compiles_and_runs(tmp_path: Path) -> None:
	"""A Copy-typed field (`Int`) read implicitly inside a boxed-callback
	lambda is downgraded from the unsupported MOVE-projected path to a
	plain COPY capture, and compiles and runs correctly — the lowering
	follow-up referenced in the module docstring."""
	res = _compile(tmp_path, _COPY_FIELD_IMPLICIT_CAPTURE_SOURCE)
	assert res.returncode == 0, f"compile failed: {res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-500:]}"


def test_copy_typed_projected_bitcopy_struct_field_compiles_and_runs(tmp_path: Path) -> None:
	"""A Copy struct field composed entirely of bitcopy fields (`Point { x:
	Int, y: Int }`, `implement core.Copy for Point {}`) read implicitly
	inside a boxed-callback lambda is downgraded the same way as a scalar
	bitcopy field (the `Int` case above) — `is_bitcopy` is transitive for
	structs, so the Copy-AND-bitcopy downgrade is not scalar-only. See the
	module docstring."""
	res = _compile(tmp_path, _COPY_FIELD_BITCOPY_STRUCT_IMPLICIT_CAPTURE_SOURCE)
	assert res.returncode == 0, f"compile failed: {res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-500:]}"


def test_copy_typed_non_bitcopy_string_field_compiles_and_runs(tmp_path: Path) -> None:
	"""FLIPPED by String Scope A (was `…_still_rejected`): a Copy-but-
	non-bitcopy field (`String`) read implicitly inside a boxed-callback
	body now compiles and runs correctly. The 0.33.70 bitcopy narrowing
	existed because the COPY-kind capture-slot read returned an unmarked
	shallow view of the env's field; Scope A routes that read through the
	central alias-marking contract (`_mark_ref_alias_if_non_bitcopy`), so
	transfer boundaries deep-copy the view (see
	`borrow_checker_pass._is_copy_projected_field`'s docstring)."""
	res = _compile(tmp_path, _COPY_FIELD_STRING_IMPLICIT_CAPTURE_SOURCE)
	assert res.returncode == 0, f"compile failed: {res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-500:]}"


def test_copy_typed_non_bitcopy_struct_field_runs_clean_asan(tmp_path: Path) -> None:
	"""FLIPPED by String Scope A (was `…_still_rejected`): the CONFIRMED-
	UAF shape from 0.33.70 review — a Copy struct field containing a
	`String` (`Tag`, `implement core.Copy for Tag {}`) captured implicitly
	into a boxed callback and moved across `conc.spawn` — now compiles and
	runs CLEAN UNDER ASAN. This is the Scope A ownership proof: the env's
	field view is deep-copied at every ownership-transfer boundary instead
	of double-releasing `Tag.label`."""
	res = _compile(tmp_path, _COPY_FIELD_STRUCT_CONTAINING_STRING_IMPLICIT_CAPTURE_SOURCE, sanitize="address,undefined")
	assert res.returncode == 0, f"compile failed: {res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-800:]
