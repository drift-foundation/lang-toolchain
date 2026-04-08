# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: pub type alias across modules breaks generic instantiation identity.

When module B defines `pub type Foo = a.Foo` and module C uses B.Foo as a generic
argument in a function parameter or return type, the caller may resolve B.Foo to a
FORWARD_NOMINAL TypeId instead of the concrete STRUCT. This creates two different
generic instantiations, failing overload resolution.

Root cause: pub type aliases were registered during per-module lowering, so modules
processed before their dependencies didn't see the alias. Fixed by pre-registering
all pub type aliases before any module is lowered.

Reproduces the drift-web add_route regression.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from lang.codegen.llvm.test_utils import sanitizer_timeout


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


ROOT = Path(__file__).resolve().parents[3]


def test_pub_type_alias_in_throwing_function_resolves(tmp_path: Path) -> None:
	"""Facade module re-exports Response via pub type alias. Function in
	facade throws with a re-exported error type. Caller catches the result.
	The Result<Response, SvcError> instantiation must be identical on both
	sides."""

	_write_file(
		tmp_path / "svc" / "response" / "response.drift",
		"module svc.response;\nexport { Response };\npub struct Response { pub code: Int }\n",
	)
	_write_file(
		tmp_path / "svc" / "errors" / "errors.drift",
		"module svc.errors;\nexport { SvcError };\npub exception SvcError(msg: String);\n",
	)
	_write_file(
		tmp_path / "svc" / "api" / "api.drift",
		"module svc.api;\n"
		"import svc.response as response;\n"
		"import svc.errors as errors;\n"
		"export { Response, SvcError, handle };\n"
		"pub type Response = response.Response;\n"
		"pub type SvcError = errors.SvcError;\n"
		"pub fn handle(path: String) -> response.Response {\n"
		"\tif path == \"\" { throw svc.errors:SvcError(msg = \"empty\"); }\n"
		"\treturn response.Response(code = 200);\n"
		"}\n",
	)
	_write_file(
		tmp_path / "main.drift",
		"module main;\n"
		"import svc.api as api;\n"
		"fn _call() -> api.Response { return api.handle(\"/health\"); }\n"
		"fn main() nothrow -> Int {\n"
		"\tval r = try _call() catch { api.Response(code = -1) };\n"
		"\treturn r.code;\n"
		"}\n",
	)

	cmd = [
		sys.executable, "-m", "lang.driftc",
		"-M", str(tmp_path),
		str(tmp_path / "svc" / "response" / "response.drift"),
		str(tmp_path / "svc" / "errors" / "errors.drift"),
		str(tmp_path / "svc" / "api" / "api.drift"),
		str(tmp_path / "main.drift"),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--entry", "main::main",
	]
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60))
	assert res.returncode == 0, (
		f"pub type alias in throwing function must resolve correctly:\n{res.stderr[:500]}"
	)


def test_drift_web_add_route_pattern(tmp_path: Path) -> None:
	"""Minimal reproduction of the drift-web add_route regression.

	Facade module re-exports Response and RestError via pub type aliases,
	then defines add_route with a Callback2 parameter that nests those
	aliased types inside Result. Caller passes a handler function whose
	return type uses the aliased types through the facade import.
	"""
	# Only run if drift-web sources are available.
	dw = Path("/home/sl/src/drift-web")
	if not (dw / "packages" / "web-rest" / "src" / "lib.drift").exists():
		pytest.skip("drift-web sources not available")

	_write_file(
		tmp_path / "test.drift",
		"module test.route;\n"
		"import std.core as core;\n"
		"import web.rest as rest;\n"
		"fn _health(req: &rest.Request, ctx: &mut rest.Context) nothrow -> core.Result<rest.Response, rest.RestError> {\n"
		"\treturn core.Result::Ok(rest.json_response(200, \"ok\"));\n"
		"}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar b = rest.new_app_builder();\n"
		"\trest.bind(&mut b, \"127.0.0.1\", 0);\n"
		"\tmatch rest.build_app(move b) {\n"
		"\t\tcore.Result::Err(_) => { return 1; },\n"
		"\t\tcore.Result::Ok(a) => {\n"
		"\t\t\tvar app = move a;\n"
		"\t\t\tmatch rest.add_route(&mut app, \"GET\", \"/health\", _health) {\n"
		"\t\t\t\tcore.Result::Err(_) => { return 2; },\n"
		"\t\t\t\tcore.Result::Ok(_) => { return 0; }\n"
		"\t\t\t}\n"
		"\t\t}\n"
		"\t}\n"
		"}\n",
	)

	src_files = sorted((dw / "packages" / "web-jwt" / "src").glob("*.drift")) + \
		sorted((dw / "packages" / "web-rest" / "src").glob("*.drift"))

	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--entry", "test.route::main",
	] + [str(f) for f in src_files] + [
		str(tmp_path / "test.drift"),
		"-o", str(tmp_path / "out"),
	]
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
	assert res.returncode == 0, (
		f"drift-web add_route pattern must compile:\n{res.stderr[:500]}"
	)