# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Repo-root pytest hooks.

Long-tail mitigation: tests marked @pytest.mark.heavy are sorted to the
front of the collection so xdist (with --dist=worksteal) dispatches them
to workers first. This prevents a slow test from becoming the lone
straggler at the end of a run while 15 other workers sit idle.

Tag a test with @pytest.mark.heavy when --durations=N reveals it as a
top contributor to wall time.
"""
from __future__ import annotations


def pytest_collection_modifyitems(config, items):
	items.sort(key=lambda it: 0 if it.get_closest_marker("heavy") else 1)
