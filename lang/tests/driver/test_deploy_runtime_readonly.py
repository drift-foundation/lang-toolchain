# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tools.deploy.steps.bundle import bundle_compiler, bundle_docs_and_examples, bundle_runtime_archives
from tools.deploy.steps.pex import build_drift_pex, build_driftc_pex
from tools.deploy.steps.stdlib import build_and_install_stdlib

ROOT = Path(__file__).resolve().parents[3]

_skip_no_pex = pytest.mark.skipif(
	shutil.which("pex") is None and not (ROOT / ".venv" / "bin" / "pex").exists(),
	reason="pex not installed; deployed bundle requires PEX --scie eager",
)


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


@_skip_no_pex
def test_deployed_wrapper_uses_runtime_archives_without_writing_install_tree(tmp_path: Path) -> None:
	dist = tmp_path / "dist"
	dist.mkdir(parents=True, exist_ok=True)
	clang = shutil.which("clang")
	assert clang, "clang not found"

	# Signing key for stdlib package.
	key_path = tmp_path / "deploy.key"
	key_path.write_text(base64.b64encode(os.urandom(32)).decode("ascii") + "\n", encoding="utf-8")
	old_sign_key = os.environ.get("DRIFT_SIGN_KEY_FILE")
	os.environ["DRIFT_SIGN_KEY_FILE"] = str(key_path)

	try:
		# Build PEX executables.
		build_driftc_pex(ROOT, dist)
		build_drift_pex(ROOT, dist)

		# Bundle compiler sources and runtime archives.
		bundle_compiler(ROOT, dist)
		bundle_runtime_archives(ROOT, dist)
		bundle_docs_and_examples(dist)

		# Build, sign, and install stdlib + core trust store.
		stage = tmp_path / "stage"
		stage.mkdir(parents=True, exist_ok=True)
		build_and_install_stdlib(ROOT, stage, dist, "0.0.0-test")
	finally:
		if old_sign_key is None:
			os.environ.pop("DRIFT_SIGN_KEY_FILE", None)
		else:
			os.environ["DRIFT_SIGN_KEY_FILE"] = old_sign_key

	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""module main;

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

	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		run_env.pop(key, None)
	run_env["HOME"] = str(tmp_path / "home")
	result = subprocess.run(
		[
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
