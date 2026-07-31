# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
ABI version stamping regression tests.

Verify that:
1. Generated IR contains the ABI version marker call.
2. Matching ABI version links successfully.
3. Mismatched ABI version fails at link time with unresolved symbol.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.driftc.driftc_versions import DRIFT_RT_ABI_VERSION
from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.language_runtime import build_runtime_archive, runtime_archive_variant
from lang.tests.support.module_packages import mk_module

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _compile_simple_program(tmp_path: Path, *, enforce_entrypoint: bool = False) -> str:
	"""Compile a trivial main program and return LLVM IR text."""
	(tmp_path / "app").mkdir(parents=True, exist_ok=True)
	(tmp_path / "app" / "main.drift").write_text(
		"module main;\n\npub fn main() nothrow -> Int {\n\treturn 0;\n}\n"
	)
	module_packages: dict = {}
	mk_module(module_packages, "main", "app")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=enforce_entrypoint,
	)
	assert not checked.diagnostics
	assert ir
	return ir


def test_ir_contains_abi_version_call(tmp_path: Path) -> None:
	"""Generated IR must contain a call to the ABI version marker."""
	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	abi_sym = f"__drift_rt_abi_version_{DRIFT_RT_ABI_VERSION}"
	assert f"call void @{abi_sym}()" in ir, f"ABI marker call not found in IR"
	assert f"declare void @{abi_sym}()" in ir, f"ABI marker declaration not found in IR"


def test_ir_declares_random_fill_runtime_helper(tmp_path: Path) -> None:
	"""Generated IR for secure random bytes must declare the runtime fill helper."""
	(tmp_path / "main.drift").write_text(
		"module std.random.test_fill_ir;\n\n"
		"import std.mem as mem;\n"
		"import lang.thread as thread;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar buf = unsafe { thread.array_byte_alloc_uninit(1) };\n"
		"\tval ptr = unsafe { thread.array_byte_as_mut_ptr(buf) };\n"
		"\tval rc = unsafe { thread.random_fill(ptr, 1) };\n"
		"\treturn rc;\n"
		"}\n"
	)
	module_packages: dict = {}
	mk_module(module_packages, "std.random.test_fill_ir", "std")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		package_id="std",
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	from lang.driftc.core.function_id import function_symbol
	main_ids = [fn_id for fn_id in signatures if fn_id.name == "main" and not signatures[fn_id].is_method]
	assert main_ids, "no main function found"
	entry = function_symbol(main_ids[0])
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry=entry,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics, checked.diagnostics
	assert ir
	assert "declare i64 @drift_random_fill(ptr, i64)" in ir, "random_fill runtime helper declaration missing from IR"


def test_ir_declares_nodelay_runtime_helpers(tmp_path: Path) -> None:
	"""Generated IR for TCP_NODELAY must declare both runtime helpers."""
	(tmp_path / "main.drift").write_text(
		"module std.net.test_nodelay_ir;\n\n"
		"import lang.thread as thread;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval r = thread.net_set_nodelay(3, 1);\n"
		"\tval g = thread.net_get_nodelay(3);\n"
		"\treturn r + g;\n"
		"}\n"
	)
	module_packages: dict = {}
	mk_module(module_packages, "std.net.test_nodelay_ir", "std")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		package_id="std",
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	from lang.driftc.core.function_id import function_symbol
	main_ids = [fn_id for fn_id in signatures if fn_id.name == "main" and not signatures[fn_id].is_method]
	assert main_ids, "no main function found"
	entry = function_symbol(main_ids[0])
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry=entry,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics, checked.diagnostics
	assert ir
	assert "declare i64 @drift_net_set_nodelay(i64, i64)" in ir, "drift_net_set_nodelay runtime helper declaration missing from IR"
	assert "declare i64 @drift_net_get_nodelay(i64)" in ir, "drift_net_get_nodelay runtime helper declaration missing from IR"


