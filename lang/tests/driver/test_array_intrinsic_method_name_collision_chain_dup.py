"""
LANGUAGE_BUG regression: method-chain receivers were lowered twice when
the method name collided with an array-intrinsic name (e.g. `truncate`,
`extend`, `set`).

Root cause: `hir_to_mir.py::_lower_array_intrinsic_method` dispatched on
the method name first (`truncate` / `extend` / ... matched the intrinsic
allow-list), THEN lowered the receiver via `self.lower_expr(expr.receiver)`,
THEN checked whether the receiver type was actually `Array<T>`.  When the
receiver was a non-Array type (e.g. a user-defined `Builder`), the
function returned `(False, None)` to fall back to the normal method-call
lowering — but the receiver MIR had already been emitted.  The caller
then re-emitted the receiver again.  For a fluent owned-builder chain
this duplicated the entire prefix once per intrinsic-name method in the
chain, leaving every duplicated allocation dead in the function body.

Surface symptom: every `lang/tests/codegen/e2e/std_io_*` fixture that
used `io.file_builder(env.drift_tmp_path("...")).read(...).write(...)
.create(...).truncate(true).timeout(t).build()` failed under memcheck
because the abandoned `file_builder(...)` prefix never released its
inner `path` String.

Fix site: hoist the `array_def.kind is not TypeKind.ARRAY` guard ahead
of any receiver lowering.  Eligibility is a pure type-table lookup; the
receiver is only lowered after the function has committed to the
array-intrinsic path.

This test pins the regression at the IR shape: the chained method must
appear exactly once in the function body, never duplicated.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


_BUILDER_CHAIN_SRC = """
module main;

import std.core as core;

pub struct Builder { value: Int }

implement core.Copy for Builder { }

implement Builder {
	pub fn step(self: &Builder, v: Int) nothrow -> Builder {
		return Builder(value = self.value + v);
	}

	// `truncate` is in the array-intrinsic dispatch allow-list.  Before
	// the fix, the array path emitted MIR for the receiver, then bailed
	// because Builder isn't an Array; the normal method-call path then
	// emitted the receiver again.
	pub fn truncate(self: &Builder, v: Int) nothrow -> Builder {
		return Builder(value = self.value - v);
	}
}

pub fn new_builder(v: Int) nothrow -> Builder {
	return Builder(value = v);
}

pub fn main() nothrow -> Int {
	val b = new_builder(10).step(1).truncate(2).step(3);
	return b.value;
}
"""


def _compile_ir(tmp_path: Path) -> str:
	src = tmp_path / "main.drift"
	src.write_text(textwrap.dedent(_BUILDER_CHAIN_SRC))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not [d for d in diags if d.severity == "error"], diags
	func_hirs, signatures, _fn_ids = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=excs,
		entry="main",
		type_table=table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not [d for d in checked.diagnostics if d.severity == "error"], checked.diagnostics
	return ir


def _isolate_main_body(ir: str) -> str:
	"""Slice the LLVM IR text down to the body of `main`.

	The user-source `main` lowers directly to `define i64 @main(...)` —
	no extra `module::main__impl` indirection — when the workspace's
	entry module is `main`.
	"""
	import re

	match = re.search(r'^define\s+[^@\n]*@main\s*\(', ir, re.MULTILINE)
	assert match is not None, "@main not found in IR"
	open_brace = ir.find("{", match.end())
	assert open_brace >= 0
	depth = 1
	i = open_brace + 1
	while i < len(ir) and depth > 0:
		if ir[i] == "{":
			depth += 1
		elif ir[i] == "}":
			depth -= 1
		i += 1
	return ir[open_brace:i]


def test_builder_chain_truncate_emits_each_method_exactly_once(tmp_path: Path) -> None:
	ir = _compile_ir(tmp_path)
	body = _isolate_main_body(ir)
	# `Builder::truncate` collides with the array-intrinsic name.  The
	# array path lowered `truncate`'s receiver chain
	# (`new_builder(10).step(1)`) before discovering Builder isn't an
	# Array — so pre-fix the RECEIVER prefix doubled, while `truncate`
	# itself was emitted only once (by the fallback normal-call path).
	# Each chained method must appear exactly once per source-level call.
	trunc_count = body.count('@"Builder::truncate"(')
	assert trunc_count == 1, (
		f"`Builder::truncate` appears {trunc_count} times in main's body "
		"(source has 1 call).\n\n"
		f"IR body (truncated):\n{body[:4000]}"
	)
	# `step` is called twice in source (`.step(1)` and `.step(3)`).  Only
	# the `.step(1)` portion sits inside `truncate`'s receiver chain, so
	# pre-fix `step` ran 3 times (duplicated `step(1)` + the unduplicated
	# trailing `step(3)`).  Expecting exactly 2 emissions pins both the
	# fix and the "only the array-call's receiver doubles" shape.
	step_count = body.count('@"Builder::step"(')
	assert step_count == 2, (
		f"`Builder::step` appears {step_count} times in main's body "
		"(source has 2 calls; pre-fix this was 3).\n\n"
		f"IR body (truncated):\n{body[:4000]}"
	)
	# `new_builder` is the chain root; sits inside `truncate`'s receiver
	# chain, so pre-fix it doubled to 2.  Source has 1 call.
	root_count = body.count("@new_builder(")
	assert root_count == 1, (
		f"`new_builder` appears {root_count} times in main's body "
		"(source has 1 call; pre-fix this was 2).\n\n"
		f"IR body (truncated):\n{body[:4000]}"
	)
