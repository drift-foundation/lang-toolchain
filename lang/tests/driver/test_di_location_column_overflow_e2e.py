# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""End-to-end regression: long-single-line input must compile through clang
without LLVM debug-info column-overflow rejection.

Pre-fix shape: a Drift source with a single very-long line (e.g. 2000+
chained else-ifs) generated `DILocation(column: <overflow>)` entries that
LLVM's IR parser rejected with `value for 'column' too large, limit is
65535`. The compile failed with a clang error wrapped by driftc as
`<source>:?:?: error: clang failed: ... value for 'column' too large`.

Post-fix: `LlvmModuleBuilder.get_di_location` clamps `column` at 65535
before emission, so the compile succeeds. The unit test in
`lang/tests/codegen/test_di_location_column_clamp.py` covers the clamp
function in isolation; this test exercises the full pipeline so the
end-to-end contract is committed.

Filed as `issues/llvm-debuginfo-column-overflow/`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _gen_long_single_line_else_if_chain(n: int) -> str:
	"""Build a Drift source where the entire else-if chain is on one line.

	This is the canonical reproducer: column counts grow linearly with
	chain length, and at ~2000 chain levels the column exceeds the LLVM
	16-bit `DILocation.column` field.
	"""
	parts = []
	for i in range(n):
		parts.append(f"if x == {i} {{ return {i}; }}")
	body = " else ".join(parts) + " else { return -1; }"
	return (
		"module main;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar x = 0;\n"
		f"\t{body}\n"
		"}\n"
	)


def _compile(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(300),
	)


def test_long_single_line_compiles_without_di_location_column_overflow(tmp_path: Path) -> None:
	"""2500 chained else-ifs on one line must compile cleanly.

	Chosen depth: 2500 puts the column counter well past the 16-bit limit
	(each else-if is ~30 columns, so column ≈ 75000 by the chain end).
	Pre-fix this would fail with a clang error about
	`value for 'column' too large`.
	"""
	res = _compile(tmp_path, _gen_long_single_line_else_if_chain(2500))
	assert res.returncode == 0, (
		f"compile must succeed; got rc={res.returncode}\n"
		f"stderr (last 800 chars): {res.stderr[-800:]}"
	)
	assert "value for 'column' too large" not in res.stderr, (
		f"DILocation column overflow regression — clamp has been removed:\n"
		f"{res.stderr[-800:]}"
	)
	assert "Traceback" not in res.stderr
