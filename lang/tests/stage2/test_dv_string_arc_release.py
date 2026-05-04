# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: string_arc must release owned string temps consumed by
ConstructDV(String), but must NOT release borrowed locals.

Verifies at the IR level that:
- DiagnosticValue::String(fmt.format_int(...)) emits drift_string_release
  for the format_int temp (owned creator → last-use release)
- throw Info(s, ...) where s is a local does NOT emit an extra release
  for the string arg to drift_dv_string (borrowed → scope-exit handles it)
"""
from __future__ import annotations

import re
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.tests.support.module_packages import mk_module


def _compile_ir(tmp_path: Path, source: str, entry: str = "main") -> str:
	mod_root = tmp_path / "mods"
	mod_root.mkdir(parents=True, exist_ok=True)
	(mod_root / "main.drift").write_text(source)
	module_packages: dict = {}
	mk_module(module_packages, "main", "main")
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths, module_paths=[mod_root], external_module_packages=module_packages,
		stdlib_root=stdlib_root(), test_build_only=True,
	)
	assert not diags, diags
	func_hirs, signatures, _ = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs, signatures=signatures, exc_env=exc_catalog,
		entry=entry, type_table=type_table, module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics, checked.diagnostics
	assert ir
	return ir


def _extract_func(ir: str, name: str) -> str:
	"""Extract one function body from LLVM IR."""
	pattern = rf'define [^\n]*@"?{re.escape(name)}"?\([^\n]*\{{(.*?)\n\}}'
	m = re.search(pattern, ir, re.DOTALL)
	assert m, f"function {name!r} not found in IR"
	return m.group(1)


def test_owned_temp_gets_string_release(tmp_path: Path) -> None:
	"""format_int temp passed to DV::String must get drift_string_release."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"import std.format as fmt;\n"
		"\n"
		"fn do_work(code: Int) nothrow -> Int {\n"
		"\tval dv = DiagnosticValue::String(fmt.format_int(code));\n"
		"\treturn code;\n"
		"}\n"
		"\n"
		"fn main() nothrow -> Int { return do_work(0); }\n"
	)
	ir = _compile_ir(tmp_path, source)
	do_work = _extract_func(ir, "do_work")
	# The format_int result is an owned temp. After drift_dv_string retains,
	# string_arc must release the original via last-use machinery.
	assert "drift_dv_string(" in do_work, "ConstructDV(String) should call drift_dv_string"
	assert "drift_string_release(" in do_work, (
		"owned string temp consumed by ConstructDV(String) must get "
		"drift_string_release from string_arc last-use tracking"
	)


