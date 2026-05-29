#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
PEX artifact e2e runner — validates codegen/e2e cases through a staged PEX driftc binary.

Runs each e2e case by invoking the PEX binary via subprocess for the full
compile-to-binary pipeline, then compares runtime output against expected.json.

This exercises the artifact as an external consumer would use it: a single
driftc executable that bundles its own Python interpreter and dependencies.

Usage:
    python3 lang/tests/codegen/e2e/pex_e2e_runner.py --driftc build/pex-staging/bin/driftc --summarize
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from ctypes.util import find_library
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[4]
CASE_ROOT = ROOT / "lang" / "tests" / "codegen" / "e2e"
STDLIB_DIR = ROOT / "stdlib"
_VALGRIND_DIR = ROOT / "lang" / "tests" / "codegen" / "valgrind"
_VALGRIND_FIBER_SUPPRESSIONS = _VALGRIND_DIR / "fiber.supp"


def _physical_cpu_count_linux() -> int | None:
	cpuinfo = Path("/proc/cpuinfo")
	if not cpuinfo.exists():
		return None
	cores: set[tuple[str, str]] = set()
	for block in cpuinfo.read_text(encoding="utf-8", errors="ignore").split("\n\n"):
		if not block.strip():
			continue
		physical_id = core_id = None
		for line in block.splitlines():
			if ":" not in line:
				continue
			k, v = line.split(":", 1)
			key = k.strip().lower()
			val = v.strip()
			if key == "physical id":
				physical_id = val
			elif key == "core id":
				core_id = val
		if physical_id is not None and core_id is not None:
			cores.add((physical_id, core_id))
	return len(cores) if cores else None


