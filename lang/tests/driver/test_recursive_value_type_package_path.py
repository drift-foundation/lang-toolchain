# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: recursive value-type validation must run on the package/consumer
build path, not only on the single-file `--dev` path.

LANGUAGE_BUG (app-team report, driftc 0.33.32 / ABI 17): a directly-recursive
value type (`variant IrType { ... TArray(elem: IrType) ... }`, no indirection)
was accepted on the normal CLI package path and a consumer that loaded a package
*and* embedded the type by value crashed with a raw Python `RecursionError`
(`stage2/string_arc.py`), with no source location.

Root cause: the normal CLI pass-1 block (`driftc.py` ~11298) ran
`validate_interface_schemas()` but NOT `validate_no_recursive_value_types()`, and
`compile_stubbed_funcs()` skips the validator whenever `pass1_state` is provided
(the two-pass path taken once any package is loaded, `if loaded_pkgs:`). So the
single-file `--dev` path caught it while the package-consume path skipped it.

These regressions exercise the consumer/emit path (the one that skipped), using
the app's true CROSS-MODULE shape: `ir` defines the recursive variant and `main`
imports it and embeds `ir.IrType` by value. A loaded `helper` package forces the
affected two-pass pass path. They must FAIL before the fix.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _emit_clean_pkg(tmp_path: Path) -> None:
	"""Emit a trivial, non-recursive helper package so a consumer build has a
	loaded package (`loaded_pkgs` non-empty → the two-pass path under test)."""
	(tmp_path / "lib").mkdir(parents=True, exist_ok=True)
	(tmp_path / "lib" / "helper.drift").write_text(
		"module helper;\nexport { bump };\npub fn bump(x: Int) nothrow -> Int { return x + 1; }\n"
	)
	dmp = tmp_path / "helper.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--target-word-bits", "64",
		 "-M", str(tmp_path), str(tmp_path / "lib" / "helper.drift"),
		 "--emit-package", str(dmp), "--package-id", "helper",
		 "--package-version", "0.1.0", "--package-target", "test-target"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0 and dmp.exists(), f"clean helper emit failed:\n{res.stdout}\n{res.stderr}"


def _consume_cross_module(tmp_path: Path, ir_src: str, main_src: str, *,
                          json_mode: bool = False) -> subprocess.CompletedProcess:
	"""Build a consumer from TWO source modules — `ir.drift` (defines the type)
	and `main.drift` (imports it) — depending on the helper package (forces the
	two-pass path)."""
	(tmp_path / "ir.drift").write_text(ir_src)
	(tmp_path / "main.drift").write_text(main_src)
	out = tmp_path / "consumer"
	argv = [sys.executable, "-m", "lang.driftc.driftc", "--target-word-bits", "64",
	        "-M", str(tmp_path), "--package-root", str(tmp_path),
	        "--dep", "helper@0.1.0", "--allow-unsigned-from", str(tmp_path)]
	if json_mode:
		argv.append("--json")
	argv += [str(tmp_path / "ir.drift"), str(tmp_path / "main.drift"),
	         "--entry", "main::main", "-o", str(out)]
	return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120))


def _has_recursive_diag(res: subprocess.CompletedProcess) -> bool:
	low = (res.stdout + res.stderr).lower()
	return (
		"e_recursive_value_type" in low
		or "recursive value type" in low
		or "infinitely recursive" in low
	)


def _has_python_crash(res: subprocess.CompletedProcess) -> bool:
	blob = res.stdout + res.stderr
	return "Traceback (most recent call last)" in blob or "RecursionError" in blob


def _json_diagnostics(res: subprocess.CompletedProcess) -> list[dict]:
	"""Parse the `--json` diagnostics array from stdout (the lane banner goes to
	stderr, so stdout is the JSON payload)."""
	try:
		return list(json.loads(res.stdout).get("diagnostics", []))
	except Exception:
		# Fallback: scan for the JSON object line.
		for line in res.stdout.splitlines():
			s = line.strip()
			if s.startswith("{"):
				try:
					return list(json.loads(s).get("diagnostics", []))
				except Exception:
					pass
		return []


# ir module: the directly-recursive value type (app's IrType shape).
_IR_RECURSIVE = """\
module ir;
export { IrType };
pub variant IrType {
\tTNull,
\tTArray(elem: IrType),
\tTOptional(inner: IrType)
}
"""

# ir module: the sanctioned Array<Self> indirection control (mirrors std.json.JsonNode).
_IR_INDIRECTED = """\
module ir;
export { IrType };
pub variant IrType {
\tTNull,
\tTArray(elem: Array<IrType>),
\tTOptional(inner: Array<IrType>)
}
"""

# main module: imports ir and embeds ir.IrType BY VALUE (app's ScriptRevision shape).
_MAIN_EMBED = """\
module main;
import helper as helper;
import ir as ir;
pub struct ScriptRevision { arg_type: ir.IrType }
pub fn main() nothrow -> Int { return helper.bump(0); }
"""

