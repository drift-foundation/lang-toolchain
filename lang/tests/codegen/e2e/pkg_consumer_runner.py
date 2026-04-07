"""
Package-consumer e2e runner (signed stdlib path).

Runs e2e test cases through the full driftc --package-root codepath with
a signed std.dmp built from the real stdlib.  This exercises the package
linking, type-table remapping, and wrapper emission paths that differ from
the single-compilation-unit e2e runner.

Usage:
    # Report-only (exit 0 even with failures):
    PYTHONPATH=. .venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py --summarize

    # Blocking smoke subset:
    PYTHONPATH=. .venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py \
        --blocking --only-cases result_ok_array_match_move_no_double_free,...

    # ASAN variant:
    DRIFT_ASAN=1 PYTHONPATH=. .venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py --summarize
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from ctypes.util import find_library
from hashlib import sha256
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[4]
CASE_ROOT = ROOT / "lang" / "tests" / "codegen" / "e2e"
STDLIB_DIR = ROOT / "stdlib"
BUILD_ROOT_DEFAULT = ROOT / "build" / "tests" / "pkg_consumer"

PHASE_COMPILE_CHECK = "compile-check"
PHASE_COMPILE_CODEGEN = "compile-codegen"
PHASE_LINK = "link"
PHASE_RUNTIME = "runtime"
PHASE_RUNTIME_ASAN = "runtime-asan"
PHASE_RUNTIME_UBSAN = "runtime-ubsan"

# Version used when building the test std package.  The --dep filter on the
# consumer compile must match exactly, so keep these in sync.
_STD_PACKAGE_VERSION = "0.0.0-test"

SMOKE_CASES = [
	"result_ok_array_match_move_no_double_free",
	"array_push_move_non_copy_implicit",
	"array_pop_move_out_non_copy",
	"match_wildcard_owned_payload_drop",
	"abi_entrypoint_cross_module_call",
	"abi_entrypoint_cross_module_struct_ok",
	"std_core_string_from_utf8_bytes_api",
	"std_runtime_scoped_stack_basic",
	"std_io_preamble_installs_stdio",
	"try_wrap_result_err_twice_min",
]

# Package-boundary regression cases: exercise codepaths that only exist
# in the signed-package consumer pipeline (trait scope through boundary,
# vtable population for external impls, visibility negatives).
BOUNDARY_CASES = [
	"pkg_iter_next_visibility",                    # K24: trait method visibility through package boundary
	"pkg_ext_module_trait_scope",                  # K25: external module trait scope for generic re-instantiation (pre-existing bug, not in CI gate)
	"pkg_vis_source_trait_scope_rejected",         # K25-guard: unscoped trait call still rejected
	"pkg_iface_impl_vtable",                       # K26: interface impl vtable populated for external impls
]


# ── Helpers ──────────────────────────────────────────────────────────


def _imports_stdlib(case_dir: Path) -> bool:
	"""True if any .drift file in case_dir imports std.* or lang.*."""
	for p in case_dir.rglob("*.drift"):
		try:
			text = p.read_text(errors="replace")
		except OSError:
			continue
		if re.search(r'import\s+(std|lang)\.', text):
			return True
	return False


def _module_name(case_dir: Path) -> str:
	"""Extract module name from main.drift."""
	main = case_dir / "main.drift"
	if not main.exists():
		return "main"
	try:
		text = main.read_text(errors="replace")
	except OSError:
		return "main"
	m = re.search(r'^module\s+(\S+)', text, re.MULTILINE)
	if m:
		return m.group(1).rstrip(";")
	return "main"


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


def _ubsan_options_with_defaults(existing: str | None) -> str:
	parts: list[str] = []
	if existing:
		parts = [p for p in existing.split(":") if p]
	keys = {p.split("=", 1)[0] for p in parts}
	if "print_stacktrace" not in keys:
		parts.append("print_stacktrace=1")
	if "halt_on_error" not in keys:
		parts.append("halt_on_error=1")
	if "abort_on_error" not in keys:
		parts.append("abort_on_error=0")
	if "symbolize" not in keys:
		parts.append("symbolize=1")
	return ":".join(parts)


def _physical_cpu_count_linux() -> int | None:
	cpuinfo = Path("/proc/cpuinfo")
	if not cpuinfo.exists():
		return None
	blocks = [b for b in cpuinfo.read_text(encoding="utf-8", errors="ignore").split("\n\n") if b.strip()]
	cores: set[tuple[str, str]] = set()
	for block in blocks:
		physical_id: str | None = None
		core_id: str | None = None
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
	if cores:
		return len(cores)
	return None


# ── Signed stdlib fixture ────────────────────────────────────────────


def _build_signed_stdlib(build_dir: Path) -> tuple[Path, Path, Path, Path]:
	"""Build signed std.dmp from real stdlib.

	Returns (pkg_root, trust_path, core_trust_path, empty_stdlib).
	"""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	# 1. Build unsigned std.dmp via subprocess
	stdlib_files = sorted(str(p) for p in STDLIB_DIR.rglob("*.drift"))
	assert stdlib_files, f"no stdlib .drift files found under {STDLIB_DIR}"

	pkg_dir = build_dir / "pkgs"
	pkg_dir.mkdir(parents=True, exist_ok=True)
	pkg_path = pkg_dir / "std.dmp"

	empty_stdlib = build_dir / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)

	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		"--dev",
		"-M", str(STDLIB_DIR),
		"--stdlib-root", str(empty_stdlib),
		*stdlib_files,
		"--package-id", "std",
		"--package-version", _STD_PACKAGE_VERSION,
		"--package-target", "test-target",
		"--emit-package", str(pkg_path),
		"--test-build-only",
	]
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
	if res.returncode != 0:
		print(f"[pkg-consumer] stdlib build failed (rc={res.returncode}):", file=sys.stderr)
		print(res.stderr[:2000], file=sys.stderr)
		raise RuntimeError(f"stdlib build failed (rc={res.returncode})")

	# 2. Generate ephemeral Ed25519 key
	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	if hasattr(pub, "public_bytes_raw"):
		pub_raw = pub.public_bytes_raw()
	else:
		pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	# 3. Sign package + write .sig sidecar
	pkg_bytes = pkg_path.read_bytes()
	sig_raw = priv.sign(pkg_bytes)
	pkg_sha_hex = sha256(pkg_bytes).hexdigest()

	sig_sidecar = pkg_path.with_suffix(".sig")
	sig_obj = {
		"format": "dmir-pkg-sig",
		"version": 0,
		"package_sha256": f"sha256:{pkg_sha_hex}",
		"signatures": [{"algo": "ed25519", "kid": kid, "sig": base64.b64encode(sig_raw).decode("ascii"), "pubkey": pub_b64}],
	}
	sig_sidecar.write_text(json.dumps(sig_obj, separators=(",", ":"), sort_keys=True))

	# 4. Write trust stores
	core_trust_path = build_dir / "core_trust.json"
	core_trust_obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid], "lang.*": [kid], "drift.*": [kid]},
		"revoked": [],
	}
	core_trust_path.write_text(json.dumps(core_trust_obj, separators=(",", ":"), sort_keys=True))

	trust_path = build_dir / "trust.json"
	trust_obj = {
		"format": "drift-trust",
		"version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid]},
		"revoked": [],
	}
	trust_path.write_text(json.dumps(trust_obj, separators=(",", ":"), sort_keys=True))

	return pkg_dir, trust_path, core_trust_path, empty_stdlib


# ── Clang link + run ─────────────────────────────────────────────────


def _link_and_run(
	ir_path: Path,
	build_dir: Path,
	argv: list[str] | None = None,
	stdin_data: str | None = None,
	timeout_s: int = 30,
) -> tuple[str | None, int, str, str]:
	"""Link IR → binary via clang, then run.

	Returns (link_error, exit_code, stdout, stderr).
	link_error is None on success, otherwise a string describing the failure.
	"""
	clang = shutil.which("clang")
	if clang is None:
		return "clang not available", 1, "", ""

	bin_path = build_dir / "a.out"
	asan_enabled = os.environ.get("DRIFT_ASAN") in ("1", "true", "True")
	ubsan_enabled = os.environ.get("DRIFT_UBSAN") in ("1", "true", "True")

	from lang.language_runtime import (
		build_runtime_archive,
		get_runtime_sources,
		runtime_archive_mode,
		runtime_archive_variant,
	)

	runtime_include = ROOT / "lang" / "language_runtime"
	search_dirs = [
		Path("/lib"), Path("/lib64"),
		Path("/usr/lib"), Path("/usr/lib64"),
		Path("/lib/x86_64-linux-gnu"), Path("/usr/lib/x86_64-linux-gnu"),
	]

	def _link_flags_for_lib(name: str) -> list[str]:
		if not find_library(name):
			return []
		for d in search_dirs:
			if (d / f"lib{name}.so").exists():
				return [f"-l{name}"]
		return []

	link_libs = _link_flags_for_lib("dw") + _link_flags_for_lib("unwind") + _link_flags_for_lib("unwind-x86_64") + _link_flags_for_lib("elf")

	c_flags: list[str] = []
	link_flags: list[str] = []
	if asan_enabled:
		c_flags.extend(["-fsanitize=address", "-g"])
		link_flags.extend(["-fsanitize=address"])
	if ubsan_enabled:
		c_flags.extend(["-fsanitize=undefined", "-fno-sanitize-recover=undefined", "-g"])
		link_flags.extend(["-fsanitize=undefined", "-fno-sanitize-recover=undefined"])

	rt_mode = runtime_archive_mode()
	if rt_mode == "archive":
		try:
			variant = runtime_archive_variant(debug_enabled=False, asan_enabled=asan_enabled, ubsan_enabled=ubsan_enabled, alloc_track_enabled=False, optimized=False)
			runtime_archive = str(build_runtime_archive(ROOT, clang=clang, variant=variant))
		except Exception as ex:
			return f"runtime archive build failed: {ex}", 1, "", ""
		compile_cmd = [
			clang, "-pthread", *c_flags,
			"-x", "ir", str(ir_path),
			"-x", "none", runtime_archive,
			*link_flags, *link_libs,
			"-Wl,--as-needed", "-o", str(bin_path),
		]
	else:
		runtime_sources = get_runtime_sources(ROOT)
		compile_cmd = [
			clang, "-pthread", *c_flags,
			"-I", str(runtime_include),
			"-x", "ir", str(ir_path),
			"-x", "c", *(str(p) for p in runtime_sources),
			*link_flags, *link_libs,
			"-Wl,--as-needed", "-o", str(bin_path),
		]

	try:
		compile_res = subprocess.run(compile_cmd, capture_output=True, text=True, cwd=ROOT, timeout=timeout_s)
	except subprocess.TimeoutExpired:
		return "clang compile timeout", 124, "", ""
	if compile_res.returncode != 0:
		return compile_res.stderr[:500], compile_res.returncode, "", ""

	# Run binary
	run_env = os.environ.copy()
	run_timeout = timeout_s
	if asan_enabled:
		run_timeout = max(timeout_s, 30) * 2
		run_env["ASAN_OPTIONS"] = _asan_options_with_defaults(run_env.get("ASAN_OPTIONS"))
	if ubsan_enabled:
		run_timeout = max(run_timeout, max(timeout_s, 30) * 2)
		run_env["UBSAN_OPTIONS"] = _ubsan_options_with_defaults(run_env.get("UBSAN_OPTIONS"))
	try:
		run_res = subprocess.run(
			[str(bin_path), *(argv or [])],
			input=stdin_data,
			capture_output=True,
			text=True,
			cwd=ROOT,
			env=run_env,
			timeout=run_timeout,
		)
	except subprocess.TimeoutExpired:
		return None, 124, "", "timeout during execution"
	return None, run_res.returncode, run_res.stdout, run_res.stderr


# ── Per-case worker ──────────────────────────────────────────────────


def _run_case(
	case_name: str,
	case_dir: Path,
	pkg_root: str,
	trust_path: str,
	core_trust_path: str,
	empty_stdlib: str,
	build_root: str,
	timeout_s: int = 60,
) -> tuple[str, bool, str, str]:
	"""Run a single case.  Returns (name, passed, phase, message)."""
	expected_path = case_dir / "expected.json"
	main_path = case_dir / "main.drift"

	if not expected_path.exists() or not main_path.exists():
		return case_name, True, "", "skipped (missing files)"

	expected = json.loads(expected_path.read_text())
	if expected.get("skip"):
		return case_name, True, "", "skipped (marked)"
	if expected.get("use_driftc_json") is False:
		return case_name, True, "", "skipped (use_driftc_json=false)"
	if expected.get("sandbox_blocks") and os.environ.get("DRIFT_SANDBOX"):
		return case_name, True, "", "skipped (sandbox)"

	drift_files = sorted(str(p) for p in case_dir.rglob("*.drift"))
	if not drift_files:
		return case_name, True, "", "skipped (no sources)"

	has_diags = bool(expected.get("diagnostics"))
	expected_exit = expected.get("exit_code", 0)
	module_paths = expected.get("module_paths") or []
	run_args = expected.get("args", [])
	stdin_data = expected.get("stdin")
	case_timeout = expected.get("timeout_s", timeout_s)
	compiler_flags = expected.get("compiler_flags") or []

	mod_name = _module_name(case_dir)
	entry = f"{mod_name}::main"

	case_build = Path(build_root) / case_name
	case_build.mkdir(parents=True, exist_ok=True)
	ir_path = case_build / "out.ll"

	# Build driftc compile command
	argv = [sys.executable, "-m", "lang.driftc.driftc"]
	for mp in module_paths:
		argv.extend(["-M", str(case_dir / mp)])
	if not module_paths:
		argv.extend(["-M", str(case_dir)])
	argv.extend([
		"--stdlib-root", empty_stdlib,
		"--package-root", pkg_root,
		"--dep", f"std@{_STD_PACKAGE_VERSION}",
		"--trust-store", trust_path,
		"--dev-core-trust-store", core_trust_path,
		"--dev",
		"--entry", entry,
		"--emit-ir", str(ir_path),
		"--json",
		*compiler_flags,
		*drift_files,
	])

	# ── Compile ──────────────────────────────────────────────────
	try:
		compile_res = subprocess.run(
			argv, capture_output=True, text=True, cwd=ROOT, timeout=case_timeout,
		)
	except subprocess.TimeoutExpired:
		return case_name, False, PHASE_COMPILE_CHECK, "compile timeout"

	compile_json: dict = {}
	if compile_res.stdout.strip():
		try:
			compile_json = json.loads(compile_res.stdout)
		except json.JSONDecodeError:
			pass

	compile_rc = compile_res.returncode

	# Compile-error tests: check that compilation fails as expected.
	if has_diags:
		if compile_rc != 0:
			return case_name, True, "", ""
		return case_name, False, PHASE_COMPILE_CHECK, "expected compile failure but got success"

	# Compile success required from here.
	if compile_rc != 0:
		diags = compile_json.get("diagnostics", [])
		msg = "; ".join(d.get("message", "") for d in diags) if diags else compile_res.stderr[:200]
		phase = PHASE_COMPILE_CHECK
		if "Traceback" in compile_res.stderr:
			phase = PHASE_COMPILE_CODEGEN
		for d in diags:
			p = d.get("phase", "")
			if p in ("codegen", "ssa", "mir"):
				phase = PHASE_COMPILE_CODEGEN
				break
		return case_name, False, phase, msg[:300]

	if not ir_path.exists():
		return case_name, False, PHASE_COMPILE_CODEGEN, "no IR file produced"

	# ── Link + Run ───────────────────────────────────────────────
	link_err, exit_code, stdout, stderr = _link_and_run(
		ir_path, case_build,
		argv=run_args,
		stdin_data=stdin_data,
		timeout_s=case_timeout,
	)
	if link_err is not None:
		return case_name, False, PHASE_LINK, link_err[:300]

	# Strip ASAN warnings
	asan_enabled = os.environ.get("DRIFT_ASAN") in ("1", "true", "True")
	ubsan_enabled = os.environ.get("DRIFT_UBSAN") in ("1", "true", "True")
	stderr_clean = stderr
	if asan_enabled:
		lines = [l for l in stderr.splitlines() if "WARNING: ASan doesn't fully support makecontext/swapcontext functions" not in l]
		stderr_clean = "\n".join(lines)
		if lines and stderr.endswith("\n"):
			stderr_clean += "\n"
	# UBSAN: never strip "runtime error:" or finding lines. Pass-through.

	# Compare results
	if exit_code != expected_exit:
		if ubsan_enabled and exit_code == 1 and "runtime error:" in stderr_clean:
			phase = PHASE_RUNTIME_UBSAN
		elif asan_enabled and exit_code == 1:
			phase = PHASE_RUNTIME_ASAN
		else:
			phase = PHASE_RUNTIME
		msg = f"exit {exit_code}, expected {expected_exit}"
		if stderr_clean:
			msg += f" | stderr: {stderr_clean[:150]}"
		return case_name, False, phase, msg[:300]

	expect_stdout = expected.get("stdout", "")
	if expect_stdout != "__ANY__" and stdout != expect_stdout:
		return case_name, False, PHASE_RUNTIME, f"stdout mismatch (got {len(stdout)}B, expected {len(expect_stdout)}B)"

	expect_stderr = expected.get("stderr", "")
	stderr_contains = expected.get("stderr_contains")
	if isinstance(stderr_contains, str) and stderr_contains not in stderr_clean:
		return case_name, False, PHASE_RUNTIME, f"stderr missing fragment: {stderr_contains!r}"
	if expect_stderr != "__ANY__" and stderr_clean != expect_stderr:
		# Check stderr_jsonl before failing
		stderr_jsonl = expected.get("stderr_jsonl")
		if isinstance(stderr_jsonl, list):
			lines = [l for l in stderr_clean.splitlines() if l.strip()]
			if len(lines) == len(stderr_jsonl):
				try:
					actual_objs = [json.loads(l) for l in lines]
					if all(_json_matches(e, a) for e, a in zip(stderr_jsonl, actual_objs)):
						return case_name, True, "", ""
				except json.JSONDecodeError:
					pass
		return case_name, False, PHASE_RUNTIME, f"stderr mismatch (got {len(stderr_clean)}B, expected {len(expect_stderr)}B)"

	return case_name, True, "", ""


def _json_matches(expected_obj: object, actual_obj: object) -> bool:
	if isinstance(expected_obj, str) and expected_obj == "__ANY__":
		return True
	if type(expected_obj) is not type(actual_obj):
		return False
	if isinstance(expected_obj, dict):
		if set(expected_obj.keys()) != set(actual_obj.keys()):
			return False
		return all(_json_matches(v, actual_obj[k]) for k, v in expected_obj.items())
	if isinstance(expected_obj, list):
		if len(expected_obj) != len(actual_obj):
			return False
		return all(_json_matches(e, a) for e, a in zip(expected_obj, actual_obj))
	return expected_obj == actual_obj


# ── Chunk worker (for ProcessPoolExecutor) ───────────────────────────


def _run_case_chunk(
	case_entries: list[tuple[str, str]],
	pkg_root: str,
	trust_path: str,
	core_trust_path: str,
	empty_stdlib: str,
	build_root: str,
	timeout_s: int,
) -> list[tuple[str, bool, str, str]]:
	results: list[tuple[str, bool, str, str]] = []
	for case_name, case_dir_str in case_entries:
		try:
			results.append(_run_case(
				case_name, Path(case_dir_str),
				pkg_root, trust_path, core_trust_path, empty_stdlib,
				build_root, timeout_s,
			))
		except Exception as err:
			results.append((case_name, False, PHASE_COMPILE_CHECK, f"worker exception: {err}"))
	return results


# ── Main ─────────────────────────────────────────────────────────────


def main(argv: Iterable[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description="Package-consumer e2e runner (signed stdlib path)")
	ap.add_argument("--blocking", action="store_true", help="Exit non-zero on any failure (CI gate mode)")
	ap.add_argument("--only-cases", type=str, default="", help="Comma-separated list of case names to run (overrides stdlib-import filter)")
	ap.add_argument("--all-cases", action="store_true", help="Run all e2e cases, not just stdlib-importing ones")
	ap.add_argument("-j", "--jobs", type=str, default="auto", help="Parallel workers (N or 'auto')")
	ap.add_argument("--work-size", type=int, default=4, help="Cases per worker chunk (default: 4)")
	ap.add_argument("--timeout", type=int, default=60, help="Per-case timeout in seconds (default: 60)")
	ap.add_argument("--summarize", action="store_true", help="Print summary with counts and timing")
	ap.add_argument("--debug", action="store_true", help="Verbose failure output")
	args = ap.parse_args(argv)

	start_time = time.monotonic() if args.summarize else None

	# ── Discover cases ───────────────────────────────────────────
	case_dirs = sorted(
		d for d in CASE_ROOT.iterdir()
		if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("__")
	) if CASE_ROOT.exists() else []

	if args.only_cases:
		names = set(args.only_cases.split(","))
		case_dirs = [d for d in case_dirs if d.name in names]
		missing = names - {d.name for d in case_dirs}
		if missing:
			print(f"[pkg-consumer] warning: cases not found: {', '.join(sorted(missing))}", file=sys.stderr)
	elif not args.all_cases:
		# Filter to stdlib-importing cases only
		case_dirs = [d for d in case_dirs if _imports_stdlib(d)]

	if not case_dirs:
		print("[pkg-consumer] no matching cases found", file=sys.stderr)
		return 0

	print(f"[pkg-consumer] {len(case_dirs)} cases selected", file=sys.stderr)

	# ── Build signed stdlib fixture ──────────────────────────────
	build_root = BUILD_ROOT_DEFAULT / f"run_{os.getpid()}"
	build_root.mkdir(parents=True, exist_ok=True)

	print("[pkg-consumer] building signed stdlib package...", file=sys.stderr)
	fixture_start = time.monotonic()
	try:
		pkg_root, trust_path, core_trust_path, empty_stdlib = _build_signed_stdlib(build_root)
	except Exception as err:
		print(f"[pkg-consumer] FATAL: {err}", file=sys.stderr)
		return 2
	fixture_elapsed = time.monotonic() - fixture_start
	print(f"[pkg-consumer] stdlib built in {fixture_elapsed:.1f}s", file=sys.stderr)

	# Serialize fixture paths for workers
	pkg_root_s = str(pkg_root)
	trust_path_s = str(trust_path)
	core_trust_path_s = str(core_trust_path)
	empty_stdlib_s = str(empty_stdlib)
	build_root_s = str(build_root)

	# ── Run cases ────────────────────────────────────────────────
	if args.jobs == "auto":
		physical = _physical_cpu_count_linux()
		if physical is not None and physical > 0:
			jobs = max(1, physical)
		else:
			cpu_count = os.cpu_count() or 1
			jobs = max(1, cpu_count // 2)
	else:
		try:
			jobs = int(args.jobs)
		except ValueError:
			print(f"[pkg-consumer] invalid --jobs: {args.jobs!r}", file=sys.stderr)
			return 2
		if jobs < 1:
			jobs = 1

	results: list[tuple[str, bool, str, str]] = []

	case_entries = [(d.name, str(d)) for d in case_dirs]

	if jobs == 1 or len(case_dirs) <= 1:
		for case_name, case_dir_str in case_entries:
			r = _run_case(
				case_name, Path(case_dir_str),
				pkg_root_s, trust_path_s, core_trust_path_s, empty_stdlib_s,
				build_root_s, args.timeout,
			)
			results.append(r)
			name, passed, phase, msg = r
			if passed:
				if msg.startswith("skipped"):
					print(f"{name}: skipped")
				else:
					print(f"{name}: PASS")
			else:
				print(f"{name}: FAIL({phase}) {msg}")
	else:
		chunk_size = max(1, args.work_size)
		chunks: list[list[tuple[str, str]]] = []
		for i in range(0, len(case_entries), chunk_size):
			chunks.append(case_entries[i:i + chunk_size])
		with ProcessPoolExecutor(max_workers=jobs) as executor:
			futures = [
				executor.submit(
					_run_case_chunk, chunk,
					pkg_root_s, trust_path_s, core_trust_path_s, empty_stdlib_s,
					build_root_s, args.timeout,
				)
				for chunk in chunks
			]
			for fut in as_completed(futures):
				for r in fut.result():
					results.append(r)
					name, passed, phase, msg = r
					if passed:
						if msg.startswith("skipped"):
							print(f"{name}: skipped")
						else:
							print(f"{name}: PASS")
					else:
						print(f"{name}: FAIL({phase}) {msg}")

	# ── Summary ──────────────────────────────────────────────────
	failures = [(n, ph, m) for n, ok, ph, m in results if not ok]
	skipped = [n for n, ok, ph, m in results if ok and m.startswith("skipped")]
	passed = [n for n, ok, ph, m in results if ok and not m.startswith("skipped")]

	if failures:
		# Group by phase
		by_phase: dict[str, list[tuple[str, str]]] = {}
		for name, phase, msg in failures:
			by_phase.setdefault(phase, []).append((name, msg))
		print("\n============[ FAILURES ]============", file=sys.stderr)
		for phase in sorted(by_phase.keys()):
			print(f"\n  [{phase}] ({len(by_phase[phase])} failures):", file=sys.stderr)
			for name, msg in sorted(by_phase[phase]):
				print(f"    {name}: {msg}", file=sys.stderr)

	if args.summarize or start_time is not None:
		elapsed = time.monotonic() - (start_time or time.monotonic())
		print("\n============[ SUMMARY ]=============", file=sys.stderr)
		print(f"Tests: {len(results)} ({len(passed)} passed, {len(skipped)} skipped, {len(failures)} failed)", file=sys.stderr)
		print(f"Elapsed: {elapsed:.2f}s (fixture: {fixture_elapsed:.1f}s)", file=sys.stderr)

	if args.blocking and failures:
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
