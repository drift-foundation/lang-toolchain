# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression-first pins for the two E-population LANGUAGE_BUGs
(E-population triage, fixed at the source in 0.33.83 — doc/history.md;
shapes 1 and 2).

Both bugs share one observable: a value silently reads as ZERO/empty
because storage was zero-backed by an implicit consume the checker never
tracked or rejected.

Shape 1 — re-match of a consumed scrutinee.  `match r` on a non-Copy
scrutinee CONSUMES it (the lowering moves it into the arm scrutinee temp
and zero-backs the source); a second `match r` (or any later use) then
read zeroed storage: probe `Ok(5)` re-matched to a binder read 0, and a
non-Copy String payload read "".  DECIDED SEMANTICS (this fix): by-value
match of a non-Copy scrutinee consumes; every later use of the scrutinee
— including a re-match — is rejected with E_USE_AFTER_MOVE.  The
ownership-preserving escape is matching a borrow.  Copy-classified
scrutinees are copied by the existing lowering branch and stay live.

Shape 2 — the explicit-move call-arg gate never reached match-arm
BODIES.  `_walk_expr_for_borrowed_boundaries`'s HMatchExpr case walked
the scrutinee and `arm.result` but not `arm.block`, so a bare non-Copy
binder at a by-value call arg inside a statement-form arm was accepted;
`_lower_call_arg`'s internal MoveOut backstop then consumed it silently
(the std_io fixtures' would-block paths misbehaved at runtime today).
The fix restores the EXISTING rejection contract (spec §1.3 "use move")
at call-arg/value positions.  The match SCRUTINEE stays the language's
ONE deliberate implicit-consume exception (bare `match r` legal and
consuming — pinned below); the refactor-trigger ruling (considered, not
fired, with that recorded exception and the capture-slot per-site
containment) is recorded in doc/refactor_triggers.md.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_REMATCH_COPYABLE_PAYLOAD = """\
module m;

import std.core as core;

pub fn main() nothrow -> Int {
	val r: core.Result<Int, Int> = core.Result::Ok(5);
	match r {
		Ok(v) => {
			match r {
				Ok(w) => { if w != 5 { return 1; } },
				Err(_) => { return 2; },
				default => { return 3; }
			}
			return 0;
		},
		Err(_) => { return 4; },
		default => { return 5; }
	}
}
"""

_REMATCH_STRING_PAYLOAD = """\
module m;

import std.core as core;

pub fn main() nothrow -> Int {
	val r: core.Result<String, Int> = core.Result::Ok("payload");
	match r {
		Ok(v) => {
			match r {
				Ok(w) => { if w.byte_length() != 7 { return 1; } },
				Err(_) => { return 2; },
				default => { return 3; }
			}
			return 0;
		},
		Err(_) => { return 4; },
		default => { return 5; }
	}
}
"""

_USE_AFTER_MATCH = """\
module m;

import std.core as core;

fn consume_result(r: core.Result<String, Int>) nothrow -> Int {
	val _ = move r;
	return 0;
}

pub fn main() nothrow -> Int {
	val r: core.Result<String, Int> = core.Result::Ok("x");
	match r {
		Ok(_) => { },
		Err(_) => { return 1; },
		default => { return 2; }
	}
	return consume_result(move r);
}
"""

# Two modules: the error lives in an IMPORT-LESS module, so ConstShare
# synthesis cannot qualify its String field against that module's
# import-visible trait world (the probe-verified refusal) — no implicit
# const_share wrap is available and the gate must REJECT the bare pass.
# (A same-module error would legally const-share-wrap instead.)
_ARM_BODY_ERRS_MODULE = """\
module errs;

export { LocalErr, code_of, mk };

pub error LocalErr { kind: String, code: Int }

pub fn code_of(e: LocalErr) nothrow -> Int {
	return e.code;
}

pub fn mk() nothrow -> core.Result<Int, LocalErr> {
	return core.Result::Err(LocalErr(kind = "errno", code = 2));
}

import std.core as core;
"""

_ARM_BODY_BARE_NONCOPY_CALL = """\
module m;

import errs as er;

pub fn main() nothrow -> Int {
	val r = er.mk();
	match r {
		Ok(_) => { return 9; },
		Err(e) => {
			val a = er.code_of(e);
			val b = er.code_of(e);
			if a == 2 {
				if b == 2 { return 0; }
				return 1;
			}
			return 2;
		},
		default => { return 8; }
	}
}
"""

# The DELIBERATE language exception (recorded in the trigger ruling):
# bare `match r` on a non-Copy place scrutinee is LEGAL and CONSUMING —
# no `move r` spelling is required at the scrutinee position (unlike
# every other consuming position).  A match with no later scrutinee use
# must therefore compile and run.
_BARE_MATCH_EXCEPTION = """\
module m;

import std.core as core;

pub fn main() nothrow -> Int {
	val r: core.Result<String, Int> = core.Result::Ok("ok");
	match r {
		Ok(v) => {
			if v.byte_length() == 2 { return 0; }
			return 1;
		},
		Err(_) => { return 2; },
		default => { return 3; }
	}
}
"""

# Capture-slot scrutinee routing (the implicit-move refactor trigger's
# predicted per-site hole, probe-confirmed on certified 0.33.82): a
# match on a MOVE-CAPTURED non-Copy scrutinee inside a callback lambda
# read ZEROED payload bytes — the tag dispatch (whose HVar read is
# capture-aware) chose the right arm while `_ensure_arm_scrut_ptr`'s
# consume targeted the never-materialized LOCAL.  Fixed by routing the
# arm consume through `_move_from_callback_capture_slot`.
_MOVE_CAPTURED_SCRUTINEE = """\
module m;

import std.core as core;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
	var r: core.Result<String, Int> = core.Result::Ok("cap-" + "payload");
	var vt = conc.spawn_cb(| | captures(move r) => {
		match r {
			Ok(v) => { return v.byte_length(); },
			Err(_) => { return -1; },
			default => { return -2; }
		}
	});
	match vt.join() {
		Ok(n) => {
			if n == 11 { return 0; }
			return 1;
		},
		Err(_) => { return 2; },
		default => { return 3; }
	}
}
"""

_IO_PREDICATES_BY_REF_INTENT = """\
module m;

import std.io as io;

pub fn main() nothrow -> Int {
	val wb = io.IoError(kind = io.IO_ERROR_KIND_ERRNO, code = io.IO_ERR_WOULD_BLOCK);
	if io.is_eof_error(wb) { return 1; }
	if io.is_would_block_error(wb) {
		if io.io_error_code(wb) == io.IO_ERR_WOULD_BLOCK { return 0; }
		return 2;
	}
	return 3;
}
"""


def _compile(tmp_path: Path, source: str, extra_sources: list[Path] | None = None):
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), *[str(p) for p in (extra_sources or [])],
		 "--entry", "m::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240), env=os.environ.copy(),
	)
	return res, out


