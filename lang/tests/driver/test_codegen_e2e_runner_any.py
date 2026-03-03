# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
import signal
import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

from lang.tests.codegen.e2e.runner import _compare_process_output
from lang.tests.codegen.e2e.runner import _asan_options_with_defaults
from lang.tests.codegen.e2e.runner import _run_case_worker
from lang.tests.codegen.e2e.runner import _run_case_chunk


def test_any_stdout_does_not_skip_stderr_mismatch() -> None:
	expected = {
		"exit_code": 0,
		"stdout": "__ANY__",
		"stderr": "expected-stderr\n",
	}
	msg = _compare_process_output(
		exit_code=0,
		stdout="actual-stdout\n",
		stderr="wrong-stderr\n",
		expected=expected,
		debug=False,
	)
	assert msg == "FAIL (stderr mismatch)"


def test_any_stderr_does_not_skip_stdout_mismatch() -> None:
	expected = {
		"exit_code": 0,
		"stdout": "expected-stdout\n",
		"stderr": "__ANY__",
	}
	msg = _compare_process_output(
		exit_code=0,
		stdout="wrong-stdout\n",
		stderr="actual-stderr\n",
		expected=expected,
		debug=False,
	)
	assert msg == "FAIL (stdout mismatch)"


def test_asan_options_defaults_added_when_missing() -> None:
	opts = _asan_options_with_defaults(None)
	assert "detect_leaks=0" in opts
	assert "halt_on_error=1" in opts


def test_asan_options_respects_existing_detect_leaks() -> None:
	opts = _asan_options_with_defaults("detect_leaks=1:fast_unwind_on_malloc=0")
	assert "detect_leaks=1" in opts
	assert "detect_leaks=0" not in opts
	assert "halt_on_error=1" in opts


