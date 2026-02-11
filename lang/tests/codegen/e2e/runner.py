"""
Drift-source end-to-end runner (clang-based).

Each case lives under `lang/tests/codegen/e2e/<case>/` and must provide:
  - main.drift   (parsed with the copied lang parser)
  - expected.json with exit_code/stdout/stderr fields
    (optional: timeout_s for per-case runtime budget override)

The runner:
  1) Parses main.drift with the lang parser copy.
  2) Lowers to HIR via AstToHIR.
  3) Runs the full stubbed pipeline to LLVM IR (`compile_to_llvm_ir_for_tests`).
  4) Compiles IR with clang and executes the binary.
  5) Asserts diagnostics are empty and compares exit/stdout/stderr to expected.json.

	Artifacts are written to `build/tests/lang/tests/codegen/e2e/<case>/`.
	"""

from __future__ import annotations

import argparse
from ctypes.util import find_library
import traceback
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional

from lang.driftc.parser import (
	parse_drift_to_hir,
	parse_drift_files_to_hir,
	parse_drift_workspace_to_hir,
	stdlib_root,
)
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.driftc import compile_to_llvm_ir_for_tests, ReservedNamespacePolicy
from lang.driftc.core.function_id import function_symbol
from lang.language_runtime import get_runtime_sources


ROOT = Path(__file__).resolve().parents[4]
BUILD_ROOT = ROOT / "build" / "tests" / "lang" / "tests" / "codegen" / "e2e"


def _env_true(name: str) -> bool:
	return os.environ.get(name) in {"1", "true", "True"}


def _asan_options_with_defaults(existing: str | None) -> str:
	parts: list[str] = []
	if existing:
		parts = [p for p in existing.split(":") if p]
	keys = {p.split("=", 1)[0] for p in parts}
	if "detect_leaks" not in keys:
		parts.append("detect_leaks=0")
	if "halt_on_error" not in keys:
		parts.append("halt_on_error=1")
	return ":".join(parts)


def _drift_debug_diags_enabled() -> bool:
	raw = os.environ.get("DRIFT_DEBUG")
	if not raw:
		return False
	if raw in ("1", "true", "True"):
		return True
	try:
		cfg = json.loads(raw)
	except json.JSONDecodeError:
		return False
	if isinstance(cfg, dict):
		return bool(cfg.get("diags") or cfg.get("e2e_diags"))
	return False