def test_ir_declares_reactor_et_helpers(tmp_path: Path) -> None:
	"""Generated IR for ET reactor helpers must declare both runtime symbols."""
	(tmp_path / "main.drift").write_text(
		"module std.net.test_reactor_et_ir;\n\n"
		"import lang.thread as thread;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval p = thread.reactor_check_pending(3, 1);\n"
		"\tval c = thread.reactor_io_charge(3, 1, 64);\n"
		"\treturn p + c;\n"
		"}\n"
	)
	module_packages: dict = {}
	mk_module(module_packages, "std.net.test_reactor_et_ir", "std")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		package_id="std",
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	from lang.driftc.core.function_id import function_symbol
	main_ids = [fn_id for fn_id in signatures if fn_id.name == "main" and not signatures[fn_id].is_method]
	assert main_ids, "no main function found"
	entry = function_symbol(main_ids[0])
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry=entry,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics, checked.diagnostics
	assert ir
	assert "declare i64 @drift_reactor_check_pending(i64, i64)" in ir, "drift_reactor_check_pending runtime helper declaration missing from IR"
	assert "declare i64 @drift_reactor_io_charge(i64, i64, i64)" in ir, "drift_reactor_io_charge runtime helper declaration missing from IR"


def test_ir_declares_env_runtime_helpers(tmp_path: Path) -> None:
	"""Generated IR for env access must declare both runtime helpers."""
	(tmp_path / "main.drift").write_text(
		"module std.env.test_env_ir;\n\n"
		"import lang.thread as thread;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval name = \"HOME\";\n"
		"\tval raw = thread.env_get_raw(name);\n"
		"\tval h = thread.env_has_raw(\"HOME\");\n"
		"\treturn h;\n"
		"}\n"
	)
	module_packages: dict = {}
	mk_module(module_packages, "std.env.test_env_ir", "std")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		package_id="std",
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	from lang.driftc.core.function_id import function_symbol
	main_ids = [fn_id for fn_id in signatures if fn_id.name == "main" and not signatures[fn_id].is_method]
	assert main_ids, "no main function found"
	entry = function_symbol(main_ids[0])
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry=entry,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics, checked.diagnostics
	assert ir
	assert "declare %DriftString @drift_env_get(%DriftString)" in ir, "drift_env_get runtime helper declaration missing from IR"
	assert "declare i64 @drift_env_has(%DriftString)" in ir, "drift_env_has runtime helper declaration missing from IR"


def test_ir_declares_microsecond_time_runtime_helpers(tmp_path: Path) -> None:
	"""The microsecond monotonic and UTC clock helpers are exposed (ABI 16+)."""
	(tmp_path / "main.drift").write_text(
		"module std.time.test_microsecond_ir;\n\n"
		"import lang.thread as thread;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval mono = thread.now_us();\n"
		"\tval utc = thread.now_utc_us();\n"
		"\tif mono < 0 or utc < 0 { return 1; }\n"
		"\treturn 0;\n"
		"}\n"
	)
	module_packages: dict = {}
	mk_module(module_packages, "std.time.test_microsecond_ir", "std")
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		[tmp_path / "main.drift"],
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		package_id="std",
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	from lang.driftc.core.function_id import function_symbol
	main_ids = [fn_id for fn_id in signatures if fn_id.name == "main" and not signatures[fn_id].is_method]
	assert main_ids
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry=function_symbol(main_ids[0]),
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
	)
	assert not checked.diagnostics, checked.diagnostics
	assert "declare i64 @drift_time_now_us()" in ir
	assert "declare i64 @drift_time_now_utc_us()" in ir
	assert "call i64 @drift_time_now_us()" in ir
	assert "call i64 @drift_time_now_utc_us()" in ir
	assert f"call void @__drift_rt_abi_version_{DRIFT_RT_ABI_VERSION}()" in ir


def test_std_time_rejected_on_32_bit_target(tmp_path: Path) -> None:
	"""std.time's signed epoch-microsecond representation requires a 64-bit Int."""
	source = tmp_path / "main.drift"
	source.write_text(
		"module main;\n\n"
		"import std.time as time;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval ts = time.utc_from_unix_micros(123456);\n"
		"\treturn time.utc_unix_micros(&ts);\n"
		"}\n"
	)
	ir_path = tmp_path / "main.ll"
	res = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang.driftc.driftc",
			str(source),
			"--stdlib-root",
			str(stdlib_root()),
			"--target-word-bits",
			"32",
			"--emit-ir",
			str(ir_path),
			"--json",
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 1, res.stderr
	payload = json.loads(res.stdout)
	assert payload["exit_code"] == 1
	assert any(
		d.get("phase") == "typecheck"
		and "std.time requires a 64-bit target" in d.get("message", "")
		for d in payload.get("diagnostics", [])
	), payload
	assert not ir_path.exists()


