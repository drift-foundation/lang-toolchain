# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Test fixtures for `tools/drift_deploy/` tests.

Two shims cover the boundary functions that would otherwise need
per-test setup for source-rebuild CLI paths.  They have different
scopes deliberately:

- `permissive_trust_store` is AUTOUSE — every test in this tree
  gets `tools.drift_deploy.trust_loader.load_merged_trust_store`
  patched to return a `PermissiveTrustStore`.  Preserved for the
  producer/staging path (orch invokes `drift deploy` against its
  own trust store).  Harmless on tests that don't exercise that
  path.

- `permissive_run_snapshot` is OPT-IN.  It patches
  `tools.drift_deploy.run_snapshot.load_run_snapshot` to return a
  `PermissiveRunSnapshot` that declares every `(pkg_id, version)`
  as matching whatever values the disk presents, AND sets
  `DRIFT_RUN_SNAPSHOT` to a sentinel path so the CLI's "snapshot
  required" check passes.  Tests that want this behaviour declare
  it as a parameter or apply `@pytest.mark.usefixtures
  ("permissive_run_snapshot")` at the class level.  Tests that
  pin snapshot-rejection, missing-snapshot hard-fail, or
  malformed-snapshot behaviour get NO patching — the 0.31.3
  contract is that source-rebuild without a valid snapshot MUST
  fail, and autouse masking would hide that contract.

Scoped to `tools/drift_deploy/` — other test trees are unaffected.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.drift_deploy._test_trust import PermissiveTrustStore


class _EveryEntryMatchesSet:
	"""`RunSnapshot.packages`-like sentinel; every key lookup returns
	an entry that matches whatever the caller supplies."""

	def __contains__(self, _key) -> bool:
		return True

	def get(self, _key, default=None):
		# Return a sentinel entry whose fields will be compared by
		# value against the disk side; the snapshot-matches-anything
		# behaviour is implemented in `PermissiveRunSnapshot.lookup`
		# below, NOT here.  `.packages.get()` is only read inside
		# the test-unfriendly `verify_disk_entry_against_snapshot`,
		# which we don't go through under the lookup API.
		return default


class PermissiveRunSnapshot:
	"""Duck-typed `RunSnapshot` that accepts every disk entry.

	`lookup(pkg_id, version)` returns an entry pre-populated with
	the same fields the caller is about to compare against — so
	every `verify_disk_entry_against_snapshot` call matches.  Used
	by the OPT-IN `permissive_run_snapshot` fixture for source-
	rebuild CLI-path tests that don't care about snapshot
	verification semantics; tests that pin rejection (missing
	snapshot / snapshot mismatch / malformed snapshot) get no
	patching and construct their own real `RunSnapshot` state
	instead.
	"""

	__slots__ = ("run_id", "packages")

	def __init__(self) -> None:
		self.run_id = "test-permissive"
		self.packages = _EveryEntryMatchesSet()

	def lookup(self, _pkg_id, _version):
		# Return a stand-in whose fields will later be compared
		# for equality to disk values.  The easiest way to make
		# all comparisons succeed is to delegate to a mock-like
		# object whose attributes equal whatever is compared.
		return _EveryFieldMatches()

	def has(self, _pkg_id, _version) -> bool:
		return True


class _EveryFieldMatches:
	"""Sentinel with `__eq__` that returns True for any RHS.

	Used as the fake `SnapshotEntry` returned by
	`PermissiveRunSnapshot.lookup`: its `source_content_id`,
	`author_key`, `source_attestation_key` all compare equal to
	whatever the caller's disk-side value is.
	"""

	class _AlwaysEqual:
		def __eq__(self, _other) -> bool:
			return True
		def __hash__(self) -> int:
			return 0
		def __repr__(self) -> str:
			return "<PermissiveRunSnapshot-match>"

	_ALWAYS = _AlwaysEqual()
	source_content_id = _ALWAYS
	author_key = _ALWAYS
	source_attestation_key = _ALWAYS
	sha256 = _ALWAYS


@pytest.fixture(autouse=True)
def permissive_trust_store():
	store = PermissiveTrustStore()
	with patch(
		"tools.drift_deploy.trust_loader.load_merged_trust_store",
		return_value=store,
	):
		yield store


@pytest.fixture
def permissive_run_snapshot(monkeypatch):
	"""OPT-IN fixture: make source-rebuild CLI paths succeed without
	a real snapshot file.  Sets `DRIFT_RUN_SNAPSHOT` to a sentinel
	path so the CLI's "snapshot required" check passes, then patches
	`load_run_snapshot` to return a `PermissiveRunSnapshot` that
	matches any disk entry.

	Tests that need this fixture declare it as a parameter:

	    def test_something(self, permissive_run_snapshot): ...

	Tests that DO NOT declare it — including every test exercising
	the "no snapshot → hard fail" contract, snapshot mismatch, or
	malformed snapshot — get no patching.  This is intentional:
	the 0.31.3 model's whole point is that source-rebuild without
	a valid snapshot MUST fail, and an autouse permissive shim
	would mask that contract in every CLI-path test.
	"""
	snap = PermissiveRunSnapshot()
	import os as _os
	if "DRIFT_RUN_SNAPSHOT" not in _os.environ:
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", "/nonexistent/test-snapshot.json")
	with patch(
		"tools.drift_deploy.run_snapshot.load_run_snapshot",
		return_value=snap,
	):
		yield snap