def _run_ir_with_clang(
	ir: str,
	build_dir: Path,
	argv: list[str] | None = None,
	stdin_data: str | None = None,
	timeout_s: int = 30,
	alloc_track_enabled: bool = False,
) -> tuple[int, str, str]:
	"""Compile the provided LLVM IR with clang and return (exit, stdout, stderr)."""
	clang = shutil.which("clang-15") or shutil.which("clang")
	if clang is None:
		return 1, "", "clang not available"

	build_dir.mkdir(parents=True, exist_ok=True)
	ir_path = build_dir / "ir.ll"
	bin_path = build_dir / "a.out"
	ir_path.write_text(ir)

	runtime_sources = get_runtime_sources(ROOT)
	# The runtime sources include vendored C code (e.g. Ryu) that expects the
	# directory containing the `ryu/` folder to be on the include path.
	runtime_include = ROOT / "lang" / "language_runtime"
	search_dirs = [
		Path("/lib"),
		Path("/lib64"),
		Path("/usr/lib"),
		Path("/usr/lib64"),
		Path("/lib/x86_64-linux-gnu"),
		Path("/usr/lib/x86_64-linux-gnu"),
	]
	def _link_flags_for_lib(name: str) -> list[str]:
		if not find_library(name):
			return []
		for d in search_dirs:
			if (d / f"lib{name}.so").exists():
				return [f"-l{name}"]
		return []
	link_libs = _link_flags_for_lib("dw") + _link_flags_for_lib("unwind") + _link_flags_for_lib("unwind-x86_64") + _link_flags_for_lib("elf")
	memcheck_enabled = _env_true("DRIFT_MEMCHECK")
	massif_enabled = _env_true("DRIFT_MASSIF")
	asan_enabled = _env_true("DRIFT_ASAN")
	if asan_enabled and (memcheck_enabled or massif_enabled):
		return 1, "", "DRIFT_ASAN is incompatible with DRIFT_MEMCHECK/DRIFT_MASSIF"
	if (memcheck_enabled or massif_enabled) and shutil.which("valgrind") is None:
		return 1, "", "valgrind not available (install valgrind or unset DRIFT_MEMCHECK/DRIFT_MASSIF)"

	try:
		c_defs: list[str] = []
		c_flags: list[str] = []
		link_flags: list[str] = []
		link_wrap_flags: list[str] = []
		if alloc_track_enabled:
			c_defs.append("-DDRIFT_ALLOC_WRAP_ENABLED=1")
			link_wrap_flags.extend(
				[
					"-Wl,--wrap=malloc",
					"-Wl,--wrap=calloc",
					"-Wl,--wrap=realloc",
					"-Wl,--wrap=free",
					"-Wl,--wrap=posix_memalign",
					"-Wl,--wrap=aligned_alloc",
				]
			)
		if asan_enabled:
			c_flags.extend(["-fsanitize=address", "-g"])
			link_flags.extend(["-fsanitize=address"])
		compile_res = subprocess.run(
			[
				clang,
				"-pthread",
				*c_flags,
				*c_defs,
				"-I",
				str(runtime_include),
				"-x",
				"ir",
				str(ir_path),
				"-x",
				"c",
				*(str(p) for p in runtime_sources),
				*link_flags,
				*link_libs,
				*link_wrap_flags,
				"-Wl,--as-needed",
				"-o",
				str(bin_path),
			],
			capture_output=True,
			text=True,
			cwd=ROOT,
			timeout=timeout_s,
		)
	except subprocess.TimeoutExpired:
		return 124, "", "timeout during clang compile"
	if compile_res.returncode != 0:
		return compile_res.returncode, "", compile_res.stderr

	try:
		run_cmd = [str(bin_path), *(argv or [])]
		run_timeout_s = timeout_s
		run_env = os.environ.copy()
		if alloc_track_enabled:
			run_env["DRIFT_ALLOC_TRACK"] = "1"
		if asan_enabled:
			run_timeout_s = max(timeout_s, 30) * 2
			run_env["ASAN_OPTIONS"] = _asan_options_with_defaults(run_env.get("ASAN_OPTIONS"))
		if memcheck_enabled:
			run_timeout_s = max(timeout_s, 30) * 10
			run_cmd = [
				"valgrind",
				"--tool=memcheck",
				"--leak-check=full",
				"--show-leak-kinds=definite,indirect,possible,reachable",
				"--errors-for-leak-kinds=definite,indirect",
				"--error-exitcode=97",
				f"--log-file={str(build_dir / 'valgrind-memcheck.log')}",
				*run_cmd,
			]
		elif massif_enabled:
			run_timeout_s = max(timeout_s, 30) * 6
			run_cmd = [
				"valgrind",
				"--tool=massif",
				f"--massif-out-file={str(build_dir / 'valgrind-massif.out')}",
				f"--log-file={str(build_dir / 'valgrind-massif.log')}",
				*run_cmd,
			]
		run_res = subprocess.run(
			run_cmd,
			input=stdin_data,
			capture_output=True,
			text=True,
			cwd=ROOT,
			env=run_env,
			timeout=run_timeout_s,
		)
	except subprocess.TimeoutExpired:
		return 124, "", "timeout during execution"
	return run_res.returncode, run_res.stdout, run_res.stderr


