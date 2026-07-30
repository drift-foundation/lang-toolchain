# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Nested boxed-callback captures — lowering, escape safety, and rejections.

A boxed callback whose body builds a NESTED boxed callback exposed three
defects (all pre-existing in certified 0.33.69; triage
`/tmp/drift-announce/2026-07-06T161920Z-ref-typed-callback-args-03373-triage.md`):  ## drift-tmp-root-audit: allow docs repro-path reference

1. **SSA ICE** for any nested capture of the ENCLOSING lambda's parameter
   (`RuntimeError: SSA: load before store for local '__b{id}'`): the
   hidden-lambda worklists constructed HIRToMIR WITHOUT the typed fn's
   `binding_names`, so nested-lambda env construction resolved the capture
   root via the `__b{id}` fallback — a load of a local nothing ever stores.
   Fixed by passing `binding_names` in both worklist constructions
   (driftc.py), mirroring the regular-fn path.
2. **Silent dangling-pointer hazard** once (1) is fixed: a nested (or
   top-level) boxed callback capturing a reference VALUE (implicit MOVE of a
   `&T` binding, or explicit `captures(copy ref)`) — or implicitly BORROWING
   a captured binding (a `&self` method call classifies the capture REF
   ahead of the boxed MOVE default) — puts a raw frame pointer into the heap
   env with no liveness tie. Enforcement is USE-AWARE
   (`lambda_validate.py::_check_boxed_capture_escapes`), not
   wrap-site-unconditional: the wrap is ACCEPTED when its value provably
   stays local (invoked in place, or let-bound with every use in
   method-call receiver position — this keeps the sound synchronous
   pattern pinned by test_match_arm_lambda_capture.py's for-binder case
   compiling) and REJECTED in any escaping position (returned,
   constructor/call/method argument, assignment, moved, or captured by
   another lambda) with E_ESCAPE_REF_CAPTURE / E_CALLBACK_BORROWED_CAPTURE.
   The borrow checker's `_lambda_escape_level` additionally bounds
   ref-valued MOVE/COPY captures at LOCAL for loan-tracked positions.
   Nested wraps are also re-validated from the hidden-lambda worklist.
3. NOT part of this slice: `val cb = h.cb` (reading an INTERFACE-typed
   struct field by value) shallow-copies the boxed callback without
   retaining its env — double-free/UAF on drop. Pre-existing on certified
   0.33.69, reproduces WITHOUT any lambda nesting, tracked separately. The
   positive tests here use move/direct-call access patterns.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _stdlib() -> Path:
	return stdlib_root() or (ROOT / "stdlib")


def _compile(tmp_path: Path, source: str, entry: str = "main::main", sanitize: str | None = None) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	cmd = [sys.executable, "-m", "lang.driftc.driftc", "--dev",
	       "--stdlib-root", str(_stdlib())]
	if sanitize:
		cmd.append(f"--sanitize={sanitize}")
	cmd += [str(src), "--entry", entry, "-o", str(out_bin)]
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(90))


def _compile_and_run(tmp_path: Path, source: str, sanitize: str | None = None) -> subprocess.CompletedProcess[str]:
	res = _compile(tmp_path, source, sanitize=sanitize)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	return subprocess.run([str(tmp_path / "bin")], capture_output=True, text=True, timeout=sanitizer_timeout(30))


def _error_diags(tmp_path: Path, source: str) -> list[dict]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--stdlib-root",
		 str(_stdlib()), "--test-build-only", str(src), "--json"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	payload = json.loads(out.stdout) if out.stdout.strip() else {}
	return [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]


_PRELUDE = """\
module main;
import std.core as core;

struct Holder {
	cb: core.Callback0<String>
}

fn make(prepare: core.CallbackThrow2<&String, Bool, Holder>) -> Holder {
	val payload = "hello-" + "payload";
	val h = try prepare.call(payload, true) catch {
		Holder(cb = core.callback0(| | => { return "call-err"; }))
	};
	return move h;
}
"""


