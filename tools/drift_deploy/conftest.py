# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Test fixtures for `tools/drift_deploy/` tests.

The autouse `permissive_trust_store` fixture patches
`tools.drift_deploy.trust_loader.load_merged_trust_store` so CLI-
level tests (`drift build --source-rebuild` / `drift deploy` /
`drift prepare --check`) don't need a fixture-written
`drift/trust.json`.  Direct callers of `verify_lock_compatibility` /
`_compare_locks_for_check` must pass their own trust store — see
`tools/drift_deploy/_test_trust.PermissiveTrustStore`.

Scoped to `tools/drift_deploy/` — other test trees are unaffected.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.drift_deploy._test_trust import PermissiveTrustStore


@pytest.fixture(autouse=True)
def permissive_trust_store():
	store = PermissiveTrustStore()
	with patch(
		"tools.drift_deploy.trust_loader.load_merged_trust_store",
		return_value=store,
	):
		yield store