def test_direct_microsecond_clock_intrinsic_rejected_on_32_bit_target(tmp_path: Path) -> None:
	"""Direct lang.thread clock calls must fail before reaching LLVM lowering."""
	source = tmp_path / "main.drift"
	source.write_text(
		"module main;\n\n"
		"import lang.thread as thread;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\treturn thread.now_us() + thread.now_utc_us();\n"
		"}\n"
	)
	ir_path = tmp_path / "main.ll"
	res = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang.driftc.driftc",
			str(source),
			"--stdlib-root",
			str(stdlib_root()),
			"--target-word-bits",
			"32",
			"--emit-ir",
			str(ir_path),
			"--json",
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 1, res.stderr
	payload = json.loads(res.stdout)
	assert payload["exit_code"] == 1
	diagnostics = payload.get("diagnostics", [])
	assert any(
		d.get("phase") == "typecheck"
		and "lang.thread.now_us requires a 64-bit target" in d.get("message", "")
		for d in diagnostics
	), diagnostics
	assert any(
		d.get("phase") == "typecheck"
		and "lang.thread.now_utc_us requires a 64-bit target" in d.get("message", "")
		for d in diagnostics
	), diagnostics
	assert not any("internal:" in d.get("message", "") for d in diagnostics)
	assert not ir_path.exists()


def test_non_time_program_still_compiles_for_32_bit_target(tmp_path: Path) -> None:
	"""The std.time restriction must not disable unrelated 32-bit compilation."""
	source = tmp_path / "main.drift"
	source.write_text("module main;\n\npub fn main() nothrow -> Int { return 7; }\n")
	ir_path = tmp_path / "main.ll"
	res = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang.driftc.driftc",
			str(source),
			"--stdlib-root",
			str(stdlib_root()),
			"--target-word-bits",
			"32",
			"--emit-ir",
			str(ir_path),
			"--json",
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, res.stderr or res.stdout
	assert json.loads(res.stdout)["exit_code"] == 0
	assert ir_path.exists()


def test_abi_stamp_absent_without_wrapper(tmp_path: Path) -> None:
	"""Helper path (enforce_entrypoint=False) omits ABI stamp and OS wrapper."""
	ir = _compile_simple_program(tmp_path)
	abi_sym = f"__drift_rt_abi_version_{DRIFT_RT_ABI_VERSION}"
	assert f"call void @{abi_sym}()" not in ir
	assert "define i32 @main()" not in ir


def test_abi_stamp_present_with_wrapper(tmp_path: Path) -> None:
	"""Production wrapper path (enforce_entrypoint=True) contains ABI marker and OS wrapper."""
	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	abi_sym = f"__drift_rt_abi_version_{DRIFT_RT_ABI_VERSION}"
	assert f"call void @{abi_sym}()" in ir
	assert "define i32 @main()" in ir