def test_nested_capture_of_nonref_outer_param_compiles_and_runs(tmp_path: Path) -> None:
	"""Inner boxed callback captures the outer lambda's `Bool` param — was the
	`__b{id}` SSA ICE; must compile and run with correct values."""
	src = _PRELUDE + """
pub fn main() nothrow -> Int {
	val prepare: core.CallbackThrow2<&String, Bool, Holder> =
		core.callback_throw2(|payload: &String, flag: Bool| => {
			return Holder(cb = core.callback0(| | => {
				val r = match flag {
					true => { "was-true" },
					false => { "was-false" }
				};
				return r;
			}));
		});
	val h = make(move prepare);
	val s = h.cb.call();
	if s == "was-true" { return 0; }
	return 1;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_nested_move_capture_of_owned_outer_local_runs_clean_asan(tmp_path: Path) -> None:
	"""Inner boxed callback takes OWNERSHIP of an owned String local of the
	outer lambda's body via `captures(move …)`; runs clean under ASAN.
	(Access via direct field call — `val cb = h.cb` is the separate
	pre-existing iface-field-copy bug.)"""
	src = _PRELUDE + """
pub fn main() nothrow -> Int {
	val prepare: core.CallbackThrow2<&String, Bool, Holder> =
		core.callback_throw2(|payload: &String, flag: Bool| => {
			val flag_note = "note-" + "n";
			return Holder(cb = core.callback0(| | captures(move flag_note) => {
				return flag_note.clone();
			}));
		});
	val h = make(move prepare);
	val s = h.cb.call();
	if s == "note-n" { return 0; }
	return 1;
}
"""
	run = _compile_and_run(tmp_path, src, sanitize="address,undefined")
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-600:]}"
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-600:]


def test_nested_implicit_borrow_capture_of_owned_outer_local_rejected(tmp_path: Path) -> None:
	"""An IMPLICIT borrow capture (`flag_note.clone()` — the `&self` method
	call classifies the capture REF ahead of the boxed MOVE default) in a
	nested boxed callback must reject: the env would store a raw pointer to
	the enclosing lambda's stack slot (was: compiled, then read a dead frame
	— wrong values / Valgrind invalid reads / double-free via drop chains)."""
	src = _PRELUDE + """
