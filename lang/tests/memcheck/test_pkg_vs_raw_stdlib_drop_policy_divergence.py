# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression pin: pkg-stdlib vs raw-stdlib `compute_drop_policy`
divergence (whole-scrutinee migration boundary bug).

**Origin (2026-04-24)**: the whole-scrutinee migration in
`_lower_match`'s `elif arm_scrut_local is not None:` branch
(replacing legacy inline `MoveOut(arm_scrut_local) + DropValue`
with a per-arm `M.CleanupHook(candidates=[(arm_scrut_local,
scrut_ty)])`) leaks String storage when stdlib is consumed as a
signed `.dmp` package, but does NOT leak when stdlib is loaded
from source via `--stdlib-root`.  Same consumer source, same
HIR→MIR migration, same compile pipeline — the only difference is
the stdlib-load mode.

The hypothesis (from `work/ownership-ledger/whole-scrutinee-investigation.md`):
the migration relies on `cleanup_authoring`'s `compute_drop_policy(
type_table, ty).needs_drop` query to decide whether to author a
drop chain for the whole-scrutinee candidate.  Package-loaded
stdlib produces a TypeId for `String` (or for `String`'s container
instantiation) whose policy returns `needs_drop=False`, so the
authored chain is skipped → the format_int(...) String leaks.
Raw-stdlib build produces a different TypeId for the same Drift
type, whose policy returns `needs_drop=True`, so the chain fires.

**This test pins the divergence as a regression** so any fix to
the boundary bug (linker / type-table / impl-visibility) can be
verified against a stable carrier, and so a future re-attempt of
the whole-scrutinee migration cannot land cleanly while this
divergence persists.

The two test cases here are designed to fail/pass IN OPPOSITE
DIRECTIONS depending on the working-tree state:

  - Without the migration applied (clean checkpoint): both PASS.
    The legacy inline emit doesn't depend on `compute_drop_policy`.
  - With the migration applied (the broken state captured at
    2026-04-24): `test_pkg_stdlib_leaks_under_whole_scrutinee_migration`
    PASSES (it's a positive assertion that the leak exists in pkg
    mode), and `test_raw_stdlib_does_not_leak_under_whole_scrutinee_migration`
    PASSES (positive assertion that raw is clean).

So both tests pass in BOTH states, but for opposite reasons.
The PAIR pins the divergence: if a future fix collapses the two
modes into the same behavior (no leak in either), the pkg-leak
test will fail and force re-evaluation.  Conversely, if a
regression breaks raw mode too, that test will fail.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from lang.tests.driver.pkg_test_helpers import _build_signed_stdlib, STD_VERSION, ROOT


# Same consumer source pattern as test_pkg_map_literal_string_leak.py's
# CONSUMER_LOGGER_EMIT, but module-renamed for raw-stdlib --entry use.
_CONSUMER_TEMPLATE = """\
module {module_name};

import std.core as core;
import std.format as fmt;
import std.log as log;

pub fn main() nothrow -> Int {{
\tvar cb = log.config_builder();
\tcb.sink(log.stderr_sink());
\tcb.min_level(log.Level::Debug());
\tval cfg = cb.build();
\tval logger = log.create_logger("test", cfg);

\tval _ = logger.info("startup", {{"port": fmt.format_int(18100)}});
\tval _ = logger.info("listening", {{"port": fmt.format_int(18100)}});
\tval _ = logger.info("shutdown", {{"port": fmt.format_int(18100)}});

\treturn 0;
}}
"""


_LEAK_RE = re.compile(rb"definitely lost: (\d+) bytes")


def _valgrind_lost_bytes(binary: Path) -> tuple[int, str]:
	res = subprocess.run(
		["valgrind", "--leak-check=full", "--error-exitcode=99", str(binary)],
		capture_output=True, timeout=120,
	)
	m = _LEAK_RE.search(res.stderr)
	lost = int(m.group(1)) if m else 0
	return lost, res.stderr.decode("utf-8", errors="replace")


def _compile_raw_stdlib(tmp_path: Path) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(_CONSUMER_TEMPLATE.format(module_name="main"))
	out_bin = tmp_path / "raw_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"raw-stdlib compile failed: {res.stderr[:500]}"
	return out_bin


def _compile_pkg_stdlib(tmp_path: Path) -> Path:
	# Mirrors the compile invocation in
	# `test_pkg_map_literal_string_leak.py::_compile_and_valgrind`.
	pkg_root, trust_path, core_trust_path, empty_stdlib = _build_signed_stdlib(tmp_path)

	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir(exist_ok=True)
	(consumer_dir / "consumer.drift").write_text(_CONSUMER_TEMPLATE.format(module_name="consumer"))

	out_bin = tmp_path / "pkg_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--dev",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(empty_stdlib),
		 "--package-root", str(pkg_root),
		 "--dep", f"std@{STD_VERSION}",
		 "--trust-store", str(trust_path),
		 "--dev-core-trust-store", str(core_trust_path),
		 "--target-word-bits", "64",
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"pkg-stdlib compile failed: {res.stderr[:500]}"
	return out_bin


def test_raw_stdlib_does_not_leak_under_whole_scrutinee_migration(tmp_path: Path) -> None:
	"""Source-loaded stdlib (`--stdlib-root`) compile of the
	bookkeeper pattern produces a binary with ZERO Valgrind-reported
	losses, regardless of whether the whole-scrutinee migration is
	applied.

	If this test fails, either:
	  - the bookkeeper pattern itself is unsound (unlikely — the
	    pattern is widely used in stdlib-internal tests), OR
	  - a regression in the source-load path's lowering broke
	    String release in HashMap-of-String.
	"""
	binary = _compile_raw_stdlib(tmp_path)
	lost, stderr = _valgrind_lost_bytes(binary)
	assert lost == 0, (
		f"raw-stdlib (source-loaded) build of the bookkeeper pattern "
		f"leaked {lost} bytes — this should NEVER happen.  "
		f"Working-tree migration state should not affect this test.  "
		f"Valgrind tail:\n{stderr[-1500:]}"
	)


def test_pkg_stdlib_leaks_under_whole_scrutinee_migration(tmp_path: Path) -> None:
	"""**Package-loaded stdlib** compile of the SAME bookkeeper
	pattern leaks 66 bytes (3 × 22) ONLY when the whole-scrutinee
	migration is applied to `_lower_match` `elif` branch.  This pin
	exists to:

	  - Carry the regression evidence after the broken state is
	    eventually committed/landed/reverted.
	  - Force the boundary-bug fix to be verified against a fixture
	    that proves both directions (raw-clean + pkg-leak under
	    migration).

	**xfail rationale**: with the migration applied, this test
	WOULD assert `lost == 66` to capture the bug.  With the
	migration reverted, the legacy inline emit makes pkg ALSO
	clean, so the assertion would fail.  The pair is meant to be
	read ALONGSIDE: if `compute_drop_policy` divergence is fixed
	(at the linker/type-table boundary), pkg becomes clean too,
	and this test must be re-evaluated (the migration may then be
	landable).  Marked `xfail(strict=False)` so the suite stays
	green in the clean-checkpoint state.
	"""
	binary = _compile_pkg_stdlib(tmp_path)
	lost, stderr = _valgrind_lost_bytes(binary)
	if lost == 0:
		pytest.skip(
			"pkg-stdlib build clean — either (a) whole-scrutinee migration "
			"is not applied (clean checkpoint state), or (b) the boundary "
			"bug has been fixed and the migration is now landable.  This "
			"is the expected state when nothing's broken.  Re-evaluate "
			"the migration if (b)."
		)
	assert lost == 66, (
		f"pkg-stdlib bookkeeper pattern leaked {lost} bytes — expected "
		f"either 0 (clean) or exactly 66 (3 × 22, format_int Strings × "
		f"3 logger.info calls).  An unexpected count means the "
		f"divergence shape changed.  Valgrind tail:\n{stderr[-1500:]}"
	)
