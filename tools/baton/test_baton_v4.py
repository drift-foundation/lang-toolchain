from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import baton_v4 as baton


REVIEWER_SEED = "1" * 32
IMPLEMENTER_SEED = "2" * 32


def _mailbox(tmp_path: Path) -> tuple[baton.Mailbox, Path]:
	work = tmp_path / "work"
	finding = work / "finding-example"
	finding.mkdir(parents=True)
	return baton.Mailbox(tmp_path), finding


def test_directed_handoff_claim_and_reply_are_role_addressed(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	published = box.send("reviewer", "implementer", "finding-example", b"Please implement.\n", actor="reviewer-root", seed=REVIEWER_SEED, kind="review", thread_id="return_authority", ttl=86400)
	message_name = Path(published["message"]).name
	assert message_name.startswith("PENDING-FROM-reviewer-TO-implementer-")
	envelope = json.loads((tmp_path / published["message"]).read_text(encoding="utf-8"))
	assert envelope["from_role"] == "reviewer"
	assert envelope["to_role"] == "implementer"
	assert envelope["thread_id"] == "return_authority"

	claimed = box.claim("implementer", message_name, actor="k", seed=IMPLEMENTER_SEED)
	assert claimed["claim"].startswith("CLAIMED-FROM-reviewer-TO-implementer-")
	assert not (tmp_path / published["message"]).exists()

	replied = box.respond("implementer", claimed["claim"], b"Ready for review.\n", actor="k", seed=IMPLEMENTER_SEED, close=False, to_role=None, destination_rel=None, kind="implementation", thread_id=None, outcome=None, ttl=86400)
	assert replied["outgoing_message"] is not None
	assert Path(replied["outgoing_message"]).name.startswith("PENDING-FROM-implementer-TO-reviewer-")
	assert not (tmp_path / "work" / claimed["claim"]).exists()


def test_broadcast_is_seen_not_claimed_and_only_author_expires_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	box, _ = _mailbox(tmp_path)
	published = box.send("reviewer", "all", "finding-example", b"Tooling changed.\n", actor="reviewer-root", seed=REVIEWER_SEED, kind="tooling_notice", thread_id="baton_v4", ttl=60)
	notice_name = Path(published["message"]).name
	assert notice_name.startswith("NOTICE-FROM-reviewer-TO-ALL-")
	envelope = json.loads((tmp_path / published["message"]).read_text(encoding="utf-8"))
	assert envelope["message_type"] == "notice"
	assert envelope["to_role"] == "ALL"
	assert envelope["expires_at"] > envelope["created_at"]

	with pytest.raises(baton.MailboxError, match="cannot be claimed"):
		box.claim("implementer", notice_name, actor="k", seed=IMPLEMENTER_SEED)
	assert box.scan("implementer", actor="k", seed=IMPLEMENTER_SEED)["unseen_notices"] == [notice_name]
	seen = box.see("implementer", notice_name, actor="k", seed=IMPLEMENTER_SEED)
	assert seen["status"] == "notice"
	assert box.scan("implementer", actor="k", seed=IMPLEMENTER_SEED)["unseen_notices"] == []

	with pytest.raises(baton.MailboxError, match="original author"):
		box.expire("implementer", notice_name, actor="k", seed=IMPLEMENTER_SEED)
	with pytest.raises(baton.MailboxError, match="has not expired"):
		box.expire("reviewer", notice_name, actor="reviewer-root", seed=REVIEWER_SEED)
	monkeypatch.setattr(baton, "_utc_now", lambda: dt.datetime(2100, 1, 1, tzinfo=dt.timezone.utc))
	expired = box.expire("reviewer", notice_name, actor="reviewer-root", seed=REVIEWER_SEED)
	assert expired["status"] == "expired"
	assert not (tmp_path / "work" / notice_name).exists()
	assert (tmp_path / "work" / expired["target_retained"]).is_file()


def test_singleton_role_claims_and_replies_without_actor_seed(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	published = box.send("implementer", "human", "finding-example", b"Approval requested.\n", actor="k", seed=IMPLEMENTER_SEED, kind="approval_request", thread_id="approval", ttl=86400)
	claimed = box.claim("human", Path(published["message"]).name, actor=None, seed=None)
	assert "-BY-slawomir-AT-" in claimed["claim"]
	assert "-SEED-" not in claimed["claim"]
	replied = box.respond("human", claimed["claim"], b"Approved.\n", actor=None, seed=None, close=False, to_role=None, destination_rel=None, kind="approval_decision", thread_id=None, outcome="approved", ttl=86400)
	assert replied["outgoing_message"] is not None
	assert Path(replied["outgoing_message"]).name.startswith("PENDING-FROM-human-TO-implementer-")
