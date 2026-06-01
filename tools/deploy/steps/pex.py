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

# Route this step's scratch under $DRIFT_TMP_ROOT so deploy runs are
# janitor-safe (deploy is not a pytest path; conftest.py's relocation
# does not apply).  See doc/conventions/tmp-root.md.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(_REPO_ROOT))
from lang.test_support.drift_tmp import session_root as _drift_session_root


# Every `tools.<pkg>` package that the deployed `drift` PEX dispatches
# to at runtime, either via `deploy_pex_entry.py`'s pre-argparse
# dispatch (drift_deploy) or via `lang/drift/cli.py`'s subcommand
# dispatchers (drift_author, drift_doc).  Adding a new
# `from tools.<X> import ...` line in `lang/drift/cli.py` requires
# extending this list -- the contract test
# `tools/deploy/test_pex_bundling.py` enforces it so the deployed PEX
# does not silently raise ModuleNotFoundError on first invocation.
BUNDLED_TOOLS_PACKAGES: tuple[str, ...] = (
	"drift_deploy",
	"drift_author",
	"drift_doc",
)


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
	entry_dir = Path(tempfile.mkdtemp(dir=str(_drift_session_root()), prefix="pex_entry_"))
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

	# Stage entry point + tools.* packages (see BUNDLED_TOOLS_PACKAGES
	# above) in a temp directory.
	entry_dir = Path(tempfile.mkdtemp(dir=str(_drift_session_root()), prefix="pex_entry_"))
	try:
		# Entry point.
		shutil.copy2(
			str(repo_root / "tools" / "deploy" / "deploy_pex_entry.py"),
			str(entry_dir / "deploy_pex_entry.py"),
		)

		# tools namespace package + __init__.
		tools_dir = entry_dir / "tools"
		tools_dir.mkdir()
		tools_init = repo_root / "tools" / "__init__.py"
		if tools_init.exists():
			shutil.copy2(str(tools_init), str(tools_dir / "__init__.py"))
		else:
			(tools_dir / "__init__.py").touch()

		# Stage every tools.* package the deployed `drift` CLI dispatches
		# to at runtime.  This list is the bundle contract; keep it in
		# sync with the `from tools.<pkg> import ...` lines in
		# `lang/drift/cli.py`.  `tools/deploy/test_pex_bundling.py`
		# enforces that contract -- if you add a new dispatcher in
		# cli.py and forget to extend `BUNDLED_TOOLS_PACKAGES`, the
		# deployed PEX will raise ModuleNotFoundError on first
		# invocation (as happened in 0.32.16 when `drift author`
		# shipped without `tools.drift_author` bundled).
		for pkg_name in BUNDLED_TOOLS_PACKAGES:
			pkg_dir = tools_dir / pkg_name
			pkg_dir.mkdir()
			for f in sorted((repo_root / "tools" / pkg_name).iterdir()):
				if f.suffix == ".py" and not f.name.startswith("test_"):
					shutil.copy2(str(f), str(pkg_dir / f.name))

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
