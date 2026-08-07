"""Drift-side policy audit for the standalone Baton v6 deployment.

The reusable distribution lives outside drift-lang. This file owns only the
repository boundary: local participant bindings remain declared, no embedded
Baton copy returns, and the removed v5 filename transport stays gone.
"""

import os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _read(rel):
	with open(os.path.join(REPO, rel)) as handle:
		return handle.read()


def test_agents_md_has_no_v5_transport_instructions():
	text = _read("AGENTS.md")
	assert "work/mailbox" not in text
	assert "protocol 6" in text
	assert "locally deployed Baton distribution" in text


def test_agents_md_declares_lang_bindings():
	text = _read("AGENTS.md")
	assert "lang.reviewer" in text
	assert "lang.implementer" in text


def test_baton_distribution_is_not_embedded_in_drift_lang():
	assert not os.path.exists(os.path.join(REPO, "AGENTS-MAILBOX-PROTO.md")), \
		"the generic protocol belongs to the standalone Baton distribution"
	assert not os.path.exists(os.path.join(REPO, "tools", "baton")), \
		"drift-lang must not regain an embedded Baton distribution"


def test_v5_artifacts_are_gone():
	for name in ("tools/baton/baton_v5.py", "tools/baton/test_baton_v5.py",
	             "tools/baton/roles.json"):
		assert not os.path.exists(os.path.join(REPO, name)), f"{name} must stay deleted"


def test_gitignore_mailbox_entry_retired():
	text = _read(".gitignore")
	assert "/work/mailbox/" not in text