def _make_case_dir(tmp: Path, name: str = "fake_case", timeout_s: int | None = None) -> Path:
	"""Create a minimal e2e case directory with expected.json."""
	d = tmp / name
	d.mkdir(parents=True, exist_ok=True)
	expected: dict = {"exit_code": 0}
	if timeout_s is not None:
		expected["timeout_s"] = timeout_s
	(d / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
	(d / "main.drift").write_text("module main\nfn main() nothrow -> Int { return 0; }\n", encoding="utf-8")
	return d


def test_worker_timeout_returns_named_failure() -> None:
	"""A timeout during _run_case produces (case_name, 'FAIL (timeout ...)')."""
	with tempfile.TemporaryDirectory() as tmp:
		case_dir = _make_case_dir(Path(tmp), "timeout_case")
		def _raise_timeout(*a, **kw):
			raise TimeoutError("timeout after 5s")
		with mock.patch("lang.tests.codegen.e2e.runner._run_case", side_effect=_raise_timeout):
			name, status = _run_case_worker(str(case_dir), 5, debug=False)
		assert name == "timeout_case"
		assert status.startswith("FAIL (timeout")


def test_late_timeout_in_cleanup_returns_named_failure_and_disarms() -> None:
	"""A late SIGALRM during the finally block does not escape _run_case_worker,
	and cleanup still completes (alarm cancelled, handler restored).

	Simulates: _run_case completes, but TimeoutError fires during the
	first SIG_IGN install in the finally block.  The outer except must
	catch it, return a named failure, AND re-run disarm so the next case
	in the chunk is not contaminated.
	"""
	_first_ign = [True]

	def _fake_run_case(*a, **kw):
		return "ok"

	original_signal_fn = signal.signal

	def _patched_signal(signum, handler):
		"""Raise TimeoutError on the FIRST SIG_IGN call only (the finally
		block).  Subsequent calls (the outer except's _disarm_alarm) pass
		through so cleanup can complete."""
		if signum == signal.SIGALRM and handler == signal.SIG_IGN and _first_ign[0]:
			_first_ign[0] = False
			raise TimeoutError("timeout after 5s")
		return original_signal_fn(signum, handler)

	sentinel_handler = signal.getsignal(signal.SIGALRM)
	with tempfile.TemporaryDirectory() as tmp:
		case_dir = _make_case_dir(Path(tmp), "late_alarm_case")
		with mock.patch("lang.tests.codegen.e2e.runner._run_case", side_effect=_fake_run_case):
			with mock.patch("lang.tests.codegen.e2e.runner.signal.signal", side_effect=_patched_signal):
				name, status = _run_case_worker(str(case_dir), 5, debug=False)
		assert name == "late_alarm_case", f"expected case name, got {name!r}"
		assert status.startswith("FAIL (timeout"), f"expected timeout failure, got {status!r}"
		# Verify alarm state is clean: no pending alarm, handler restored.
		assert signal.alarm(0) == 0, "pending alarm leaked after late timeout"
		restored = signal.getsignal(signal.SIGALRM)
		assert restored == sentinel_handler, f"handler not restored: {restored!r}"


def test_late_timeout_does_not_contaminate_next_case() -> None:
	"""After a late-timeout case, the next case in the same chunk runs
	with clean alarm state and succeeds normally."""
	_first_ign = [True]
	_call_count = [0]

	original_signal_fn = signal.signal

	def _patched_run_case(*a, **kw):
		_call_count[0] += 1
		if _call_count[0] == 1:
			return "ok"  # completes fine, late alarm will fire in cleanup
		return "ok"  # second case should also succeed

	def _patched_signal(signum, handler):
		if signum == signal.SIGALRM and handler == signal.SIG_IGN and _first_ign[0]:
			_first_ign[0] = False
			raise TimeoutError("timeout after 5s")
		return original_signal_fn(signum, handler)

	with tempfile.TemporaryDirectory() as tmp:
		case_a = _make_case_dir(Path(tmp), "late_alarm_case")
		case_b = _make_case_dir(Path(tmp), "clean_case")
		with mock.patch("lang.tests.codegen.e2e.runner._run_case", side_effect=_patched_run_case):
			with mock.patch("lang.tests.codegen.e2e.runner.signal.signal", side_effect=_patched_signal):
				results = _run_case_chunk([str(case_a), str(case_b)], 5, debug=False)
		assert len(results) == 2
		assert results[0][0] == "late_alarm_case"
		assert results[0][1].startswith("FAIL (timeout")
		assert results[1][0] == "clean_case"
		assert results[1][1] == "ok", f"second case contaminated: {results[1][1]!r}"


def test_chunk_catches_escaped_worker_exception() -> None:
	"""_run_case_chunk returns a named failure even if _run_case_worker raises."""
	with tempfile.TemporaryDirectory() as tmp:
		case_dir = _make_case_dir(Path(tmp), "exploding_case")
		def _raise_runtime(*a, **kw):
			raise RuntimeError("unexpected boom")
		with mock.patch("lang.tests.codegen.e2e.runner._run_case", side_effect=_raise_runtime):
			results = _run_case_chunk([str(case_dir)], 5, debug=False)
		assert len(results) == 1
		name, status = results[0]
		assert name == "exploding_case"
		assert "FAIL" in status


def test_chunk_continues_after_failed_case() -> None:
	"""A failing case in a chunk does not prevent subsequent cases from running."""
	call_count = [0]

	def _alternating_run_case(*a, **kw):
		call_count[0] += 1
		if call_count[0] == 1:
			raise RuntimeError("first case boom")
		return "ok"

	with tempfile.TemporaryDirectory() as tmp:
		case_a = _make_case_dir(Path(tmp), "case_a")
		case_b = _make_case_dir(Path(tmp), "case_b")
		with mock.patch("lang.tests.codegen.e2e.runner._run_case", side_effect=_alternating_run_case):
			results = _run_case_chunk([str(case_a), str(case_b)], 5, debug=False)
		assert len(results) == 2
		assert results[0][0] == "case_a"
		assert results[0][1].startswith("FAIL")
		assert results[1][0] == "case_b"
		assert results[1][1] == "ok"