def _compare_process_output(
	exit_code: int,
	stdout: str,
	stderr: str,
	expected: dict,
	debug: bool = False,
	alloc_stats: dict | None = None,
	alloc_track_check_enabled: bool = False,
) -> str | None:
	def _json_matches(expected_obj: object, actual_obj: object) -> bool:
		if isinstance(expected_obj, str) and expected_obj == "__ANY__":
			return True
		if type(expected_obj) is not type(actual_obj):
			return False
		if isinstance(expected_obj, dict):
			if set(expected_obj.keys()) != set(actual_obj.keys()):
				return False
			for k, v in expected_obj.items():
				if not _json_matches(v, actual_obj[k]):
					return False
			return True
		if isinstance(expected_obj, list):
			if len(expected_obj) != len(actual_obj):
				return False
			for e, a in zip(expected_obj, actual_obj):
				if not _json_matches(e, a):
					return False
			return True
		return expected_obj == actual_obj

	if exit_code != expected.get("exit_code", 0):
		msg = f"FAIL (exit {exit_code}, expected {expected.get('exit_code', 0)})"
		if debug and (stdout or stderr):
			msg = f"{msg}\nstdout:\n{stdout}\nstderr:\n{stderr}"
		return msg
	expect_stdout = expected.get("stdout", "")
	if expect_stdout != "__ANY__" and stdout != expect_stdout:
		msg = "FAIL (stdout mismatch)"
		if debug and (stdout or stderr):
			msg = f"{msg}\nstdout:\n{stdout}\nstderr:\n{stderr}"
		return msg
	expect_stderr = expected.get("stderr", "")
	if expect_stderr != "__ANY__" and stderr != expect_stderr:
		stderr_jsonl = expected.get("stderr_jsonl")
		if isinstance(stderr_jsonl, list):
			lines = [line for line in stderr.splitlines() if line.strip() != ""]
			if len(lines) == len(stderr_jsonl):
				try:
					actual_objs = [json.loads(line) for line in lines]
				except json.JSONDecodeError:
					actual_objs = []
				if len(actual_objs) == len(stderr_jsonl):
					ok = True
					for exp_obj, act_obj in zip(stderr_jsonl, actual_objs):
						if not _json_matches(exp_obj, act_obj):
							ok = False
							break
					if ok:
						return None
		msg = "FAIL (stderr mismatch)"
		if debug and (stdout or stderr):
			msg = f"{msg}\nstdout:\n{stdout}\nstderr:\n{stderr}"
		return msg
	if alloc_track_check_enabled:
		if alloc_stats is None:
			return "FAIL (alloc tracking enabled but no alloc stats line emitted)"
		if int(alloc_stats.get("live_blocks", -1)) != 0 or int(alloc_stats.get("live_bytes", -1)) != 0:
			return f"FAIL (alloc leak detected: live_blocks={alloc_stats.get('live_blocks')}, live_bytes={alloc_stats.get('live_bytes')})"
	return None


def _extract_alloc_track_stats(stderr: str) -> tuple[str, dict | None]:
	stats: dict | None = None
	kept: list[str] = []
	for line in stderr.splitlines():
		if line.startswith("__DRIFT_ALLOC_TRACK__ "):
			try:
				stats = json.loads(line[len("__DRIFT_ALLOC_TRACK__ ") :])
			except json.JSONDecodeError:
				stats = None
		else:
			kept.append(line)
	clean = "\n".join(kept)
	if kept and stderr.endswith("\n"):
		clean += "\n"
	return clean, stats


def _strip_asan_runtime_warnings(stderr: str) -> str:
	# ASan emits a known runtime warning for makecontext/swapcontext paths.
	# Keep ASan hard failures, but ignore this non-fatal warning line so
	# stderr-asserting tests stay stable under DRIFT_ASAN=1.
	lines: list[str] = []
	for line in stderr.splitlines():
		if "WARNING: ASan doesn't fully support makecontext/swapcontext functions" in line:
			continue
		lines.append(line)
	clean = "\n".join(lines)
	if lines and stderr.endswith("\n"):
		clean += "\n"
	return clean


