"""Drift-side deployment audit for the Baton v6 cutover.

The reusable package under tools/baton/ stays host-neutral; THIS file owns
the repository-side contracts: the permanent docs and launcher must never
regress to the removed v5 filename transport, and this project's participant
bindings stay declared.
"""

import os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _read(rel):
	with open(os.path.join(REPO, rel)) as handle:
		return handle.read()


def test_agents_md_has_no_v5_transport_instructions():
	text = _read("AGENTS.md")
	assert "work/mailbox" not in text
	assert "protocol 6" in text or "AGENTS-MAILBOX-PROTO" in text


def test_agents_md_declares_lang_bindings():
	text = _read("AGENTS.md")
	assert "lang.reviewer" in text
	assert "lang.implementer" in text


def test_generic_proto_lives_with_the_distribution_only():
	assert not os.path.exists(os.path.join(REPO, "AGENTS-MAILBOX-PROTO.md")), \
		"the generic protocol lives with the Baton distribution, not the repo root"
	text = _read("tools/baton/AGENTS-MAILBOX-PROTO.md")
	assert "protocol 6" in text
	assert "work/mailbox" not in text
	assert "PENDING-FROM" not in text


def test_launcher_targets_v6():
	text = _read("tools/baton/baton")
	assert "baton_v6" in text
	assert "baton_v5" not in text


def test_v5_artifacts_are_gone():
	for name in ("tools/baton/baton_v5.py", "tools/baton/test_baton_v5.py",
	             "tools/baton/roles.json"):
		assert not os.path.exists(os.path.join(REPO, name)), f"{name} must stay deleted"


def test_gitignore_mailbox_entry_retired():
	text = _read(".gitignore")
	assert "/work/mailbox/" not in text
