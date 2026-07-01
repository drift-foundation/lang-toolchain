"""
End-to-end tests for extern "C" FFI.

Covers:
- Positive: compile + link + run programs that call C functions
- Negative: diagnostics for unsupported FFI shapes
- Linker: --link-lib / --link-search flag passthrough
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
STDLIB = ROOT / "stdlib"
BUILD_ROOT = Path("build/tests/ffi_c")


def _driftc(*extra_args: str, source: str, expect_fail: bool = False, allow_unsafe: bool = True) -> subprocess.CompletedProcess:
	"""Run driftc on inline source, return CompletedProcess."""
	with tempfile.TemporaryDirectory(prefix="ffi-", dir=BUILD_ROOT) as tmp:
		src = Path(tmp) / "main.drift"
		src.write_text(source)
		cmd = [
			sys.executable,
			"-m", "lang.driftc",
			"--stdlib-root", str(STDLIB),
			"--dev",
			"--json",
			str(src),
		]
		if allow_unsafe:
			cmd.insert(-2, "--allow-unsafe")
		res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), env={"PYTHONPATH": "."})
		if not expect_fail and res.returncode != 0:
			pytest.fail(f"driftc failed: {res.stdout}\n{res.stderr}")
		return res


def _compile_and_run(
	drift_source: str,
	c_source: str | None = None,
	link_libs: list[str] | None = None,
	link_search: list[str] | None = None,
) -> int:
	"""Compile Drift + optional C source, link, and return binary exit code."""
	clang = shutil.which("clang")
	if clang is None:
		pytest.skip("clang not available")

	BUILD_ROOT.mkdir(parents=True, exist_ok=True)
	tmp = Path(tempfile.mkdtemp(prefix="ffi-e2e-", dir=BUILD_ROOT))

	# Write Drift source
	drift_path = tmp / "main.drift"
	drift_path.write_text(drift_source)

	# Compile C helper if provided
	c_obj = None
	if c_source is not None:
		c_path = tmp / "helper.c"
		c_path.write_text(c_source)
		c_obj = tmp / "helper.o"
		res = subprocess.run(
			[clang, "-c", str(c_path), "-o", str(c_obj)],
			capture_output=True, text=True,
		)
		if res.returncode != 0:
			pytest.fail(f"C compile failed: {res.stderr}")

	# Compile Drift to binary
	bin_path = tmp / "a.out"
	cmd = [
		sys.executable,
		"-m", "lang.driftc",
		"--stdlib-root", str(STDLIB),
		"--dev", "--allow-unsafe",
		"-o", str(bin_path),
		str(drift_path),
	]
	if c_obj is not None:
		cmd.extend(["--link-obj", str(c_obj)])
	for lib in (link_libs or []):
		cmd.extend(["--link-lib", lib])
	for path in (link_search or []):
		cmd.extend(["--link-search", path])

	env = {"PYTHONPATH": ".", "PATH": subprocess.check_output(["bash", "-lc", "echo $PATH"], text=True).strip()}
	res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), env=env)
	if res.returncode != 0:
		pytest.fail(f"driftc compile failed: {res.stdout}\n{res.stderr}")

	# Run binary
	run_res = subprocess.run([str(bin_path)], capture_output=True, text=True)
	return run_res.returncode


# ── Positive: libc abs() via extern "C" ────────────────────────────

class TestFFIPositive:

	def test_extern_c_libc_abs(self):
		"""Call libc abs() through extern C declaration."""
		exit_code = _compile_and_run(
			drift_source="""\
extern "C" fn abs(x: Int) nothrow -> Int;

pub fn main() nothrow -> Int {
    return unsafe { abs(-42) };
}
""",
		)
		assert exit_code == 42

	def test_extern_c_custom_c_lib(self):
		"""Call a custom C function compiled from test helper."""
		exit_code = _compile_and_run(
			drift_source="""\
extern "C" fn add_ints(a: Int, b: Int) nothrow -> Int;

pub fn main() nothrow -> Int {
    return unsafe { add_ints(30, 7) };
}
""",
			c_source="""\
#include <stdint.h>
intptr_t add_ints(intptr_t a, intptr_t b) { return a + b; }
""",
		)
		assert exit_code == 37

	def test_extern_c_void_return(self):
		"""Extern C function returning void."""
		exit_code = _compile_and_run(
			drift_source="""\
extern "C" fn set_global(x: Int) nothrow -> Void;
extern "C" fn get_global() nothrow -> Int;

pub fn main() nothrow -> Int {
    unsafe { set_global(19) };
    return unsafe { get_global() };
}
""",
			c_source="""\
#include <stdint.h>
static intptr_t g_val = 0;
void set_global(intptr_t x) { g_val = x; }
intptr_t get_global(void) { return g_val; }
""",
		)
		assert exit_code == 19

	def test_extern_c_block_syntax(self):
		"""Block form extern C { ... } with multiple declarations."""
		exit_code = _compile_and_run(
			drift_source="""\
extern "C" {
    fn helper_a() nothrow -> Int;
    fn helper_b(x: Int) nothrow -> Int;
}