def _run_case(case_dir: Path, timeout_s: int, debug: bool = False) -> str:
	def _json_matches(expected_obj: object, actual_obj: object) -> bool:
		if isinstance(expected_obj, str) and expected_obj == "__ANY__":
			return True
		if type(expected_obj) is not type(actual_obj):
			return False
		if isinstance(expected_obj, dict):
			if set(expected_obj.keys()) != set(actual_obj.keys()):
				return False
			for k, v in expected_obj.items():
				if not _json_matches(v, actual_obj[k]):
					return False
			return True
		if isinstance(expected_obj, list):
			if len(expected_obj) != len(actual_obj):
				return False
			for e, a in zip(expected_obj, actual_obj):
				if not _json_matches(e, a):
					return False
			return True
		return expected_obj == actual_obj

	expected_path = case_dir / "expected.json"
	source_path = case_dir / "main.drift"
	drift_files = sorted(case_dir.rglob("*.drift"))
	if not expected_path.exists() or not source_path.exists():
		return "skipped (missing expected.json or main.drift)"
	if not drift_files:
		return "skipped (missing .drift sources)"

	expected = json.loads(expected_path.read_text())
	if expected.get("skip"):
		return "skipped (marked)"
	if expected.get("sandbox_blocks") and os.environ.get("DRIFT_SANDBOX"):
		return "skipped (sandbox)"
	allow_reserved_flag = expected.get("dev_allow_reserved_namespaces")
	if allow_reserved_flag is not None:
		allow_reserved = bool(allow_reserved_flag)
	else:
		expect_diags = expected.get("diagnostics") or []
		expects_reserved = any(
			isinstance(d, dict) and "reserved module namespace" in str(d.get("message_contains", ""))
			for d in expect_diags
		)
		allow_reserved = (stdlib_root() is not None) and not expects_reserved
	module_paths = expected.get("module_paths") or []
	module_args: list[str] = []
	for mp in module_paths:
		module_args.extend(["-M", str(case_dir / mp)])
	# Compile-error path: delegate to driftc --json for structured diags.
	if expected.get("diagnostics") and expected.get("use_driftc_json", True):
		def _match_diag_span(exp: dict, d: dict) -> bool:
			# Optional span assertions for diagnostics emitted via driftc --json.
			#
			# These are best-effort and intentionally minimal: we only compare file/line/column
			# when the expectation includes them. This keeps tests stable while still locking
			# the key “pinned span” invariants for important parse/typecheck errors.
			if "file" in exp and exp["file"] != d.get("file"):
				return False
			if "line" in exp and exp["line"] != d.get("line"):
				return False
			if "column" in exp and exp["column"] != d.get("column"):
				return False
			return True

		cmd = [
			str(Path(sys.executable)),
			"-m",
			"lang.driftc.driftc",
			*module_args,
			*[str(p) for p in drift_files],
			"--json",
		]
		stdlib_path = stdlib_root()
		if stdlib_path is not None:
			cmd.extend(["--stdlib-root", str(stdlib_path)])
		if allow_reserved:
			cmd.append("--dev")
		if allow_reserved:
			cmd.insert(3, "--dev")
		res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
		try:
			payload = json.loads(res.stdout)
		except json.JSONDecodeError as err:
			return f"FAIL (expected JSON diagnostics, got parse error: {err})"
		exit_expected = expected.get("exit_code", 1)
		if payload.get("exit_code") != exit_expected:
			return f"FAIL (exit {payload.get('exit_code')} != expected {exit_expected})"
		diag_expect = expected.get("diagnostics", [])
		diags = payload.get("diagnostics", [])
		for exp in diag_expect:
			msg_sub = exp.get("message_contains")
			phase = exp.get("phase")
			match_found = False
			for d in diags:
				if phase is not None and d.get("phase") != phase:
					continue
				if msg_sub is not None and msg_sub not in d.get("message", ""):
					continue
				if not _match_diag_span(exp, d):
					continue
				match_found = True
				break
			if not match_found:
				if _drift_debug_diags_enabled():
					diag_text = "; ".join(d.get("message", "") for d in diags)
					return f"FAIL (missing expected diagnostic: {diag_text})"
				return "FAIL (missing expected diagnostic)"
		return "ok"
	# Always parse using the workspace loader (even for single-file cases) so
	# import resolution behavior is consistent:
	# - missing module imports are diagnosed,
	# - multi-file modules and multi-module cases share the same entry path.
	allow_reserved_flag = expected.get("dev_allow_reserved_namespaces")
	if allow_reserved_flag is not None:
		allow_reserved = bool(allow_reserved_flag)
	else:
		expect_diags = expected.get("diagnostics") or []
		expects_reserved = any(
			isinstance(d, dict) and "reserved module namespace" in str(d.get("message_contains", ""))
			for d in expect_diags
		)
		allow_reserved = (stdlib_root() is not None) and not expects_reserved
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[case_dir / mp for mp in module_paths] or None,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	origin_by_fn_id: dict[object, Path] = {}
	for mod in modules.values():
		origin_by_fn_id.update(mod.origin_by_fn_id)
	expected_phase = expected.get("phase")

	if parse_diags:
		actual_exit = 1  # Parsing produced diagnostics; treat as compile failure.
		expected_exit = expected.get("exit_code", actual_exit)
		actual_stdout = ""
		actual_stderr = "\n".join(d.message for d in parse_diags)
		if expected_phase not in (None, "parser"):
			return f"FAIL (expected phase {expected_phase}, but diagnostics occurred during parser phase)"
		if expected_exit != actual_exit:
			return f"FAIL (parser phase exit {actual_exit}, expected {expected_exit})"
		if expected.get("stdout", actual_stdout) != actual_stdout:
			return "FAIL (parser phase stdout mismatch)"
		if expected.get("stderr", actual_stderr) != actual_stderr:
			return "FAIL (parser phase stderr mismatch)"
		return "ok"

	main_ids = [fn_id for fn_id, sig in signatures.items() if fn_id.name == "main" and not sig.is_method]
	main_id = main_ids[0] if len(main_ids) == 1 else None
	entry = function_symbol(main_id) if main_id is not None else "main"
	main_sig = signatures.get(main_id) if main_id is not None else None
	needs_argv = False
	if main_sig and main_sig.param_type_ids and len(main_sig.param_type_ids) == 1 and type_table is not None:
		param_ty = main_sig.param_type_ids[0]
		td = type_table.get(param_ty)
		if td.kind.name == "ARRAY" and td.param_types:
			elem_def = type_table.get(td.param_types[0])
			if elem_def.name == "String":
				needs_argv = True
	try:
		reserved_policy = (
			ReservedNamespacePolicy.ALLOW_DEV
			if allow_reserved
			else ReservedNamespacePolicy.ENFORCE
		)
		ir, checked = compile_to_llvm_ir_for_tests(
			func_hirs=func_hirs,
			signatures=signatures,
			exc_env=exception_catalog,
			entry=entry,
			type_table=type_table,
			module_exports=module_exports,
			module_deps=module_deps,
			origin_by_fn_id=origin_by_fn_id,
			enforce_entrypoint=True,
			reserved_namespace_policy=reserved_policy,
		)
	except Exception as err:  # pragma: no cover - defensive for negative e2e cases
		expected_diags = expected.get("diagnostics", [])
		if expected_diags:
			msg = str(err)
			for exp in expected_diags:
				if exp.get("message_contains") and exp["message_contains"] not in msg:
					return f"FAIL (missing expected diagnostic; saw exception {msg})"
			return "ok"
		if debug:
			trace = traceback.format_exc()
			return f"FAIL (worker exception: {err})\n{trace}"
		raise

	# If the checker produced diagnostics and the test expects them, validate and
	# short-circuit before running codegen output.
	checked_diags = getattr(checked, "diagnostics", [])
	expected_diags = expected.get("diagnostics")
	if expected_diags is not None:
		expected_phase = expected.get("phase", "typecheck")
		expected_exit = expected.get("exit_code", 1)
		diag_has_error = any(getattr(d, "severity", None) == "error" for d in checked_diags)
		if expected_exit != (1 if diag_has_error else 0):
			return f"FAIL (expected exit {expected_exit} but diagnostics imply exit {1 if diag_has_error else 0})"
		for exp in expected_diags:
			msg_sub = exp.get("message_contains")
			phase = exp.get("phase", expected_phase)
			match_found = False
			for d in checked_diags:
				if phase is not None and getattr(d, "phase", None) != phase:
					continue
				if msg_sub is not None and msg_sub not in d.message:
					continue
				match_found = True
				break
			if not match_found:
				if _drift_debug_diags_enabled():
					diag_text = "; ".join(d.message for d in checked_diags)
					return f"FAIL (missing expected diagnostic: {diag_text})"
				return "FAIL (missing expected diagnostic)"
		return "ok"
	if checked_diags:
		if _drift_debug_diags_enabled():
			diag_text = "; ".join(d.message for d in checked_diags)
			return f"FAIL (unexpected checker diagnostics: {diag_text})"
		return "FAIL (unexpected checker diagnostics)"

	if checked.diagnostics:
		expected_exit = expected.get("exit_code", 1)
		expected_phase = expected.get("phase", "typecheck")
		if expected.get("diagnostics") is None:
			return "FAIL (unexpected diagnostics during compilation)"
		if expected_exit != 1:
			return f"FAIL (diagnostics present, expected exit {expected_exit})"
		for exp in expected.get("diagnostics", []):
			msg_sub = exp.get("message_contains")
			phase = exp.get("phase", expected_phase)
			match_found = False
			for d in checked.diagnostics:
				if phase is not None and getattr(d, "phase", None) != phase:
					continue
				if msg_sub is not None and msg_sub not in d.message:
					continue
				match_found = True
				break
			if not match_found:
				return "FAIL (missing expected diagnostic)"
		return "ok"

	build_dir = BUILD_ROOT / case_dir.name
	run_args = expected.get("args", [])
	stdin_data = expected.get("stdin")
	if needs_argv and not run_args:
		return "FAIL (argv main requires args in expected.json)"
	alloc_track_env_enabled = os.environ.get("DRIFT_ALLOC_TRACK") in {"1", "true", "True"}
	alloc_track_case_required = bool(expected.get("alloc_track_leak") or expected.get("alloc_track_no_leak_if_enabled"))
	alloc_track_enabled = alloc_track_case_required or alloc_track_env_enabled
	exit_code, stdout, stderr = _run_ir_with_clang(
		ir,
		build_dir,
		argv=run_args,
		stdin_data=stdin_data,
		timeout_s=timeout_s,
		alloc_track_enabled=alloc_track_enabled,
	)
	stderr_clean, alloc_stats = _extract_alloc_track_stats(stderr)
	if _env_true("DRIFT_ASAN"):
		stderr_clean = _strip_asan_runtime_warnings(stderr_clean)
	if exit_code != 0 and stderr == "clang not available":
		return "FAIL (clang not available)"
	if exit_code != 0 and stderr.startswith("valgrind not available"):
		return f"FAIL ({stderr})"
	if exit_code == 124 and "timeout" in stderr:
		return f"FAIL (timeout: {stderr})"

	if checked.diagnostics:
		actual_exit = 1
		actual_stdout = ""
		actual_stderr = "\n".join(d.message for d in checked.diagnostics)
		expected_exit = expected.get("exit_code", actual_exit)
		if expected_phase not in (None, "checker", "codegen"):
			return f"FAIL (expected phase {expected_phase}, but diagnostics occurred during checker/codegen phase)"
		if expected_exit != actual_exit:
			return f"FAIL (checker/codegen phase exit {actual_exit}, expected {expected_exit})"
		if expected.get("stdout", actual_stdout) != actual_stdout:
			return "FAIL (checker/codegen phase stdout mismatch)"
		if expected.get("stderr", actual_stderr) != actual_stderr:
			return "FAIL (checker/codegen phase stderr mismatch)"
		return "ok"

	mismatch = _compare_process_output(
		exit_code,
		stdout,
		stderr_clean,
		expected,
		debug,
		alloc_stats=alloc_stats,
		alloc_track_check_enabled=alloc_track_enabled,
	)
	if mismatch is not None:
		return mismatch
	return "ok"


