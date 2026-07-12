# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Unit pins for the e2e runner's `_effective_case_timeout`.

`timeout_s` in a case's expected.json is a BASE budget and must receive
the same `sanitizer_timeout()` scaling as the runner default (host
DRIFT_TEST_TIMEOUT_SCALE, sanitizer lanes, xdist).  Pre-fix it was
returned verbatim, so on a scaled host an OVERRIDDEN case became
tighter than an un-overridden one — `treemap_iter_order` (override 40 ==
the default's base 40) timed out at 40s while default cases ran at 120s
under DRIFT_TEST_TIMEOUT_SCALE=3 (slow-box full-suite failure,
2026-07-13).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.tests.codegen.e2e.runner import _effective_case_timeout
from lang.codegen.llvm.test_utils import sanitizer_timeout


def _case(tmp_path: Path, expected: dict | str | None) -> Path:
	d = tmp_path / "case"
	d.mkdir(parents=True, exist_ok=True)
	if expected is not None:
		text = expected if isinstance(expected, str) else json.dumps(expected)
		(d / "expected.json").write_text(text)
	return d


def _clean_scale_env(monkeypatch) -> None:
	"""Make sanitizer_timeout's environment deterministic for the pin:
	only the host-scale knob set, no lane/xdist multipliers."""
	for var in ("DRIFT_ASAN", "DRIFT_UBSAN", "DRIFT_MEMCHECK", "PYTEST_XDIST_WORKER"):
		monkeypatch.delenv(var, raising=False)


def test_override_receives_host_scale(tmp_path: Path, monkeypatch) -> None:
	_clean_scale_env(monkeypatch)
	monkeypatch.setenv("DRIFT_TEST_TIMEOUT_SCALE", "3")
	case = _case(tmp_path, {"exit_code": 0, "timeout_s": 40})
	# The treemap_iter_order shape: override base 40 under scale 3 -> 120.
	assert _effective_case_timeout(case, sanitizer_timeout(40)) == 120


def test_override_never_tighter_than_unoverridden_default(tmp_path: Path, monkeypatch) -> None:
	_clean_scale_env(monkeypatch)
	monkeypatch.setenv("DRIFT_TEST_TIMEOUT_SCALE", "3")
	scaled_default = sanitizer_timeout(40)
	overridden = _effective_case_timeout(_case(tmp_path, {"timeout_s": 40}), scaled_default)
	unoverridden = _effective_case_timeout(_case(tmp_path / "plain", None), scaled_default)
	assert overridden >= unoverridden, (
		f"an override equal to the default base must not be tighter than "
		f"no override: {overridden} < {unoverridden}"
	)


@pytest.mark.parametrize("expected", [
	None,                                  # no expected.json at all
	{},                                    # no timeout_s key
	{"timeout_s": "soon"},                 # non-int
	{"timeout_s": 0},                      # non-positive
	{"timeout_s": -5},
	"not json {",                          # unparseable file
])
def test_fallbacks_return_scaled_default_untouched(tmp_path: Path, monkeypatch, expected) -> None:
	_clean_scale_env(monkeypatch)
	monkeypatch.setenv("DRIFT_TEST_TIMEOUT_SCALE", "3")
	# default_timeout_s arrives ALREADY scaled by the argparse default;
	# fallbacks must pass it through without double-scaling.
	case = _case(tmp_path, expected)
	assert _effective_case_timeout(case, 120) == 120


def test_override_composes_with_lane_multipliers(tmp_path: Path, monkeypatch) -> None:
	"""Sanitizer-lane scaling applies to overrides too (the same path as
	the default budget)."""
	_clean_scale_env(monkeypatch)
	monkeypatch.delenv("DRIFT_TEST_TIMEOUT_SCALE", raising=False)
	monkeypatch.setenv("DRIFT_MEMCHECK", "1")
	case = _case(tmp_path, {"timeout_s": 40})
	assert _effective_case_timeout(case, sanitizer_timeout(40)) == 80