# main module: also CONSTRUCTS + matches ir.IrType, forcing the stage-2 drop-analysis
# traversal that produced the app's RecursionError.
_MAIN_CONSTRUCTED = """\
module main;
import helper as helper;
import ir as ir;
pub struct ScriptRevision { arg_type: ir.IrType }
pub fn make() nothrow -> ScriptRevision { return ScriptRevision(arg_type = ir.IrType::TNull()); }
pub fn main() nothrow -> Int {
\tval r = make();
\tmatch r.arg_type {
\t\tir.IrType::TNull => { return helper.bump(0); },
\t\tir.IrType::TArray(e) => { return 1; },
\t\tir.IrType::TOptional(i) => { return 2; }
\t}
}
"""


def test_consumer_direct_recursive_value_type_rejected_json_span(tmp_path: Path) -> None:
	"""R1: cross-module — `ir` defines the recursive variant, `main` embeds
	`ir.IrType` by value. Must be rejected with a STRUCTURED `E_RECURSIVE_VALUE_TYPE`
	diagnostic (code, phase, severity, positive line/column via --json) — never a
	Traceback, never a silently-produced binary."""
	_emit_clean_pkg(tmp_path)
	res = _consume_cross_module(tmp_path, _IR_RECURSIVE, _MAIN_EMBED, json_mode=True)
	assert not _has_python_crash(res), f"raw Python crash (compiler bug):\n{res.stdout}\n{res.stderr}"
	assert res.returncode != 0, f"recursive value type wrongly accepted:\n{res.stdout}\n{res.stderr}"
	assert not (tmp_path / "consumer").exists(), "no binary should be produced for a rejected build"

	diags = _json_diagnostics(res)
	rec = [d for d in diags if d.get("code") == "E_RECURSIVE_VALUE_TYPE"]
	assert rec, f"no structured E_RECURSIVE_VALUE_TYPE diagnostic in JSON:\n{res.stdout}"
	d = rec[0]
	assert d.get("phase") == "typecheck", f"unexpected phase: {d}"
	assert d.get("severity") == "error", f"unexpected severity: {d}"
	assert isinstance(d.get("line"), int) and d["line"] > 0, f"non-positive line: {d}"
	assert isinstance(d.get("column"), int) and d["column"] > 0, f"non-positive column: {d}"


def test_consumer_recursive_constructed_no_python_recursionerror(tmp_path: Path) -> None:
	"""R4: cross-module + CONSTRUCT/match forces the stage-2 drop-analysis traversal
	that produced the app's RecursionError. Must reject cleanly, never a Traceback."""
	_emit_clean_pkg(tmp_path)
	res = _consume_cross_module(tmp_path, _IR_RECURSIVE, _MAIN_CONSTRUCTED)
	assert not _has_python_crash(res), f"raw RecursionError/Traceback (compiler bug):\n{res.stdout}\n{res.stderr}"
	assert res.returncode != 0, f"recursive value type wrongly accepted:\n{res.stdout}\n{res.stderr}"
	assert _has_recursive_diag(res), f"missing E_RECURSIVE_VALUE_TYPE diagnostic:\n{res.stdout}\n{res.stderr}"


def test_emit_direct_recursive_value_type_rejected_no_dmp(tmp_path: Path) -> None:
	"""R2: emitting the minimal direct-recursive variant must fail with
	E_RECURSIVE_VALUE_TYPE and write no .dmp."""
	(tmp_path / "ir.drift").write_text(
		"module ir;\nexport { IrType };\npub variant IrType { TNull, TArray(elem: IrType) }\n"
	)
	dmp = tmp_path / "ir.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--target-word-bits", "64", "--test-build-only",
		 "-M", str(tmp_path), str(tmp_path / "ir.drift"),
		 "--emit-package", str(dmp), "--package-id", "ir",
		 "--package-version", "0.1.0", "--package-target", "test-target"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert not _has_python_crash(res), f"raw Python crash (compiler bug):\n{res.stdout}\n{res.stderr}"
	assert res.returncode != 0, f"emit wrongly succeeded:\n{res.stdout}\n{res.stderr}"
	assert _has_recursive_diag(res), f"missing E_RECURSIVE_VALUE_TYPE diagnostic:\n{res.stdout}\n{res.stderr}"
	assert not dmp.exists(), "no .dmp should be written for a rejected emit"


def test_consumer_array_self_indirection_accepted(tmp_path: Path) -> None:
	"""R3 (control): the sanctioned Array<Self> indirection compiles cleanly on the
	same cross-module consumer path — the validator must not over-reject."""
	_emit_clean_pkg(tmp_path)
	res = _consume_cross_module(tmp_path, _IR_INDIRECTED, _MAIN_EMBED)
	assert res.returncode == 0, f"indirected (Array<Self>) type wrongly rejected:\n{res.stdout}\n{res.stderr}"
	assert not _has_recursive_diag(res), f"false-positive recursive diagnostic:\n{res.stdout}\n{res.stderr}"
	assert (tmp_path / "consumer").exists(), "consumer binary should be produced"
