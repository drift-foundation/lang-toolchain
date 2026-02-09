# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.tests.codegen.e2e.runner import _compare_process_output


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
