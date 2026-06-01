# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: a dotted `-o` value must not collapse driftc's scratch IR/obj
paths, so concurrent compiles of `a.b.x` and `a.b.y` don't clobber each other.

driftc derives its scratch LLVM IR (`.ll`) and intermediate object (`.ir.o`)
paths from `-o`.  It used `Path.with_suffix`, which REPLACES the last
dot-segment: `-o web-jwt.unit.claims_test#plain` and
`-o web-jwt.unit.claims_test#asan` both collapsed to `web-jwt.unit.ll`, so two
distinct targets sharing a dotted prefix wrote the SAME scratch IR file.  Under
a parallel test/build runner the concurrent compiles clobbered each other →
intermittent corrupt-IR link failures.  Reported by drift-web (3 such failures
on first adoption of the shared runner).  Fix: append the extension instead of
replacing, so every distinct `-o` maps to a distinct scratch path.

This pins driftc directly (independent of any runner): two dotted outputs
sharing a prefix each emit IR, and neither's IR is the other's.
"""
from __future__ import annotations

import io
import contextlib
from pathlib import Path

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


_SRC = """
module main;
fn main() nothrow -> Int { return %d; }
"""


def _compile(tmp_path: Path, out_name: str, ret: int) -> tuple[int, Path]:
	src = tmp_path / f"{out_name.replace('#', '_').replace('.', '_')}.drift"
	src.write_text(_SRC % ret, encoding="utf-8")
	out = tmp_path / out_name
	argv = [
		"--target-word-bits", "64",
		"-M", str(tmp_path), str(src),
		"--entry", "main::main",
		"--emit-ir", str(tmp_path / f"{out_name}.user.ll"),
		"-o", str(out),
	]
	root = stdlib_root()
	if root:
		argv += ["--stdlib-root", str(root)]
	buf = io.StringIO()
	with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
		try:
			rc = driftc_main(argv)
		except SystemExit as e:
			rc = int(e.code) if e.code is not None else 0
	return rc, out


def test_dotted_outputs_sharing_prefix_get_distinct_scratch_ll(tmp_path: Path) -> None:
	# Two targets that, under the old with_suffix derivation, both collapsed to
	# `web-jwt.unit.ll`.
	rc1, out1 = _compile(tmp_path, "web-jwt.unit.claims_test#plain", 1)
	assert rc1 == 0
	rc2, out2 = _compile(tmp_path, "web-jwt.unit.claims_test#asan", 2)
	assert rc2 == 0

	# The scratch IR for each is now <output>.ll (append), distinct per target.
	ll1 = Path(str(out1) + ".ll")
	ll2 = Path(str(out2) + ".ll")
	assert ll1.exists(), f"missing scratch IR {ll1}"
	assert ll2.exists(), f"missing scratch IR {ll2}"
	assert ll1 != ll2

	# The OLD collapsed path must NOT be what either wrote (proves no collision).
	collapsed = tmp_path / "web-jwt.unit.ll"
	# (with_suffix('.ll') of either output)
	assert Path(str(out1)).with_suffix(".ll") == collapsed
	# Both real scratch files are distinct from the collapsed name.
	assert ll1 != collapsed and ll2 != collapsed

	# Both binaries built and are distinct files.
	assert out1.exists() and out2.exists()
	assert out1 != out2


def test_dotfree_output_scratch_unchanged(tmp_path: Path) -> None:
	# Dot-free names behave exactly as before (append == with_suffix here),
	# so existing callers that reconstruct `<out>.with_suffix('.ll')` still match.
	rc, out = _compile(tmp_path, "plain_bin", 7)
	assert rc == 0
	assert Path(str(out) + ".ll").exists()
	assert Path(str(out) + ".ll") == out.with_suffix(".ll")