def test_rematch_of_consumed_scrutinee_rejected_copyable_payload(tmp_path: Path) -> None:
	"""Shape 1: `Result<Int, Int>` is NOT Copy (the checker already
	rejects `val s = r`), so the re-match must reject too — pre-fix it
	compiled and the inner binder read 0 instead of 5."""
	res, out = _compile(tmp_path, _REMATCH_COPYABLE_PAYLOAD)
	if res.returncode == 0:
		run = subprocess.run([str(out)], capture_output=True, text=True,
		                     timeout=sanitizer_timeout(20))
		raise AssertionError(
			f"re-match of a consumed scrutinee compiled; run exit "
			f"{run.returncode} (0 would mean the inner binder read the "
			f"true value; pre-fix it read ZERO and exited 1)"
		)
	assert "E_USE_AFTER_MOVE" in res.stderr or "use after move" in res.stderr, res.stderr[-800:]


def test_rematch_of_consumed_scrutinee_rejected_string_payload(tmp_path: Path) -> None:
	"""Shape 1, non-Copy payload: pre-fix the inner binder silently read
	an EMPTY string."""
	res, out = _compile(tmp_path, _REMATCH_STRING_PAYLOAD)
	if res.returncode == 0:
		run = subprocess.run([str(out)], capture_output=True, text=True,
		                     timeout=sanitizer_timeout(20))
		raise AssertionError(
			f"re-match of a consumed scrutinee compiled; run exit "
			f"{run.returncode} (pre-fix the binder read \"\" and exited 1)"
		)
	assert "E_USE_AFTER_MOVE" in res.stderr or "use after move" in res.stderr, res.stderr[-800:]


