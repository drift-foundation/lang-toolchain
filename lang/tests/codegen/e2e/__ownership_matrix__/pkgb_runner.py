#!/usr/bin/env python3
"""
Ownership-matrix package-boundary runner.

Complements the single-compilation-unit matrix generator by exercising
the ownership shapes defined there across a SIGNED PACKAGE BOUNDARY —
producer source compiled and signed into a `.dmp`, consumer compiled
against the producer via `--package-root` / `--dep`.

Targeted risks (per work/ownership-matrix-followups.md):
  - copy_status / is_bitcopy for imported types differs from source mode
  - struct / variant field metadata reconstructed incompletely
  - generic variant tombstone metadata differs after package linking
  - Result<T, E> identity / visibility across boundary
  - exported nominal identity influences ownership decisions

Fixture layout:

    lang/tests/codegen/e2e/pkgb_<name>/
      producer/
        acme/
          <module>/
            <module>.drift      # module acme.<module>; pub struct/variant ...
      main.drift                # module main; import acme.<module> as m; ...
      expected.json             # {"exit_code": 0, "description": "..."}

Environment:
  DRIFT_ASAN=1       — compile/link with AddressSanitizer; run under ASAN.
  DRIFT_MEMCHECK=1   — run the linked binary under valgrind memcheck.
  DRIFT_DEBUG=1      — select debug-style runtime lane (no -O2).

Usage:
  PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/pkgb_runner.py [CASE ...]
  PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/pkgb_runner.py --summarize

Exit code: 0 iff all cases pass (or all selected cases pass).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from ctypes.util import find_library
from hashlib import sha256
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
CASE_ROOT = ROOT / "lang" / "tests" / "codegen" / "e2e"
STDLIB_DIR = ROOT / "stdlib"
BUILD_ROOT = ROOT / "build" / "tests" / "ownership_matrix_pkgb"

STD_VERSION = "0.0.0-test"
PRODUCER_VERSION = "0.0.0-test"
PRODUCER_PKG_ID = "acme.matrix"


def _b64(data: bytes) -> str:
	return base64.b64encode(data).decode("ascii")


def _sha(data: bytes) -> str:
	return sha256(data).hexdigest()


def _public_key_bytes(pub):
	if hasattr(pub, "public_bytes_raw"):
		return pub.public_bytes_raw()
	from cryptography.hazmat.primitives import serialization
	return pub.public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)


def _write_sig(pkg_path: Path, *, pkg_bytes: bytes, kid: str, sig_raw: bytes, pub_b64: str) -> None:
	sig = {
		"format": "dmir-pkg-sig",
		"version": 0,
		"package_sha256": f"sha256:{_sha(pkg_bytes)}",
		"signatures": [{"algo": "ed25519", "kid": kid, "sig": _b64(sig_raw), "pubkey": pub_b64}],
	}
	pkg_path.with_suffix(".sig").write_text(json.dumps(sig, separators=(",", ":"), sort_keys=True))


def _build_signed_stdlib(build_dir: Path, *, priv, kid: str, pub_b64: str) -> Path:
	"""Build signed std.dmp under build_dir/pkgs/std/VERSION/std.dmp."""
	stdlib_files = sorted(str(p) for p in STDLIB_DIR.rglob("*.drift"))
	assert stdlib_files, f"no stdlib .drift files under {STDLIB_DIR}"

	std_dest_dir = build_dir / "pkgs" / "std" / STD_VERSION
	std_dest_dir.mkdir(parents=True, exist_ok=True)
	std_pkg = std_dest_dir / "std.dmp"

	empty_stdlib = build_dir / "_empty_stdlib"
	empty_stdlib.mkdir(parents=True, exist_ok=True)

	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		"--dev",
		"-M", str(STDLIB_DIR),
		"--stdlib-root", str(empty_stdlib),
		*stdlib_files,
		"--package-id", "std",
		"--package-version", STD_VERSION,
		"--package-target", "test-target",
		"--emit-package", str(std_pkg),
		"--test-build-only",
	]
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
	if res.returncode != 0:
		print(f"[pkgb] stdlib build failed (rc={res.returncode}):", file=sys.stderr)
		print(res.stderr[:2000], file=sys.stderr)
		raise RuntimeError("stdlib build failed")

	sig_raw = priv.sign(std_pkg.read_bytes())
	_write_sig(std_pkg, pkg_bytes=std_pkg.read_bytes(), kid=kid, sig_raw=sig_raw, pub_b64=pub_b64)
	return empty_stdlib


def _producer_imports_stdlib(producer_src: Path) -> bool:
	"""True if any producer .drift imports `std.*` or `lang.*`.

	Producers that only use built-in types (`String`, `Array<T>`,
	generic vars, user types) don't need the signed stdlib on their
	compile classpath — skipping it avoids a cascading generic-
	instantiation requirement for every stdlib Destructible impl.
	Producers that DO import `std.*` pass through the stdlib-as-dep
	path, matching consumer compile behavior.
	"""
	import re as _re
	pat = _re.compile(r'^\s*import\s+(std|lang)\.', _re.MULTILINE)
	for p in producer_src.rglob("*.drift"):
		try:
			if pat.search(p.read_text(encoding="utf-8", errors="replace")):
				return True
		except OSError:
			continue
	return False


def _build_signed_producer(
	case_build: Path,
	producer_src: Path,
	shared_pkg_root: Path,
	*,
	priv,
	kid: str,
	pub_b64: str,
	empty_stdlib: Path,
) -> Path | None:
	"""Build a per-fixture producer package into shared_pkg_root.

	Returns the producer .dmp path, or None if no producer/ dir exists.
	"""
	drift_files = sorted(str(p) for p in producer_src.rglob("*.drift"))
	if not drift_files:
		return None

	prod_dest_dir = shared_pkg_root / PRODUCER_PKG_ID / PRODUCER_VERSION
	prod_dest_dir.mkdir(parents=True, exist_ok=True)
	prod_pkg = prod_dest_dir / f"{PRODUCER_PKG_ID}.dmp"

	uses_stdlib = _producer_imports_stdlib(producer_src)

	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		"--dev",
		"-M", str(producer_src),
		"--stdlib-root", str(empty_stdlib),
		*drift_files,
		"--package-id", PRODUCER_PKG_ID,
		"--package-version", PRODUCER_VERSION,
		"--package-target", "test-target",
		"--emit-package", str(prod_pkg),
		"--test-build-only",
	]
	if uses_stdlib:
		# Producer uses std.* or lang.* — compile against the signed
		# stdlib package, with trust stores so --package-root resolution
		# succeeds.  Write temp trusts alongside the case build dir.
		core_trust = case_build / "core_trust.json"
		core_trust.write_text(json.dumps({
			"format": "drift-trust", "version": 0,
			"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
			"namespaces": {"std.*": [kid], "lang.*": [kid], "drift.*": [kid]},
			"revoked": [],
		}, separators=(",", ":"), sort_keys=True))
		trust = case_build / "trust.json"
		trust.write_text(json.dumps({
			"format": "drift-trust", "version": 0,
			"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
			"namespaces": {"std.*": [kid], "acme.*": [kid]},
			"revoked": [],
		}, separators=(",", ":"), sort_keys=True))
		cmd.extend([
			"--package-root", str(shared_pkg_root),
			"--dep", f"std@{STD_VERSION}",
			"--dev-core-trust-store", str(core_trust),
			"--trust-store", str(trust),
		])

	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
	if res.returncode != 0:
		raise RuntimeError(
			f"producer build failed (rc={res.returncode}):\n{res.stderr[:1500]}\n{res.stdout[:500]}"
		)
	sig_raw = priv.sign(prod_pkg.read_bytes())
	_write_sig(prod_pkg, pkg_bytes=prod_pkg.read_bytes(), kid=kid, sig_raw=sig_raw, pub_b64=pub_b64)
	return prod_pkg


def _compile_consumer(
	case_build: Path,
	consumer_src: Path,
	*,
	pkg_root: Path,
	trust_path: Path,
	core_trust_path: Path,
	empty_stdlib: Path,
	deps: list[str],
) -> tuple[int, str, Path]:
	"""Compile consumer main.drift to IR.  Returns (rc, stderr_tail, ir_path)."""
	ir_path = case_build / "consumer.ll"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		"--dev",
		"-M", str(consumer_src.parent),
		"--stdlib-root", str(empty_stdlib),
		"--package-root", str(pkg_root),
		"--dev-core-trust-store", str(core_trust_path),
		"--trust-store", str(trust_path),
		"--entry", "main::main",
		"--emit-ir", str(ir_path),
	]
	for dep in deps:
		cmd.extend(["--dep", dep])
	cmd.append(str(consumer_src))
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
	return res.returncode, (res.stderr or "")[-1500:], ir_path


def _link_and_run(ir_path: Path, case_build: Path) -> tuple[int, str]:
	"""Link IR to a.out via clang, then run.  Returns (exit_code, stderr_tail).

	Respects DRIFT_ASAN / DRIFT_UBSAN / DRIFT_DEBUG env.  If DRIFT_MEMCHECK=1,
	runs under valgrind memcheck with --error-exitcode=97.
	"""
	from lang.language_runtime import build_runtime_archive, runtime_archive_variant

	clang = shutil.which("clang")
	if clang is None:
		return 1, "clang not available"
	bin_path = case_build / "a.out"

	asan = os.environ.get("DRIFT_ASAN") in ("1", "true", "True")
	ubsan = os.environ.get("DRIFT_UBSAN") in ("1", "true", "True")
	memcheck = os.environ.get("DRIFT_MEMCHECK") in ("1", "true", "True")
	debug_style = os.environ.get("DRIFT_DEBUG") in ("1", "true", "True")

	c_flags: list[str] = []
	link_flags: list[str] = []
	if asan:
		c_flags.extend(["-fsanitize=address", "-g"])
		link_flags.extend(["-fsanitize=address"])
	if ubsan:
		c_flags.extend(["-fsanitize=undefined", "-fno-sanitize-recover=undefined", "-g"])
		link_flags.extend(["-fsanitize=undefined", "-fno-sanitize-recover=undefined"])
	if not debug_style:
		c_flags.append("-O2")

	try:
		variant = runtime_archive_variant(
			debug_style=debug_style, asan_enabled=asan,
			ubsan_enabled=ubsan, alloc_track_enabled=False,
		)
		runtime_archive = str(build_runtime_archive(ROOT, clang=clang, variant=variant))
	except Exception as ex:
		return 1, f"runtime archive build failed: {ex}"

	search_dirs = [Path("/lib"), Path("/lib64"), Path("/usr/lib"), Path("/usr/lib64"),
		Path("/lib/x86_64-linux-gnu"), Path("/usr/lib/x86_64-linux-gnu")]
	link_libs: list[str] = []
	for name in ("dw", "unwind", "unwind-x86_64", "elf"):
		if find_library(name):
			for d in search_dirs:
				if (d / f"lib{name}.so").exists():
					link_libs.append(f"-l{name}")
					break

	compile_cmd = [
		clang, "-pthread", *c_flags,
		"-x", "ir", str(ir_path),
		"-x", "none", runtime_archive,
		*link_flags, *link_libs,
		"-Wl,--as-needed", "-o", str(bin_path),
	]
	res = subprocess.run(compile_cmd, capture_output=True, text=True, cwd=ROOT, timeout=60)
	if res.returncode != 0:
		return res.returncode, (res.stderr or "")[-1500:]

	run_env = os.environ.copy()
	if asan:
		run_env["ASAN_OPTIONS"] = "detect_leaks=0:halt_on_error=1"
	if memcheck:
		valgrind = shutil.which("valgrind")
		if valgrind is None:
			return 1, "valgrind not available"
		run_cmd = [valgrind, "--tool=memcheck", "--leak-check=full",
			"--error-exitcode=97", str(bin_path)]
		timeout = 120
	else:
		run_cmd = [str(bin_path)]
		timeout = 60
	res = subprocess.run(run_cmd, capture_output=True, text=True, cwd=ROOT, timeout=timeout, env=run_env)
	return res.returncode, (res.stderr or "")[-1500:]


def _run_case(
	case_name: str,
	case_dir_str: str,
	shared_pkg_root_str: str,
	empty_stdlib_str: str,
	trust_path_str: str,
	core_trust_path_str: str,
	build_root_str: str,
	priv_bytes_raw: bytes,
	kid: str,
	pub_b64: str,
) -> tuple[str, bool, str]:
	"""Run one pkgb fixture.  Returns (case_name, passed, message).

	All path-like / key-like arguments are plain serializable types so
	this worker can run under `ProcessPoolExecutor`.  The private key
	is reconstructed from raw bytes per worker; signing is a per-
	fixture one-shot, so no shared signer state is needed.
	"""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	case_dir = Path(case_dir_str)
	shared_pkg_root = Path(shared_pkg_root_str)
	empty_stdlib = Path(empty_stdlib_str)
	trust_path = Path(trust_path_str)
	core_trust_path = Path(core_trust_path_str)
	build_root = Path(build_root_str)
	priv = Ed25519PrivateKey.from_private_bytes(priv_bytes_raw)

	expected = json.loads((case_dir / "expected.json").read_text())
	expected_exit = expected.get("exit_code", 0)

	case_build = build_root / case_name
	case_build.mkdir(parents=True, exist_ok=True)

	# Per-fixture pkg_root overlay: symlink the shared std package and
	# place this fixture's producer alongside.  Prevents one fixture's
	# producer from leaking into another fixture's --package-root view.
	case_pkg_root = case_build / "pkgs"
	case_pkg_root.mkdir(parents=True, exist_ok=True)
	std_shared = shared_pkg_root / "std"
	std_link = case_pkg_root / "std"
	if std_link.exists() or std_link.is_symlink():
		std_link.unlink()
	std_link.symlink_to(std_shared, target_is_directory=True)

	try:
		producer_pkg = _build_signed_producer(
			case_build, case_dir / "producer", case_pkg_root,
			priv=priv, kid=kid, pub_b64=pub_b64,
			empty_stdlib=empty_stdlib,
		)
	except RuntimeError as ex:
		return case_name, False, f"producer: {ex}"

	deps = [f"std@{STD_VERSION}"]
	if producer_pkg is not None:
		deps.append(f"{PRODUCER_PKG_ID}@{PRODUCER_VERSION}")

	rc, stderr, ir_path = _compile_consumer(
		case_build, case_dir / "main.drift",
		pkg_root=case_pkg_root,
		trust_path=trust_path,
		core_trust_path=core_trust_path,
		empty_stdlib=empty_stdlib,
		deps=deps,
	)
	if rc != 0:
		return case_name, False, f"consumer compile rc={rc}: {stderr[-500:]}"
	if not ir_path.exists():
		return case_name, False, "no IR produced"

	run_rc, run_stderr = _link_and_run(ir_path, case_build)
	if run_rc != expected_exit:
		return case_name, False, f"exit {run_rc}, expected {expected_exit}; stderr: {run_stderr[-300:]}"
	return case_name, True, ""


def _physical_cpu_count_linux() -> int | None:
	cpuinfo = Path("/proc/cpuinfo")
	if not cpuinfo.exists():
		return None
	cores: set[tuple[str, str]] = set()
	for block in cpuinfo.read_text(encoding="utf-8", errors="ignore").split("\n\n"):
		phys = core = None
		for line in block.splitlines():
			if ":" not in line:
				continue
			k, v = line.split(":", 1)
			kk = k.strip().lower()
			vv = v.strip()
			if kk == "physical id":
				phys = vv
			elif kk == "core id":
				core = vv
		if phys is not None and core is not None:
			cores.add((phys, core))
	return len(cores) if cores else None


def _resolve_jobs(jobs_arg: str) -> int:
	"""Resolve `--jobs` arg.  `auto` picks from DRIFT_TEST_JOBS, then
	physical-core count, then os.cpu_count()//2."""
	if jobs_arg != "auto":
		try:
			n = int(jobs_arg)
		except ValueError:
			raise SystemExit(f"[pkgb] invalid --jobs: {jobs_arg!r}")
		return max(1, n)
	env_jobs = os.environ.get("DRIFT_TEST_JOBS", "").strip()
	if env_jobs:
		try:
			n = int(env_jobs)
			if n > 0:
				return n
		except ValueError:
			pass
	phys = _physical_cpu_count_linux()
	if phys is not None and phys > 0:
		return max(1, phys)
	return max(1, (os.cpu_count() or 1) // 2)


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
	ap.add_argument("cases", nargs="*", help="Case names (default: all pkgb_*)")
	ap.add_argument("--summarize", action="store_true")
	ap.add_argument("-j", "--jobs", type=str, default="auto",
		help="Parallel workers (N or 'auto'; auto honors DRIFT_TEST_JOBS)")
	args = ap.parse_args(argv)

	selected = [CASE_ROOT / name for name in args.cases] if args.cases else sorted(
		d for d in CASE_ROOT.iterdir()
		if d.is_dir() and d.name.startswith("pkgb_")
	)
	selected = [d for d in selected if d.exists() and (d / "main.drift").exists()]
	if not selected:
		print("[pkgb] no cases found", file=sys.stderr)
		return 0

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	start = time.monotonic()
	run_id = os.getpid()
	build_root = BUILD_ROOT / f"run_{run_id}"
	build_root.mkdir(parents=True, exist_ok=True)
	shared_pkg_root = build_root / "shared_pkgs"
	shared_pkg_root.mkdir(parents=True, exist_ok=True)

	priv = Ed25519PrivateKey.generate()
	priv_bytes_raw = priv.private_bytes_raw() if hasattr(priv, "private_bytes_raw") else None
	if priv_bytes_raw is None:
		# Older cryptography versions — fall back to private_bytes raw format.
		from cryptography.hazmat.primitives import serialization
		priv_bytes_raw = priv.private_bytes(
			encoding=serialization.Encoding.Raw,
			format=serialization.PrivateFormat.Raw,
			encryption_algorithm=serialization.NoEncryption(),
		)
	pub_raw = _public_key_bytes(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = _b64(pub_raw)

	fixture_t0 = time.monotonic()
	print("[pkgb] building signed stdlib package...", file=sys.stderr)
	try:
		empty_stdlib = _build_signed_stdlib(shared_pkg_root.parent, priv=priv, kid=kid, pub_b64=pub_b64)
	except Exception as ex:
		print(f"[pkgb] FATAL: {ex}", file=sys.stderr)
		return 2
	# The stdlib was built under shared_pkg_root.parent / "pkgs"; move its
	# contents into shared_pkg_root so per-fixture pkg roots can symlink
	# directly to `shared_pkg_root / "std"`.
	built_pkgs = shared_pkg_root.parent / "pkgs"
	if built_pkgs.exists() and built_pkgs != shared_pkg_root:
		for child in built_pkgs.iterdir():
			dest = shared_pkg_root / child.name
			if not dest.exists():
				shutil.move(str(child), str(dest))
	fixture_elapsed = time.monotonic() - fixture_t0
	print(f"[pkgb] stdlib built in {fixture_elapsed:.1f}s", file=sys.stderr)

	core_trust_path = build_root / "core_trust.json"
	core_trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid], "lang.*": [kid], "drift.*": [kid]},
		"revoked": [],
	}, separators=(",", ":"), sort_keys=True))
	trust_path = build_root / "trust.json"
	trust_path.write_text(json.dumps({
		"format": "drift-trust", "version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
		"namespaces": {"std.*": [kid], "acme.*": [kid]},
		"revoked": [],
	}, separators=(",", ":"), sort_keys=True))

	# Serializable worker args (paths as strings, key as raw bytes).
	shared_pkg_root_s = str(shared_pkg_root)
	empty_stdlib_s = str(empty_stdlib)
	trust_path_s = str(trust_path)
	core_trust_path_s = str(core_trust_path)
	build_root_s = str(build_root)

	jobs = _resolve_jobs(args.jobs)
	# Cap at len(selected): a pool larger than the workload just pays
	# process-creation overhead with no speedup.
	jobs = max(1, min(jobs, len(selected)))
	print(f"[pkgb] running {len(selected)} cases with {jobs} worker(s)...", file=sys.stderr)

	results: list[tuple[str, bool, str]] = []

	if jobs == 1 or len(selected) == 1:
		for case_dir in selected:
			name = case_dir.name
			try:
				_n, ok, msg = _run_case(
					name, str(case_dir), shared_pkg_root_s, empty_stdlib_s,
					trust_path_s, core_trust_path_s, build_root_s,
					priv_bytes_raw, kid, pub_b64,
				)
			except Exception as ex:
				ok, msg = False, f"runner exception: {ex}"
			results.append((name, ok, msg))
			status = "ok" if ok else "FAIL"
			suffix = "" if ok else f" — {msg}"
			print(f"{name}: {status}{suffix}")
	else:
		# ProcessPoolExecutor: each worker reconstructs the signing key
		# from raw bytes and runs one fixture to completion (producer
		# build + consumer compile + link + run).  Per-fixture build
		# dirs are siblings under `build_root / <case_name>`, so
		# concurrent workers do not race on filesystem state.
		with ProcessPoolExecutor(max_workers=jobs) as executor:
			futs = [
				executor.submit(
					_run_case, case_dir.name, str(case_dir),
					shared_pkg_root_s, empty_stdlib_s,
					trust_path_s, core_trust_path_s, build_root_s,
					priv_bytes_raw, kid, pub_b64,
				)
				for case_dir in selected
			]
			for fut in as_completed(futs):
				try:
					name, ok, msg = fut.result()
				except Exception as ex:
					name, ok, msg = "<unknown>", False, f"worker exception: {ex}"
				results.append((name, ok, msg))
				status = "ok" if ok else "FAIL"
				suffix = "" if ok else f" — {msg}"
				print(f"{name}: {status}{suffix}")

	failed = [r for r in results if not r[1]]
	if args.summarize:
		elapsed = time.monotonic() - start
		print(f"\n[pkgb] {len(results)} cases ({len(results) - len(failed)} ok, {len(failed)} failed) in {elapsed:.1f}s (stdlib: {fixture_elapsed:.1f}s)", file=sys.stderr)

	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(main())
