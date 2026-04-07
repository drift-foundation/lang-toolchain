# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""End-to-end regression for robustness matrix row #4: long binary chain.

The row #4 fix has two parts:
1. `lang/driftc/stage1/ast_to_hir.py::_visit_expr_Binary` was converted to
   iteratively flatten the left spine (synthetic stage1 unit test pins this
   in `lang/tests/stage1/test_long_binary_chain.py`).
2. `lang/driftc/driftc.py::main` raises `sys.setrecursionlimit` to 8192 at
   compile-pipeline entry, giving the still-recursive stage2/checker walks
   enough headroom to handle inputs that produce ~thousand-deep HBinary
   trees without overflowing.

This file pins both halves through the full driver path:

- d=500 long add chain: compiles cleanly (without the row #4 stage1 fix
  this hits `RecursionError` in `_visit_expr_Binary`; without the
  recursion-limit bump it hits `RecursionError` in stage2's
  `_visit_expr_HBinary`)
- d=2000 long add chain: also compiles cleanly (deeper sanity)

If either fix regresses, these tests catch it as a Python `Traceback` /
`RecursionError` in stderr.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _gen_long_add_chain(n: int) -> str:
	expr = "1" + "+1" * n
	return f"module main;\npub fn main() nothrow -> Int {{\n\treturn {expr};\n}}\n"


def _compile(tmp_path: Path, source: str, timeout_s: int = 180) -> subprocess.CompletedProcess:
	"""Per-call timeout in seconds. The d=2000 case is wall-clock-bound by
	Tier 3 type-checker scaling and needs a much larger budget than the
	d=500 default — and the budget needs to absorb 4-8x slowdowns under
	parallel pytest execution."""
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(timeout_s),
	)


def test_long_add_chain_500_compiles_through_pipeline(tmp_path: Path) -> None:
	"""500 chained `+` operands must compile cleanly through the full pipeline.

	Pre-fix shape: `RecursionError` in stage1 `_visit_expr_Binary`. Post-stage1
	fix without the recursion-limit bump: `RecursionError` in stage2
	`_visit_expr_HBinary` (which also descends `expr.left`).
	"""
	res = _compile(tmp_path, _gen_long_add_chain(500))
	assert res.returncode == 0, (
		f"d=500 should compile, got rc={res.returncode}\n"
		f"stderr (last 800 chars): {res.stderr[-800:]}"
	)
	assert "Traceback" not in res.stderr, (
		f"unexpected Python traceback at d=500:\n{res.stderr[-800:]}"
	)
	assert "RecursionError" not in res.stderr


def test_long_add_chain_2000_compiles_through_pipeline(tmp_path: Path) -> None:
	"""2000 chained `+` operands must compile cleanly (deeper sanity).

	Stage2/checker time on this is ~40s under default build. The point is to
	catch a regression that only manifests at depths beyond 500. If
	wall-clock becomes a CI concern, this test is the candidate to mark
	`slow` (after registering the marker in pytest.ini).
	"""
	# d=2000 takes ~40s solo under default build but the type checker is
	# single-threaded so under high parallel load (16-way `just test`)
	# CPU contention can multiply this by 4-8x. 600s gives ~15x headroom.
	res = _compile(tmp_path, _gen_long_add_chain(2000), timeout_s=600)
	assert res.returncode == 0, (
		f"d=2000 should compile, got rc={res.returncode}\n"
		f"stderr (last 800 chars): {res.stderr[-800:]}"
	)
	assert "Traceback" not in res.stderr
	assert "RecursionError" not in res.stderr
