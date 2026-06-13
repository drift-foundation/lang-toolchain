# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""End-to-end regression for `Array<T>.clone()` (stdlib slice 2026-05-24).

Background: `String.clone()` exists (cheap refcount bump on shared
backing) but `Array<T>.clone()` did not, even for `T: Copy`.  App team
hit this drafting cross-VT fan-out where each spawned task needs its
own owned copy of a small `Array<Byte>` (UUID material).

Coverage:
- Compile + run an Array<Byte>.clone() round-trip and assert the
  clone matches element-by-element AND survives independently after
  the source is mutated.
- Same for Array<Int> (sanity for a different Copy element type).
- Same for Array<String> (Copy but non-bitcopy; clone must retain
  element ownership correctly).
- Empty-array clone (len 0 / no over-allocation crash).
- Non-Copy element rejection (Array<UnCopyable>.clone() must NOT
  type-check) — pins the `require T is core.Copy` gate so future
  refactors can't accidentally widen the surface.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout


ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str, *, entry: str = "m::main") -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str] | None]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "test_bin"
	env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
	env["PYTHONPATH"] = str(ROOT)
	build = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc.driftc",
			"--stdlib-root", str(ROOT / "stdlib"),
			str(src),
			"--entry", entry,
			"-o", str(out_bin),
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(120),
		env=env,
	)
	if build.returncode != 0:
		return (build, None)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	return (build, run)


_SOURCE_BYTE_ROUND_TRIP = """
module m;

pub fn main() nothrow -> Int {
	var src: Array<Byte> = [];
	src.push(cast<Byte>(0x10));
	src.push(cast<Byte>(0x20));
	src.push(cast<Byte>(0x30));
	src.push(cast<Byte>(0x40));

	val dup = src.clone();

	// Element-equality check.
	if dup.len != src.len { return 1; }
	var i = 0;
	while i < src.len {
		if dup[i] != src[i] { return 2; }
		i = i + 1;
	}

	// Mutate src; dup must not observe the change (independent buffer).
	src.push(cast<Byte>(0xFF));
	if dup.len == src.len { return 3; }
	if dup.len != 4 { return 4; }

	return 0;
}
"""


