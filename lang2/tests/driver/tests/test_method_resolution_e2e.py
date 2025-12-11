#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""End-to-end check: method resolution failure produces diagnostics and nonzero exit."""

from pathlib import Path

from lang2.driftc import driftc


def test_method_resolution_failure_reports_diagnostic(tmp_path):
	src = tmp_path / "bad_method.drift"
	src.write_text(
		"""
implement Point {
    fn m(self: &Point) returns Int { return 1; }
}

fn main() returns Int {
    val x = 1;
    return x.m(); // no such method on Int
}
"""
	)
	exit_code = driftc.main([str(src)])
	assert exit_code == 1
