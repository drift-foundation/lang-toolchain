# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: empty map/array literals as function arguments must infer
their type from the function parameter signature.

{:} and [] as call arguments have no entries to infer types from, but
the callee's parameter type is known after overload resolution.  The
type checker must propagate the expected type from the resolved
signature into these literal expressions.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, source: str, *, expect_fail: bool = False) -> tuple[int, str]:
	"""Compile source and return (returncode, stderr)."""
	src = tmp_path / "main.drift"
	src.write_text(source)
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main",
		 "--emit-ir", str(tmp_path / "out.ll"), "--test-build-only"],
		cwd=ROOT, capture_output=True, text=True, timeout=60,
	)
	return res.returncode, res.stderr


# --- Tests that must PASS after the fix ---

def test_empty_map_concrete_param(tmp_path: Path) -> None:
	"""Empty map {:} as arg to concrete HashMap<String, String> param."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"import std.containers as containers;\n"
		"\n"
		"fn consume(m: containers.HashMap<String, String>) nothrow -> Int {\n"
		"\treturn 0;\n"
		"}\n"
		"\n"
		"pub fn main() nothrow -> Int {\n"
		"\treturn consume({:});\n"
		"}\n"
	)
	rc, stderr = _compile(tmp_path, source)
	assert rc == 0, f"empty map as concrete param should compile: {stderr[:300]}"


def test_empty_map_generic_debuggable_param(tmp_path: Path) -> None:
	"""Empty map {:} as arg to generic HashMap<String, V> require Debuggable.

	NOTE: This currently requires a no-attrs overload on Logger because
	the generic V cannot be inferred from an empty map literal.  This
	test documents the limitation — it should be updated when generic
	type inference for empty literals is implemented.
	"""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"import std.log as log;\n"
		"\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar cfgb = log.config_builder();\n"
		"\tcfgb.sink(log.stderr_sink());\n"
		"\tcfgb.min_level(log.Level::Error());\n"
		"\tval logger = log.create_logger(\"test\", cfgb.build());\n"
		"\tlogger.info(\"event\", {:});\n"
		"\treturn 0;\n"
		"}\n"
	)
	rc, stderr = _compile(tmp_path, source)
	# Layer 2 (generic param inference) is not yet implemented.
	# For now, this correctly fails. When Layer 2 lands, change to assert rc == 0.
	assert rc != 0, "generic param inference for empty map not yet implemented"


# --- Tests that must CONTINUE to pass (no regression) ---

def test_nonempty_map_unchanged(tmp_path: Path) -> None:
	"""Non-empty map literal continues to work."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"import std.log as log;\n"
		"\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar cfgb = log.config_builder();\n"
		"\tcfgb.sink(log.stderr_sink());\n"
		"\tcfgb.min_level(log.Level::Error());\n"
		"\tval logger = log.create_logger(\"test\", cfgb.build());\n"
		"\tlogger.info(\"event\", {\"k\": \"v\"});\n"
		"\treturn 0;\n"
		"}\n"
	)
	rc, stderr = _compile(tmp_path, source)
	assert rc == 0, f"non-empty map should compile: {stderr[:300]}"


def test_empty_map_no_context_still_errors(tmp_path: Path) -> None:
	"""Bare val m = {:} with no type context must still error."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval m = {:};\n"
		"\treturn 0;\n"
		"}\n"
	)
	rc, stderr = _compile(tmp_path, source)
	assert rc != 0, "empty map without type context should fail"
	assert "cannot infer" in stderr, f"expected 'cannot infer' diagnostic: {stderr[:300]}"


def test_empty_array_concrete_param(tmp_path: Path) -> None:
	"""Empty array [] as arg to concrete Array<Int> param."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"\n"
		"fn consume(a: Array<Int>) nothrow -> Int {\n"
		"\treturn 0;\n"
		"}\n"
		"\n"
		"pub fn main() nothrow -> Int {\n"
		"\treturn consume([]);\n"
		"}\n"
	)
	rc, stderr = _compile(tmp_path, source)
	assert rc == 0, f"empty array as concrete param should compile: {stderr[:300]}"


def test_braces_still_block(tmp_path: Path) -> None:
	"""{} in statement position remains a block, not a map."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar x = 0;\n"
		"\t{ x = 42; }\n"
		"\treturn x;\n"
		"}\n"
	)
	rc, stderr = _compile(tmp_path, source)
	assert rc == 0, f"block in statement position should compile: {stderr[:300]}"