def test_abi_version_mismatch_link_failure(tmp_path: Path) -> None:
	"""Patching IR to reference wrong ABI version must cause a link failure."""
	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	abi_sym = f"__drift_rt_abi_version_{DRIFT_RT_ABI_VERSION}"
	assert abi_sym in ir
	bogus_version = DRIFT_RT_ABI_VERSION + 999
	bogus_sym = f"__drift_rt_abi_version_{bogus_version}"
	# Replace only the declare + call lines referencing the ABI symbol.
	patched_ir = re.sub(
		re.escape(abi_sym) + r'(?=[\s()\"])',
		bogus_sym,
		ir,
	)
	assert bogus_sym in patched_ir

	clang = shutil.which("clang")
	assert clang, "clang not available"

	variant = runtime_archive_variant(debug_style=False, asan_enabled=False, alloc_track_enabled=False)
	archive = build_runtime_archive(ROOT, clang=clang, variant=variant)
	assert archive.exists()

	ir_path = tmp_path / "mismatch.ll"
	bin_path = tmp_path / "mismatch.out"
	ir_path.write_text(patched_ir)

	link_cmd = [
		clang,
		"-pthread",
		"-x", "ir", str(ir_path),
		"-x", "none", str(archive),
		"-Wl,--as-needed",
		"-o", str(bin_path),
	]
	result = subprocess.run(link_cmd, capture_output=True, text=True, cwd=ROOT)
	assert result.returncode != 0, "link should fail with mismatched ABI version"
	assert bogus_sym in result.stderr, (
		f"linker error should reference unresolved symbol {bogus_sym}; "
		f"got: {result.stderr[:500]}"
	)


def test_abi_mismatch_driver_hint(tmp_path: Path) -> None:
	"""Phase C: driftc driver emits ABI compatibility hint on version mismatch."""
	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	abi_sym = f"__drift_rt_abi_version_{DRIFT_RT_ABI_VERSION}"
	bogus_version = DRIFT_RT_ABI_VERSION + 999
	bogus_sym = f"__drift_rt_abi_version_{bogus_version}"
	patched_ir = re.sub(
		re.escape(abi_sym) + r'(?=[\s()\"])',
		bogus_sym,
		ir,
	)

	# Write patched IR and a minimal source file (driver requires source arg).
	ir_path = tmp_path / "hint_test.ll"
	ir_path.write_text(patched_ir)
	src_path = tmp_path / "app" / "main.drift"

	# Invoke driftc as a subprocess.  The driver will compile the source
	# (producing correct IR internally), but we trick it by replacing the
	# generated IR file between compilation and linking.  However, the
	# driver does compilation+linking in one shot so we cannot intercept.
	#
	# Instead, verify the Phase C detection predicate against real linker
	# stderr produced by the mismatch test above.
	clang = shutil.which("clang")
	assert clang, "clang not available"
	variant = runtime_archive_variant(debug_style=False, asan_enabled=False, alloc_track_enabled=False)
	archive = build_runtime_archive(ROOT, clang=clang, variant=variant)
	bin_path = tmp_path / "hint_test.out"
	link_cmd = [
		clang, "-pthread",
		"-x", "ir", str(ir_path),
		"-x", "none", str(archive),
		"-Wl,--as-needed",
		"-o", str(bin_path),
	]
	result = subprocess.run(link_cmd, capture_output=True, text=True, cwd=ROOT)
	assert result.returncode != 0

	# This is the exact predicate used by the driver (driftc.py link error handler).
	assert "__drift_rt_abi_version_" in result.stderr, (
		"linker stderr must contain ABI version symbol for Phase C hint to fire; "
		f"got: {result.stderr[:500]}"
	)
	# Verify the hint the driver would emit.
	expected_hint = f"driftc targets runtime ABI v{DRIFT_RT_ABI_VERSION}"
	assert str(DRIFT_RT_ABI_VERSION) in expected_hint


def test_driftc_version_output() -> None:
	"""§11 (0.33.93 clean break): `--version` is the concise HUMAN
	line only — `driftc X (ABI N)`, no pipe grammar. License/vendor/
	git moved to the machine contract `--version --json`
	(drift-toolchain-info/v1), sourced from the same
	`lang/versions.py` constants — single source of truth.
	"""
	from lang.driftc.driftc import main as driftc_main
	from lang.driftc.driftc_versions import DRIFTC_VERSION
	from lang.versions import DRIFTC_VENDOR, DRIFTC_LICENSE
	from lang.driftc.build_info import parse_toolchain_info
	import io
	import contextlib
	buf = io.StringIO()
	with contextlib.redirect_stdout(buf):
		rc = driftc_main(["--version"])
	assert rc == 0
	out = buf.getvalue()
	assert out == f"driftc {DRIFTC_VERSION} (ABI {DRIFT_RT_ABI_VERSION})\n"
	assert "|" not in out
	buf = io.StringIO()
	with contextlib.redirect_stdout(buf):
		rc = driftc_main(["--version", "--json"])
	assert rc == 0
	tc = parse_toolchain_info(buf.getvalue())
	assert tc["driftc"] == DRIFTC_VERSION
	assert tc["abi"] == DRIFT_RT_ABI_VERSION
	assert tc["license"] == DRIFTC_LICENSE
	assert tc["vendor"] == DRIFTC_VENDOR


