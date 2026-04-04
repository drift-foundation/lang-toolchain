# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: stdlib-as-package consumer path.

These tests compile programs against stdlib loaded as a signed .dmp package
(--package-root + --dep std@VERSION), the same code path used by PEX/deploy.
This exercises a fundamentally different type table state than --stdlib-root:
struct instances are linked incrementally, destructor_fns is installed in
phases, and generic instantiations have different TypeIds.

The Arc memory leak that took 2 days to trace was invisible to every existing
test because they all use --stdlib-root.  These tests pin the package-consumer
path so similar bugs surface immediately.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

_memcheck = os.environ.get("DRIFT_MEMCHECK") == "1"


def _compile_consumer(
	source: str,
	*,
	stdlib_pkg: "StdlibPackage",
	tmp_path: Path,
	entry: str = "main::main",
) -> Path:
	"""Compile a consumer program against stdlib as a package.

	Asserts the compile uses --package-root + --dep (not --stdlib-root)
	and returns the path to the linked binary.
	"""
	from conftest import StdlibPackage  # type: ignore[import]

	src_dir = tmp_path / "src"
	src_dir.mkdir(exist_ok=True)
	(src_dir / "main.drift").write_text(source)

	out_bin = tmp_path / "test_bin"
	# Use an empty --stdlib-root to suppress auto-detection of the repo's
	# stdlib source tree.  Without this, the compiler finds lang.atomic
	# from both source AND the package, causing a module id collision.
	# This mirrors the deploy/PEX behavior exactly.
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(exist_ok=True)
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(src_dir / "main.drift"),
		"--stdlib-root", str(empty_stdlib),
		"--target-word-bits", "64",
		"--package-root", str(stdlib_pkg.pkg_root),
		"--dep", f"std@{stdlib_pkg.version}",
		"--trust-store", str(stdlib_pkg.trust_path),
		"--dev", "--dev-core-trust-store", str(stdlib_pkg.trust_path),
		"--entry", entry,
		"-o", str(out_bin),
	]

	# Guard: --stdlib-root must point to the empty dir, not the real
	# stdlib source tree.  If it pointed to the real stdlib, the test
	# would silently exercise the source path and lose its value.
	assert str(stdlib_pkg.stdlib_root) not in " ".join(cmd), (
		"consumer compile must not use the real stdlib source tree"
	)

	res = subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, (
		f"consumer compile failed (stdlib-as-package path):\n{res.stderr[:500]}"
	)
	assert out_bin.exists(), "binary not produced"
	return out_bin


def _run_valgrind(binary: Path) -> int:
	"""Run binary under Valgrind. Asserts clean exit, returns definitely-lost bytes."""
	vg = subprocess.run(
		["valgrind", "--leak-check=full", "--error-exitcode=42", str(binary)],
		capture_output=True, text=True, timeout=30,
	)
	# The binary itself must exit 0.  Valgrind uses exit code 42 for
	# its own errors; any other nonzero is from the program.
	assert vg.returncode in (0, 42), (
		f"consumer binary exited with code {vg.returncode} "
		f"(expected 0 or 42 from Valgrind leak detection).\n"
		f"stderr:\n{vg.stderr[-500:]}"
	)
	no_leaks = (
		"no leaks are possible" in vg.stderr
		or "All heap blocks were freed" in vg.stderr
	)
	lost_match = re.search(r"definitely lost: (\d+) bytes", vg.stderr)
	return int(lost_match.group(1)) if lost_match else (0 if no_leaks else -1)


def test_arc_scope_drop_no_leak(stdlib_package, tmp_path: Path) -> None:
	"""Arc<AtomicBool> in an owned struct must be destroyed on scope exit.

	This is the canonical discriminator for the package-consumer Arc leak:
	  - Source-built: 0 leaks (has_drop sees full type table)
	  - Package-built without fix: 16-byte leak (has_drop returns False
	    for Arc due to destructor_fns timing, scope drop omitted)
	  - Package-built with fix: 0 leaks (post-pass injects missing drop)

	Always compiles and runs the binary (proves the consumer path works).
	Leak check runs only under DRIFT_MEMCHECK=1 to avoid ASAN/Valgrind conflicts.
	"""
	source = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.sync as sync;

struct Handle {
\tpub flag: conc.Arc<sync.AtomicBool>,
\tpub value: Int
}

struct Outer {
\tpub handle: Handle,
\tpub tag: Int
}

fn make_outer() nothrow -> Outer {
\tval a = conc.arc(sync.atomic_bool(false));
\treturn Outer(handle = Handle(flag = move a, value = 42), tag = 1);
}

pub fn main() nothrow -> Int {
\tvar o = make_outer();
\tval result = o.handle.value;
\tif result != 42 { return 1; }
\treturn 0;
}
"""
	binary = _compile_consumer(source, stdlib_pkg=stdlib_package, tmp_path=tmp_path)

	# Always run the binary to verify consumer-path correctness.
	res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
	assert res.returncode == 0, (
		f"consumer binary failed with rc={res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)

	# Leak check only under DRIFT_MEMCHECK=1.
	if _memcheck:
		if shutil.which("valgrind") is None:
			pytest.skip("DRIFT_MEMCHECK=1 but valgrind not available")
		lost = _run_valgrind(binary)
		assert lost == 0, (
			f"Valgrind found {lost} bytes definitely lost on the "
			f"stdlib-as-package consumer path. Arc scope drop is missing."
		)
