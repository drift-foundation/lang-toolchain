# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Runner-level regressions for the DRIFT_DEBUG env knob.

These pin the dual-runtime selection contract directly on the test
populations that DRIFT_DEBUG plumbing actually flows through:

  - lang/tests/codegen/e2e/runner.py        (in-process codegen e2e)
  - lang/tests/codegen/e2e/pex_e2e_runner.py (PEX-staged driftc e2e)
  - lang/tests/codegen/e2e/pkg_consumer_runner.py (package-consumer e2e)

The regressions mock subprocess.run inside each runner module so the actual
clang/valgrind invocations are never executed; they only inspect the cmd
vectors the runner constructed.  This makes them fast and hermetic, but still
exercises the real code paths that compose env state into compile/link/run
commands.

Polarity contract (inverted from the retired DRIFT_OPTIMIZED knob):

  - default (no env)                     → -O2 present, variant = "default"
                                            (production "normal" lane)
  - DRIFT_DEBUG=1                        → no -O2, variant = "debug"
                                            (explicit `_debug` opt-in lane)
  - DRIFT_ASAN=1 (no DRIFT_DEBUG)        → -fsanitize=address + -O2, variant = "asan"
  - DRIFT_ASAN=1 + DRIFT_DEBUG=1         → -fsanitize=address, no -O2, variant = "asan"
                                            (sanitizers take precedence at variant
                                             selection; -O2 still suppressed)
  - DRIFT_MEMCHECK=1                     → -O2, variant = "default", valgrind run wrap
  - DRIFT_MEMCHECK=1 + DRIFT_DEBUG=1     → no -O2, variant = "debug", valgrind run wrap
  - DRIFT_MASSIF=1                       → -O2, variant = "default", massif run wrap
  - DRIFT_MASSIF=1 + DRIFT_DEBUG=1       → no -O2, variant = "debug", massif run wrap

`just test` (no env) → normal lane end-to-end.
`DRIFT_DEBUG=1 just test` → debug-style lane end-to-end.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest


@contextlib.contextmanager
def _stack(patches):
	with contextlib.ExitStack() as stack:
		for p in patches:
			stack.enter_context(p)
		yield


# ── Helpers ──────────────────────────────────────────────────────────


def _capture(runner_module, env_overrides: dict[str, str], call):
	"""Run `call()` with subprocess.run mocked inside runner_module.

	Returns (compile_cmd, run_cmd, variant_used) — the first two are the cmd
	vectors of the first and second subprocess.run calls; variant_used is the
	variant string passed to build_runtime_archive.
	"""
	captured_cmds: list[list[str]] = []
	captured_variant: dict[str, str] = {}

	def fake_archive(root, *, clang, variant):
		captured_variant["variant"] = variant
		return Path("/tmp/fake_runtime_archive.a")

	def fake_run(cmd, *args, **kwargs):
		captured_cmds.append(list(cmd))
		return mock.Mock(returncode=0, stdout="", stderr="")

	# Build a clean env containing only the explicit overrides for the modes
	# we are testing — strips any inherited DRIFT_* state from the harness.
	clean_env = {k: v for k, v in os.environ.items() if not k.startswith("DRIFT_")}
	clean_env.update(env_overrides)

	# Patch build_runtime_archive both at the source module (for runners
	# that import it lazily inside the function) and at the runner module
	# itself (for runners that import it at module top into their namespace).
	import lang.language_runtime as _lrt_mod
	patches = [
		mock.patch.object(_lrt_mod, "build_runtime_archive", side_effect=fake_archive),
		mock.patch.object(runner_module.subprocess, "run", side_effect=fake_run),
		mock.patch.object(runner_module.shutil, "which", return_value="/usr/bin/clang"),
		mock.patch.dict(os.environ, clean_env, clear=True),
	]
	if hasattr(runner_module, "build_runtime_archive"):
		patches.append(mock.patch.object(runner_module, "build_runtime_archive", side_effect=fake_archive))
	with _stack(patches):
		call()

	compile_cmd = captured_cmds[0] if captured_cmds else []
	run_cmd = captured_cmds[1] if len(captured_cmds) > 1 else []
	return compile_cmd, run_cmd, captured_variant.get("variant", "")


