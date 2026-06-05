# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression (bug #4, bookkeeper team): named variant construction
`V::Arm(label = value)`, mirroring struct construction.

Variant payloads are declared with labels (`Granted(lease: WorkLease)`), so the
construction should accept the same labels by name — additive to the positional
form `Granted(x)`.  Rules mirror structs:

  - named args bind BY LABEL (order-independent);
  - positional form still works;
  - unknown label / missing payload / duplicate label / mixed positional+named
    are rejected with CLEAR diagnostics.

The named form already resolved correctly on the current toolchain; the missing
fix was the diagnostic for a MISSING payload, which previously fell through to an
internal "CallInfo param layout mismatch (checker bug)" message.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, src: str, *, out: str) -> subprocess.CompletedProcess:
	p = tmp_path / "main.drift"
	p.write_text(src, encoding="utf-8")
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(p), "--entry", "repro::main", "--target-word-bits", "64",
		"-o", str(tmp_path / out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)


# A 2-payload variant + an extractor, so binding can be verified at runtime.
_HDR = (
	"module repro;\n"
	"variant P { Pair(a: Int, b: Int), None }\n"
	"fn extract(p: P) nothrow -> Int {\n"
	"\treturn match p { P::Pair(a, b) => { a * 100 + b }, P::None => { 0 } };\n"
	"}\n"
)


def test_named_args_bind_by_label_out_of_order(tmp_path: Path) -> None:
	# Reversed-order named args must bind a=1, b=2 (102), not positionally (201).
	src = _HDR + (
		"fn main() nothrow -> Int {\n"
		"\tval p = P::Pair(b = 2, a = 1);\n"
		"\treturn extract(p) - 102;\n}\n"
	)
	r = _compile(tmp_path, src, out="ord")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "ord")]).returncode == 0


def test_positional_construction_still_works(tmp_path: Path) -> None:
	src = _HDR + (
		"fn main() nothrow -> Int {\n"
		"\tval p = P::Pair(1, 2);\n"
		"\treturn extract(p) - 102;\n}\n"
	)
	r = _compile(tmp_path, src, out="pos")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "pos")]).returncode == 0


def test_unknown_label_rejected(tmp_path: Path) -> None:
	src = _HDR + "fn main() nothrow -> Int { val p = P::Pair(a = 1, c = 2); return 0; }\n"
	r = _compile(tmp_path, src, out="u")
	assert r.returncode != 0
	assert "E-QMEM-NO-FIELD" in r.stderr and "'c'" in r.stderr, r.stderr


def test_missing_payload_clear_diagnostic(tmp_path: Path) -> None:
	# The fix: a clear missing-payload message, not "CallInfo param layout
	# mismatch (checker bug)".
	src = _HDR + "fn main() nothrow -> Int { val p = P::Pair(a = 1); return 0; }\n"
	r = _compile(tmp_path, src, out="m")
	assert r.returncode != 0
	assert "E-QMEM-MISSING-FIELD" in r.stderr and "b" in r.stderr, r.stderr
	assert "checker bug" not in r.stderr, "missing payload must not surface an internal-error message"


def test_duplicate_label_rejected(tmp_path: Path) -> None:
	src = _HDR + "fn main() nothrow -> Int { val p = P::Pair(a = 1, a = 2); return 0; }\n"
	r = _compile(tmp_path, src, out="d")
	assert r.returncode != 0
	assert "E-QMEM-DUP-FIELD" in r.stderr, r.stderr


def test_mixed_positional_and_named_rejected(tmp_path: Path) -> None:
	src = _HDR + "fn main() nothrow -> Int { val p = P::Pair(1, b = 2); return 0; }\n"
	r = _compile(tmp_path, src, out="x")
	assert r.returncode != 0
	assert "E-QMEM-MIXED-ARGS" in r.stderr, r.stderr