def _effective_case_timeout(case_dir: Path, default_timeout_s: int) -> int:
	expected_path = case_dir / "expected.json"
	if not expected_path.exists():
		return default_timeout_s
	try:
		expected = json.loads(expected_path.read_text())
	except Exception:
		return default_timeout_s
	raw = expected.get("timeout_s")
	if raw is None:
		return default_timeout_s
	try:
		case_timeout = int(raw)
	except Exception:
		return default_timeout_s
	if case_timeout <= 0:
		return default_timeout_s
	return case_timeout


def _run_case_with_timeout(case_dir: Path, timeout_s: int, debug: bool = False) -> str:
	effective_timeout = _effective_case_timeout(case_dir, timeout_s)
	if not effective_timeout:
		return _run_case(case_dir, effective_timeout, debug=debug)
	old_handler = None
	def _on_timeout(signum, frame) -> None:
		raise TimeoutError(f"timeout after {effective_timeout}s")
	old_handler = signal.signal(signal.SIGALRM, _on_timeout)
	signal.alarm(effective_timeout)
	try:
		return _run_case(case_dir, effective_timeout, debug=debug)
	except TimeoutError:
		return f"FAIL (timeout after {effective_timeout}s)"
	finally:
		signal.alarm(0)
		if old_handler is not None:
			signal.signal(signal.SIGALRM, old_handler)


