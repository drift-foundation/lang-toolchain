# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy step: build PEX --scie eager executables for driftc and drift CLI.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def read_pinned_version(repo_root: Path, package: str, *, allow_gte: bool = False) -> str:
	"""Read a pinned version from requirements.txt.

	Returns 'package==X.Y.Z' if pinned, or bare 'package' if not found.
	If allow_gte is True, also accepts '>=' constraints.
	"""
	req_file = repo_root / "requirements.txt"
	if not req_file.exists():
		return package
	text = req_file.read_text(encoding="utf-8")
	# Exact pin: package==X.Y.Z
	for line in text.splitlines():
		if re.match(rf"^{re.escape(package)}==", line, re.IGNORECASE):
			return line.strip()
	# Optional >= fallback.
	if allow_gte:
		for line in text.splitlines():
			if re.match(rf"^{re.escape(package)}>=", line, re.IGNORECASE):
				return line.strip()
	return package


def detect_python_version(venv: Path) -> str:
	"""Detect Python major.minor from the venv."""
	python = venv / "bin" / "python3"
	result = subprocess.run(
		[str(python), "-c",
		 "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
		capture_output=True, text=True, check=True,
	)
	return result.stdout.strip()


def build_driftc_pex(repo_root: Path, dist: Path) -> Path:
	"""Build PEX --scie eager executable for driftc.

	Returns path to the built executable.
	"""
	venv = repo_root / ".venv"
	pex_cmd = venv / "bin" / "pex"
	_require_pex(pex_cmd)

	lark_req = read_pinned_version(repo_root, "lark")
	llvmlite_req = read_pinned_version(repo_root, "llvmlite")
	crypto_req = read_pinned_version(repo_root, "cryptography")
	zstd_req = read_pinned_version(repo_root, "zstandard")
	python_version = detect_python_version(venv)

	# Stage entry point in a temp directory.
	entry_dir = Path(tempfile.mkdtemp())
	try:
		shutil.copy2(
			str(repo_root / "tools" / "deploy" / "pex_entry.py"),
			str(entry_dir / "pex_entry.py"),
		)

		out = dist / "bin" / "driftc"
		out.parent.mkdir(parents=True, exist_ok=True)

		deps = f"{lark_req}, {llvmlite_req}, {crypto_req}, {zstd_req}"
		print(f"[deploy] building PEX --scie eager executable (deps: {deps})...", flush=True)

		subprocess.run([
			str(pex_cmd),
			lark_req, llvmlite_req, crypto_req, zstd_req,
			"-D", str(entry_dir),
			"-e", "pex_entry:main",
			"--scie", "eager",
			"--scie-python-version", python_version,
			"--python", str(venv / "bin" / "python3"),
			"-o", str(out),
		], check=True)

		out.chmod(0o755)
		print(f"[deploy] PEX executable built: {out}", flush=True)
		return out
	finally:
		shutil.rmtree(str(entry_dir), ignore_errors=True)


def build_drift_pex(repo_root: Path, dist: Path) -> Path:
	"""Build PEX --scie eager executable for the drift CLI.

	Returns path to the built executable.
	"""
	venv = repo_root / ".venv"
	pex_cmd = venv / "bin" / "pex"
	_require_pex(pex_cmd)

	crypto_req = read_pinned_version(repo_root, "cryptography")
	zstd_req = read_pinned_version(repo_root, "zstandard", allow_gte=True)
	python_version = detect_python_version(venv)

	# Stage entry point + tools.drift_deploy package in temp directory.
	entry_dir = Path(tempfile.mkdtemp())
	try:
		# Entry point.
		shutil.copy2(
			str(repo_root / "tools" / "deploy" / "deploy_pex_entry.py"),
			str(entry_dir / "deploy_pex_entry.py"),
		)

		# tools.drift_deploy package (exclude tests).
		tools_dir = entry_dir / "tools"
		tools_dir.mkdir()
		tools_init = repo_root / "tools" / "__init__.py"
		if tools_init.exists():
			shutil.copy2(str(tools_init), str(tools_dir / "__init__.py"))
		else:
			(tools_dir / "__init__.py").touch()

		dd_dir = tools_dir / "drift_deploy"
		dd_dir.mkdir()
		for f in sorted((repo_root / "tools" / "drift_deploy").iterdir()):
			if f.suffix == ".py" and not f.name.startswith("test_"):
				shutil.copy2(str(f), str(dd_dir / f.name))

		out = dist / "bin" / "drift"
		out.parent.mkdir(parents=True, exist_ok=True)

		print(f"[deploy] building drift CLI PEX --scie eager executable (deps: {crypto_req}, {zstd_req})...", flush=True)

		subprocess.run([
			str(pex_cmd),
			crypto_req, zstd_req,
			"-D", str(entry_dir),
			"-e", "deploy_pex_entry:main",
			"--scie", "eager",
			"--scie-python-version", python_version,
			"--python", str(venv / "bin" / "python3"),
			"-o", str(out),
		], check=True)

		out.chmod(0o755)
		print(f"[deploy] drift CLI PEX executable built: {out}", flush=True)
		return out
	finally:
		shutil.rmtree(str(entry_dir), ignore_errors=True)


def _require_pex(pex_cmd: Path) -> None:
	if not pex_cmd.is_file():
		print(f"error: pex not found at {pex_cmd}", file=sys.stderr)
		print("  install into the project venv:", file=sys.stderr)
		print("    ./.venv/bin/pip install pex", file=sys.stderr)
		raise SystemExit(1)
