# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4d of the terminal-`throws` work: typed-catch runtime regression for the
framework or_throw() user story.

Slice 5 (pub-error track) reshape:
  1. `Result<T, E>.or_throw()` requires E to be a `pub error` type
     (E_OR_THROW_NOT_ERROR_TYPE).  Auto-generated `Throw for E` throws
     E directly — the typed catch arm fires.
  2. The legacy `Result<T, String>.or_throw()` → ResultError fallback
     is replaced by a compile-time NEGATIVE test (Phase 5a strict
     enforcement).

Each runtime test compiles via `--dev` and runs the binary, asserting
exit code as a runtime sentinel.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.driftc import main as driftc_main

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> int:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	rc = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert rc.returncode == 0, f"compile failed: {rc.stderr[-600:]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	return run.returncode


def test_user_throw_impl_typed_catch_runtime(tmp_path: Path) -> None:
	"""A user-defined `pub error E` is auto-throwable — the auto-gen
	`implement core.Throw for E` throws E directly.  The runtime sentinel
	proves the typed catch arm for E fires (exit 10), NOT the
	`ResultError` fallback arm (exit 20) or the catch-all (exit 30).

	Slice 5 (pub-error track): replaces the prior `pub error ServiceDown`
	+ wrapping `struct ServiceError` shape — now ServiceError IS the
	pub error and the auto-gen Throw routes Err → ServiceError envelope.
	"""
	source = """\
module main;
import std.core as core;
import std.err as err;

pub error ServiceError {
	reason: String,
}

fn call_service() -> core.Result<Int, ServiceError> {
	return core.Result::Err(ServiceError(reason = "down"));
}

fn run() -> Int {
	val r = call_service();
	return (move r).or_throw();
}

pub fn main() nothrow -> Int {
	val result = try run() catch ServiceError(e) {
		10
	} catch err:ResultError(e) {
		20
	} catch {
		30
	};
	return result;
}
"""
	rc = _compile_and_run(tmp_path, source)
	assert rc == 10, (
		f"expected typed catch arm for `pub error ServiceError` (exit 10), "
		f"got exit {rc}; exit 20 = ResultError fallback, exit 30 = catch-all"
	)


def test_scalar_err_or_throw_rejected(tmp_path: Path) -> None:
	"""Phase 5a strict enforcement: `Result<T, String>.or_throw()` is a
	compile error (E_OR_THROW_NOT_ERROR_TYPE).  Replaces the legacy
	runtime test that pinned the `Result<T, String>` → `ResultError`
	wrap fallback — that fallback is no longer reachable through
	`or_throw` under the new model.
	"""
	src = tmp_path / "main.drift"
	src.write_text("""\
module main;
import std.core as core;

fn parse_number() -> core.Result<Int, String> {
	return core.Result::Err("bad input");
}

pub fn main() nothrow -> Int {
	val r = parse_number();
	return (move r).or_throw();
}
""")
	rc = driftc_main(["--stdlib-root", str(ROOT / "stdlib"), "--test-build-only", str(src), "--json"])
	# rc != 0 expected; capsys-equivalent via the JSON we produced is parsed below
	# from stderr/stdout indirectly; we run via subprocess for a robust read.
	out = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--stdlib-root",
		 str(ROOT / "stdlib"), "--test-build-only", str(src), "--json"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(30),
	)
	payload = json.loads(out.stdout) if out.stdout.strip() else {}
	codes = [d.get("code") for d in payload.get("diagnostics", [])
	         if d.get("severity") == "error"]
	assert "E_OR_THROW_NOT_ERROR_TYPE" in codes, (
		f"expected E_OR_THROW_NOT_ERROR_TYPE for `Result<Int, String>.or_throw()`; "
		f"got codes={codes}"
	)