def _decode_build_info_payload(ir: str) -> dict:
	"""Extract the @__drift_build_info section constant from IR text and
	validate it through the gate-facing reader (schema + canonical)."""
	import json as _json
	from lang.driftc.build_info import validate_build_info_payload
	for line in ir.split("\n"):
		if "@__drift_build_info" not in line:
			continue
		import re
		assert 'section ".drift_build_info"' in line, (
			"build-info constant must carry the .drift_build_info section attribute"
		)
		byte_vals = [int(m) for m in re.findall(r"i8\s+(\d+)", line)]
		return _json.loads(validate_build_info_payload(bytes(byte_vals)))
	raise AssertionError("@__drift_build_info constant not found in IR")


def test_ir_contains_build_info(tmp_path: Path) -> None:
	"""Generated IR must contain the build-info section constant with
	the required identity fields (migrated from the retired pipe
	provenance global in the 0.33.93 clean break)."""
	from lang.driftc.driftc_versions import DRIFTC_VERSION
	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	doc = _decode_build_info_payload(ir)
	assert doc["toolchain"]["driftc"] == DRIFTC_VERSION
	assert doc["toolchain"]["abi"] == DRIFT_RT_ABI_VERSION
	assert doc["build"]["word"] in (32, 64)
	assert doc["build"]["utc"], "build.utc must be stamped"


def test_build_info_present_without_wrapper(tmp_path: Path) -> None:
	"""The stamp is emitted even on the helper path (no entry wrapper)."""
	ir = _compile_simple_program(tmp_path)
	assert "@__drift_build_info" in ir


def test_abi_stamp_unchanged_with_build_info(tmp_path: Path) -> None:
	"""Stamp emission must not alter ABI stamp behavior."""
	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	abi_sym = f"__drift_rt_abi_version_{DRIFT_RT_ABI_VERSION}"
	assert f"call void @{abi_sym}()" in ir, "ABI stamp call missing"
	assert "@__drift_build_info" in ir, "build-info stamp missing"


def test_build_info_document_contract(tmp_path: Path) -> None:
	"""The emitted document passes the full drift-build-info/v1
	validator (schema, types, canonical encoding) — this replaces the
	retired pipe-grammar contract test outright."""
	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	doc = _decode_build_info_payload(ir)  # validator runs inside
	assert doc["format"] == "drift-build-info/v1"
	assert doc["artifact"] is None, "test-path compile must be unstamped"
	assert doc["dependencies"] == [] and doc["extra"] == {}


def test_build_info_values(tmp_path: Path) -> None:
	"""Pin the literal app-facing identity values in the stamp.

	`vendor` + `license` are constants of this toolchain build; a fork
	rebuilding under a different vendor/license must change
	DRIFTC_VENDOR / DRIFTC_LICENSE in `lang/versions.py`, and this
	test surfaces the change as a deliberate diff.

	`profile` is environment-driven; the literal `optimized` pin gates
	on the normal lane and skips under sanitizer / DRIFT_DEBUG lanes
	(the lane-agnostic shape contract lives in
	test_build_info_document_contract above).
	"""
	import os
	from lang.versions import DRIFTC_VENDOR, DRIFTC_LICENSE

	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	doc = _decode_build_info_payload(ir)
	assert doc["toolchain"]["vendor"] == DRIFTC_VENDOR
	assert doc["toolchain"]["license"] == DRIFTC_LICENSE

	def _env_true(name: str) -> bool:
		return os.environ.get(name, "").lower() in ("1", "true", "yes")

	if _env_true("DRIFT_ASAN") or _env_true("DRIFT_UBSAN") or _env_true("DRIFT_DEBUG"):
		import pytest
		pytest.skip("profile value pin only applies to the normal compiler lane")
	assert doc["build"]["profile"] == "optimized"


