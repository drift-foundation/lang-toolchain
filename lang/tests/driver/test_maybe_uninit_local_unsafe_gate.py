# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Driver pin: standalone-local `MaybeUninit<T>` pattern requires
unsafe.

Pre-0.31.16 the standalone-local form was unreachable because the
`mem.maybe_uninit` constructor's HIR→MIR lowering raised
`NotImplementedError`.  After the lowering landed, the pattern
must remain `unsafe`-gated by the standing intrinsic contract:
all `mem.maybe_*` intrinsics are declared `pub unsafe fn …` in
`stdlib/std/mem/mem.drift`, so a call from a safe context (no
surrounding `unsafe { … }` block, no `--allow-unsafe`) must
produce the standard "unsafe call requires --allow-unsafe"
diagnostic.

This pins two facts together to keep them honest:

1. **Inside `unsafe { … }` with `--allow-unsafe`**, the canonical
   write/read round-trip compiles and runs end-to-end.
2. **Without `--allow-unsafe`**, the same source produces the
   expected unsafe diagnostic and fails to compile.

If the diagnostic surface ever changes (e.g. specialized error
code for `mem.maybe_*`, or a new "MaybeUninit-specific" gate),
update this test in lockstep — the standing rule is *all*
`mem.maybe_*` intrinsics are unsafe by virtue of their stdlib
signatures, not by some local checker rule.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_STDLIB = _ROOT / "stdlib"


_SOURCE = """\
module main;

import std.core as core;
import std.mem as mem;

pub struct Box {
\tpub n: Int,
}

implement core.Destructible for Box {
\tpub fn destroy(var self: Box) nothrow -> Void {
\t\treturn;
\t}
}

pub fn main() nothrow -> Int {
\tval b = Box(n = 5);
\tunsafe {
\t\tvar slot = mem.maybe_uninit<type Box>();
\t\tmem.maybe_write<type Box>(&mut slot, b);
\t\tval b2 = mem.maybe_assume_init_read<type Box>(&mut slot);
\t\tcore.drop_value<type Box>(b2);
\t}
\treturn 0;
}
"""


def _compile(tmp_path: Path, *, allow_unsafe: bool) -> subprocess.CompletedProcess:
	src = tmp_path / "main.drift"
	src.write_text(_SOURCE)
	out_bin = tmp_path / "bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--stdlib-root", str(_STDLIB),
		"--dev",
		str(src),
		"--entry", "main::main",
		"-o", str(out_bin),
	]
	if allow_unsafe:
		cmd.append("--allow-unsafe")
	# Merge PYTHONPATH into the inherited env so the linker driver
	# (clang) can resolve gold via PATH; empty-env runs lose PATH
	# and fail with `invalid linker name in argument '-fuse-ld=gold'`.
	return subprocess.run(
		cmd, capture_output=True, text=True, cwd=str(_ROOT),
		env={**os.environ, "PYTHONPATH": "."}, timeout=120,
	)


def test_maybe_uninit_local_compiles_under_unsafe(tmp_path: Path) -> None:
	"""Positive: with `--allow-unsafe` and a surrounding `unsafe { }`
	block, the constructor + write + read pattern compiles to a
	working binary."""
	res = _compile(tmp_path, allow_unsafe=True)
	assert res.returncode == 0, (
		f"standalone-local MaybeUninit pattern failed to compile "
		f"under --allow-unsafe.\n"
		f"stderr:\n{res.stderr[:1500]}\n"
		f"stdout:\n{res.stdout[:1500]}"
	)
	out_bin = tmp_path / "bin"
	assert out_bin.exists()
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=30)
	assert run.returncode == 0, (
		f"compiled binary did not exit 0: rc={run.returncode}, "
		f"stderr={run.stderr[:300]!r}"
	)


def test_maybe_uninit_local_rejected_without_allow_unsafe(tmp_path: Path) -> None:
	"""Negative: without `--allow-unsafe`, the same source must
	fail to compile and surface the canonical unsafe diagnostic.

	`mem.maybe_uninit` is declared `pub unsafe fn …` in
	`stdlib/std/mem/mem.drift`, so this is enforced by the shared
	`unsafe call requires --allow-unsafe` checker rule
	(`lang/driftc/checker/unsafe_gate.py`,
	`lang/driftc/type_checker.py:5198`).  No new diagnostic is
	added by the standalone-local feature."""
	res = _compile(tmp_path, allow_unsafe=False)
	assert res.returncode != 0, (
		f"standalone-local MaybeUninit pattern compiled WITHOUT "
		f"--allow-unsafe; the unsafe gate regressed for "
		f"`mem.maybe_*` intrinsics.\n"
		f"stdout:\n{res.stdout[:1500]}"
	)
	combined = (res.stdout or "") + (res.stderr or "")
	assert "--allow-unsafe" in combined, (
		f"expected the diagnostic to mention `--allow-unsafe` "
		f"(canonical phrasing from `unsafe_gate.py`); got:\n"
		f"stdout:\n{res.stdout[:1500]}\n"
		f"stderr:\n{res.stderr[:1500]}"
	)
