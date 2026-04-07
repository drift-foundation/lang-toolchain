# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""End-to-end regression for robustness matrix row #11: deeply nested
generic types through the full compiler pipeline.

The row #11 fix touched three sequential walker sites in the type-key
handling pipeline:
1. `lang/driftc/parser/__init__.py::_type_expr_key`
2. `lang/driftc/traits/world.py::type_key_from_typeid`
3. `TypeKey.__hash__` and `TypeKey.__eq__` (frozen dataclass overrides)

Helper-level unit tests cover each site in isolation
(`lang/tests/parser/test_type_expr_key_deep_nesting.py`,
`lang/tests/traits/test_type_key_deep_nesting.py`). This file pins the
end-to-end contract that a Drift source with deeply nested generic types
in fn-parameter position compiles through the full pipeline without a
Python crash. Without these regressions, the row #11 deep-depth claim is
probe-backed only — this file makes it committed coverage.

Wall-clock note: depths above ~1000 hit a Tier 3 scaling cliff in the
type checker and take many minutes per case. The "compiles cleanly" pin
is at d=500 to keep routine CI fast; the "no Python traceback" pin uses
d=2000 with a generous timeout, exercising the same shape that would
crash pre-fix.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _gen_deep_nested_array_param(n: int) -> str:
	"""Drift source with `Array<Array<...<Int>>>` of `n` levels in a fn
	parameter position. Parameter position avoids the empty-init type
	inference issue that affects `var x: T;` declarations.
	"""
	t = "Int"
	for _ in range(n):
		t = f"Array<{t}>"
	return (
		"module main;\n"
		f"fn take(_: {t}) nothrow -> Int {{ return 0; }}\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)


def _compile(tmp_path: Path, source: str, timeout_s: int) -> subprocess.CompletedProcess:
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


def test_deep_nested_array_500_compiles_through_pipeline(tmp_path: Path) -> None:
	"""500 levels of nested `Array<Array<...<Int>>>` must compile cleanly.

	Pre-fix shape: `RecursionError` somewhere in the type-key pipeline
	(any of `_type_expr_key`, `type_key_from_typeid`, or `TypeKey.__hash__`).
	A regression to recursive form in any of the three row #11 fix sites
	would surface here as a Python `Traceback` / `RecursionError` in
	stderr.

	d=500 is chosen as the "compiles cleanly" pin because it is well past
	the pre-fix cliff (~250) but stays within ~30s wall-clock under the
	Tier 3 scaling envelope. Higher depths are pinned by the d=2000 test
	below, which only asserts the absence of a Python crash.
	"""
	res = _compile(tmp_path, _gen_deep_nested_array_param(500), timeout_s=120)
	assert res.returncode == 0, (
		f"d=500 should compile cleanly, got rc={res.returncode}\n"
		f"stderr (last 800 chars): {res.stderr[-800:]}"
	)
	assert "Traceback" not in res.stderr, (
		f"unexpected Python traceback at d=500:\n{res.stderr[-800:]}"
	)
	assert "RecursionError" not in res.stderr


def test_deep_nested_array_2000_no_python_crash(tmp_path: Path) -> None:
	"""2000 levels of nested `Array<Array<...<Int>>>` must produce neither
	a Python traceback nor a RecursionError.

	At this depth the compile may still fail or hit Tier 3 scaling, but
	**no Python crash is acceptable**. The robustness contract for row #11
	is exactly this: the recursion sites are eliminated and the failure
	mode at deep depths becomes a controlled scaling cliff or a clean
	downstream diagnostic, never a Python `RecursionError`.

	Wall-clock at d=2000 is dominated by the Tier 3 type-checker scaling
	(measured ~600s in development); the timeout is generous enough that
	routine CI captures the result. If the test wall-clock becomes a CI
	concern, this is the candidate to mark slow (after registering the
	marker in pytest.ini); the smaller d=500 test continues to pin the
	"compiles cleanly" half of the contract.
	"""
	res = _compile(tmp_path, _gen_deep_nested_array_param(2000), timeout_s=1800)
	# Either rc=0 (Tier 3 scaling fix landed) or rc!=0 with a clean
	# downstream diagnostic. The crucial property is the absence of a
	# Python crash in either case.
	assert "Traceback" not in res.stderr, (
		f"row #11 has regressed: Python traceback at d=2000\n"
		f"{res.stderr[-1200:]}"
	)
	assert "RecursionError" not in res.stderr, (
		f"row #11 has regressed: RecursionError at d=2000\n"
		f"{res.stderr[-1200:]}"
	)
