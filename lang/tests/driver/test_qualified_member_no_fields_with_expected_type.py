# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: typing a no-fields qualified-member variant ctor reference
(e.g. `Maybe<Int>::None`) in an expression position with `expected_type`
propagated MUST NOT crash on a stale `TypeKind.VARIANT_INSTANCE` reference.

The original crash was an `AttributeError` at `type_checker.py:6952`
attempting to read a `TypeKind` enum member that does not exist (only
`TypeKind.VARIANT` does); the same code line referenced a non-existent
`TypeDef.base_type_id` attribute.  A sibling stale reference lived at
`lang/driftc/checker/typed_validator.py:93`.

App-team-reported crash shape:

  outer match on VirtualThread<Optional<gw.ClaimResult>>::join() result
  -> inner Optional match arm
  -> AttributeError: TypeKind.VARIANT_INSTANCE

Minimal repro the test exercises: a bare qualified-member-with-explicit-
type-args reference to a no-fields variant ctor (`Maybe<Int>::None`)
used as the value of a `return` whose function return type is the same
variant instance.  That's the precise shape that flows expected_type
into the HQualifiedMember branch and trips the guarded short-circuit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str] | None]:
	"""Returns (compile_result, run_result_or_None_when_compile_failed)."""
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
			"--entry", "m::main",
			"-o", str(out_bin),
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=120,
		env=env,
	)
	if build.returncode != 0:
		return (build, None)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=30)
	return (build, run)


_SOURCE = """
module m;

variant Maybe<T> { Some(x: T), None }

fn pick() -> Maybe<Int> {
	return Maybe<Int>::None;
}

pub fn main() nothrow -> Int {
	val r = pick();
	match r {
		Maybe<Int>::Some(x) => { return x; },
		Maybe<Int>::None    => { return 0; },
	}
}
"""


def test_no_fields_qualified_member_with_expected_type_does_not_crash(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _SOURCE)
	# Pre-fix: AttributeError aborts driftc; the crash text appears on stderr.
	assert "AttributeError" not in build.stderr, (
		"driftc crashed on stale TypeKind.VARIANT_INSTANCE reference:\n"
		f"--- stderr ---\n{build.stderr}"
	)
	assert "VARIANT_INSTANCE" not in build.stderr, (
		f"unexpected VARIANT_INSTANCE reference in stderr:\n{build.stderr}"
	)
	assert build.returncode == 0, (
		f"compile failed (rc={build.returncode}):\n"
		f"--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	# Bug crossed checker → HIR→MIR → codegen.  Compile-success alone does not
	# prove `Maybe<Int>::None` lowered with the correct ctor tag.  Execute the
	# binary: `pick()` returns the None variant, the match dispatches to the
	# `Maybe<Int>::None` arm, which returns 0 — so a non-zero exit means the
	# tag/dispatch is wrong even though the type checker accepted the program.
	assert run is not None
	assert run.returncode == 0, (
		f"program produced unexpected exit (rc={run.returncode}) — "
		"likely a codegen/dispatch error: the None arm should return 0 but did not. "
		f"\n--- run stdout ---\n{run.stdout}\n--- run stderr ---\n{run.stderr}"
	)