def test_owned_call_result_to_colliding_local_no_spurious_retain(tmp_path: Path) -> None:
	"""Storing a String-returning call's result into a user local whose
	source name collides with the MIR temp counter (e.g. user `val t2 = ...`
	at a point where the call dest happens to be SSA temp `t2`) must NOT
	insert an extra drift_string_retain before the store.

	Bug: string_arc.py used name-based `_is_local_name(val)` filtering on
	SSA value names. When an SSA dest happened to share a name string with
	a user storage local, the dest was excluded from `owned_values`, the
	StoreLocal rewriter went down the `_ensure_owned` path and inserted a
	spurious retain.  The original +1 from the call return was never
	balanced by a release, producing a memcheck-visible leak.

	Repro shape: a String-returning call followed by `val t<N> = call_result`
	where t<N> matches the SSA temp counter at that point.
	"""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"import std.io as io;\n"
		"import std.mem as mem;\n"
		"\n"
		"fn make_owned() nothrow -> String {\n"
		"\tvar b = io.buffer(5);\n"
		"\tio.buffer_write(&mut b, 0, cast<Byte>(104));\n"
		"\tio.buffer_write(&mut b, 1, cast<Byte>(105));\n"
		"\tio.buffer_write(&mut b, 2, cast<Byte>(0));\n"
		"\tio.buffer_write(&mut b, 3, cast<Byte>(0));\n"
		"\tio.buffer_write(&mut b, 4, cast<Byte>(0));\n"
		"\treturn core.string_from_utf8_bytes(io.buffer_ptr(&b), 2);\n"
		"}\n"
		"\n"
		"fn main() nothrow -> Int {\n"
		# `t2` collides with the MIR temp counter (the call dest at this
		# point becomes SSA temp `t2`).  This is the exact name-collision
		# shape that triggered the original bug.
		"\tval t2 = make_owned();\n"
		"\tif t2.byte_length() != 2 { return 1; }\n"
		"\treturn 0;\n"
		"}\n"
	)
	ir = _compile_ir(tmp_path, source)
	main = _extract_func(ir, "main")

	# The bug pattern: between `%tN = call make_owned()` and the store
	# into the matching `%tN__addr` storage local, the buggy compiler
	# inserted `%__arcM = call drift_string_retain(%DriftString %tN)`
	# followed by `store ... %__arcM ...`.  The fix is a direct
	# `store ... %tN ...` with no intervening retain.
	#
	# Find the call site for make_owned and inspect the next ~6 lines.
	lines = main.splitlines()
	call_idx = None
	call_dest = None
	for i, line in enumerate(lines):
		m = re.search(r'(%\w+)\s*=\s*call\s+%DriftString\s+@make_owned\(', line)
		if m:
			call_idx = i
			call_dest = m.group(1)
			break
	assert call_idx is not None, "make_owned() call not found in main"
	assert call_dest is not None

	# Scan the few instructions after the call for a retain of call_dest.
	window = "\n".join(lines[call_idx + 1 : call_idx + 12])
	# Pattern that the buggy compiler emits: retain(call_dest) + store retained
	bad_retain = re.search(
		rf"call\s+%DriftString\s+@drift_string_retain\(\s*%DriftString\s+{re.escape(call_dest)}\s*\)",
		window,
	)
	assert bad_retain is None, (
		f"Found a spurious drift_string_retain({call_dest}) immediately after "
		f"the make_owned() call.  This is the SSA-temp / storage-local name-"
		f"collision bug: the call result is already +1 (owned), so storing it "
		f"into the user local binding must be a direct move, not a retain.\n\n"
		f"Window after call:\n{window}"
	)


def test_borrowed_local_no_extra_release(tmp_path: Path) -> None:
	"""Exception field from local var must NOT get extra drift_string_release."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"\n"
		"error Info {\n"
		"\tmsg: String,\n"
		"}\n"
		"\n"
		"fn do_throw(s: String) -> Int {\n"
		"\tthrow Info(s);\n"
		"}\n"
		"\n"
		"fn main() nothrow -> Int {\n"
		"\tval r = try do_throw(\"hello\") catch { 0 };\n"
		"\treturn r;\n"
		"}\n"
	)
	ir = _compile_ir(tmp_path, source)
	do_throw = _extract_func(ir, "do_throw")
	dv_calls = do_throw.count("drift_dv_string(")
	# `Info(s)` projects the borrowed param `s` into a DiagnosticValue
	# via `drift_dv_string`.  This call must still happen — the DV path
	# is the legacy field-storage path until Slice 5 deletes DV.
	assert dv_calls >= 1, "do_throw should call drift_dv_string for Info(s)"
	# The original invariant on this test was `assert releases <= 1`,
	# pinning that the legacy DV-only throw path borrowed `s` and emitted
	# only the scope-exit release.  Slice 1 of the DV→JSON migration
	# (release 0.31.48, commit 5c6132c7) added throw-side JSON-params
	# projection: the throw lowering now emits a String-concat chain to
	# build `params_json`, producing several intermediate-string releases
	# inside the same function.  Total release count is no longer a
	# meaningful invariant — concat temps are real and expected.
	#
	# What we still pin: the BORROWED param `s` is not double-released
	# by DV construction.  An incorrect DV path that clones-and-also-
	# releases the borrowed source would surface as multiple
	# `drift_string_release(%DriftString %s)` calls.  Concat-temp
	# releases use other SSA names (e.g. `%t7`, `%t13`) and are out of
	# scope for this regression.
	import re as _re
	param_release_pat = _re.compile(r"drift_string_release\(%DriftString\s+%s\)")
	param_releases = len(param_release_pat.findall(do_throw))
	assert param_releases == 1, (
		f"do_throw must release the borrowed param `s` exactly once "
		f"(scope-exit), got {param_releases}.  An extra release would be "
		f"a double-free bug from DV construction borrowing-then-releasing "
		f"the source value."
	)
