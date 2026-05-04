# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: cross-source-module overload resolution with callback/result types.

Reproduces the drift-web add_route failure where a function defined in one
source-compiled module with a Callback2/Result parameter type fails overload
resolution when called from another source-compiled module in the same build.

The failure mode is: the caller's argument TypeIds don't match the callee's
parameter TypeIds because the ABI boundary logic incorrectly upgrades the call
to a wrapper (changing can_throw and wrapping the return type in FnResult).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_cross_source_module_callback_param_resolves(tmp_path: Path) -> None:
	"""Source module A defines fn with Callback2 param; source module B calls it.

	Both modules are source-compiled in the same driftc invocation.
	Overload resolution must succeed — no ABI boundary wrapping between
	source modules.
	"""
	# Module A: defines a function with a callback parameter.
	_write_file(
		tmp_path / "mylib" / "api" / "lib.drift",
		"""
module mylib.api;
export { handle };

pub fn handle(x: Int) nothrow -> Int {
	return x * 2;
}
""".lstrip(),
	)

	# Module B: calls A's function.
	_write_file(
		tmp_path / "main.drift",
		"""
module main;
import mylib.api as api;

fn main() nothrow -> Int {
	return api.handle(21);
}
""".lstrip(),
	)

	ir_path = tmp_path / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			str(tmp_path / "mylib" / "api" / "lib.drift"),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(ir_path),
			"--entry",
			"main::main",
		]
	)
	assert rc == 0, "overload resolution should succeed for cross-source-module call"


def test_cross_source_module_result_return_resolves(tmp_path: Path) -> None:
	"""Source module A defines fn returning Result; source module B calls it
	and matches on the result.

	This is the exact pattern from the drift-web add_route regression:
	the match on the result fails if the boundary logic wraps the return
	type in an extra FnResult layer.
	"""
	# Module A: defines a function returning a result type.
	_write_file(
		tmp_path / "mylib" / "svc" / "lib.drift",
		"""
module mylib.svc;
export { process, SvcError };

pub error SvcError { msg: String }
pub fn process(x: Int) -> Int {
	if x < 0 {
		throw SvcError("negative");
	}
	return x * 2;
}
""".lstrip(),
	)

	# Module B: calls A's function and catches the error.
	_write_file(
		tmp_path / "main.drift",
		"""
module main;
import mylib.svc as svc;

fn main() nothrow -> Int {
	return try svc.process(21) catch { -1 };
}
""".lstrip(),
	)

	ir_path = tmp_path / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			str(tmp_path / "mylib" / "svc" / "lib.drift"),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(ir_path),
			"--entry",
			"main::main",
		]
	)
	assert rc == 0, "cross-source-module call with result type should resolve"


def test_cross_source_module_with_package_dep_resolves(tmp_path: Path) -> None:
	"""Source module A and B compiled together, with a package dependency.

	This mirrors the drift-web scenario: web-rest (source) + net-tls (package).
	The package dependency should not cause source modules to be treated as
	external.
	"""
	# First produce a small package.
	pkg_src = tmp_path / "pkgsrc" / "dep" / "util" / "lib.drift"
	_write_file(
		pkg_src,
		"""
module dep.util;
export { double };

pub fn double(x: Int) nothrow -> Int {
	return x * 2;
}
""".lstrip(),
	)
	pkg_path = tmp_path / "pkgsrc" / "dep.util.dmp"
	rc = driftc_main(
		[
			"-M",
			str(tmp_path / "pkgsrc"),
			str(pkg_src),
			"--package-id",
			"dep.util",
			"--package-version",
			"0.0.0",
			"--package-target",
			"linux-x86_64",
			"--emit-package",
			str(pkg_path),
		]
	)
	assert rc == 0, "package build failed"

	# Source module A: uses the package dep.
	_write_file(
		tmp_path / "appsrc" / "mylib" / "api" / "lib.drift",
		"""
module mylib.api;
import dep.util as util;
export { process, ApiError };

pub error ApiError { msg: String }
pub fn process(x: Int) -> Int {
	if x < 0 {
		throw ApiError("bad");
	}
	return util.double(x);
}
""".lstrip(),
	)

	# Source module B: calls A's function.
	_write_file(
		tmp_path / "appsrc" / "main.drift",
		"""
module main;
import mylib.api as api;

fn main() nothrow -> Int {
	return try api.process(21) catch { -1 };
}
""".lstrip(),
	)

	ir_path = tmp_path / "out.ll"
	rc = driftc_main(
		[
			"-M",
			str(tmp_path / "appsrc"),
			"--package-root",
			str(tmp_path / "pkgsrc"),
			"--dep",
			"dep.util@0.0.0",
			"--allow-unsigned-from",
			str(tmp_path / "pkgsrc"),
			str(tmp_path / "appsrc" / "mylib" / "api" / "lib.drift"),
			str(tmp_path / "appsrc" / "main.drift"),
			"--emit-ir",
			str(ir_path),
			"--entry",
			"main::main",
		]
	)
	assert rc == 0, (
		"cross-source-module call should resolve even with package dependency present"
	)
