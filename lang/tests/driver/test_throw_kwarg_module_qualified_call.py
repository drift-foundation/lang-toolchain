"""LANGUAGE_BUG regression — Slice 7a (0.31.62, 2026-05-05).

Module-qualified function call (`alias.fn(...)`) used as a keyword-arg value
inside a `throw E(field = ...)` expression must resolve through the parser's
module-qualified call rewrite walker.  Before the fix, the generic
`walk_expr` recursion descended through `H.HExpr` / `H.HBlock` / `HMatchArm`
items inside lists but did NOT recognize `H.HKwArg` items — so
`HExceptionInit.kw_args[i].value` was never visited.  The unresolved
`HMethodCall(receiver=HVar("alias"), method_name="fn", ...)` survived into
the type-checker, which then reported `unknown name 'alias'` and
`exception field value must implement Diagnostic`.

The same expression resolved correctly when used as the return value of a
function body or as a keyword arg to a non-throw struct constructor.
"""

import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_throw_constructor_kw_value_resolves_module_qualified_call(tmp_path: Path) -> None:
	main_src = tmp_path / "main.drift"
	main_src.write_text(textwrap.dedent(
		"""
		module main;

		import std.core as core;
		import std.err as err;

		pub error MyError {
			tag: String,
		}

		implement core.Throw for MyError {
			pub fn throw_self(self: MyError) throws {
				throw err:ResultError(diag_json = core.diagnostic_json_string(&self.tag));
			}
		}

		fn main() nothrow -> Int {
			return 0;
		}
		"""
	).lstrip())

	modules, table, excs, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		paths=[main_src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	parse_errors = [d for d in parse_diags if d.severity == "error"]
	assert not parse_errors, "parse-stage errors: " + "; ".join(d.message for d in parse_errors)

	func_hirs, signatures, _fn_ids = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=excs,
		entry="main",
		type_table=table,
		module_exports=module_exports,
		module_deps=module_deps,
	)

	# Strict shape: this program must compile cleanly.  Under the
	# regression, the parser left `core.diagnostic_json_string(...)` as
	# `HMethodCall(receiver=HVar("core"))` inside `HExceptionInit.kw_args[0]
	# .value`, surfacing `unknown name 'core'` plus a cascade of
	# `exception field value must implement Diagnostic` and
	# `typecheck contract failure: missing CallInfo` errors.  Asserting
	# no error diagnostics at all (rather than the specific old message
	# strings) catches future regressions that surface a different
	# diagnostic shape but still leave the kw value unresolved.
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert not errors, (
		"throw-constructor kw value with module-qualified call regressed: "
		+ "; ".join(f"{d.message[:120]}" for d in errors)
	)