# ── runner.py: in-process codegen e2e runner ─────────────────────────


def _call_run_ir_with_clang(tmp_path: Path):
	from lang.tests.codegen.e2e import runner as e2e_runner
	def _do():
		e2e_runner._run_ir_with_clang(
			ir="; trivial\n",
			build_dir=tmp_path / "b",
			argv=None,
			stdin_data=None,
			timeout_s=10,
		)
	return e2e_runner, _do


def test_runner_default_lane_is_normal_optimized(tmp_path: Path) -> None:
	"""Default (no env) → -O2 present, default variant — the production normal lane."""
	mod, call = _call_run_ir_with_clang(tmp_path)
	compile_cmd, _, variant = _capture(mod, {}, call)
	assert "-O2" in compile_cmd
	assert variant == "default"


def test_runner_drift_debug_selects_debug_style_lane(tmp_path: Path) -> None:
	"""DRIFT_DEBUG=1 → -O2 suppressed, debug variant — the explicit `_debug` lane."""
	mod, call = _call_run_ir_with_clang(tmp_path)
	compile_cmd, _, variant = _capture(mod, {"DRIFT_DEBUG": "1"}, call)
	assert "-O2" not in compile_cmd
	assert variant == "debug"


def test_runner_memcheck_default_keeps_normal_lane(tmp_path: Path) -> None:
	"""DRIFT_MEMCHECK=1 alone → still normal lane (-O2, default variant), valgrind wrap on run."""
	mod, call = _call_run_ir_with_clang(tmp_path)
	compile_cmd, run_cmd, variant = _capture(
		mod, {"DRIFT_MEMCHECK": "1"}, call
	)
	assert "-O2" in compile_cmd
	assert variant == "default"
	assert "valgrind" in run_cmd
	assert "--tool=memcheck" in run_cmd


def test_runner_memcheck_drift_debug_composes(tmp_path: Path) -> None:
	"""DRIFT_MEMCHECK=1 + DRIFT_DEBUG=1: debug-style compile under memcheck run."""
	mod, call = _call_run_ir_with_clang(tmp_path)
	compile_cmd, run_cmd, variant = _capture(
		mod, {"DRIFT_MEMCHECK": "1", "DRIFT_DEBUG": "1"}, call
	)
	# Compile flips to debug-style: no -O2.  Variant flips to debug.
	assert "-O2" not in compile_cmd
	assert variant == "debug"
	# Run is still wrapped in valgrind memcheck — memcheck is a runtime mode
	# orthogonal to the dual-runtime lane choice.
	assert "valgrind" in run_cmd
	assert "--tool=memcheck" in run_cmd


def test_runner_massif_drift_debug_composes(tmp_path: Path) -> None:
	"""DRIFT_MASSIF=1 + DRIFT_DEBUG=1: debug-style compile under massif run."""
	mod, call = _call_run_ir_with_clang(tmp_path)
	compile_cmd, run_cmd, variant = _capture(
		mod, {"DRIFT_MASSIF": "1", "DRIFT_DEBUG": "1"}, call
	)
	assert "-O2" not in compile_cmd
	assert variant == "debug"
	assert "valgrind" in run_cmd
	assert "--tool=massif" in run_cmd


def test_runner_asan_default_lane(tmp_path: Path) -> None:
	"""DRIFT_ASAN=1 alone → -fsanitize + -O2, asan variant (sanitizer ride on normal lane)."""
	mod, call = _call_run_ir_with_clang(tmp_path)
	compile_cmd, _, variant = _capture(mod, {"DRIFT_ASAN": "1"}, call)
	assert "-fsanitize=address" in compile_cmd
	assert "-O2" in compile_cmd
	assert variant == "asan"