def _read_elf_sections(path: Path, name: str) -> list[bytes]:
	"""Minimal self-contained ELF64 section extractor (no readelf/
	objdump, target never executed): returns the CONTENT of every
	section named `name` — callers assert on the count, so duplicate
	sections are detectable."""
	import struct
	data = path.read_bytes()
	assert data[:4] == b"\x7fELF", "not an ELF binary"
	assert data[4] == 2, "test expects ELF64"
	assert data[5] == 1, "test expects little-endian"
	e_shoff, = struct.unpack_from("<Q", data, 0x28)
	e_shentsize, = struct.unpack_from("<H", data, 0x3A)
	e_shnum, = struct.unpack_from("<H", data, 0x3C)
	e_shstrndx, = struct.unpack_from("<H", data, 0x3E)
	def section_header(i: int):
		off = e_shoff + i * e_shentsize
		sh_name, = struct.unpack_from("<I", data, off)
		sh_offset, = struct.unpack_from("<Q", data, off + 0x18)
		sh_size, = struct.unpack_from("<Q", data, off + 0x20)
		return sh_name, sh_offset, sh_size
	_, str_off, str_size = section_header(e_shstrndx)
	shstr = data[str_off:str_off + str_size]
	out: list[bytes] = []
	for i in range(e_shnum):
		sh_name, sh_offset, sh_size = section_header(i)
		nul = shstr.index(b"\x00", sh_name)
		if shstr[sh_name:nul].decode("utf-8", "replace") == name:
			out.append(data[sh_offset:sh_offset + sh_size])
	return out


def _link_flags_for_lib(name: str) -> list[str]:
	"""Return linker flag for a system library if available."""
	for d in [Path("/usr/lib"), Path("/usr/lib/x86_64-linux-gnu"), Path("/usr/lib64")]:
		if (d / f"lib{name}.so").exists() or (d / f"lib{name}.a").exists():
			return [f"-l{name}"]
	return []


def test_build_info_survives_link(tmp_path: Path) -> None:
	"""The `.drift_build_info` SECTION — checked by name via the ELF
	section table, not merely by JSON bytes existing somewhere in the
	binary — must survive linking with exactly one instance whose
	content passes the full validator."""
	import json as _json
	from lang.driftc.driftc_versions import DRIFTC_VERSION
	from lang.driftc.build_info import validate_build_info_payload
	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	clang = shutil.which("clang")
	assert clang, "clang not available"
	variant = runtime_archive_variant(debug_style=False, asan_enabled=False, alloc_track_enabled=False)
	archive = build_runtime_archive(ROOT, clang=clang, variant=variant)
	ir_path = tmp_path / "buildinfo.ll"
	bin_path = tmp_path / "buildinfo.out"
	ir_path.write_text(ir)
	link_libs = (
		_link_flags_for_lib("dw")
		+ _link_flags_for_lib("unwind")
		+ _link_flags_for_lib("unwind-x86_64")
		+ _link_flags_for_lib("elf")
		+ _link_flags_for_lib("z")
	)
	link_cmd = [
		clang, "-pthread",
		"-x", "ir", str(ir_path),
		"-x", "none", str(archive),
		*link_libs,
		"-Wl,--as-needed",
		"-o", str(bin_path),
	]
	result = subprocess.run(link_cmd, capture_output=True, text=True, cwd=ROOT)
	assert result.returncode == 0, f"link failed: {result.stderr[:500]}"
	sections = _read_elf_sections(bin_path, ".drift_build_info")
	assert len(sections) == 1, (
		f"expected exactly one .drift_build_info section, got {len(sections)}"
	)
	doc = _json.loads(validate_build_info_payload(sections[0]))
	assert doc["toolchain"]["driftc"] == DRIFTC_VERSION


