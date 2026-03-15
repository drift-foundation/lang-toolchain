from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.tests.driver.test_drift_trust_cli import with_target_word_bits, _write_file


def test_package_root_does_not_duplicate_std_methods(tmp_path: Path) -> None:
	# Build a tiny package so --package-root has something to load.
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	repo_root = Path.cwd()

	build_pkg = subprocess.run(
		with_target_word_bits(
			[
				sys.executable,
				"-m",
				"lang.driftc.driftc",
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				"--package-id",
				"test.pkg",
				"--package-version",
				"0.0.0",
				"--package-target",
				"test-target",
				"--emit-package",
				str(pkg),
				"--json",
			]
		),
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert build_pkg.returncode == 0, build_pkg.stderr
	out = json.loads(build_pkg.stdout or "{}")
	assert out.get("exit_code") == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import std.containers as containers;

fn main() nothrow -> Int {
	var d = containers.deque<type Int>();
	d.push_back(1);
	val _ = d.len();
	return 0;
}
""".lstrip(),
	)

	consume = subprocess.run(
		with_target_word_bits(
			[
				sys.executable,
				"-m",
				"lang.driftc.driftc",
				"-M",
				str(tmp_path),
				"--package-root",
				str(tmp_path),
				"--allow-unsigned-from",
				str(tmp_path),
				str(tmp_path / "main.drift"),
				"--emit-ir",
				str(tmp_path / "out.ll"),
				"--json",
			]
		),
		cwd=str(repo_root),
		check=False,
		capture_output=True,
		text=True,
	)
	assert consume.returncode in (0, 1), consume.stderr
	result = json.loads(consume.stdout or "{}")
	assert result.get("exit_code") == 0
