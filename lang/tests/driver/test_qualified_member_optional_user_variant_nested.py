# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""App-shape regression for the `TypeKind.VARIANT_INSTANCE` stale-ref
LANGUAGE_BUG (see `test_qualified_member_no_fields_with_expected_type.py`).

This pair-fixture mirrors the *actual* application-team trigger more
closely than the minimal user-variant case:

  - the no-fields qualified-member ref is `Optional<Claim>::None`
    (stdlib variant parameterised by a *user* variant), and
  - it appears inside a nested match where the outer match's arm
    result expression carries the propagated expected type.

The crash signature was `AttributeError: type object 'TypeKind' has
no attribute 'VARIANT_INSTANCE'` at `type_checker.py:6952`; the
fix replaced the stale enum reference with `TypeKind.VARIANT` plus
a TypeId equality short-circuit, and made HIR→MIR consume the
checker's recorded type rather than re-resolve the base.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _build_run(tmp_path: Path, source: str) -> tuple[int, str, str, int]:
	"""Compile and execute. Returns (compile_rc, compile_stdout, compile_stderr, run_rc)."""
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
		return (build.returncode, build.stdout, build.stderr, -1)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=30)
	return (build.returncode, build.stdout, build.stderr, run.returncode)


_SOURCE_NESTED_MATCH = """
module m;

variant Claim { Ok(v: Int), Failed }

fn produce(x: Int) -> Optional<Claim> {
	if x == 0 {
		return Optional<Claim>::None;
	}
	return Optional::Some(Claim::Ok(x));
}

pub fn main() nothrow -> Int {
	val outer = produce(7);
	match outer {
		Optional::Some(inner) => {
			match inner {
				Claim::Ok(v)   => { return v; },
				Claim::Failed  => { return -1; },
			}
		},
		Optional::None        => { return -2; },
	}
}
"""


def test_optional_user_variant_nested_match_no_crash(tmp_path: Path) -> None:
	build_rc, build_stdout, build_stderr, run_rc = _build_run(tmp_path, _SOURCE_NESTED_MATCH)
	# Compiler must not crash on the stale enum reference.
	assert "AttributeError" not in build_stderr, (
		f"driftc crashed:\n--- stderr ---\n{build_stderr}"
	)
	assert "VARIANT_INSTANCE" not in build_stderr, (
		f"unexpected VARIANT_INSTANCE reference in stderr:\n{build_stderr}"
	)
	assert build_rc == 0, (
		f"compile failed (rc={build_rc}):\n--- stdout ---\n{build_stdout}\n--- stderr ---\n{build_stderr}"
	)
	# main(7) -> Optional::Some(Claim::Ok(7)) -> inner match -> 7
	assert run_rc == 7, f"unexpected program exit code: {run_rc}"