pub fn main() nothrow -> Int {
	val prepare: core.CallbackThrow2<&String, Bool, Holder> =
		core.callback_throw2(|payload: &String, flag: Bool| => {
			val flag_note = "note-" + "n";
			return Holder(cb = core.callback0(| | => {
				return flag_note.clone();
			}));
		});
	val h = make(move prepare);
	val s = h.cb.call();
	if s == "note-n" { return 0; }
	return 1;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_CALLBACK_BORROWED_CAPTURE" in codes, codes


def test_nested_capture_of_ref_outer_param_rejected(tmp_path: Path) -> None:
	"""Inner boxed callback implicitly captures the outer's `&String` param —
	must reject (was: SSA ICE shielding a dangling-pointer hazard)."""
	src = _PRELUDE + """
pub fn main() nothrow -> Int {
	val prepare: core.CallbackThrow2<&String, Bool, Holder> =
		core.callback_throw2(|payload: &String, flag: Bool| => {
			return Holder(cb = core.callback0(| | => {
				return *payload;
			}));
		});
	val h = make(move prepare);
	val s = h.cb.call();
	if s == "hello-payload" { return 1; }
	return 0;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_ESCAPE_REF_CAPTURE" in codes, codes


def test_nested_explicit_copy_of_ref_outer_param_rejected(tmp_path: Path) -> None:
	"""`captures(copy payload)` where `payload: &String` — same hazard through
	the explicit-copy path (`&T` is Copy); same rejection."""
	src = _PRELUDE + """
pub fn main() nothrow -> Int {
	val prepare: core.CallbackThrow2<&String, Bool, Holder> =
		core.callback_throw2(|payload: &String, flag: Bool| => {
			return Holder(cb = core.callback0(| | captures(copy payload) => {
				return *payload;
			}));
		});
	val h = make(move prepare);
	val s = h.cb.call();
	if s == "hello-payload" { return 1; }
	return 0;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_ESCAPE_REF_CAPTURE" in codes, codes


def test_toplevel_boxed_callback_ref_param_capture_rejected(tmp_path: Path) -> None:
	"""Non-nested control: a plain fn's `&String` param captured by a returned
	boxed callback rejects through the user-fn validation walk."""
	src = """\
module main;
import std.core as core;

fn build(s: &String) -> core.Callback0<String> {
	return core.callback0(| | => {
		return *s;
	});
}

pub fn main() nothrow -> Int {
	val owned = "x-" + "y";
	val cb = build(owned);
	val r = cb.call();
	if r == "x-y" { return 0; }
	return 1;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_ESCAPE_REF_CAPTURE" in codes, codes


def test_toplevel_explicit_copy_of_ref_param_rejected(tmp_path: Path) -> None:
	"""Top-level `captures(copy s)` where `s: &String` — `&T` is Copy, so the
	pre-existing COPY-capture type check passes; the ref-valued rule must
	still reject the raw-pointer copy into the escaping env."""
	src = """\
module main;
import std.core as core;

fn build(s: &String) -> core.Callback0<String> {
	return core.callback0(| | captures(copy s) => {
		return *s;
	});
}

pub fn main() nothrow -> Int {
	val owned = "x-" + "y";
	val cb = build(owned);
	val r = cb.call();
	if r == "x-y" { return 0; }
	return 1;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_ESCAPE_REF_CAPTURE" in codes, codes


def test_nested_capture_of_mut_ref_outer_param_rejected(tmp_path: Path) -> None:
	"""`&mut Int` outer-lambda param captured by a nested boxed callback —
	the mutable-ref flavor of the same raw-pointer escape; same rejection."""
	src = """\
module main;
import std.core as core;

struct Holder {
	cb: core.Callback0<Int>
}

fn make(prepare: core.CallbackThrow2<&mut Int, Bool, Holder>) -> Holder {
	var counter = 41;
	val h = try prepare.call(counter, true) catch {
		Holder(cb = core.callback0(| | => { return -1; }))
	};
	return move h;
}

pub fn main() nothrow -> Int {
	val prepare: core.CallbackThrow2<&mut Int, Bool, Holder> =
		core.callback_throw2(|counter: &mut Int, flag: Bool| => {
			return Holder(cb = core.callback0(| | => {
				return *counter + 1;
			}));
		});
	val h = make(move prepare);
	val n = h.cb.call();
	if n == 42 { return 1; }
	return 0;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_ESCAPE_REF_CAPTURE" in codes, codes


def test_toplevel_capture_of_optional_ref_rejected(tmp_path: Path) -> None:
	"""`Optional<&String>` param captured by a returned boxed callback — the
	optional-ref flavor handled by `_is_ref_valued_type`; same rejection."""
	src = """\
module main;
import std.core as core;

fn build(maybe: Optional<&String>) -> core.Callback0<String> {
	return core.callback0(| | => {
		return match maybe {
			Some(s) => { *s },
			None => { "none" }
		};
	});
}

pub fn main() nothrow -> Int {
	val owned = "x-" + "y";
	val maybe: Optional<&String> = Some(&owned);
	val cb = build(maybe);
	val r = cb.call();
	if r == "x-y" { return 0; }
	return 1;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_ESCAPE_REF_CAPTURE" in codes, codes


def test_nested_captureless_callback_still_works(tmp_path: Path) -> None:
	"""Control: nested boxed callback with no captures keeps working."""
	src = _PRELUDE + """
pub fn main() nothrow -> Int {
	val prepare: core.CallbackThrow2<&String, Bool, Holder> =
		core.callback_throw2(|payload: &String, flag: Bool| => {
			return Holder(cb = core.callback0(| | => { return "static-x"; }));
		});
	val h = make(move prepare);
	val s = h.cb.call();
	if s == "static-x" { return 0; }
	return 1;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_sync_ref_params_with_owned_captures_still_accepted(tmp_path: Path) -> None:
	"""Web/rest-style control: the callback's OWN params are refs (used
	synchronously) and its captures are owned values — must stay accepted."""
	src = """\
module main;
import std.core as core;

struct Req { method: String }

fn dispatch(cb: core.Callback2<&Req, Bool, String>, req: &Req) -> String {
	return cb.call(req, true);
}

pub fn main() nothrow -> Int {
	val tag = "t-" + "1";
	val cb: core.Callback2<&Req, Bool, String> =
		core.callback2(|req: &Req, verbose: Bool| captures(copy tag) => {
			return tag.clone() + ":" + req.method.clone();
		});
	val req = Req(method = "GET");
	val out = dispatch(move cb, req);
	if out == "t-1:GET" { return 0; }
	return 1;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-600:]}"
