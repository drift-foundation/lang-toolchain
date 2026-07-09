# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Unit pins for `drift_tmp.session_root(base=...)` — the repo-local
disk-backed gate scratch root (tmpfs-exhaustion guard, 2026-07-08
ENOSPC incident).

Contract:
  1. An explicit $DRIFT_TMP_ROOT always wins — `base` is ignored.
  2. With the env unset and `base` given, the session dir lands under
     `base` with the janitor-safe `session-<pid>-<ts>` layout, and the
     choice is exported so children inherit it.
  3. With the env unset and no `base`, the legacy `/tmp/drift-$USER/`  ## drift-tmp-root-audit: allow docs contract description
     namespace still applies (non-repo/direct tooling keeps its
     behavior).

These tests must not disturb the ambient session root, so they
save/restore the env var around each scenario (the module under test
mutates os.environ by design).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from lang.test_support.drift_tmp import session_root

_ENV = "DRIFT_TMP_ROOT"
_SESSION_RE = re.compile(r"^session-\d+-\d+$")


@pytest.fixture
def _isolated_env():
	saved = os.environ.get(_ENV)
	try:
		yield
	finally:
		if saved is None:
			os.environ.pop(_ENV, None)
		else:
			os.environ[_ENV] = saved


def test_explicit_env_override_wins_over_base(_isolated_env, tmp_path: Path) -> None:
	override = tmp_path / "user-chosen-root"
	os.environ[_ENV] = str(override)
	got = session_root(base=tmp_path / "ignored-base")
	assert got == override
	assert got.is_dir()
	assert not (tmp_path / "ignored-base").exists()


def test_base_used_when_env_unset(_isolated_env, tmp_path: Path) -> None:
	os.environ.pop(_ENV, None)
	base = tmp_path / "build" / "tmp"
	got = session_root(base=base)
	assert got.parent == base
	assert _SESSION_RE.match(got.name), got.name
	assert got.is_dir()
	# Exported for child processes.
	assert os.environ[_ENV] == str(got)


def test_legacy_tmp_namespace_without_base(_isolated_env) -> None:
	os.environ.pop(_ENV, None)
	got = session_root()
	try:
		assert str(got).startswith("/tmp/drift-"), got  # drift-tmp-root-audit: allow negative-test asserts the legacy namespace shape, dir created then removed
		assert _SESSION_RE.match(got.name), got.name
	finally:
		# Don't leave an empty legacy session dir behind.
		try:
			got.rmdir()
		except OSError:
			pass


def test_second_call_reuses_first_choice(_isolated_env, tmp_path: Path) -> None:
	os.environ.pop(_ENV, None)
	first = session_root(base=tmp_path / "b1")
	second = session_root(base=tmp_path / "b2")
	assert first == second
	assert not (tmp_path / "b2").exists()
