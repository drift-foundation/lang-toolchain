# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4d of the terminal-`throws` work: typed-catch runtime regression for the
framework or_throw() user story.

Proves at RUNTIME (not just compile-time) that:
  1. A user-defined exception type with `implement core.Throw` can
     `.or_throw()` on a `Result<T, UserError>` and the resulting typed
     domain exception lands in the right catch arm — NOT in the
     `ResultError` fallback arm.
  2. `Result<T, String>.or_throw()` throws `ResultError` (scalar types
     use the stable generic diagnostic fallback) and the `ResultError`
     catch arm fires.

Each test compiles via `--dev` and runs the binary, asserting exit code
as a runtime sentinel.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import sanitizer_timeout

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
	"""A user-defined error type with `implement core.Throw` should throw
	the typed domain exception. The runtime sentinel proves the typed catch
	arm fires (exit 10), NOT the ResultError arm (exit 20) or the fallback
	(exit 30)."""
	source = """\
module main;
import std.core as core;
import std.err as err;
use trait core.Try;

pub exception ServiceDown(reason: String)

struct ServiceError {
	pub reason: String
}

implement core.Throw for ServiceError {
	pub fn throw_self(self: ServiceError) throws {
		throw ServiceDown(reason = self.reason);
	}
}

fn call_service() -> core.Result<Int, ServiceError> {
	return core.Result::Err(ServiceError(reason = "down"));
}

fn run() -> Int {
	val r = call_service();
	return (move r).or_throw();
}

fn main() nothrow -> Int {
	val result = try run() catch ServiceDown(e) {
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
		f"expected typed catch arm (exit 10), got exit {rc}; "
		f"exit 20 = ResultError fallback, exit 30 = catch-all"
	)


def test_scalar_throw_impl_result_error_runtime(tmp_path: Path) -> None:
	"""Result<T, String>.or_throw() should throw ResultError (the stable
	generic diagnostic fallback for scalar types). The runtime sentinel
	proves the ResultError catch arm fires (exit 10), NOT the catch-all
	(exit 20)."""
	source = """\
module main;
import std.core as core;
import std.err as err;
use trait core.Try;

fn parse_number() -> core.Result<Int, String> {
	return core.Result::Err("bad input");
}

fn run() -> Int {
	val r = parse_number();
	return (move r).or_throw();
}

fn main() nothrow -> Int {
	val result = try run() catch err:ResultError(e) {
		10
	} catch {
		20
	};
	return result;
}
"""
	rc = _compile_and_run(tmp_path, source)
	assert rc == 10, (
		f"expected ResultError catch arm (exit 10), got exit {rc}; "
		f"exit 20 = catch-all"
	)
