from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import baton_v5 as baton


REVIEWER_SEED = "1" * 32
IMPLEMENTER_SEED = "2" * 32


def _mailbox(tmp_path: Path) -> tuple[baton.Mailbox, Path]:
	work = tmp_path / "work"
	finding = work / "finding-example"
	finding.mkdir(parents=True)
	box = baton.Mailbox(tmp_path)
	assert box.mailbox == work / "mailbox"
	assert box.mailbox.is_dir()
	return box, finding


def test_directed_handoff_claim_and_reply_are_role_addressed(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	published = box.send("reviewer", "implementer", "finding-example", b"Please implement.\n", actor="reviewer-root", seed=REVIEWER_SEED, retention="durable", kind="review", thread_id="return_authority", outcome=None, ttl=86400)
	message_name = Path(published["message"]).name
	assert message_name.startswith("PENDING-FROM-reviewer-TO-implementer-")
	envelope = json.loads((tmp_path / published["message"]).read_text(encoding="utf-8"))
	assert Path(published["message"]).parent == Path("work/mailbox")
	assert not any(path.name.startswith("PENDING-") for path in (tmp_path / "work").iterdir())
	assert envelope["from_role"] == "reviewer"
	assert envelope["to_role"] == "implementer"
	assert envelope["thread_id"] == "return_authority"
	assert envelope["protocol_version"] == 5
	assert envelope["retention"] == "durable"
	assert envelope["outcome"] is None
	assert envelope["responds_to"] is None
	assert "target" in envelope
	assert "body" not in envelope

	claimed = box.claim("implementer", message_name, actor="k", seed=IMPLEMENTER_SEED)
	assert claimed["claim"].startswith("CLAIMED-FROM-reviewer-TO-implementer-")
	assert not (tmp_path / published["message"]).exists()

	replied = box.respond("implementer", claimed["claim"], b"Ready for review.\n", actor="k", seed=IMPLEMENTER_SEED, close=False, to_role=None, destination_rel=None, retention=None, kind="implementation", thread_id=None, outcome=None, ttl=86400)
	assert replied["outgoing_message"] is not None
	assert replied["retention"] == "durable"
	assert (tmp_path / replied["detail"]).is_file()
	assert Path(replied["outgoing_message"]).name.startswith("PENDING-FROM-implementer-TO-reviewer-")
	assert not (tmp_path / "work" / "mailbox" / claimed["claim"]).exists()


def test_broadcast_is_seen_not_claimed_and_only_author_expires_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	box, _ = _mailbox(tmp_path)
	published = box.send("reviewer", "all", "finding-example", b"Tooling changed.\n", actor="reviewer-root", seed=REVIEWER_SEED, retention="durable", kind="tooling_notice", thread_id="baton_v5", outcome=None, ttl=60)
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
	assert not (tmp_path / "work" / "mailbox" / notice_name).exists()
	assert (tmp_path / "work" / expired["target_retained"]).is_file()


def test_singleton_role_claims_and_replies_without_actor_seed(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	published = box.send("implementer", "human", "finding-example", b"Approval requested.\n", actor="k", seed=IMPLEMENTER_SEED, retention="durable", kind="approval_request", thread_id="approval", outcome=None, ttl=86400)
	claimed = box.claim("human", Path(published["message"]).name, actor=None, seed=None)
	assert "-BY-slawomir-AT-" in claimed["claim"]
	assert "-SEED-" not in claimed["claim"]
	replied = box.respond("human", claimed["claim"], b"Approved.\n", actor=None, seed=None, close=False, to_role=None, destination_rel=None, retention=None, kind="approval_decision", thread_id=None, outcome="approved", ttl=86400)
	assert replied["outgoing_message"] is not None
	assert Path(replied["outgoing_message"]).name.startswith("PENDING-FROM-human-TO-implementer-")


def test_transient_handoff_embeds_body_and_inherited_reply_is_consumable(tmp_path: Path) -> None:
	box, finding = _mailbox(tmp_path)
	published = box.send("reviewer", "implementer", None, b"Short-lived request.\n", actor="reviewer-root", seed=REVIEWER_SEED, retention="transient", kind="status", thread_id="stream", outcome=None, ttl=86400)
	assert published["detail"] is None
	message_name = Path(published["message"]).name
	envelope = json.loads((tmp_path / published["message"]).read_text(encoding="utf-8"))
	assert envelope["retention"] == "transient"
	assert envelope["body"] == "Short-lived request.\n"
	assert "target" not in envelope
	assert list(finding.iterdir()) == []

	claimed = box.claim("implementer", message_name, actor="k", seed=IMPLEMENTER_SEED)
	assert claimed["retention"] == "transient"
	assert claimed["body"] == "Short-lived request.\n"
	replied = box.respond("implementer", claimed["claim"], b"Short-lived response.\n", actor="k", seed=IMPLEMENTER_SEED, close=False, to_role=None, destination_rel=None, retention=None, kind="status", thread_id=None, outcome="ready", ttl=86400)
	assert replied["retention"] == "transient"
	assert replied["detail"] is None
	assert not (tmp_path / "work" / "mailbox" / claimed["claim"]).exists()
	outgoing = tmp_path / replied["outgoing_message"]
	outgoing_envelope = json.loads(outgoing.read_text(encoding="utf-8"))
	assert outgoing_envelope["body"] == "Short-lived response.\n"
	assert outgoing_envelope["responds_to"] == claimed["claim"]
	assert outgoing_envelope["outcome"] == "ready"

	reviewer_claim = box.claim("reviewer", outgoing.name, actor="reviewer-root", seed=REVIEWER_SEED)
	closed = box.respond("reviewer", reviewer_claim["claim"], b"Consumed.\n", actor="reviewer-root", seed=REVIEWER_SEED, close=True, to_role=None, destination_rel=None, retention=None, kind="close", thread_id=None, outcome="accepted", ttl=86400)
	assert closed["retention"] == "transient"
	assert closed["detail"] is None
	assert closed["outgoing_message"] is None
	assert not (tmp_path / "work" / "mailbox" / reviewer_claim["claim"]).exists()
	assert list(finding.iterdir()) == []


def test_message_stream_allows_multiple_independent_handoffs_in_one_thread(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	first = box.send("reviewer", "implementer", None, b"Status one.\n", actor="reviewer-root", seed=REVIEWER_SEED, retention="transient", kind="status", thread_id="same_thread", outcome=None, ttl=86400)
	second = box.send("reviewer", "implementer", None, b"Status two.\n", actor="reviewer-root", seed=REVIEWER_SEED, retention="transient", kind="results", thread_id="same_thread", outcome=None, ttl=86400)
	assert first["message"] != second["message"]
	assert box.scan("implementer", actor="k", seed=IMPLEMENTER_SEED)["eligible_pending"] == sorted((Path(first["message"]).name, Path(second["message"]).name))


def test_response_can_explicitly_switch_from_durable_to_transient(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	published = box.send("reviewer", "implementer", "finding-example", b"Durable review.\n", actor="reviewer-root", seed=REVIEWER_SEED, retention="durable", kind="review", thread_id="retention_switch", outcome=None, ttl=86400)
	original_detail = tmp_path / published["detail"]
	claimed = box.claim("implementer", Path(published["message"]).name, actor="k", seed=IMPLEMENTER_SEED)
	replied = box.respond("implementer", claimed["claim"], b"Ephemeral acknowledgement.\n", actor="k", seed=IMPLEMENTER_SEED, close=False, to_role=None, destination_rel=None, retention="transient", kind="ack", thread_id=None, outcome=None, ttl=86400)
	assert replied["retention"] == "transient"
	assert replied["detail"] is None
	assert original_detail.is_file()
	outgoing = json.loads((tmp_path / replied["outgoing_message"]).read_text(encoding="utf-8"))
	assert outgoing["retention"] == "transient"
	assert outgoing["body"] == "Ephemeral acknowledgement.\n"


def test_response_switch_from_transient_to_durable_requires_destination(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	published = box.send("reviewer", "implementer", None, b"Transient request.\n", actor="reviewer-root", seed=REVIEWER_SEED, retention="transient", kind="status", thread_id="retention_switch", outcome=None, ttl=86400)
	claimed = box.claim("implementer", Path(published["message"]).name, actor="k", seed=IMPLEMENTER_SEED)
	with pytest.raises(baton.MailboxError, match="requires --destination"):
		box.respond("implementer", claimed["claim"], b"Durable report.\n", actor="k", seed=IMPLEMENTER_SEED, close=False, to_role=None, destination_rel=None, retention="durable", kind="implementation", thread_id=None, outcome=None, ttl=86400)
	assert (tmp_path / "work" / "mailbox" / claimed["claim"]).is_file()
	replied = box.respond("implementer", claimed["claim"], b"Durable report.\n", actor="k", seed=IMPLEMENTER_SEED, close=False, to_role=None, destination_rel="finding-example", retention="durable", kind="implementation", thread_id=None, outcome=None, ttl=86400)
	assert replied["retention"] == "durable"
	assert (tmp_path / replied["detail"]).is_file()


def test_transient_broadcast_body_is_removed_when_author_expires_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	box, finding = _mailbox(tmp_path)
	published = box.send("reviewer", "all", None, b"Read the v5 protocol.\n", actor="reviewer-root", seed=REVIEWER_SEED, retention="transient", kind="tooling_notice", thread_id="baton_v5", outcome=None, ttl=60)
	notice_name = Path(published["message"]).name
	assert published["detail"] is None
	assert box.see("implementer", notice_name, actor="k", seed=IMPLEMENTER_SEED)["body"] == "Read the v5 protocol.\n"
	monkeypatch.setattr(baton, "_utc_now", lambda: dt.datetime(2100, 1, 1, tzinfo=dt.timezone.utc))
	expired = box.expire("reviewer", notice_name, actor="reviewer-root", seed=REVIEWER_SEED)
	assert expired == {"status": "expired", "notice": notice_name, "retention": "transient", "target_retained": None, "transient_body_removed": True}
	assert not (tmp_path / "work" / "mailbox" / notice_name).exists()
	assert list(finding.iterdir()) == []


def test_transient_publish_rejects_destination_empty_oversized_and_non_utf8_bodies(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	common = {"actor": "reviewer-root", "seed": REVIEWER_SEED, "retention": "transient", "kind": "status", "thread_id": "validation", "outcome": None, "ttl": 86400}
	with pytest.raises(baton.MailboxError, match="does not accept a detail destination"):
		box.send("reviewer", "implementer", "finding-example", b"Body.\n", **common)
	with pytest.raises(baton.MailboxError, match="empty"):
		box.send("reviewer", "implementer", None, b" \n", **common)
	with pytest.raises(baton.MailboxError, match="exceeds"):
		box.send("reviewer", "implementer", None, b"x" * (baton.TRANSIENT_BODY_MAX_BYTES + 1), **common)
	with pytest.raises(baton.MailboxError, match="valid UTF-8"):
		box.send("reviewer", "implementer", None, b"\xff", **common)


def test_cli_transient_send_takes_body_file_without_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
	_mailbox(tmp_path)
	body_path = tmp_path / "body.txt"
	body_path.write_text("Inline envelope body.\n", encoding="utf-8")
	monkeypatch.setenv("BATON_REPO_ROOT", str(tmp_path))
	rc = baton.main(["reviewer", "send", "implementer", str(body_path), "--retention", "transient", "--kind", "status", "--actor", "reviewer-root", "--seed", REVIEWER_SEED, "--json"])
	assert rc == 0
	result = json.loads(capsys.readouterr().out)
	assert result["retention"] == "transient"
	assert result["detail"] is None
	envelope = json.loads((tmp_path / result["message"]).read_text(encoding="utf-8"))
	assert envelope["body"] == "Inline envelope body.\n"


def test_durable_details_cannot_use_work_root_or_mailbox(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	common = {"actor": "reviewer-root", "seed": REVIEWER_SEED, "retention": "durable", "kind": "review", "thread_id": "detail_boundary", "outcome": None, "ttl": 86400}
	with pytest.raises(baton.MailboxError, match="detail subdirectory"):
		box.send("reviewer", "implementer", ".", b"Do not publish at work root.\n", **common)
	with pytest.raises(baton.MailboxError, match="work/mailbox"):
		box.send("reviewer", "implementer", "mailbox", b"Do not retain in transport.\n", **common)


def test_doctor_reports_transport_stranded_at_old_work_root(tmp_path: Path) -> None:
	box, _ = _mailbox(tmp_path)
	stranded = tmp_path / "work" / "PENDING-FROM-reviewer-TO-implementer-2026-08-05T00-00-00Z-0123456789ab"
	stranded.write_text("{}\n", encoding="utf-8")
	result = box.doctor()
	assert result["checked"] == [stranded.name]
	assert result["errors"] == [f"{stranded.name}: mailbox transport is outside work/mailbox/ and requires explicit human recovery"]