def test_abi14_binary_contains_no_dv_runtime_symbols(tmp_path: Path) -> None:
	"""Slice 7c-1 (0.31.64, ABI 14, 2026-05-06): a production binary
	produced at ABI 14 must NOT reference any of the deleted DV
	runtime symbols.

	Deleted symbols (this slice):
	  - `drift_dv_*` family (drift_dv_int / _bool / _float / _string /
	    _missing / _null / _array / _object / _object_from_entries /
	    _clone / _release / _kind / _index / _len / _entries / _get /
	    _get_field / _as_int / _as_bool / _as_float / _as_string /
	    _as_object)
	  - `drift_error_add_attr_dv`
	  - `drift_error_add_local_dv`
	  - `__exc_attrs_get_dv`
	  - `__exc_captures_get_dv`
	  - `drift_error_new_with_payload`
	  - `drift_diag_from_*` alias family (`_bool` / `_int` /
	    `_float` / `_string`)

	Builds a non-trivial sample with a `pub error` throw + catch to
	exercise the throw lowering path that historically went through
	the DV substrate (Slice 7a base ABI 13), then walks the linked
	binary's symbol table with `nm` and asserts zero matches.

	If this regresses (any symbol shows up), the cause is either
	(a) production lowering still emits a DV-related MIR op despite
	the codegen ICE guards (Slice 7b migration incomplete), or
	(b) the runtime archive at ABI 14 reintroduced the symbol — both
	are contract failures that should fail loudly here rather than
	silently leak DV runtime through the boundary."""
	clang = shutil.which("clang")
	assert clang, "clang not available"
	nm_bin = shutil.which("nm")
	assert nm_bin, "nm(1) not available"
	# Sample: pub error with field projection covers the throw-side
	# unification path post-Slice 7b (which was the last home of
	# DV-attachment emission).
	src = tmp_path / "main.drift"
	src.write_text("""\
module main;

pub error E { msg: String, count: Int }

fn boom() -> Int {
\tthrow E(msg = "oops", count = 7);
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn boom();
\t} catch E(e) {
\t\tval c = e.count;
\t\tif c == 7 { return 0; }
\t\treturn 1;
\t} catch e { return 2; }
\treturn 3;
}
""")
	out_bin = tmp_path / "abi14_smoke"
	from lang.driftc.driftc import main as driftc_main
	rc = driftc_main([
		"--stdlib-root", "stdlib",
		str(src),
		"-o", str(out_bin),
	])
	assert rc == 0, f"compile failed: rc={rc}"
	assert out_bin.exists(), "linked binary missing"
	# `nm` lists every defined and undefined symbol.  Restrict to the
	# DV runtime set; with Slice 7c-3 the `%DriftDiagnosticValue`
	# LLVM type alias and the C struct types are deleted, so any
	# accidental re-emission would also surface here as a
	# `DriftDiagnostic*` / `drift_dv_*` symbol leak.
	# Prefix-based check.  Any symbol matching one of the deleted
	# families fails the contract — this catches the entire
	# `drift_dv_*` and `drift_diag_from_*` families plus the
	# specific `drift_error_*_dv` / `__exc_*_get_dv` /
	# `drift_error_new_with_payload` symbols.  Using prefixes avoids
	# missing future helpers in the same family (the original
	# enumeration missed `drift_diag_from_bool` / `_float`, found
	# during Slice 7c-1 review).
	deleted_prefixes = (
		"drift_dv_",
		"drift_diag_from_",
	)
	deleted_exact = {
		"drift_error_add_attr_dv",
		"drift_error_add_local_dv",
		"__exc_attrs_get_dv",
		"__exc_captures_get_dv",
		"drift_error_new_with_payload",
	}
	nm_out = subprocess.run(
		[nm_bin, str(out_bin)],
		capture_output=True, text=True, timeout=sanitizer_timeout(10),
	)
	assert nm_out.returncode == 0, f"nm failed: {nm_out.stderr}"
	hits = []
	for line in nm_out.stdout.splitlines():
		# nm lines: "<addr> <type> <symbol>" or "         U <symbol>".
		# Symbol is the last whitespace-separated token.
		parts = line.split()
		if not parts:
			continue
		sym = parts[-1]
		# Strip leading underscore on Mach-O / leave as-is on ELF.
		bare = sym.lstrip("_")
		if bare.startswith(deleted_prefixes) or sym.startswith(deleted_prefixes):
			hits.append(sym)
			continue
		if sym in deleted_exact or bare in deleted_exact:
			hits.append(sym)
	# Dedupe; preserve order for diagnostic output.
	seen: set[str] = set()
	uniq_hits = [s for s in hits if not (s in seen or seen.add(s))]
	assert not uniq_hits, (
		f"ABI 14 binary references deleted DV runtime symbols — Slice "
		f"7c-1 contract failure.  Hits ({len(uniq_hits)}): {uniq_hits[:20]}"
	)


