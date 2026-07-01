# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""A `match` arm whose trailing result `move`s a local declared in the SAME
arm body must yield that value — not a zeroed variant (CORE_BUG).

`pattern => { val out = f(); move out }` is a single block expression: the
trailing `move out` shares the arm body's scope and consumes `out`.  The
HIR→MIR lowering used to route the arm body through `lower_block`, which pushed
a NESTED scope and emitted its scope-cleanup `CleanupHook` (candidate `out`)
BEFORE `arm.result` was lowered.  `cleanup_authoring` then saw `out` as still
LIVE at that hook and authored a drop — running the destructor AND zeroing the
storage that the subsequent `move out` read.  For a nested variant value
(`Result<Optional<Array<Byte>>, _>`) the zeroed storage decodes as `Ok(None)`,
so a present value was observed as absent (and its heap payload was freed — a
latent UAF).

Reported by drift-query (lmdb-engine-poc) as BUG-2: a `Storage.get` of a present
key returned `Some` internally but the caller observed `None`, through a
`with_read_txn<R>` driver returning `move out` as a `match` arm tail.  The
defect is NOT specific to generics, dynamic dispatch, callbacks, or prior
mutation — the minimal trigger is a plain `match` arm returning `move <local>`.

Fixed by lowering the arm body statements directly into the arm scope so the
consumed local's cleanup defers to the arm-end hook, which runs AFTER the
result's `MoveOut`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--target-word-bits", "64",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	return subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))


def test_match_arm_move_local_nested_variant_result(tmp_path: Path) -> None:
	"""`Ok(t) => { val out = get(0); move out }` yields the real nested-variant
	value, not a zeroed `Ok(None)`."""
	src = """\
module main;
import std.core as core;
fn begin() nothrow -> core.Result<Int, Int> { return core.Result::Ok(0); }
fn get(k: Int) nothrow -> core.Result<Optional<Array<Byte> >, Int> {
	val _ = k;
	var a: Array<Byte> = [];
	a.push(cast<Byte>(7));
	return core.Result::Ok(Optional::Some(move a));
}
fn drive() nothrow -> core.Result<Optional<Array<Byte> >, Int> {
	return match begin() {
		Err(e) => { core.Result::Err(e) },
		Ok(t) => { val _ = t; val out = get(0); move out }
	};
}
pub fn main() nothrow -> Int {
	match drive() {
		Err(e) => { return 1; },
		Ok(opt) => {
			match opt {
				Some(v) => { return cast<Int>(v[0]); },
				None => { return 2; }
			}
		}
	}
}
"""
	# Some(v) with v[0]==7 -> returns 7; the bug returned 2 (None).
	assert _compile_and_run(tmp_path, src).returncode == 7


def test_match_arm_move_local_string_result(tmp_path: Path) -> None:
	"""Same shape with a droppable `String` payload: the value survives and is
	not double-freed (program runs clean, returns the right branch)."""
	src = """\
module main;
import std.core as core;
fn begin() nothrow -> core.Result<Int, Int> { return core.Result::Ok(0); }
fn pick(k: Int) nothrow -> Optional<String> {
	val _ = k;
	var s = "hello";
	return Optional::Some(move s);
}
fn drive() nothrow -> Optional<String> {
	return match begin() {
		Err(e) => { val _ = e; Optional<String>::None() },
		Ok(t) => { val _ = t; val out = pick(0); move out }
	};
}
pub fn main() nothrow -> Int {
	match drive() {
		Some(s) => { return s.byte_length(); },
		None => { return 99; }
	}
}
"""
	# "hello" -> byte_length 5; the bug returned 99 (None).
	assert _compile_and_run(tmp_path, src).returncode == 5


def test_match_arm_direct_result_still_works(tmp_path: Path) -> None:
	"""Control: an arm whose result is a direct call (no intermediate local) was
	never affected and still works."""
	src = """\
module main;
import std.core as core;
fn begin() nothrow -> core.Result<Int, Int> { return core.Result::Ok(0); }
fn get(k: Int) nothrow -> core.Result<Optional<Array<Byte> >, Int> {
	val _ = k;
	var a: Array<Byte> = [];
	a.push(cast<Byte>(9));
	return core.Result::Ok(Optional::Some(move a));
}
fn drive() nothrow -> core.Result<Optional<Array<Byte> >, Int> {
	return match begin() {
		Err(e) => { core.Result::Err(e) },
		Ok(t) => { val _ = t; get(0) }
	};
}
pub fn main() nothrow -> Int {
	match drive() {
		Err(e) => { return 1; },
		Ok(opt) => { match opt { Some(v) => { return cast<Int>(v[0]); }, None => { return 2; } } }
	}
}
"""
	assert _compile_and_run(tmp_path, src).returncode == 9