def _run_case_worker(case_dir: str, timeout_s: int, debug: bool) -> tuple[str, str]:
	path = Path(case_dir)
	effective_timeout = _effective_case_timeout(path, timeout_s)
	old_handler = None
	if effective_timeout:
		def _on_timeout(signum, frame) -> None:
			raise TimeoutError(f"timeout after {effective_timeout}s")
		old_handler = signal.signal(signal.SIGALRM, _on_timeout)
		signal.alarm(effective_timeout)
	try:
		status = _run_case(path, effective_timeout, debug=debug)
	except TimeoutError:
		return path.name, f"FAIL (timeout after {effective_timeout}s)"
	except Exception as err:  # pragma: no cover - worker guardrail
		if debug:
			trace = traceback.format_exc()
			return path.name, f"FAIL (worker exception: {err})\n{trace}"
		return path.name, f"FAIL (worker exception: {err})"
	finally:
		if effective_timeout:
			signal.alarm(0)
			if old_handler is not None:
				signal.signal(signal.SIGALRM, old_handler)
	return path.name, status


def _run_case_chunk(case_dirs: list[str], timeout_s: int, debug: bool) -> list[tuple[str, str]]:
	results: list[tuple[str, str]] = []
	for case_dir in case_dirs:
		results.append(_run_case_worker(case_dir, timeout_s, debug))
	return results