def test_use_after_match_rejected(tmp_path: Path) -> None:
	"""Shape 1 corollary: ANY later use of the consumed scrutinee (here
	an explicit `move r` into a call) is a use-after-move."""
	res, _ = _compile(tmp_path, _USE_AFTER_MATCH)
	assert res.returncode != 0, "use of scrutinee after a consuming match must reject"
	assert "E_USE_AFTER_MOVE" in res.stderr or "use after move" in res.stderr, res.stderr[-800:]


def test_arm_body_bare_noncopy_call_arg_rejected(tmp_path: Path) -> None:
	"""Shape 2: a bare non-Copy non-ConstShare-provable binder at a
	by-value call arg inside a match-arm BODY must get the standard
	`cannot copy ... (use move ...)` rejection.  Pre-fix the boundary
	walk skipped arm bodies and the second call read a zeroed binder
	(probe: both predicates returned false — "took NEITHER")."""
	errs = tmp_path / "errs.drift"
	errs.write_text(_ARM_BODY_ERRS_MODULE)
	res, out = _compile(tmp_path, _ARM_BODY_BARE_NONCOPY_CALL, extra_sources=[errs])
	if res.returncode == 0:
		run = subprocess.run([str(out)], capture_output=True, text=True,
		                     timeout=sanitizer_timeout(20))
		raise AssertionError(
			f"bare non-Copy binder call-arg in arm body compiled; run "
			f"exit {run.returncode} (pre-fix the second call read a "
			f"ZEROED binder)"
		)
	assert "cannot copy 'e'" in res.stderr and "use move e" in res.stderr, res.stderr[-800:]


def test_bare_match_exception_legal_and_consuming(tmp_path: Path) -> None:
	"""The recorded language exception: bare `match r` on a non-Copy
	place scrutinee needs no `move` spelling (consumption is implicit
	and tracked; test_use_after_match_rejected pins the tracking half)."""
	res, out = _compile(tmp_path, _BARE_MATCH_EXCEPTION)
	assert res.returncode == 0, res.stderr[-1200:]
	run = subprocess.run([str(out)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(20))
	assert run.returncode == 0, f"exit {run.returncode}"


def test_move_captured_scrutinee_match_reads_true_payload(tmp_path: Path) -> None:
	"""Match on a move-captured non-Copy scrutinee inside a callback
	lambda must read the REAL payload.  Pre-fix (incl. certified
	0.33.82) the arm consume read the never-materialized local — zeroed
	bytes, empty payload — and this pin exits 1."""
	res, out = _compile(tmp_path, _MOVE_CAPTURED_SCRUTINEE)
	assert res.returncode == 0, res.stderr[-1200:]
	run = subprocess.run([str(out)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(60))
	assert run.returncode == 0, (
		f"exit {run.returncode} — 1 means the arm binder saw a zeroed "
		f"payload (the capture-slot routing regression)"
	)


def test_io_error_predicates_take_borrow(tmp_path: Path) -> None:
	"""The stdlib intent path: the IoError classification predicates
	take `&IoError`, so classifying an error never consumes it."""
	res, out = _compile(tmp_path, _IO_PREDICATES_BY_REF_INTENT)
	assert res.returncode == 0, res.stderr[-1200:]
	run = subprocess.run([str(out)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(20))
	assert run.returncode == 0, f"exit {run.returncode}: {run.stderr[-400:]}"