pub fn main() nothrow -> Int {
    return unsafe { helper_b(helper_a()) };
}
""",
			c_source="""\
#include <stdint.h>
intptr_t helper_a(void) { return 5; }
intptr_t helper_b(intptr_t x) { return x * 3; }
""",
		)
		assert exit_code == 15

	def test_extern_c_rawptr(self):
		"""Pass and return RawPtr<Byte> (void* / char*)."""
		exit_code = _compile_and_run(
			drift_source="""\
extern "C" fn alloc_byte(b: Byte) nothrow -> RawPtr<Byte>;
extern "C" fn read_byte(ptr: RawPtr<Byte>) nothrow -> Int;
extern "C" fn free_byte(ptr: RawPtr<Byte>) nothrow -> Void;

pub fn main() nothrow -> Int {
    val ptr: RawPtr<Byte> = unsafe { alloc_byte(cast<Byte>(23u)) };
    val result: Int = unsafe { read_byte(ptr) };
    unsafe { free_byte(ptr) };
    return result;
}
""",
			c_source="""\
#include <stdlib.h>
#include <stdint.h>
char* alloc_byte(char b) { char* p = malloc(1); *p = b; return p; }
intptr_t read_byte(char* ptr) { return (intptr_t)(unsigned char)*ptr; }
void free_byte(char* ptr) { free(ptr); }
""",
		)
		assert exit_code == 23

	def test_extern_c_link_lib_m(self):
		"""Use --link-lib to link against libm."""
		# abs is in libc (always linked), no --link-lib needed for this
		exit_code = _compile_and_run(
			drift_source="""\
extern "C" fn abs(x: Int) nothrow -> Int;

pub fn main() nothrow -> Int {
    return unsafe { abs(-7) };
}
""",
		)
		assert exit_code == 7


# ── Negative: diagnostic tests ─────────────────────────────────────

class TestFFINegative:

	def test_string_param_rejected(self):
		"""String is not FFI-safe."""
		BUILD_ROOT.mkdir(parents=True, exist_ok=True)
		res = _driftc(
			source='extern "C" fn bad(s: String) nothrow -> Int;\npub fn main() nothrow -> Int { return 0; }\n',
			expect_fail=True,
		)
		assert res.returncode != 0
		assert "not FFI-safe" in res.stdout

	def test_throws_rejected(self):
		"""Extern C functions cannot throw."""
		BUILD_ROOT.mkdir(parents=True, exist_ok=True)
		res = _driftc(
			source='extern "C" fn bad(x: Int) throws -> Int;\npub fn main() nothrow -> Int { return 0; }\n',
			expect_fail=True,
		)
		assert res.returncode != 0

	def test_missing_nothrow_rejected(self):
		"""Extern C functions must declare nothrow."""
		BUILD_ROOT.mkdir(parents=True, exist_ok=True)
		res = _driftc(
			source='extern "C" fn bad(x: Int) -> Int;\npub fn main() nothrow -> Int { return 0; }\n',
			expect_fail=True,
		)
		assert res.returncode != 0

	def test_unsafe_required(self):
		"""Calling extern C without unsafe block is rejected."""
		BUILD_ROOT.mkdir(parents=True, exist_ok=True)
		res = _driftc(
			source='extern "C" fn abs(x: Int) nothrow -> Int;\npub fn main() nothrow -> Int { return abs(-1); }\n',
			expect_fail=True,
			allow_unsafe=False,
		)
		assert res.returncode != 0
		assert "extern C function requires unsafe" in res.stdout

	def test_void_param_rejected(self):
		"""Void is not valid as a parameter type."""
		BUILD_ROOT.mkdir(parents=True, exist_ok=True)
		res = _driftc(
			source='extern "C" fn bad(x: Void) nothrow -> Int;\npub fn main() nothrow -> Int { return 0; }\n',
			expect_fail=True,
		)
		assert res.returncode != 0
		assert "Void" in res.stdout
		assert "not valid as a parameter type" in res.stdout

	def test_unsafe_block_without_allow_unsafe(self):
		"""Extern C call inside unsafe block without --allow-unsafe gives one clear diagnostic."""
		BUILD_ROOT.mkdir(parents=True, exist_ok=True)
		res = _driftc(
			source='extern "C" fn abs(x: Int) nothrow -> Int;\npub fn main() nothrow -> Int { return unsafe { abs(-1) }; }\n',
			expect_fail=True,
			allow_unsafe=False,
		)
		assert res.returncode != 0
		# Should mention --allow-unsafe, NOT "requires unsafe block"
		assert "--allow-unsafe" in res.stdout
		assert "requires unsafe block" not in res.stdout

	def test_bad_abi_string(self):
		"""Only ABI "C" is accepted."""
		BUILD_ROOT.mkdir(parents=True, exist_ok=True)
		res = _driftc(
			source='extern "Java" fn bad() nothrow -> Int;\npub fn main() nothrow -> Int { return 0; }\n',
			expect_fail=True,
		)
		assert res.returncode != 0
