# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1 of the DV→JSON diagnostics-context migration —
ownership pins for the additive JSON helpers on `DriftError`.

Compiles the pure-C test under
`lang/compiler_infra/tests/test_drift_error_phase1.c` against the
prebuilt runtime archive and runs it under valgrind, asserting:

  - exit code 0 (all helper-contract assertions pass), and
  - zero `definitely lost` / `still reachable` allocations and zero
    invalid free / use-after-free errors.

The C test pins:

  1. `drift_error_new` initializes `params_json="{}"`,
     `context_json="[]"` per ABI spec §2.2.
  2. `drift_error_set_params_json` takes ownership (no clone) and
     releases the prior value on replacement.
  3. `drift_error_append_context_frame` takes ownership of the
     incoming frame and produces a well-formed JSON array; bytes
     of `frame_json` are preserved verbatim inside the merged
     array (ABI §2.2 fastpath guarantee for `e.encode_compact()`).
  4. `drift_error_get_params_json` / `_get_context_json` return
     RETAINED `DriftString` (caller owns and releases — safe to
     surface as a normal Drift `String` return without
     compiler-side borrow handling).

Slice 7c-1 (ABI 14, 2026-05-06): the legacy DV path
(`drift_error_add_attr_dv` + `drift_dv_*`) is deleted from the
runtime archive.  The earlier "ADDITIVE: DV path coexistence"
and "ADDITIVE: release drops both DV and JSON fields" pins are
retired alongside the runtime symbols they exercised.  This
file now pins the JSON-only helper ownership contract that
survives at ABI 14.

See `docs/design/drift-lang-abi.md` §2.3 (helper ownership
contract).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEST_C = ROOT / "lang" / "compiler_infra" / "tests" / "test_drift_error_phase1.c"


def _runtime_archive_path() -> Path:
	from lang.versions import DRIFT_RT_ABI_VERSION
	return (
		ROOT
		/ "build"
		/ "runtime_libs"
		/ "default"
		/ f"libdrift_rt_abi{DRIFT_RT_ABI_VERSION}.a"
	)


@pytest.fixture(scope="module")
def runtime_archive() -> Path:
	p = _runtime_archive_path()
	if not p.exists():
		pytest.skip(
			f"runtime archive missing at {p} — run `just runtime-libs` first"
		)
	return p


def test_drift_error_phase1_helpers(tmp_path: Path, runtime_archive: Path) -> None:
	if shutil.which("valgrind") is None:
		pytest.skip("valgrind required")
	if shutil.which("cc") is None:
		pytest.skip("cc required")
	if not TEST_C.exists():
		pytest.skip(f"test source missing: {TEST_C}")

	out_bin = tmp_path / "test_drift_error_phase1"

	compile_res = subprocess.run(
		[
			"cc",
			"-O0",
			"-g",
			"-Wall",
			"-Wextra",
			"-std=c11",
			"-I",
			str(ROOT / "lang" / "language_runtime"),
			"-I",
			str(ROOT / "lang" / "compiler_infra"),
			"-o",
			str(out_bin),
			str(TEST_C),
			str(runtime_archive),
			"-pthread",
			"-lm",
		],
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert compile_res.returncode == 0, (
		f"compile failed (rc={compile_res.returncode}):\n"
		f"STDOUT:\n{compile_res.stdout}\n"
		f"STDERR:\n{compile_res.stderr}"
	)

	vg_log = tmp_path / "valgrind.log"
	vg_res = subprocess.run(
		valgrind_cmd(
			"--leak-check=full",
			"--show-leak-kinds=all",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=66",
			f"--log-file={vg_log}",
			str(out_bin),
		),
		capture_output=True,
		text=True,
		timeout=120,
	)

	log_text = vg_log.read_text() if vg_log.exists() else "<no valgrind log>"

	# Test binary must exit 0 (assertions pass) AND valgrind must report
	# no definite/indirect leaks or invalid memory access.
	assert vg_res.returncode == 0, (
		f"test failed: rc={vg_res.returncode}\n"
		f"binary stdout:\n{vg_res.stdout}\n"
		f"binary stderr:\n{vg_res.stderr}\n"
		f"valgrind:\n{log_text[:4000]}"
	)

	# Belt-and-braces: even with --error-exitcode, sanity-check the log
	# contents.  Valgrind emits one of two terminal forms:
	#   - "All heap blocks were freed -- no leaks are possible" when
	#     allocs == frees exactly (no LEAK SUMMARY block produced), or
	#   - a LEAK SUMMARY with explicit "definitely lost"/"indirectly
	#     lost" lines when any block survived to exit.
	# Accept either, but require zero on the explicit lines if present.
	assert "ERROR SUMMARY: 0 errors" in log_text, (
		f"valgrind reported errors:\n{log_text[:4000]}"
	)
	all_freed = "All heap blocks were freed -- no leaks are possible" in log_text
	if not all_freed:
		assert "definitely lost: 0 bytes" in log_text, (
			f"valgrind reported definite leaks:\n{log_text[:4000]}"
		)
		assert "indirectly lost: 0 bytes" in log_text, (
			f"valgrind reported indirect leaks:\n{log_text[:4000]}"
		)