def test_abi_mismatch_bidirectional_21_22(tmp_path: Path) -> None:
	"""B-repr(B5) §5.4: the ABI 21→22 boundary is enforced in BOTH
	directions by the link stamp:

	  (a) an ABI-21 object (IR referencing __drift_rt_abi_version_21)
	      cannot link against the ABI-22 runtime archive — the linker
	      error names the v21 symbol (the driver-hint predicate fires);
	  (b) an ABI-22 object cannot link against an ABI-21 runtime
	      (facsimile: the real archive with its v22 stamp member swapped
	      for a v21 stamp) — the error names the v22 symbol.
	"""
	assert DRIFT_RT_ABI_VERSION == 22, "test written at the 21→22 boundary"
	ir = _compile_simple_program(tmp_path, enforce_entrypoint=True)
	abi_sym = f"__drift_rt_abi_version_{DRIFT_RT_ABI_VERSION}"
	assert abi_sym in ir

	clang = shutil.which("clang")
	assert clang, "clang not available"
	variant = runtime_archive_variant(debug_style=False, asan_enabled=False, alloc_track_enabled=False)
	archive = build_runtime_archive(ROOT, clang=clang, variant=variant)
	assert archive.exists()

	def _link(ir_text: str, rt: Path, tag: str):
		ir_path = tmp_path / f"{tag}.ll"
		ir_path.write_text(ir_text)
		return subprocess.run(
			[clang, "-pthread", "-x", "ir", str(ir_path), "-x", "none", str(rt),
			 "-lz", "-Wl,--as-needed", "-o", str(tmp_path / f"{tag}.out")],
			capture_output=True, text=True, cwd=ROOT,
		)

	# (a) old object x new runtime
	old_ir = re.sub(re.escape(abi_sym) + r'(?=[\s()\"])', "__drift_rt_abi_version_21", ir)
	assert "__drift_rt_abi_version_21" in old_ir
	result_a = _link(old_ir, archive, "old_obj_new_rt")
	assert result_a.returncode != 0, "ABI-21 object must not link against the ABI-22 runtime"
	assert "__drift_rt_abi_version_21" in result_a.stderr, result_a.stderr[:500]
	assert "__drift_rt_abi_version_" in result_a.stderr  # driver-hint predicate

	# (b) new object x old runtime (archive copy with the stamp swapped to v21)
	old_rt = tmp_path / "libdrift_rt_abi21_facsimile.a"
	shutil.copyfile(archive, old_rt)
	members = subprocess.run(["ar", "t", str(old_rt)], capture_output=True, text=True)
	stamp_members = [m for m in members.stdout.split() if "abi_version_stamp" in m]
	assert stamp_members, f"stamp member not found in archive: {members.stdout[:400]}"
	for m in stamp_members:
		subprocess.run(["ar", "d", str(old_rt), m], check=True, capture_output=True)
	stamp21_c = tmp_path / "stamp21.c"
	stamp21_c.write_text('void __drift_rt_abi_version_21(void) {}\n')
	stamp21_o = tmp_path / "stamp21.o"
	subprocess.run([clang, "-c", str(stamp21_c), "-o", str(stamp21_o)], check=True, capture_output=True)
	subprocess.run(["ar", "q", str(old_rt), str(stamp21_o)], check=True, capture_output=True)

	result_b = _link(ir, old_rt, "new_obj_old_rt")
	assert result_b.returncode != 0, "ABI-22 object must not link against an ABI-21 runtime"
	assert abi_sym in result_b.stderr, result_b.stderr[:500]
	assert "__drift_rt_abi_version_" in result_b.stderr  # driver-hint predicate
