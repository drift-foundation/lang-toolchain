# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import base64
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _write_no_site_python(tmp_path: Path) -> Path:
	shim = tmp_path / "python-no-site.sh"
	_write_file(
		shim,
		"""#!/usr/bin/env bash
set -euo pipefail
exec python3 -S "$@"
""",
	)
	shim.chmod(0o755)
	return shim


def test_deployed_wrapper_uses_runtime_archives_without_writing_install_tree(tmp_path: Path) -> None:
	dist = tmp_path / "dist"
	dist.mkdir(parents=True, exist_ok=True)
	clang = shutil.which("clang-15") or shutil.which("clang")
	assert clang, "clang not found"
	env = dict(os.environ)
	env["REPO_ROOT"] = str(ROOT)
	env["DIST"] = str(dist)
	env["CLANG"] = clang
	env["STAGE"] = str(tmp_path / "stage")
	env["DRIFTC_VERSION"] = "0.0.0-test"
	key_path = tmp_path / "deploy.key"
	key_path.write_text(base64.b64encode(os.urandom(32)).decode("ascii") + "\n", encoding="utf-8")
	env["DRIFT_SIGN_KEY_FILE"] = str(key_path)
	bundle = subprocess.run(
		["/bin/bash", str(ROOT / "tools" / "deploy" / "step_bundle.sh")],
		text=True,
		capture_output=True,
		env=env,
		timeout=180,
	)
	assert bundle.returncode == 0, bundle.stderr
	stdlib_pkg = subprocess.run(
		["/bin/bash", str(ROOT / "tools" / "deploy" / "step_stdlib_pkg.sh")],
		text=True,
		capture_output=True,
		env=env,
		timeout=180,
	)
	assert stdlib_pkg.returncode == 0, stdlib_pkg.stderr

	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""module main

fn main() nothrow -> Int {
	return 0;
}
""",
	)
	out = tmp_path / "a.out"
	runtime_root = dist / "lib" / "runtime"
	for path in [runtime_root, *runtime_root.rglob("*")]:
		if path.is_dir():
			path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
		else:
			path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

	no_site_python = _write_no_site_python(tmp_path)
	run_env = dict(os.environ)
	run_env["DRIFT_PYTHON"] = str(no_site_python)
	result = subprocess.run(
		[
			"/bin/bash",
			str(dist / "bin" / "driftc"),
			"--target-word-bits", "64",
			"-M", str(tmp_path),
			str(src),
			"-o", str(out),
		],
		text=True,
		capture_output=True,
		env=run_env,
		cwd=tmp_path,
		timeout=180,
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
	assert out.exists()
	assert not list(runtime_root.rglob(".build.lock"))
