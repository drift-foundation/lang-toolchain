# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.tests.codegen.e2e.runner import _compare_process_output
from lang.tests.codegen.e2e.runner import _asan_options_with_defaults


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
