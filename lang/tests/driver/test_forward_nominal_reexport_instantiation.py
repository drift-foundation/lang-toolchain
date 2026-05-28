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
		"module svc.errors;\nexport { SvcError };\npub error SvcError { msg: String }\n",
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


def test_pub_type_alias_through_callback_param(tmp_path: Path) -> None:
	"""Companion to `test_pub_type_alias_in_throwing_function_resolves`.

	Facade module re-exports `Response` and `SvcError` via pub type
	aliases.  A function `register_handler` takes a `Callback2`
	parameter whose call signature returns
	`Result<aliased_response, aliased_error>`.  The caller passes a
	handler function whose return type uses the aliased types
	through the facade import; the resolver must collapse the
	aliases on both sides so the handler's signature matches the
	Callback2 contract.

	Pre-fix (the drift-web `add_route` regression): the
	Callback2 instantiation seen by the caller would mint a
	FORWARD_NOMINAL TypeId for the aliased types, while
	`register_handler` saw the concrete STRUCT — overload
	resolution then failed.  Fixed by pre-registering all pub type
	aliases before any module is lowered (see
	`test_pub_type_alias_in_throwing_function_resolves`).

	This test was previously a cross-repo dependency on drift-web
	(`/home/sl/src/drift-web/packages/web-rest`).  Replaced with a
	synthetic fixture per K's directive: tests must recreate the
	pattern in isolation, not lean on a sibling repo.
	"""
	_write_file(
		tmp_path / "svc" / "response" / "response.drift",
		"module svc.response;\nexport { Response };\npub struct Response { pub code: Int }\n",
	)
	_write_file(
		tmp_path / "svc" / "errors" / "errors.drift",
		"module svc.errors;\nexport { SvcError };\npub error SvcError { msg: String }\n",
	)
	_write_file(
		tmp_path / "svc" / "api" / "api.drift",
		"module svc.api;\n"
		"import std.core as core;\n"
		"import svc.response as response;\n"
		"import svc.errors as errors;\n"
		"export { Response, SvcError, register_handler };\n"
		"pub type Response = response.Response;\n"
		"pub type SvcError = errors.SvcError;\n"
		"pub fn register_handler(handler: core.Callback2<String, Int, core.Result<response.Response, errors.SvcError>>, path: String, code: Int) nothrow -> core.Result<response.Response, errors.SvcError> {\n"
		"\treturn handler.call(path, code);\n"
		"}\n",
	)
	_write_file(
		tmp_path / "main.drift",
		"module main;\n"
		"import std.core as core;\n"
		"import svc.api as api;\n"
		"fn _health(path: String, code: Int) nothrow -> core.Result<api.Response, api.SvcError> {\n"
		"\treturn core.Result::Ok(api.Response(code = code));\n"
		"}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval cb = core.callback2(_health);\n"
		"\tmatch api.register_handler(move cb, \"/health\", 200) {\n"
		"\t\tcore.Result::Err(_) => { return 1; },\n"
		"\t\tcore.Result::Ok(r) => { return r.code; }\n"
		"\t}\n"
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
		f"pub type alias through Callback2 param must compile:\n{res.stderr[:600]}"
	)