def _resolve_jobs(arg: str) -> int:
	# Override order: explicit int > DRIFT_TEST_JOBS env > physical CPU > cpu_count//2.
	if arg != "auto":
		try:
			n = int(arg)
			return max(1, n)
		except ValueError:
			return 1
	env_jobs = os.environ.get("DRIFT_TEST_JOBS", "").strip()
	if env_jobs:
		try:
			n = int(env_jobs)
			if n > 0:
				return n
		except ValueError:
			pass
	physical = _physical_cpu_count_linux()
	if physical is not None and physical > 0:
		return max(1, physical)
	return max(1, (os.cpu_count() or 1) // 2)

def _env_true(name: str) -> bool:
	return os.environ.get(name, "") in ("1", "true", "True")

# Cases that cannot pass through the CLI path.
# Goal: keep this set empty.  Any entry here needs a documented reason.
CLI_KNOWN_SKIP: set[str] = set()


# ── Link + run helpers ──────────────────────────────────────────────


def _link_and_run(
	ir_path: Path,
	build_dir: Path,
	*,
	argv: list[str] | None = None,
	stdin_data: str | None = None,
	timeout_s: int = 60,
	extra_objects: list[Path] | None = None,
	memcheck_enabled: bool = False,
	massif_enabled: bool = False,
	fiber_suppressions_enabled: bool = False,
) -> tuple[str | None, int, str, str]:
	"""Compile IR with clang, link with runtime archive, and run the binary.

	Returns (link_error_or_None, exit_code, stdout, stderr).
	"""
	from lang.language_runtime import (
		build_runtime_archive,
		runtime_archive_variant,
	)
	from lang.codegen.llvm.test_utils import valgrind_cmd

	clang = shutil.which("clang")
	if clang is None:
		return "clang not found", 1, "", ""

	build_dir.mkdir(parents=True, exist_ok=True)
	bin_path = build_dir / "a.out"
	search_dirs = [
		Path("/lib"), Path("/lib64"), Path("/usr/lib"), Path("/usr/lib64"),
		Path("/lib/x86_64-linux-gnu"), Path("/usr/lib/x86_64-linux-gnu"),
	]

	def _link_flags_for_lib(name: str) -> list[str]:
		if not find_library(name):
			return []
		for d in search_dirs:
			if (d / f"lib{name}.so").exists():
				return [f"-l{name}"]
		return []

	link_libs = (
		_link_flags_for_lib("dw")
		+ _link_flags_for_lib("unwind")
		+ _link_flags_for_lib("unwind-x86_64")
		+ _link_flags_for_lib("elf")
	)
	asan_enabled = os.environ.get("DRIFT_ASAN") in ("1", "true", "True")
	ubsan_enabled = os.environ.get("DRIFT_UBSAN") in ("1", "true", "True")
	# Dual-runtime workstream: DRIFT_DEBUG=1 selects the debug-style
	# runtime variant and suppresses -O2; default (no env) is the
	# normal/optimized lane.
	debug_style_enabled = _env_true("DRIFT_DEBUG")
	link_flags: list[str] = []
	if asan_enabled:
		link_flags.extend(["-fsanitize=address"])
	if ubsan_enabled:
		link_flags.extend(["-fsanitize=undefined", "-fno-sanitize-recover=undefined"])
	if not debug_style_enabled:
		link_flags.append("-O2")

	# Runtime is always linked from the pre-built variant archive.
	# The legacy `DRIFT_RUNTIME_LINK_MODE=source` inline-compile path
	# was removed in 0.27.179.
	variant = runtime_archive_variant(
		debug_style=debug_style_enabled,
		asan_enabled=asan_enabled,
		ubsan_enabled=ubsan_enabled,
		alloc_track_enabled=False,
	)
	try:
		archive = str(build_runtime_archive(ROOT, clang=clang, variant=variant))
	except Exception as ex:
		return f"runtime archive build failed: {ex}", 1, "", ""
	compile_cmd = [
		clang, "-pthread", *link_flags,
		"-x", "ir", str(ir_path),
		"-x", "none", archive,
		*(str(o) for o in (extra_objects or [])),
		*link_libs, "-Wl,--as-needed", "-o", str(bin_path),
	]

	try:
		res = subprocess.run(compile_cmd, capture_output=True, text=True, cwd=ROOT, timeout=timeout_s)
	except subprocess.TimeoutExpired:
		return "clang compile timeout", 124, "", ""
	if res.returncode != 0:
		return res.stderr[:500], res.returncode, "", ""

	run_cmd = [str(bin_path), *(argv or [])]
	run_env = os.environ.copy()
	run_timeout_s = timeout_s
	if asan_enabled:
		run_timeout_s = max(timeout_s, 30) * 2
		asan_opts = run_env.get("ASAN_OPTIONS", "")
		defaults = "detect_leaks=1:halt_on_error=0"
		run_env["ASAN_OPTIONS"] = f"{defaults}:{asan_opts}" if asan_opts else defaults
	if ubsan_enabled:
		run_timeout_s = max(run_timeout_s, max(timeout_s, 30) * 2)
		ubsan_opts = run_env.get("UBSAN_OPTIONS", "")
		ubsan_defaults = "print_stacktrace=1:halt_on_error=1:abort_on_error=0:symbolize=1"
		run_env["UBSAN_OPTIONS"] = f"{ubsan_defaults}:{ubsan_opts}" if ubsan_opts else ubsan_defaults
	if memcheck_enabled:
		valgrind_suppressions: list[str] = []
		if fiber_suppressions_enabled and _VALGRIND_FIBER_SUPPRESSIONS.exists():
			valgrind_suppressions = [f"--suppressions={str(_VALGRIND_FIBER_SUPPRESSIONS)}"]
		run_timeout_s = max(timeout_s, 30) * 10
		run_cmd = valgrind_cmd(
			"--leak-check=full",
			"--show-leak-kinds=definite,indirect,possible,reachable",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			*valgrind_suppressions,
			f"--log-file={str(build_dir / 'valgrind-memcheck.log')}",
			*run_cmd,
		)
	elif massif_enabled:
		valgrind_suppressions = []
		if fiber_suppressions_enabled and _VALGRIND_FIBER_SUPPRESSIONS.exists():
			valgrind_suppressions = [f"--suppressions={str(_VALGRIND_FIBER_SUPPRESSIONS)}"]
		run_timeout_s = max(timeout_s, 30) * 6
		run_cmd = valgrind_cmd(
			*valgrind_suppressions,
			f"--massif-out-file={str(build_dir / 'valgrind-massif.out')}",
			f"--log-file={str(build_dir / 'valgrind-massif.log')}",
			*run_cmd,
			tool="massif",
		)
	try:
		run_res = subprocess.run(
			run_cmd, capture_output=True, text=True, timeout=run_timeout_s,
			input=stdin_data, env=run_env,
		)
	except subprocess.TimeoutExpired:
		return None, 124, "", "timeout during execution"
	return None, run_res.returncode, run_res.stdout, run_res.stderr


def _strip_asan_warnings(stderr: str) -> str:
	lines = [
		l for l in stderr.splitlines()
		if "WARNING: ASan doesn't fully support makecontext/swapcontext" not in l
	]
	clean = "\n".join(lines)
	if lines and stderr.endswith("\n"):
		clean += "\n"
	return clean


def _strip_ubsan_warnings(stderr: str) -> str:
	# Narrow stripper. CRITICAL: never strip "runtime error:" lines or any
	# UBSAN finding line — those must reach the test. Currently a pass-through;
	# add specific noise lines here only after the triage walk identifies them.
	return stderr


# ── Case introspection helpers ──────────────────────────────────────


def _module_name(case_dir: Path) -> str | None:
	"""Extract module name from main.drift.  Returns None when no declaration."""
	main = case_dir / "main.drift"
	if not main.exists():
		return None
	try:
		text = main.read_text(errors="replace")
	except OSError:
		return None
	m = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
	if m:
		return m.group(1).rstrip(";")
	return None


def _imports_stdlib(case_dir: Path) -> bool:
	"""True if any .drift file in case_dir imports std.* or lang.*."""
	for p in case_dir.rglob("*.drift"):
		try:
			text = p.read_text(errors="replace")
		except OSError:
			continue
		if re.search(r"import\s+(std|lang)\.", text):
			return True
	return False


# ── Per-case runner ──────────────────────────────────────────────────


def _run_case(
	case_dir: Path,
	driftc_bin: str,
	build_root: Path,
	timeout_s: int = 60,
) -> tuple[str, str]:
	"""Run a single e2e case.  Returns (case_name, status_string)."""
	case_name = case_dir.name
	expected_path = case_dir / "expected.json"
	main_path = case_dir / "main.drift"

	# `expected.json` + `main.drift` presence is validated at discovery
	# time in `main` (after CLI filtering, so a directly-named `--cases`
	# target cannot bypass the check); reaching here without them means
	# the filesystem was mutated between discovery and run.
	assert expected_path.exists() and main_path.exists(), (
		f"case '{case_name}' lost required fixture files between "
		f"discovery and run"
	)
	if case_name in CLI_KNOWN_SKIP:
		return case_name, "skipped (cli-known-skip)"

	expected = json.loads(expected_path.read_text())
	if expected.get("skip"):
		return case_name, "skipped (marked)"
	if expected.get("package_consumer_only"):
		return case_name, "skipped (package-consumer-only)"
	if expected.get("skip_memcheck") and _env_true("DRIFT_MEMCHECK"):
		return case_name, "skipped (memcheck)"
	if expected.get("sandbox_blocks") and os.environ.get("DRIFT_SANDBOX"):
		return case_name, "skipped (sandbox)"
	# Dual-runtime workstream: narrow per-case lane requirement.  Mirrors
	# the in-process runner's contract — only the debug-style lane
	# produces a stable backtrace symbol shape; cases that pin specific
	# frame names belong here.  Assertion semantics themselves are
	# lane-independent — only backtrace fidelity is lane-restricted.
	required_lane = expected.get("requires_lane")
	if required_lane == "debug-style" and not _env_true("DRIFT_DEBUG"):
		return case_name, "skipped (requires debug-style lane)"

	drift_files = sorted(str(p) for p in case_dir.rglob("*.drift"))
	if not drift_files:
		return case_name, "skipped (no sources)"

	# Distinguish three cases:
	# - diagnostics key absent → compile+run case (verify binary output)
	# - diagnostics key is [] → compile-success-only (verify zero diagnostics, no binary run)
	# - diagnostics key is [...] → diagnostic case (verify specific diagnostics)
	raw_diags = expected.get("diagnostics")
	has_diags = raw_diags is not None and len(raw_diags) > 0
	compile_only = raw_diags is not None and len(raw_diags) == 0
	expected_exit = expected.get("exit_code", 0)
	module_paths = expected.get("module_paths") or []
	run_args = expected.get("args", [])
	stdin_data = expected.get("stdin")
	case_timeout = expected.get("timeout_s", timeout_s)
	compiler_flags = expected.get("compiler_flags") or []
	c_sources = expected.get("c_sources") or []

	case_build = build_root / case_name
	case_build.mkdir(parents=True, exist_ok=True)

	mod_name = _module_name(case_dir)
	has_module_decl = mod_name is not None

	# PEX binaries must not inherit PYTHONPATH — it would cause the
	# embedded interpreter to import repo-local modules instead of
	# the bundled ones, leading to cryptic bootstrap failures.
	compile_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

	# ── Compile companion C sources ────────────────────────────
	extra_objects: list[Path] = []
	if c_sources:
		clang = shutil.which("clang")
		if clang is None:
			return case_name, "FAIL (clang not available for C helper compilation)"
		for c_name in c_sources:
			c_path = case_dir / c_name
			if not c_path.exists():
				return case_name, f"FAIL (c_sources: {c_name} not found in case dir)"
			obj_path = case_build / (c_path.stem + ".o")
			c_res = subprocess.run(
				[clang, "-c", str(c_path), "-o", str(obj_path)],
				capture_output=True, text=True, cwd=ROOT, timeout=30,
			)
			if c_res.returncode != 0:
				return case_name, f"FAIL (c compile {c_name}: {c_res.stderr[:200]})"
			extra_objects.append(obj_path)

	# ── Build compile command ───────────────────────────────────
	cmd: list[str] = [driftc_bin]

	# Module paths — use workspace mode (-M) when sources have module
	# declarations, OR when expected.json explicitly specifies module_paths
	# (diagnostic cases may intentionally lack declarations to test that
	# the compiler rejects them in workspace mode).
	if has_module_decl or (has_diags and module_paths):
		for mp in module_paths:
			cmd.extend(["-M", str(case_dir / mp)])
		if not module_paths:
			cmd.extend(["-M", str(case_dir)])

	# Always provide stdlib source root — even cases that don't explicitly
	# import std.* need the prelude for builtin methods (byte_length, etc.).
	cmd.extend(["--stdlib-root", str(STDLIB_DIR)])
	# --dev bypasses reserved namespace enforcement; omit it when the case
	# explicitly tests reserved namespace rejection (mirroring runner.py's
	# allow_reserved logic).
	expects_reserved = any(
		isinstance(d, dict) and "reserved module namespace" in str(d.get("message_contains", ""))
		for d in (raw_diags or [])
	)
	if not expects_reserved:
		cmd.append("--dev")
	cmd.append("--test-build-only")
	if has_module_decl:
		cmd.extend(["--entry", f"{mod_name}::main"])
	cmd.extend(compiler_flags)

	# ── Diagnostic cases ────────────────────────────────────────
	if has_diags:
		# Use --emit-ir so the compiler runs the full pipeline (including
		# entrypoint validation and namespace checks that only fire during
		# codegen).  The IR output is discarded; we only inspect diagnostics.
		diag_ir = case_build / "diag_check.ll"
		cmd.extend(["--emit-ir", str(diag_ir), "--json", *drift_files])
		try:
			res = subprocess.run(cmd, capture_output=True, text=True, timeout=case_timeout, env=compile_env)
		except subprocess.TimeoutExpired:
			return case_name, "FAIL (compile timeout)"

		if expected.get("use_driftc_json") is False:
			return case_name, "skipped (use_driftc_json=false)"

		# For diagnostic cases, compilation should fail.
		if res.returncode == 0 and expected_exit != 0:
			return case_name, "FAIL (expected compile failure but got success)"

		# If we expected failure and got it, check diagnostics match.
		try:
			payload = json.loads(res.stdout) if res.stdout.strip() else {}
		except json.JSONDecodeError:
			# If compilation failed as expected, that's sufficient for diagnostic cases
			if expected_exit != 0 and res.returncode != 0:
				return case_name, "ok"
			return case_name, "FAIL (invalid JSON output)"

		if payload.get("exit_code", res.returncode) != expected_exit:
			return case_name, f"FAIL (exit {payload.get('exit_code', res.returncode)} != expected {expected_exit})"

		diags = payload.get("diagnostics", [])
		for exp in expected.get("diagnostics", []):
			msg_sub = exp.get("message_contains")
			phase = exp.get("phase")
			found = False
			for d in diags:
				if phase is not None and d.get("phase") != phase:
					continue
				if msg_sub is not None and msg_sub not in d.get("message", ""):
					continue
				found = True
				break
			if not found:
				return case_name, "FAIL (missing expected diagnostic)"

		return case_name, "ok"

	# ── Compile-success-only cases (diagnostics: []) ───────────
	# The in-process runner short-circuits when expected diagnostics is an
	# empty list — it verifies compilation succeeds with zero diagnostics
	# and never runs the binary.  Mirror that behavior here.
	if compile_only:
		compile_check_cmd = list(cmd) + ["--json", *drift_files]
		try:
			res = subprocess.run(compile_check_cmd, capture_output=True, text=True, timeout=case_timeout, env=compile_env)
		except subprocess.TimeoutExpired:
			return case_name, "FAIL (compile timeout)"
		try:
			payload = json.loads(res.stdout) if res.stdout.strip() else {}
		except json.JSONDecodeError:
			payload = {}
		actual_exit = payload.get("exit_code", res.returncode)
		if actual_exit != expected_exit:
			return case_name, f"FAIL (compile exit {actual_exit} != expected {expected_exit})"
		diags = payload.get("diagnostics", [])
		if diags:
			msg = "; ".join(d.get("message", "") for d in diags[:3])
			return case_name, f"FAIL (unexpected diagnostics: {msg[:200]})"
		return case_name, "ok"

	# ── Compile-to-IR cases ─────────────────────────────────────
	ir_path = case_build / "out.ll"
	cmd.extend(["--emit-ir", str(ir_path), "--json", *drift_files])

	try:
		res = subprocess.run(cmd, capture_output=True, text=True, timeout=case_timeout, env=compile_env)
	except subprocess.TimeoutExpired:
		return case_name, "FAIL (compile timeout)"

	if res.returncode != 0:
		# Cases that expect compile failure (exit_code != 0 with no diagnostics
		# key) are satisfied by any non-zero compile exit code.
		if expected_exit != 0 and not has_diags:
			try:
				payload = json.loads(res.stdout) if res.stdout.strip() else {}
			except json.JSONDecodeError:
				payload = {}
			actual_exit = payload.get("exit_code", res.returncode)
			if actual_exit == expected_exit:
				return case_name, "ok"
		try:
			payload = json.loads(res.stdout) if res.stdout.strip() else {}
		except json.JSONDecodeError:
			payload = {}
		diags = payload.get("diagnostics", [])
		msg = "; ".join(d.get("message", "") for d in diags) if diags else res.stderr[:200]
		return case_name, f"FAIL (compile: {msg[:200]})"

	if not ir_path.exists():
		return case_name, "FAIL (no IR output)"

	# ── Link + Run ──────────────────────────────────────────────
	memcheck_enabled = _env_true("DRIFT_MEMCHECK")
	massif_enabled = _env_true("DRIFT_MASSIF")
	fiber_suppressions_enabled = _env_true("DRIFT_VALGRIND_SUPPRESS_FIBER") or bool(expected.get("valgrind_suppress_fiber"))
	link_err, exit_code, stdout, stderr = _link_and_run(
		ir_path, case_build,
		argv=run_args,
		stdin_data=stdin_data,
		timeout_s=case_timeout,
		extra_objects=extra_objects,
		memcheck_enabled=memcheck_enabled,
		massif_enabled=massif_enabled,
		fiber_suppressions_enabled=fiber_suppressions_enabled,
	)
	if link_err is not None:
		return case_name, f"FAIL (link: {link_err[:200]})"

	asan_enabled = _env_true("DRIFT_ASAN")
	ubsan_enabled = _env_true("DRIFT_UBSAN")
	stderr_clean = _strip_asan_warnings(stderr) if asan_enabled else stderr
	if ubsan_enabled:
		stderr_clean = _strip_ubsan_warnings(stderr_clean)

	# ── Compare output ──────────────────────────────────────────
	if exit_code != expected_exit:
		return case_name, f"FAIL (exit {exit_code} != expected {expected_exit})"

	expected_stdout = expected.get("stdout")
	if expected_stdout is not None and expected_stdout != "__ANY__" and stdout != expected_stdout:
		return case_name, f"FAIL (stdout mismatch: got {stdout!r})"

	stderr_contains = expected.get("stderr_contains")
	if isinstance(stderr_contains, str) and stderr_contains not in stderr_clean:
		return case_name, f"FAIL (stderr missing fragment: {stderr_contains!r})"

	expected_stderr = expected.get("stderr")
	if expected_stderr is not None and expected_stderr != "__ANY__" and stderr_clean != expected_stderr:
		return case_name, f"FAIL (stderr mismatch: got {stderr_clean!r})"

	return case_name, "ok"


def _run_case_worker(args: tuple[str, str, str, int]) -> tuple[str, str]:
	case_dir_str, driftc_bin, build_root_str, timeout_s = args
	return _run_case(Path(case_dir_str), driftc_bin, Path(build_root_str), timeout_s)


# ── Main ────────────────────────────────────────────────────────────


def main(argv: Iterable[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description="Run e2e cases through a PEX-staged driftc binary")
	ap.add_argument("--driftc", required=True, help="Path to the PEX driftc binary")
	ap.add_argument("cases", nargs="*", help="Specific case names (default: all)")
	ap.add_argument("-j", "--jobs", default="auto", help="Parallel workers ('auto' reads DRIFT_TEST_JOBS / physical CPU; default: auto)")
	ap.add_argument("--timeout", type=int, default=60, help="Per-case timeout (default: 60s)")
	ap.add_argument("--summarize", action="store_true", help="Print summary at end")
	ap.add_argument("--blocking", action="store_true", help="Exit non-zero on any failure")
	args = ap.parse_args(argv)
	jobs = _resolve_jobs(str(args.jobs))

	driftc_bin = str(Path(args.driftc).resolve())
	if not Path(driftc_bin).exists():
		print(f"error: driftc binary not found: {driftc_bin}", file=sys.stderr)
		return 2
	if not os.access(driftc_bin, os.X_OK):
		print(f"error: driftc binary not executable: {driftc_bin}", file=sys.stderr)
		return 2

	# Preflight: validate runtime mode env vars.
	memcheck_enabled = _env_true("DRIFT_MEMCHECK")
	massif_enabled = _env_true("DRIFT_MASSIF")
	asan_enabled = _env_true("DRIFT_ASAN")
	ubsan_enabled = _env_true("DRIFT_UBSAN")
	if asan_enabled and (memcheck_enabled or massif_enabled):
		print("error: DRIFT_ASAN is incompatible with DRIFT_MEMCHECK/DRIFT_MASSIF", file=sys.stderr)
		return 2
	if ubsan_enabled and (memcheck_enabled or massif_enabled):
		print("error: DRIFT_UBSAN is incompatible with DRIFT_MEMCHECK/DRIFT_MASSIF", file=sys.stderr)
		return 2
	if memcheck_enabled and massif_enabled:
		print("error: DRIFT_MEMCHECK and DRIFT_MASSIF are mutually exclusive", file=sys.stderr)
		return 2
	if (memcheck_enabled or massif_enabled) and shutil.which("valgrind") is None:
		print("error: valgrind not found (install valgrind or unset DRIFT_MEMCHECK/DRIFT_MASSIF)", file=sys.stderr)
		return 2

	start_time = time.monotonic() if args.summarize else None

	build_root = ROOT / "build" / "tests" / "pex_e2e" / f"run_{os.getpid()}"
	build_root.mkdir(parents=True, exist_ok=True)

	case_dirs = sorted(
		d for d in CASE_ROOT.iterdir()
		if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("__")
	) if CASE_ROOT.exists() else []

	if args.cases:
		names = set(args.cases)
		case_dirs = [d for d in case_dirs if d.name in names]

	# Discovery-time fixture validation (matches runner.py).  Empty /
	# malformed case dirs used to produce "skipped (missing files)"
	# lines indistinguishable from legitimate policy skips; now they
	# fail loudly before any case runs.
	malformed = [
		d for d in case_dirs
		if not (d / "expected.json").exists() or not (d / "main.drift").exists()
	]
	if malformed:
		print(
			"error: e2e case directories missing required fixture files "
			"(expected.json and/or main.drift):",
			file=sys.stderr,
		)
		for d in malformed:
			missing = [
				name for name in ("expected.json", "main.drift")
				if not (d / name).exists()
			]
			print(f"  {d.name}  (missing: {', '.join(missing)})", file=sys.stderr)
		print(
			"Populate the fixture or remove the directory.",
			file=sys.stderr,
		)
		return 1

	failures: list[tuple[str, str]] = []
	skipped = 0
	passed = 0

	if jobs <= 1 or len(case_dirs) <= 1:
		for case_dir in case_dirs:
			name, status = _run_case(case_dir, driftc_bin, build_root, args.timeout)
			print(f"{name}: {status}")
			if status.startswith("FAIL"):
				failures.append((name, status))
			elif status.startswith("skipped"):
				skipped += 1
			else:
				passed += 1
	else:
		work_items = [
			(str(d), driftc_bin, str(build_root), args.timeout) for d in case_dirs
		]
		with ProcessPoolExecutor(max_workers=jobs) as executor:
			futures = [executor.submit(_run_case_worker, item) for item in work_items]
			for fut in as_completed(futures):
				name, status = fut.result()
				print(f"{name}: {status}")
				if status.startswith("FAIL"):
					failures.append((name, status))
				elif status.startswith("skipped"):
					skipped += 1
				else:
					passed += 1

	if failures:
		print(file=sys.stderr)
		for name, status in failures:
			print(f"[pex e2e] {name}: {status}", file=sys.stderr)

	if start_time is not None:
		elapsed = time.monotonic() - start_time
		total = len(case_dirs)
		print("============[ PEX E2E SUMMARY ]=============", file=sys.stderr)
		print(f"Tests: {total} ({passed} passed, {skipped} skipped, {len(failures)} failed)", file=sys.stderr)
		print(f"Elapsed: {elapsed:.2f} seconds", file=sys.stderr)

	if args.blocking and failures:
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