def main(argv: Iterable[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description="Run Drift-source codegen e2e cases (clang-based)")
	ap.add_argument(
		"cases",
		nargs="*",
		help="Specific test case names to run (directories under lang/tests/codegen/e2e)",
	)
	ap.add_argument(
		"-j",
		"--jobs",
		type=str,
		default="auto",
		help="Run cases in parallel across N worker processes (N or 'auto', default: auto)",
	)
	ap.add_argument(
		"--work-size",
		type=int,
		default=4,
		help="Run this many cases sequentially per worker task (default: 4)",
	)
	ap.add_argument(
		"--ordered",
		action="store_true",
		help="Print results in deterministic case order instead of as they finish",
	)
	ap.add_argument(
		"--timeout",
		type=int,
		default=30,
		help="Per-case timeout in seconds (default: 30)",
	)
	ap.add_argument(
		"--debug",
		action="store_true",
		help="Emit detailed diagnostics on failures (stdout/stderr/tracebacks)",
	)
	ap.add_argument(
		"--summarize",
		action="store_true",
		help="Print a summary line with test counts and elapsed time",
	)
	args = ap.parse_args(argv)
	start_time = time.monotonic() if args.summarize else None

	case_root = ROOT / "lang" / "tests" / "codegen" / "e2e"
	case_dirs = (
		sorted(
			d
			for d in case_root.iterdir()
			if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("__")
		)
		if case_root.exists()
		else []
	)
	if args.cases:
		names = set(args.cases)
		case_dirs = [d for d in case_dirs if d.name in names]

	failures: list[tuple[Path, str]] = []
	skipped: set[str] = set()
	if args.jobs == "auto":
		cpu_count = os.cpu_count() or 1
		jobs = max(1, cpu_count - 1)
	else:
		try:
			jobs = int(args.jobs)
		except ValueError:
			print(f"invalid --jobs value: {args.jobs!r} (expected integer or 'auto')", file=sys.stderr)
			return 2
		if jobs < 1:
			print(f"invalid --jobs value: {jobs} (must be >= 1 or 'auto')", file=sys.stderr)
			return 2
	if jobs == 1 or len(case_dirs) <= 1:
		for case_dir in case_dirs:
			status = _run_case_with_timeout(case_dir, args.timeout, debug=args.debug)
			print(f"{case_dir.name}: {status}")
			if status.startswith("FAIL"):
				failures.append((case_dir, status))
			elif status.startswith("skipped"):
				skipped.add(case_dir.name)
	else:
		if args.work_size < 1:
			print(f"invalid --work-size value: {args.work_size} (must be >= 1)", file=sys.stderr)
			return 2
		chunk_size = args.work_size
		chunks: list[list[str]] = []
		for i in range(0, len(case_dirs), chunk_size):
			chunks.append([str(p) for p in case_dirs[i : i + chunk_size]])
		with ProcessPoolExecutor(max_workers=jobs) as executor:
			futures = [executor.submit(_run_case_chunk, chunk, args.timeout, args.debug) for chunk in chunks]
			if args.ordered:
				results: dict[str, str] = {}
				next_idx = 0
				for fut in as_completed(futures):
					for name, status in fut.result():
						results[name] = status
					# Emit any newly available results in deterministic order.
					while next_idx < len(case_dirs):
						case_dir = case_dirs[next_idx]
						status = results.get(case_dir.name)
						if status is None:
							break
						print(f"{case_dir.name}: {status}")
						if status.startswith("FAIL"):
							failures.append((case_dir, status))
						elif status.startswith("skipped"):
							skipped.add(case_dir.name)
						next_idx += 1
				for case_dir in case_dirs[next_idx:]:
					status = results.get(case_dir.name, "FAIL (missing result)")
					print(f"{case_dir.name}: {status}")
					if status.startswith("FAIL"):
						failures.append((case_dir, status))
					elif status.startswith("skipped"):
						skipped.add(case_dir.name)
			else:
				for fut in as_completed(futures):
					for name, status in fut.result():
						print(f"{name}: {status}")
						if status.startswith("FAIL"):
							failures.append((case_root / name, status))
						elif status.startswith("skipped"):
							skipped.add(name)

	if failures:
		for case, status in failures:
			print(f"[codegen e2e] {case.name}: {status}", file=sys.stderr)
		exit_code = 1
	else:
		exit_code = 0
	if start_time is not None:
		elapsed = time.monotonic() - start_time
		total = len(case_dirs)
		failed = len(failures)
		skipped_count = len(skipped)
		passed = total - failed - skipped_count
		print("============[ SUMMARY ]=============", file=sys.stderr)
		print(f"Tests: {total} ({passed} successful, {skipped_count} skipped, {failed} failed)", file=sys.stderr)
		print(f"Elapsed: {elapsed:.2f} seconds", file=sys.stderr)
		if skipped:
			print("Skipped tests:", file=sys.stderr)
			for case_dir in case_dirs:
				if case_dir.name in skipped:
					print(case_dir.name, file=sys.stderr)
	return exit_code


if __name__ == "__main__":
	raise SystemExit(main())