def test_array_byte_clone_round_trip_and_independence(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _SOURCE_BYTE_ROUND_TRIP)
	assert build.returncode == 0, (
		f"compile failed:\n--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	assert run is not None
	assert run.returncode == 0, (
		f"program exit {run.returncode} indicates clone semantic failure:\n"
		f"--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
	)


_SOURCE_INT_AND_EMPTY = """
module m;

pub fn main() nothrow -> Int {
	// Empty array clone must succeed and produce an empty result.
	val empty: Array<Int> = [];
	val empty_dup = empty.clone();
	if empty_dup.len != 0 { return 10; }

	// Array<Int>.clone() preserves order and values.
	var ints: Array<Int> = [];
	ints.push(7);
	ints.push(-3);
	ints.push(42);
	val ints_dup = ints.clone();
	if ints_dup.len != 3 { return 11; }
	if ints_dup[0] != 7   { return 12; }
	if ints_dup[1] != -3  { return 13; }
	if ints_dup[2] != 42  { return 14; }

	return 0;
}
"""


def test_array_int_clone_and_empty_clone(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _SOURCE_INT_AND_EMPTY)
	assert build.returncode == 0, (
		f"compile failed:\n--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	assert run is not None
	assert run.returncode == 0, (
		f"program exit {run.returncode} indicates Int/empty clone failure:\n"
		f"--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
	)


_SOURCE_CLONE_WITH_CAPACITY = """
module m;

pub fn main() nothrow -> Int {
	var src: Array<Byte> = [];
	src.push(cast<Byte>(0x10));
	src.push(cast<Byte>(0x20));
	src.push(cast<Byte>(0x30));

	// Clone with extra headroom — every src element lands AND the
	// returned cap reflects the requested capacity (the entire point
	// of this method vs plain clone()).
	val dup_big = src.clone_with_capacity(src.len + 64);
	if dup_big.len != src.len { return 30; }
	var i = 0;
	while i < src.len {
		if dup_big[i] != src[i] { return 31; }
		i = i + 1;
	}
	// Cap contract: `out.cap >= max(self.len, capacity)` = src.len + 64.
	// A future regression where clone_with_capacity() silently became plain
	// clone() (cap == len) would fail right here, even though len/elements
	// would still match.
	if dup_big.cap < src.len + 64 { return 32; }

	// Capacity smaller than len degrades to plain clone() behavior:
	// every element still copies; no truncation; cap floored at len.
	val dup_small = src.clone_with_capacity(1);
	if dup_small.len != src.len { return 33; }
	if dup_small[0] != src[0] { return 34; }
	if dup_small[2] != src[2] { return 35; }
	if dup_small.cap < src.len { return 36; }

	// Zero capacity degrades to clone() (cap floored at len).
	val dup_zero = src.clone_with_capacity(0);
	if dup_zero.len != src.len { return 37; }
	if dup_zero.cap < src.len { return 38; }

	// Negative capacity also degrades to clone() — must not error,
	// must not under-allocate.
	val dup_neg = src.clone_with_capacity(-5);
	if dup_neg.len != src.len { return 39; }
	if dup_neg.cap < src.len { return 40; }

	// Empty source + non-zero capacity: len stays 0 AND cap honors
	// the request (this is the case where len/elements alone would
	// fail to distinguish clone_with_capacity from clone).
	val empty: Array<Int> = [];
	val empty_dup = empty.clone_with_capacity(16);
	if empty_dup.len != 0 { return 41; }
	if empty_dup.cap < 16 { return 42; }

	// Mutate src; dup_big must not observe the change.
	src.push(cast<Byte>(0xFF));
	if dup_big.len == src.len { return 43; }

	return 0;
}
"""


def test_array_clone_with_capacity(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _SOURCE_CLONE_WITH_CAPACITY)
	assert build.returncode == 0, (
		f"compile failed:\n--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	assert run is not None
	assert run.returncode == 0, (
		f"program exit {run.returncode} indicates clone_with_capacity failure:\n"
		f"--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
	)


_SOURCE_STRING_ROUND_TRIP = """
module m;

fn heap_str(tag: String) nothrow -> String {
	val prefix = "z-";
	return prefix + tag;
}

pub fn main() nothrow -> Int {
	var src: Array<String> = [];
	var s = heap_str("alpha");
	src.push(s);
	s = heap_str("beta");
	src.push(s);

	val dup = src.clone();
	if dup.len != 2 { return 20; }
	if dup[0].byte_length() != 7 { return 21; }
	if dup[1].byte_length() != 6 { return 22; }

	s = heap_str("gamma");
	src.push(s);
	if src.len != 3 { return 23; }
	if dup.len != 2 { return 24; }
	if dup[0].byte_length() != 7 { return 25; }
	if dup[1].byte_length() != 6 { return 26; }
	return 0;
}
"""


def test_array_string_clone_round_trip_and_independence(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _SOURCE_STRING_ROUND_TRIP)
	assert build.returncode == 0, (
		f"compile failed:\n--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	assert run is not None
	assert run.returncode == 0, (
		f"program exit {run.returncode} indicates String clone semantic failure:\n"
		f"--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
	)


_SOURCE_NON_COPY_REJECTED = """
module m;

import std.core as core;

// `Holder` is not Copy (no impl), and is Destructible — it's exactly
// the element shape `clone()` is gated against.
struct Holder { x: Int }

implement core.Destructible for Holder {
	pub fn destroy(var self: Holder) nothrow -> Void {
		// No-op: existence of the impl makes Holder non-Copy.
	}
}

pub fn main() nothrow -> Int {
	var a: Array<Holder> = [];
	a.push(Holder(x = 1));
	val _dup = a.clone();   // EXPECTED: type-check rejection
	return 0;
}
"""


def test_array_clone_rejects_non_copy_element(tmp_path: Path) -> None:
	build, _ = _compile_and_run(tmp_path, _SOURCE_NON_COPY_REJECTED)
	# Must NOT compile due to the `T is core.Copy` bound.
	assert build.returncode != 0, (
		"Array<Holder>::clone() must be rejected (Holder is not Copy), but compile succeeded.\n"
		f"--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	combined = build.stdout + build.stderr
	assert "Copy" in combined or "clone" in combined, (
		"Array<Holder>::clone() rejected for an unexpected reason:\n"
		f"--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	assert "Traceback" not in combined
