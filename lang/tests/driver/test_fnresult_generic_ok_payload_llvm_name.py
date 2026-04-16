# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG regression: `_ensure_nothrow_wrap_thunk` uses
`type_table.type_key_string(ret_tid)` directly when synthesizing the
FnResult ok-payload identifier for a nothrow function-pointer wrapper.
For generic return types the raw key contains `<`, `>`, `:`, `.` —
characters illegal in LLVM type identifiers — so clang's IR parser
rejects the emitted .ll with `expected '=' after name`.

Trigger shape: store a `nothrow` function pointer whose return type is a
generic instantiation (e.g. `Optional<Int>`) in a struct field.  Taking
the address of the nothrow function flips
`instr.call_sig.can_throw == False`, and the can-throw wrapper thunk is
generated with the un-sanitized ok-payload key.

Surfaced by the std.log resolver fixture (a `Fn() nothrow ->
Optional<LogContext>` field on `ContextResolver`), but the bug is
independent of std.log — this test reproduces it with a tiny stand-alone
struct so the regression has no stdlib coupling beyond the prelude
`Optional<T>` variant.

Fix: `lang/codegen/llvm/llvm_codegen.py:_declare_fnresult_named_type`
sanitizes non-alnum characters to `_` and appends a 16-hex hash suffix
when sanitization is non-identity (collision-safe).
"""
from __future__ import annotations

import re
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


_FNRESULT_DECL_RE = re.compile(r"^%(FnResult_\S+?)\s*=\s*type\b", re.MULTILINE)
_LLVM_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NOTHROW_WRAP_REPRO = """
module m;

struct Holder {
	f: Fn() nothrow -> Optional<Int>
}

fn produce() nothrow -> Optional<Int> {
	return Optional::Some(7);
}

fn main() nothrow -> Int {
	val h = Holder(f = produce);
	val fp = h.f;
	val r = fp();
	match r {
		Some(v) => { return v; },
		None => { return -1; }
	}
}
""".lstrip()


def _compile_ir(tmp_path: Path, source: str) -> str:
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == []
	func_hirs, signatures, _ = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="m::main",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], f"unexpected codegen errors: {errors}"
	return ir


def test_fnresult_generic_ok_payload_emits_valid_llvm_identifier(tmp_path: Path) -> None:
	"""Storing a nothrow function pointer returning Optional<Int> must produce
	an FnResult named-type whose identifier is a valid LLVM identifier
	(alnum + `_`)."""
	ir = _compile_ir(tmp_path, _NOTHROW_WRAP_REPRO)
	# Confirm the buggy path actually fired: a __nothrow_wrap_ thunk must
	# be emitted (otherwise the regression isn't exercising the right code).
	assert "@__nothrow_wrap_m_produce" in ir, (
		"nothrow wrapper thunk not generated — repro no longer exercises "
		"_ensure_nothrow_wrap_thunk; update the test source"
	)
	# Every declared FnResult identifier must be a valid LLVM identifier.
	# Bug shape: %FnResult_lang.core::lang.core.Optional<Int>_Error
	# Fix shape: %FnResult_lang_core__lang_core_Optional_Int__<hash>_Error
	decls = _FNRESULT_DECL_RE.findall(ir)
	assert decls, "expected at least one %FnResult_..._Error type declaration"
	for ident in decls:
		assert _LLVM_IDENT_RE.match(ident), (
			f"FnResult type identifier %{ident!r} contains characters "
			f"illegal in LLVM type names (must be alnum + underscore)"
		)


def test_fnresult_generic_ok_payload_no_angle_brackets_in_ir(tmp_path: Path) -> None:
	"""Belt-and-suspenders: scan the entire IR for any `%FnResult_` token
	containing '<', '>', ':', or '.' — clang would reject any such
	identifier, regardless of whether the regex above missed it."""
	ir = _compile_ir(tmp_path, _NOTHROW_WRAP_REPRO)
	for line in ir.split("\n"):
		if "%FnResult_" not in line:
			continue
		# Match every %FnResult_… token, including the bug shape that
		# contains illegal punctuation we want to catch.
		for match in re.finditer(r"%(FnResult_[A-Za-z0-9_<>:.]+)", line):
			ident = match.group(1)
			illegal = set("<>:.") & set(ident)
			assert not illegal, (
				f"FnResult identifier {ident!r} on line {line.strip()!r} "
				f"contains LLVM-illegal characters: {sorted(illegal)}"
			)