def test_runner_asan_drift_debug_composes(tmp_path: Path) -> None:
	"""DRIFT_ASAN=1 + DRIFT_DEBUG=1: sanitizer wins variant selection; -O2 still suppressed."""
	mod, call = _call_run_ir_with_clang(tmp_path)
	compile_cmd, _, variant = _capture(
		mod, {"DRIFT_ASAN": "1", "DRIFT_DEBUG": "1"}, call
	)
	assert "-fsanitize=address" in compile_cmd
	# DRIFT_DEBUG=1 suppresses -O2 even when stacked with a sanitizer.
	assert "-O2" not in compile_cmd
	# Sanitizer test variants take precedence over the dual-runtime distinction.
	assert variant == "asan"


# ── pex_e2e_runner.py ────────────────────────────────────────────────


def _call_pex_link_and_run(tmp_path: Path):
	from lang.tests.codegen.e2e import pex_e2e_runner as pex
	ir_path = tmp_path / "out.ll"
	ir_path.write_text("; trivial\n")
	build_dir = tmp_path / "b"
	build_dir.mkdir(parents=True, exist_ok=True)
	def _do():
		pex._link_and_run(
			ir_path=ir_path,
			build_dir=build_dir,
			argv=None,
			stdin_data=None,
			timeout_s=10,
		)
	return pex, _do


def test_pex_runner_default_lane_is_normal_optimized(tmp_path: Path) -> None:
	mod, call = _call_pex_link_and_run(tmp_path)
	compile_cmd, _, variant = _capture(mod, {}, call)
	assert "-O2" in compile_cmd
	assert variant == "default"


def test_pex_runner_drift_debug_selects_debug_style_lane(tmp_path: Path) -> None:
	mod, call = _call_pex_link_and_run(tmp_path)
	compile_cmd, _, variant = _capture(mod, {"DRIFT_DEBUG": "1"}, call)
	assert "-O2" not in compile_cmd
	assert variant == "debug"


def test_pex_runner_memcheck_drift_debug_composes(tmp_path: Path) -> None:
	from lang.tests.codegen.e2e import pex_e2e_runner as pex
	ir_path = tmp_path / "out.ll"
	ir_path.write_text("; trivial\n")
	build_dir = tmp_path / "b"
	build_dir.mkdir(parents=True, exist_ok=True)
	def _do():
		pex._link_and_run(
			ir_path=ir_path,
			build_dir=build_dir,
			argv=None,
			stdin_data=None,
			timeout_s=10,
			memcheck_enabled=True,
		)
	compile_cmd, run_cmd, variant = _capture(pex, {"DRIFT_DEBUG": "1"}, _do)
	assert "-O2" not in compile_cmd
	assert variant == "debug"
	assert "valgrind" in run_cmd
	assert "--tool=memcheck" in run_cmd


# ── pkg_consumer_runner.py ───────────────────────────────────────────


def _call_pkg_consumer_link_and_run(tmp_path: Path):
	from lang.tests.codegen.e2e import pkg_consumer_runner as pkg
	ir_path = tmp_path / "out.ll"
	ir_path.write_text("; trivial\n")
	build_dir = tmp_path / "b"
	build_dir.mkdir(parents=True, exist_ok=True)
	def _do():
		pkg._link_and_run(
			ir_path=ir_path,
			build_dir=build_dir,
			argv=None,
			stdin_data=None,
			timeout_s=10,
		)
	return pkg, _do


def test_pkg_consumer_default_lane_is_normal_optimized(tmp_path: Path) -> None:
	mod, call = _call_pkg_consumer_link_and_run(tmp_path)
	compile_cmd, _, variant = _capture(mod, {}, call)
	assert "-O2" in compile_cmd
	assert variant == "default"


def test_pkg_consumer_drift_debug_selects_debug_style_lane(tmp_path: Path) -> None:
	mod, call = _call_pkg_consumer_link_and_run(tmp_path)
	compile_cmd, _, variant = _capture(mod, {"DRIFT_DEBUG": "1"}, call)
	assert "-O2" not in compile_cmd
	assert variant == "debug"
