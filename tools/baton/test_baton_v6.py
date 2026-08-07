"""Protocol-6 Baton storage-core tests (Handoff 1: T1-T6, T9, T10, T16-T18, T23-T25 core).

Fixtures are deliberately neutral (no host-project names): a small
multi-workspace shop with participants under `acme.*` and `hq.*`.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3

import pytest

import baton_v6 as b6

SEED_A = "a" * 32
SEED_B = "b" * 32
SEED_C = "c" * 32


def make_config(generation: int = 1) -> dict:
	return {
		"config_version": 1,
		"protocol_version": 6,
		"generation": generation,
		"mailbox": {"name": "acme-local"},
		"participants": {
			"acme.reviewer": {"identity": "agent"},
			"acme.implementer": {"identity": "agent"},
			"hq.lead": {"identity": "singleton", "singleton_actor": "lead",
			            "capabilities": ["recovery", "config"]},
		},
		"roots": {},
	}


@pytest.fixture
def instance(tmp_path):
	config_path = str(tmp_path / "baton.json")
	with open(config_path, "w") as handle:
		json.dump(make_config(), handle)
	b6.init_instance(config_path)
	return config_path


@pytest.fixture
def store(instance):
	st = b6.open_instance(instance)
	yield st
	st.close()


def send_one(store, body=b"hello", retention="durable", sender="acme.reviewer",
             recipient="acme.implementer", kind="question", thread="topic-1"):
	return store.send(sender, recipient, actor="rev1", seed=SEED_A, kind=kind,
	                  body=body, thread_id=thread, retention=retention)


# ---------------------------------------------------------------------------
# init / open validation (T10, T22-core, T24)
# ---------------------------------------------------------------------------

class TestInitOpen:
	def test_init_creates_wal_instance_beside_config(self, instance, tmp_path):
		assert (tmp_path / "mailbox.sqlite3").is_file()
		with b6.open_instance(instance) as st:
			assert st.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
			row = st.conn.execute("SELECT * FROM instance_meta").fetchone()
			assert row["protocol"] == 6
			assert row["accepted_generation"] == 1

	def test_init_refuses_existing_db(self, instance):
		with pytest.raises(b6.BatonError, match="refusing to initialize"):
			b6.init_instance(instance)

	def test_open_without_db_fails_closed_never_creates(self, tmp_path):
		config_path = str(tmp_path / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		with pytest.raises(b6.BatonError, match="run init"):
			b6.open_instance(config_path)
		assert not (tmp_path / "mailbox.sqlite3").exists()

	def test_symlink_db_refused(self, tmp_path):
		config_path = str(tmp_path / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		real = tmp_path / "elsewhere.sqlite3"
		real.write_bytes(b"")
		os.symlink(real, tmp_path / "mailbox.sqlite3")
		with pytest.raises(b6.BatonError, match="symlink"):
			b6.open_instance(config_path)

	def test_symlink_config_refused(self, tmp_path):
		real = tmp_path / "real.json"
		with open(real, "w") as handle:
			json.dump(make_config(), handle)
		link = tmp_path / "baton.json"
		os.symlink(real, link)
		with pytest.raises(b6.BatonError, match="symlink"):
			b6.load_config(str(link))

	def test_wrong_journal_mode_refused(self, instance, tmp_path):
		conn = sqlite3.connect(tmp_path / "mailbox.sqlite3")
		conn.execute("PRAGMA journal_mode=DELETE")
		conn.close()
		with pytest.raises(b6.BatonError, match="journal_mode"):
			b6.open_instance(instance)

	def test_user_version_gate(self, instance, tmp_path):
		conn = sqlite3.connect(tmp_path / "mailbox.sqlite3")
		conn.execute("PRAGMA user_version=5")
		conn.close()
		with pytest.raises(b6.BatonError, match="protocol 5"):
			b6.open_instance(instance)

	def test_schema_drift_refused(self, instance, tmp_path):
		conn = sqlite3.connect(tmp_path / "mailbox.sqlite3")
		conn.execute("DROP INDEX contents_sha_idx")
		conn.close()
		with pytest.raises(b6.BatonError, match="schema validation failed"):
			b6.open_instance(instance)

	def test_corrupted_db_fails_closed(self, instance, tmp_path):
		db = tmp_path / "mailbox.sqlite3"
		with open(db, "r+b") as handle:
			handle.seek(0)
			handle.write(b"\x00" * 32)
		with pytest.raises(b6.BatonError):
			b6.open_instance(instance)

	def test_config_generation_mismatch_refused(self, instance, tmp_path):
		with open(instance, "w") as handle:
			json.dump(make_config(generation=2), handle)
		with pytest.raises(b6.BatonError, match="regen"):
			b6.open_instance(instance)

	def test_config_content_drift_refused(self, instance, tmp_path):
		cfg = make_config()
		cfg["participants"]["acme.newcomer"] = {"identity": "agent"}
		with open(instance, "w") as handle:
			json.dump(cfg, handle)
		with pytest.raises(b6.BatonError, match="digest"):
			b6.open_instance(instance)

	def test_readonly_store_rejects_writes(self, instance):
		with b6.open_instance(instance, readonly=True) as st:
			with pytest.raises(b6.BatonError, match="read-only"):
				send_one(st)

	def test_sidecars_beside_db(self, instance, tmp_path):
		with b6.open_instance(instance) as st:
			send_one(st)
			names = {p.name for p in tmp_path.iterdir()}
			assert "mailbox.sqlite3-wal" in names


class TestStrictJson:
	def test_duplicate_keys_rejected(self):
		with pytest.raises(b6.BatonError, match="duplicate"):
			b6.loads_strict('{"a": 1, "a": 2}')

	def test_nan_rejected(self):
		with pytest.raises(b6.BatonError, match="non-finite"):
			b6.loads_strict('{"a": NaN}')

	def test_trailing_content_rejected(self):
		with pytest.raises(b6.BatonError, match="parse error"):
			b6.loads_strict('{"a": 1} trailing')

	def test_bool_is_not_int(self):
		cfg = make_config()
		cfg["generation"] = True
		with pytest.raises(b6.BatonError, match="integer"):
			b6.validate_config(cfg)

	def test_unknown_field_rejected(self):
		cfg = make_config()
		cfg["surprise"] = 1
		with pytest.raises(b6.BatonError, match="unknown field"):
			b6.validate_config(cfg)

	def test_unknown_participant_field_rejected(self):
		cfg = make_config()
		cfg["participants"]["acme.reviewer"]["extra"] = 1
		with pytest.raises(b6.BatonError, match="unknown field"):
			b6.validate_config(cfg)


# ---------------------------------------------------------------------------
# send / claim (T1, T9)
# ---------------------------------------------------------------------------

class TestSendClaim:
	def test_send_then_claim_roundtrip(self, store):
		mid = send_one(store, body=b"payload")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		assert claim["message_id"] == mid
		assert claim["state"] == "active"
		msg = store.get_message(mid)
		assert msg["state"] == "claimed"
		assert msg["body"] == b"payload"

	def test_claim_empty_mailbox_is_none(self, store):
		with pytest.raises(b6.BatonError) as excinfo:
			store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		assert excinfo.value.exit_code == b6.EXIT_NONE

	def test_undeclared_participants_rejected(self, store):
		with pytest.raises(b6.BatonError, match="not declared"):
			store.send("acme.reviewer", "acme.ghost", actor="rev1", seed=SEED_A,
			           kind="question", body=b"x")
		with pytest.raises(b6.BatonError, match="not declared"):
			store.send("acme.ghost", "acme.reviewer", actor="rev1", seed=SEED_A,
			           kind="question", body=b"x")

	def test_singleton_actor_enforced(self, store):
		with pytest.raises(b6.BatonError, match="singleton"):
			store.send("hq.lead", "acme.reviewer", actor="intruder", seed=SEED_C,
			           kind="ruling", body=b"x")
		store.send("hq.lead", "acme.reviewer", actor="lead", seed=SEED_C,
		           kind="ruling", body=b"x")

	def test_actor_grammar_and_budget(self, store):
		with pytest.raises(b6.BatonError, match="invalid actor"):
			store.claim("acme.implementer", actor="Bad", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="invalid actor"):
			store.claim("acme.implementer", actor="a" * 33, seed=SEED_B)
		with pytest.raises(b6.BatonError, match="invalid seed"):
			store.claim("acme.implementer", actor="imp1", seed="short")

	def test_body_xor_attach_exactly_one(self, store):
		with pytest.raises(b6.BatonError, match="exactly one"):
			store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			           kind="question", body=b"x", attach={"root_id": "r", "path": "p"})
		with pytest.raises(b6.BatonError, match="exactly one"):
			store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			           kind="question", body=None)
		store._txn_begin("send", "rev1", SEED_A)
		try:
			with pytest.raises(sqlite3.IntegrityError):
				store.conn.execute(
					"INSERT INTO messages(id, from_participant, to_participant, kind, retention, "
					"created_ts, state) VALUES('nobody1', 'acme.reviewer', 'acme.implementer', "
					"'k', 'durable', 'now', 'pending')")
		finally:
			store._txn_rollback()

	def test_transient_body_cap(self, store):
		with pytest.raises(b6.BatonError, match="exceeds"):
			send_one(store, body=b"x" * (b6.TRANSIENT_BODY_MAX_BYTES + 1), retention="transient")


def _race_claim(config_path, results):
	try:
		with b6.open_instance(config_path) as st:
			claim = st.claim("acme.implementer", actor=f"imp{os.getpid() % 97}", seed=SEED_B)
			results.put(("won", claim["claim_id"]))
	except b6.BatonError as exc:
		results.put(("lost", exc.exit_code))


class TestClaimRace:
	def test_concurrent_claim_single_winner(self, instance):
		with b6.open_instance(instance) as st:
			send_one(st)
		ctx = multiprocessing.get_context("spawn")
		results = ctx.Queue()
		procs = [ctx.Process(target=_race_claim, args=(instance, results)) for _ in range(8)]
		for p in procs:
			p.start()
		for p in procs:
			p.join(60)
		outcomes = [results.get(timeout=10) for _ in procs]
		wins = [o for o in outcomes if o[0] == "won"]
		losses = [o for o in outcomes if o[0] == "lost"]
		assert len(wins) == 1
		assert len(losses) == 7
		assert all(code in (b6.EXIT_NONE, b6.EXIT_RACE) for _, code in losses)

	def test_partial_unique_index_backstop(self, store):
		mid = send_one(store)
		store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		store._txn_begin("claim", "imp2", SEED_C)
		try:
			with pytest.raises(sqlite3.IntegrityError):
				store.conn.execute(
					"INSERT INTO claims(claim_id, message_id, actor, seed, claimed_ts, state) "
					"VALUES(?, ?, 'imp2', ?, 'now', 'active')", (b6.new_id(), mid, SEED_C))
		finally:
			store._txn_rollback()


# ---------------------------------------------------------------------------
# reply / close / retry idempotence (T3, T4, T5)
# ---------------------------------------------------------------------------

class TestReplyClose:
	def test_reply_publishes_and_completes(self, store):
		mid = send_one(store, body=b"question?")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		result = store.reply(claim["claim_id"], actor="imp1", seed=SEED_B,
		                     kind="answer", body=b"the answer", outcome="done")
		assert result["already_committed"] is False
		out = store.get_message(result["response_message_id"])
		assert out["to_participant"] == "acme.reviewer"
		assert out["state"] == "pending"
		assert out["responds_to"] == mid
		assert out["body"] == b"the answer"
		assert store.get_message(mid)["state"] == "completed"
		assert store.get_claim(claim["claim_id"])["state"] == "completed"

	def test_reply_retry_is_redelivery_not_recreation(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		first = store.reply(claim["claim_id"], actor="imp1", seed=SEED_B,
		                    kind="answer", body=b"same bytes", outcome="ok")
		retry = store.reply(claim["claim_id"], actor="imp1", seed=SEED_B,
		                    kind="answer", body=b"same bytes", outcome="ok")
		assert retry["already_committed"] is True
		assert retry["response_message_id"] == first["response_message_id"]
		count = store.conn.execute(
			"SELECT COUNT(*) FROM messages WHERE responds_to IS NOT NULL").fetchone()[0]
		assert count == 1

	def test_reply_retry_content_mismatch_fails_closed(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.reply(claim["claim_id"], actor="imp1", seed=SEED_B,
		            kind="answer", body=b"committed", outcome="ok")
		with pytest.raises(b6.BatonError, match="content differs"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B,
			            kind="answer", body=b"different", outcome="ok")
		with pytest.raises(b6.BatonError, match="outcome differs"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B,
			            kind="answer", body=b"committed", outcome="changed")
		with pytest.raises(b6.BatonError, match="mismatches"):
			store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B, outcome="ok")

	def test_failed_reply_leaves_nothing(self, store):
		mid = send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="not declared"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B,
			            kind="answer", body=b"x", recipient="acme.ghost")
		assert store.get_message(mid)["state"] == "claimed"
		assert store._existing_disposition(claim["claim_id"]) is None
		result = store.reply(claim["claim_id"], actor="imp1", seed=SEED_B,
		                     kind="answer", body=b"x")
		assert result["already_committed"] is False

	def test_reply_wrong_owner_refused(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="owned by"):
			store.reply(claim["claim_id"], actor="imp2", seed=SEED_C, kind="answer", body=b"x")

	def test_close_with_outcome_and_body(self, store):
		mid = send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		result = store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
		                           body=b"final signoff", outcome="signed_off")
		assert result["already_committed"] is False
		assert store.get_message(mid)["state"] == "closed"
		retry = store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
		                          body=b"final signoff", outcome="signed_off")
		assert retry["already_committed"] is True

	def test_bodyless_close(self, store):
		mid = send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		assert store.get_message(mid)["state"] == "closed"

	def test_second_disposition_blocked_by_constraint(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B, outcome="ok")
		store._txn_begin("close", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError):
				store.conn.execute(
					"INSERT INTO dispositions(claim_id, kind, outcome, created_ts) "
					"VALUES(?, 'close', 'dup', 'now')", (claim["claim_id"],))
		finally:
			store._txn_rollback()


# ---------------------------------------------------------------------------
# transient retention (T16) and per-owner content (T17)
# ---------------------------------------------------------------------------

class TestRetentionContent:
	def test_transient_scrub_in_consuming_txn(self, store):
		mid = send_one(store, body=b"ephemeral", retention="transient")
		sha = store.get_message(mid)["content_sha256"]
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B, outcome="seen")
		msg = store.get_message(mid)
		assert msg["content_id"] is None
		assert msg["body"] is None
		assert msg["content_sha256"] == sha
		assert msg["state"] == "closed"

	def test_durable_body_retained(self, store):
		mid = send_one(store, body=b"the record", retention="durable")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		assert store.get_message(mid)["body"] == b"the record"

	def test_per_owner_content_rows_no_dedup(self, store):
		send_one(store, body=b"identical bytes", retention="transient")
		mid2 = send_one(store, body=b"identical bytes", retention="durable")
		count = store.conn.execute(
			"SELECT COUNT(*) FROM contents WHERE sha256=?",
			(store.get_message(mid2)["content_sha256"],)).fetchone()[0]
		assert count == 2
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		assert store.get_message(mid2)["body"] == b"identical bytes"

	def test_contents_immutable(self, store):
		send_one(store)
		store._txn_begin("send", "rev1", SEED_A)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="immutable"):
				store.conn.execute("UPDATE contents SET body=X'00'")
		finally:
			store._txn_rollback()

	def test_content_delete_needs_authorized_verb(self, store):
		send_one(store)
		store._txn_begin("send", "rev1", SEED_A)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="retention"):
				store.conn.execute("DELETE FROM contents")
		finally:
			store._txn_rollback()


# ---------------------------------------------------------------------------
# ledger + attribution + state graph (T2-core, T6, T18, T25)
# ---------------------------------------------------------------------------

class TestLedger:
	def test_birth_and_transition_events(self, store):
		mid = send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"x")
		rows = store.conn.execute(
			"SELECT entity, entity_id, from_state, to_state, verb, actor FROM transitions ORDER BY seq").fetchall()
		events = [(r["entity"], r["from_state"], r["to_state"], r["verb"]) for r in rows]
		assert ("message", None, "pending", "send") in events
		assert ("claim", None, "active", "claim") in events
		assert ("message", "pending", "claimed", "claim") in events
		assert ("message", "claimed", "completed", "reply") in events
		assert ("claim", "active", "completed", "reply") in events
		assert ("message", None, "pending", "reply") in events  # outgoing birth
		assert all(r["actor"] in ("rev1", "imp1") for r in rows)

	def test_uncontextual_mutation_fails_closed(self, store):
		mid = send_one(store)
		with pytest.raises(sqlite3.IntegrityError, match="uncontextual|context"):
			store.conn.execute("UPDATE messages SET state='claimed' WHERE id=?", (mid,))
		with pytest.raises(sqlite3.IntegrityError, match="context"):
			store.conn.execute(
				"INSERT INTO messages(id, from_participant, to_participant, kind, retention, "
				"created_ts, state) VALUES('x', 'a.b', 'c.d', 'k', 'durable', 'now', 'pending')")

	def test_context_bearing_direct_sql_is_logged(self, store):
		mid = send_one(store)
		store._txn_begin("claim", "imp9", SEED_C)
		try:
			store.conn.execute(
				"INSERT INTO claims(claim_id, message_id, actor, seed, claimed_ts, state) "
				"VALUES('deadbeef', ?, 'imp9', ?, 'now', 'active')", (mid, SEED_C))
			store._txn_commit()
		except BaseException:
			store._txn_rollback()
			raise
		row = store.conn.execute(
			"SELECT actor, verb FROM transitions WHERE entity='claim' AND entity_id='deadbeef'").fetchone()
		assert row["actor"] == "imp9"
		assert row["verb"] == "claim"

	def test_illegal_state_edge_aborts(self, store):
		mid = send_one(store)
		store._txn_begin("claim", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="illegal message state edge"):
				store.conn.execute("UPDATE messages SET state='completed' WHERE id=?", (mid,))
		finally:
			store._txn_rollback()

	def test_ledger_append_only(self, store):
		send_one(store)
		store._txn_begin("send", "rev1", SEED_A)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="append-only"):
				store.conn.execute("DELETE FROM transitions")
			with pytest.raises(sqlite3.IntegrityError, match="append-only"):
				store.conn.execute("UPDATE transitions SET actor='forged'")
		finally:
			store._txn_rollback()

	def test_claim_history_immutable_columns(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store._txn_begin("claim", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="immutable claim column"):
				store.conn.execute("UPDATE claims SET actor='rewritten' WHERE claim_id=?",
				                   (claim["claim_id"],))
		finally:
			store._txn_rollback()


# ---------------------------------------------------------------------------
# writer serialization (T9)
# ---------------------------------------------------------------------------

class TestBusy:
	def test_second_writer_gets_bounded_busy(self, instance, monkeypatch):
		monkeypatch.setattr(b6, "BUSY_TIMEOUT_MS", 200)
		with b6.open_instance(instance) as st1, b6.open_instance(instance) as st2:
			st1._txn_begin("send", "rev1", SEED_A)
			try:
				with pytest.raises(b6.BatonError) as excinfo:
					st2.send("acme.reviewer", "acme.implementer", actor="rev2",
					         seed=SEED_A, kind="question", body=b"x")
				assert excinfo.value.exit_code == b6.EXIT_RACE
			finally:
				st1._txn_rollback()


# ---------------------------------------------------------------------------
# extraction purity (T26 partial: grep sweep)
# ---------------------------------------------------------------------------

class TestExtractionPurity:
	def test_no_host_project_references(self):
		source = open(os.path.join(os.path.dirname(__file__), "baton_v6.py")).read()
		for banned in ("dri" + "ft", "wo" + "rk/", "fin" + "ding-", "AGE" + "NTS"):
			assert banned not in source, f"host-project reference {banned!r} in reusable module"


# ---------------------------------------------------------------------------
# Review round 1 fixes: transaction-time gates, stranded txn, transient close,
# scrub/timestamp guards, crash-atomic init, retry thread pin, retention override
# ---------------------------------------------------------------------------

class TestTxnTimeGates:
	def test_stale_open_writer_blocked_by_maintenance(self, instance):
		with b6.open_instance(instance) as a:
			b6.maintenance_enter(instance, participant="hq.lead", actor="lead", seed=SEED_C,
			                     reason="gate test")
			with pytest.raises(b6.BatonError) as excinfo:
				send_one(a)
			assert excinfo.value.exit_code == b6.EXIT_GATED
			assert not a.conn.in_transaction

	def test_stale_open_writer_blocked_by_move(self, instance, tmp_path):
		dest = tmp_path / "dest"
		dest.mkdir()
		dest_config = str(dest / "baton.json")
		with b6.open_instance(instance) as a:
			token = b6.maintenance_enter(instance, participant="hq.lead", actor="lead",
			                             seed=SEED_C, reason="moving", move=True,
			                             destination=dest_config)["move_token"]
			b6.move_copy(instance, participant="hq.lead", actor="lead", seed=SEED_C)
			b6.move_bind_destination(dest_config, participant="hq.lead", actor="lead",
			                         seed=SEED_C, token=token)
			b6.move_activate(dest_config, participant="hq.lead", actor="lead", seed=SEED_C,
			                 token=token)
			b6.move_decommission(instance, participant="hq.lead", actor="lead", seed=SEED_C,
			                     token=token, moved_to=dest_config)
			with pytest.raises(b6.BatonError) as excinfo:
				send_one(a)
			assert excinfo.value.exit_code == b6.EXIT_GATED

	def test_raw_gate_mutation_is_corruption_negative(self, store):
		with pytest.raises(sqlite3.IntegrityError, match="authorized ceremony"):
			store.conn.execute("UPDATE instance_meta SET maintenance=1 WHERE one_row=1")
		with pytest.raises(sqlite3.IntegrityError, match="immutable"):
			store.conn.execute("UPDATE instance_meta SET uuid='forged' WHERE one_row=1")
		with pytest.raises(sqlite3.IntegrityError, match="regen/migrate"):
			store.conn.execute("UPDATE instance_meta SET accepted_generation=9 WHERE one_row=1")
		store._txn_begin("move", "lead", SEED_C, ceremony=None)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="illegal move_status edge"):
				store.conn.execute(
					"UPDATE instance_meta SET maintenance=1, move_status='moved', "
					"move_token='t', move_role='source', move_peer='/x', moved_to='/x' "
					"WHERE one_row=1")
		finally:
			store._txn_rollback()

	def test_stale_open_writer_blocked_after_regen(self, instance, tmp_path):
		a = b6.open_instance(instance)
		try:
			with open(instance, "w") as handle:
				json.dump(make_config(generation=2), handle)
			b6.regen_instance(instance, participant="hq.lead", actor="lead", seed=SEED_C)
			with pytest.raises(b6.BatonError, match="stale"):
				send_one(a)
		finally:
			a.close()
		with b6.open_instance(instance) as fresh:
			send_one(fresh)

	def test_regen_generation_must_be_exactly_next(self, instance):
		with open(instance, "w") as handle:
			json.dump(make_config(generation=3), handle)
		with pytest.raises(b6.BatonError, match="regen requires config generation 2"):
			b6.regen_instance(instance, participant="hq.lead", actor="lead", seed=SEED_C)


class TestTxnStrand:
	def test_begin_failure_never_strands_transaction(self, store):
		store.conn.execute(
			"CREATE TEMP TRIGGER break_ctx BEFORE UPDATE ON op_context "
			"BEGIN SELECT RAISE(ABORT, 'break'); END")
		try:
			with pytest.raises(b6.BatonError):
				send_one(store)
			assert not store.conn.in_transaction
		finally:
			store.conn.execute("DROP TRIGGER break_ctx")
		mid = send_one(store)
		assert store.get_message(mid)["state"] == "pending"
		assert store.conn.execute(
			"SELECT op_id FROM op_context WHERE one_row=1").fetchone()[0] is None


class TestTransientClose:
	def test_transient_close_retains_identity_not_bytes(self, store):
		send_one(store, body=b"incoming", retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		body = b"should be transient"
		result = store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
		                           body=body, outcome="noted")
		import hashlib
		sha = hashlib.sha256(body).hexdigest()
		assert result["content_sha256"] == sha
		count = store.conn.execute(
			"SELECT COUNT(*) FROM contents WHERE sha256=?", (sha,)).fetchone()[0]
		assert count == 0
		disp = store.conn.execute(
			"SELECT content_id, content_sha256 FROM dispositions WHERE claim_id=?",
			(claim["claim_id"],)).fetchone()
		assert disp["content_id"] is None
		assert disp["content_sha256"] == sha
		retry = store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
		                          body=body, outcome="noted")
		assert retry["already_committed"] is True
		with pytest.raises(b6.BatonError, match="content differs"):
			store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
			                  body=b"other", outcome="noted")

	def test_transient_close_body_cap(self, store):
		send_one(store, body=b"x", retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="exceeds"):
			store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
			                  body=b"y" * (b6.TRANSIENT_BODY_MAX_BYTES + 1))

	def test_durable_close_retains_body(self, store):
		send_one(store, body=b"incoming", retention="durable")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B, body=b"kept record")
		row = store.conn.execute(
			"SELECT c.body FROM dispositions d JOIN contents c ON c.content_id=d.content_id "
			"WHERE d.claim_id=?", (claim["claim_id"],)).fetchone()
		assert row["body"] == b"kept record"


class TestScrubAndTimestampGuards:
	def test_uncontextual_scrub_rejected(self, store):
		mid = send_one(store, body=b"x", retention="transient")
		with pytest.raises(sqlite3.IntegrityError, match="consuming operation"):
			store.conn.execute("UPDATE messages SET content_id=NULL WHERE id=?", (mid,))

	def test_wrong_verb_scrub_rejected(self, store):
		mid = send_one(store, body=b"x", retention="transient")
		store._txn_begin("claim", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="consuming operation"):
				store.conn.execute("UPDATE messages SET content_id=NULL WHERE id=?", (mid,))
		finally:
			store._txn_rollback()

	def test_uncontextual_timestamp_rewrites_rejected(self, store):
		mid = send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		with pytest.raises(sqlite3.IntegrityError, match="completed_ts"):
			store.conn.execute("UPDATE messages SET completed_ts='1999-01-01T00:00:00Z' WHERE id=?", (mid,))
		with pytest.raises(sqlite3.IntegrityError, match="terminal_ts"):
			store.conn.execute("UPDATE claims SET terminal_ts='1999-01-01T00:00:00Z' WHERE claim_id=?",
			                   (claim["claim_id"],))


class TestCrashAtomicInit:
	def test_final_db_mode_is_private(self, instance, tmp_path):
		mode = os.stat(tmp_path / "mailbox.sqlite3").st_mode & 0o777
		assert mode == 0o600

	def test_stale_scratch_does_not_block_init(self, tmp_path):
		config_path = str(tmp_path / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		(tmp_path / ".init-deadbeef.sqlite3").write_bytes(b"stale scratch")
		b6.init_instance(config_path)
		assert (tmp_path / "mailbox.sqlite3").is_file()
		assert (tmp_path / ".init-deadbeef.sqlite3").is_file()  # doctor's, not ours

	def test_partial_final_refused_scratch_cleaned(self, tmp_path):
		config_path = str(tmp_path / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		(tmp_path / "mailbox.sqlite3").write_bytes(b"partial garbage")
		with pytest.raises(b6.BatonError, match="refusing to initialize"):
			b6.init_instance(config_path)
		leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".init-")]
		assert leftovers == []


class TestRetryRouting:
	def test_retry_thread_mismatch_fails_closed(self, store):
		send_one(store, thread="topic-1")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
		            body=b"x", thread_id="topic-1")
		with pytest.raises(b6.BatonError, match="thread differs"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
			            body=b"x", thread_id="topic-2")

	def test_retry_recipient_and_kind_mismatch(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"x")
		with pytest.raises(b6.BatonError, match="kind differs"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="other", body=b"x")
		with pytest.raises(b6.BatonError, match="recipient differs"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
			            body=b"x", recipient="hq.lead")


class TestRetentionOverride:
	def test_response_inherits_by_default(self, store):
		send_one(store, retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		result = store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"r")
		assert store.get_message(result["response_message_id"])["retention"] == "transient"

	def test_explicit_override_preserved_from_v5(self, store):
		send_one(store, retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		result = store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
		                     body=b"r", retention="durable")
		assert store.get_message(result["response_message_id"])["retention"] == "durable"
		with pytest.raises(b6.BatonError, match="invalid retention"):
			send_one(store)
			claim2 = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
			store.reply(claim2["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
			            body=b"r", retention="forever")


# ---------------------------------------------------------------------------
# Handoff 2: notices (T12), recovery (T2, T11), gc (T16), attachments, regen
# ---------------------------------------------------------------------------

class TestNotices:
	def test_see_marks_and_dedupes(self, store):
		nid = store.send_notice("hq.lead", actor="lead", seed=SEED_C, kind="announcement",
		                        body=b"all hands")
		seen = store.see("acme.implementer", actor="imp1", seed=SEED_B)
		assert [n["id"] for n in seen] == [nid]
		assert seen[0]["body"] == b"all hands"
		assert store.see("acme.implementer", actor="imp1", seed=SEED_B) == []
		other = store.see("acme.reviewer", actor="rev1", seed=SEED_A)
		assert [n["id"] for n in other] == [nid]

	def test_author_early_expire_single_txn(self, store):
		nid = store.send_notice("hq.lead", actor="lead", seed=SEED_C, kind="announcement",
		                        body=b"oops")
		store.see("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="not its exact author instance"):
			store.expire("acme.implementer", actor="imp1", seed=SEED_B, notice_id=nid)
		removed = store.expire("hq.lead", actor="lead", seed=SEED_C, notice_id=nid)
		assert removed == [nid]
		assert store.conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 0
		assert store.conn.execute("SELECT COUNT(*) FROM notice_seen").fetchone()[0] == 0
		assert store.conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 0

	def test_ttl_sweep(self, store):
		import time
		store.send_notice("hq.lead", actor="lead", seed=SEED_C, kind="tick",
		                  body=b"short", ttl_seconds=1)
		keep = store.send_notice("hq.lead", actor="lead", seed=SEED_C, kind="keep", body=b"long")
		time.sleep(1.1)
		removed = store.expire("hq.lead", actor="lead", seed=SEED_C)
		assert len(removed) == 1
		remaining = [r[0] for r in store.conn.execute("SELECT id FROM notices")]
		assert remaining == [keep]


class TestRecovery:
	def test_recover_then_reclaim_preserves_history(self, store):
		mid = send_one(store)
		claim1 = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		result = store.recover_claim(claim1["claim_id"], participant="hq.lead", actor="lead", seed=SEED_C,
		                             reason="imp1 host died")
		assert result["message_id"] == mid
		assert store.get_message(mid)["state"] == "pending"
		old = store.get_claim(claim1["claim_id"])
		assert old["state"] == "recovered"
		assert old["actor"] == "imp1"
		claim2 = store.claim("acme.implementer", actor="imp2", seed=SEED_C)
		assert claim2["claim_id"] != claim1["claim_id"]
		assert claim2["message_id"] == mid
		audits = store.conn.execute(
			"SELECT claim_id, actor, reason FROM recoveries").fetchall()
		assert len(audits) == 1
		assert audits[0]["claim_id"] == claim1["claim_id"]
		assert audits[0]["reason"] == "imp1 host died"

	def test_recovery_requires_reason(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="reason"):
			store.recover_claim(claim["claim_id"], participant="hq.lead", actor="lead", seed=SEED_C, reason="  ")

	def test_recovered_claim_cannot_reply(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.recover_claim(claim["claim_id"], participant="hq.lead", actor="lead", seed=SEED_C, reason="dead")
		with pytest.raises(b6.BatonError, match="recovered"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"late")

	def test_recover_inactive_claim_refused(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="not active"):
			store.recover_claim(claim["claim_id"], participant="hq.lead", actor="lead", seed=SEED_C, reason="x")


class TestGc:
	def _consume_transient(self, store):
		mid = send_one(store, body=b"old news", retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		return mid

	def test_gc_removes_aged_transient_with_ledger_events(self, store):
		mid = self._consume_transient(store)
		future = "2027-01-01T00:00:00Z"
		result = store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now=future)
		assert mid in result["messages"]
		assert store.conn.execute(
			"SELECT COUNT(*) FROM messages WHERE id=?", (mid,)).fetchone()[0] == 0
		events = store.conn.execute(
			"SELECT entity, to_state, verb FROM transitions WHERE entity_id=? AND to_state='gc'",
			(mid,)).fetchall()
		assert len(events) == 1
		assert events[0]["verb"] == "gc"
		assert store.conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] > 0

	def test_gc_spares_recent_durable_and_recovered(self, store):
		durable_mid = send_one(store, body=b"keep me", retention="durable")
		recent_mid = self._consume_transient(store)
		rec_mid = send_one(store, body=b"rec", retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=rec_mid)
		store.recover_claim(claim["claim_id"], participant="hq.lead", actor="lead", seed=SEED_C, reason="dead")
		claim2 = store.claim("acme.implementer", actor="imp2", seed=SEED_C, message_id=rec_mid)
		store.close_claim(claim2["claim_id"], actor="imp2", seed=SEED_C)
		result = store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now="2027-01-01T00:00:00Z")
		assert recent_mid in result["messages"]
		assert rec_mid not in result["messages"]  # recovery-referenced audit chain preserved
		assert durable_mid not in result["messages"]
		result2 = store.gc(participant="hq.lead", actor="lead", seed=SEED_C)  # real now: nothing aged
		assert result2["messages"] == []

	def test_gc_permanent_ledger_and_recoveries(self, store):
		self._consume_transient(store)
		before = store.conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
		store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now="2027-01-01T00:00:00Z")
		after = store.conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
		assert after > before  # gc appended events, deleted none


class TestAttachments:
	@pytest.fixture
	def rooted(self, tmp_path):
		root = tmp_path / "evidence"
		(root / "sub").mkdir(parents=True)
		(root / "sub" / "report.md").write_bytes(b"evidence bytes")
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		st = b6.open_instance(config_path)
		yield st, root
		st.close()

	def test_attachment_pinned_and_claimable(self, rooted):
		store, root = rooted
		mid = store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
		                 kind="evidence", body=None,
		                 attach={"root_id": "evidence", "path": "sub/report.md"})
		msg = store.get_message(mid)
		assert msg["attach_sha256"] is not None
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		assert claim["message_id"] == mid

	def test_post_publication_mutation_fails_at_claim(self, rooted):
		store, root = rooted
		store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
		           kind="evidence", body=None,
		           attach={"root_id": "evidence", "path": "sub/report.md"})
		(root / "sub" / "report.md").write_bytes(b"tampered")
		with pytest.raises(b6.BatonError, match="pinned hash") as excinfo:
			store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		assert excinfo.value.exit_code == b6.EXIT_DAMAGE

	def test_containment_and_symlink_refusal(self, rooted, tmp_path):
		store, root = rooted
		outside = tmp_path / "outside.md"
		outside.write_bytes(b"outside")
		os.symlink(outside, root / "escape.md")
		with pytest.raises(b6.BatonError, match="symlink"):
			store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			           kind="evidence", body=None,
			           attach={"root_id": "evidence", "path": "escape.md"})
		for bad in ("../outside.md", "/etc/passwd", "sub/../../x", ""):
			with pytest.raises(b6.BatonError):
				store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
				           kind="evidence", body=None,
				           attach={"root_id": "evidence", "path": bad})

	def test_undeclared_root_refused(self, rooted):
		store, root = rooted
		with pytest.raises(b6.BatonError, match="not declared"):
			store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			           kind="evidence", body=None,
			           attach={"root_id": "ghost", "path": "x.md"})


# ---------------------------------------------------------------------------
# Review round 2 pins: disposition retention, notice authorship, recovery
# authority, regen live-state guards, state-coupled triggers, init fault
# matrix, attachment snapshot, root validation
# ---------------------------------------------------------------------------

class TestDispositionRetention:
	def test_reply_retry_retention_mismatch_fails_closed(self, store):
		send_one(store, retention="durable")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"x")
		with pytest.raises(b6.BatonError, match="retention differs"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
			            body=b"x", retention="transient")
		retry = store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"x")
		assert retry["already_committed"] is True
		assert retry["retention"] == "durable"

	def test_close_override_transient_to_durable_retains_body(self, store):
		send_one(store, retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
		                  body=b"promoted record", retention="durable")
		row = store.conn.execute(
			"SELECT c.body FROM dispositions d JOIN contents c ON c.content_id=d.content_id "
			"WHERE d.claim_id=?", (claim["claim_id"],)).fetchone()
		assert row["body"] == b"promoted record"

	def test_close_override_durable_to_transient_drops_body(self, store):
		send_one(store, retention="durable")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		result = store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
		                           body=b"ephemeral note", retention="transient")
		count = store.conn.execute(
			"SELECT COUNT(*) FROM contents WHERE sha256=?", (result["content_sha256"],)).fetchone()[0]
		assert count == 0
		with pytest.raises(b6.BatonError, match="retention differs"):
			store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
			                  body=b"ephemeral note", retention="durable")
		retry = store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
		                          body=b"ephemeral note", retention="transient")
		assert retry["already_committed"] is True

	def test_close_invalid_retention(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="invalid retention"):
			store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
			                  body=b"x", retention="forever")


class TestNoticeAuthorship:
	def test_same_participant_different_actor_cannot_early_expire(self, store):
		nid = store.send_notice("acme.reviewer", actor="rev1", seed=SEED_A,
		                        kind="announcement", body=b"mine")
		with pytest.raises(b6.BatonError, match="exact author instance"):
			store.expire("acme.reviewer", actor="rev2", seed=SEED_B, notice_id=nid)
		with pytest.raises(b6.BatonError, match="exact author instance"):
			store.expire("acme.reviewer", actor="rev1", seed=SEED_C, notice_id=nid)
		removed = store.expire("acme.reviewer", actor="rev1", seed=SEED_A, notice_id=nid)
		assert removed == [nid]

	def test_ttl_default_finite(self, store):
		nid = store.send_notice("hq.lead", actor="lead", seed=SEED_C, kind="note", body=b"x")
		ttl = store.conn.execute("SELECT ttl_seconds FROM notices WHERE id=?", (nid,)).fetchone()[0]
		assert ttl == b6.DEFAULT_NOTICE_TTL_SECONDS
		with pytest.raises(b6.BatonError, match="positive"):
			store.send_notice("hq.lead", actor="lead", seed=SEED_C, kind="note",
			                  body=b"x", ttl_seconds=0)
		with pytest.raises(sqlite3.IntegrityError):
			store._txn_begin("send", "lead", SEED_C)
			try:
				store.conn.execute(
					"INSERT INTO notices(id, from_participant, author_actor, author_seed, kind, "
					"content_sha256, created_ts, ttl_seconds) "
					"VALUES('immortal', 'hq.lead', 'lead', ?, 'k', 'sha', 'now', 0)", (SEED_C,))
			finally:
				store._txn_rollback()

	def test_notice_immutability_and_context(self, store):
		nid = store.send_notice("hq.lead", actor="lead", seed=SEED_C, kind="note", body=b"x")
		with pytest.raises(sqlite3.IntegrityError, match="immutable"):
			store.conn.execute("UPDATE notices SET author_actor='forged' WHERE id=?", (nid,))
		with pytest.raises(sqlite3.IntegrityError, match="context"):
			store.conn.execute(
				"INSERT INTO notices(id, from_participant, author_actor, author_seed, kind, "
				"content_sha256, created_ts, ttl_seconds) "
				"VALUES('raw', 'hq.lead', 'lead', 'seed', 'k', 'sha', 'now', 60)")
		with pytest.raises(sqlite3.IntegrityError, match="context"):
			store.conn.execute(
				"INSERT INTO notice_seen(notice_id, participant, actor, seed, seen_ts) "
				"VALUES(?, 'acme.reviewer', 'rev1', 'seed', 'now')", (nid,))


class TestRecoveryAuthority:
	def test_unconfigured_participant_refused(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="not declared"):
			store.recover_claim(claim["claim_id"], participant="ghost.admin",
			                    actor="unconfigured", seed=SEED_C, reason="x")

	def test_participant_without_capability_refused(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="'recovery' capability"):
			store.recover_claim(claim["claim_id"], participant="acme.reviewer",
			                    actor="rev1", seed=SEED_A, reason="x")

	def test_wrong_singleton_actor_refused(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="singleton"):
			store.recover_claim(claim["claim_id"], participant="hq.lead",
			                    actor="impostor", seed=SEED_C, reason="x")

	def test_agent_with_declared_capability_allowed(self, tmp_path):
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["participants"]["acme.oncall"] = {"identity": "agent", "capabilities": ["recovery"]}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		with b6.open_instance(config_path) as st:
			send_one(st)
			claim = st.claim("acme.implementer", actor="imp1", seed=SEED_B)
			st.recover_claim(claim["claim_id"], participant="acme.oncall",
			                 actor="oncall1", seed=SEED_C, reason="authority is a capability")

	def test_unknown_capability_rejected_in_config(self):
		cfg = make_config()
		cfg["participants"]["acme.reviewer"]["capabilities"] = ["sudo"]
		with pytest.raises(b6.BatonError, match="unknown capabilities"):
			b6.validate_config(cfg)

	def test_audit_row_carries_full_identity(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.recover_claim(claim["claim_id"], participant="hq.lead", actor="lead",
		                    seed=SEED_C, reason="host died")
		row = store.conn.execute("SELECT participant, actor, seed FROM recoveries").fetchone()
		assert (row["participant"], row["actor"], row["seed"]) == ("hq.lead", "lead", SEED_C)
		ledger = store.conn.execute(
			"SELECT participant FROM transitions WHERE verb='recover' AND entity='claim'").fetchone()
		assert ledger["participant"] == "hq.lead"

	def test_regen_requires_config_capability(self, instance):
		with open(instance, "w") as handle:
			json.dump(make_config(generation=2), handle)
		with pytest.raises(b6.BatonError, match="'config' capability"):
			b6.regen_instance(instance, participant="acme.reviewer", actor="rev1", seed=SEED_A)

	def test_gc_requires_configured_participant(self, store):
		with pytest.raises(b6.BatonError, match="not declared"):
			store.gc(participant="ghost.admin", actor="x", seed=SEED_C)


class TestRegenLiveState:
	def test_participant_removal_refused_while_live(self, instance):
		with b6.open_instance(instance) as st:
			send_one(st)  # pending message to acme.implementer
		cfg = make_config(generation=2)
		del cfg["participants"]["acme.implementer"]
		with open(instance, "w") as handle:
			json.dump(cfg, handle)
		with pytest.raises(b6.BatonError, match="named by live"):
			b6.regen_instance(instance, participant="hq.lead", actor="lead", seed=SEED_C)

	def test_additive_change_accepted(self, instance):
		with b6.open_instance(instance) as st:
			send_one(st)
		cfg = make_config(generation=2)
		cfg["participants"]["acme.newcomer"] = {"identity": "agent"}
		with open(instance, "w") as handle:
			json.dump(cfg, handle)
		result = b6.regen_instance(instance, participant="hq.lead", actor="lead", seed=SEED_C)
		assert result["accepted_generation"] == 2

	def test_attachment_root_remap_refused(self, tmp_path):
		root = tmp_path / "evidence"
		root.mkdir()
		(root / "e.md").write_bytes(b"evidence")
		other = tmp_path / "other"
		other.mkdir()
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		with b6.open_instance(config_path) as st:
			mid = st.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			              kind="evidence", body=None, attach={"root_id": "evidence", "path": "e.md"})
			claim = st.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
			st.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)  # durable, retained
		cfg2 = make_config(generation=2)
		cfg2["roots"] = {"evidence": str(other)}
		with open(config_path, "w") as handle:
			json.dump(cfg2, handle)
		with pytest.raises(b6.BatonError, match="keep its accepted mapping"):
			b6.regen_instance(config_path, participant="hq.lead", actor="lead", seed=SEED_C)
		cfg3 = make_config(generation=2)
		cfg3["roots"] = {"evidence": str(root), "extra": str(other)}
		with open(config_path, "w") as handle:
			json.dump(cfg3, handle)
		result = b6.regen_instance(config_path, participant="hq.lead", actor="lead", seed=SEED_C)
		assert result["accepted_generation"] == 2

	def test_regen_exact_next_race(self, instance):
		with open(instance, "w") as handle:
			json.dump(make_config(generation=2), handle)
		b6.regen_instance(instance, participant="hq.lead", actor="lead", seed=SEED_C)
		with pytest.raises(b6.BatonError, match="regen requires config generation 3"):
			b6.regen_instance(instance, participant="hq.lead", actor="lead", seed=SEED_C)


class TestStateCoupledTriggers:
	def test_context_bearing_wrong_row_timestamp_rejected(self, store):
		mid = send_one(store)  # pending
		store._txn_begin("reply", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="terminal transition"):
				store.conn.execute(
					"UPDATE messages SET completed_ts='1999-01-01T00:00:00Z' WHERE id=?", (mid,))
		finally:
			store._txn_rollback()

	def test_context_bearing_pending_scrub_rejected(self, store):
		mid = send_one(store, retention="transient")  # pending transient
		store._txn_begin("reply", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="terminal transient"):
				store.conn.execute("UPDATE messages SET content_id=NULL WHERE id=?", (mid,))
		finally:
			store._txn_rollback()

	def test_durable_terminal_scrub_rejected(self, store):
		mid = send_one(store, retention="durable")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		store._txn_begin("close", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="terminal transient"):
				store.conn.execute("UPDATE messages SET content_id=NULL WHERE id=?", (mid,))
		finally:
			store._txn_rollback()

	def test_terminal_ts_without_edge_rejected(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store._txn_begin("reply", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError, match="terminal transition"):
				store.conn.execute(
					"UPDATE claims SET terminal_ts='1999-01-01T00:00:00Z' WHERE claim_id=?",
					(claim["claim_id"],))
		finally:
			store._txn_rollback()


def _init_with_fault(config_path, point, queue):
	import baton_v6 as mod
	def hook(p):
		if p == point:
			os._exit(9)
	mod._FAULT_HOOK = hook
	try:
		mod.init_instance(config_path)
		queue.put("completed")
	except mod.BatonError as exc:
		queue.put(f"error:{exc}")


class TestInitFaultMatrix:
	POINTS = ["init:post-commit", "init:post-checkpoint", "init:pre-link",
	          "init:post-link", "init:post-unlink"]

	@pytest.mark.parametrize("point", POINTS)
	def test_kill_at_boundary_leaves_absent_or_valid(self, tmp_path, point):
		config_path = str(tmp_path / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		ctx = multiprocessing.get_context("spawn")
		queue = ctx.Queue()
		proc = ctx.Process(target=_init_with_fault, args=(config_path, point, queue))
		proc.start()
		proc.join(60)
		assert proc.exitcode == 9
		final = tmp_path / "mailbox.sqlite3"
		if final.exists():
			with b6.open_instance(config_path) as st:  # fully valid or open would fail closed
				st.conn.execute("SELECT 1 FROM instance_meta").fetchone()
			with pytest.raises(b6.BatonError, match="refusing to initialize"):
				b6.init_instance(config_path)
		else:
			b6.init_instance(config_path)  # retry-safe
			with b6.open_instance(config_path) as st:
				st.conn.execute("SELECT 1 FROM instance_meta").fetchone()
		scratch = [p.name for p in tmp_path.iterdir() if p.name.startswith(".init-")]
		assert all(name.startswith(".init-") for name in scratch)  # recognizable, never partial finals


class TestAttachmentSnapshot:
	def test_mid_hash_mutation_refused(self, tmp_path, monkeypatch):
		root = tmp_path / "evidence"
		root.mkdir()
		target = root / "e.md"
		target.write_bytes(b"original")
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		def mutate(point):
			if point == "attach:post-hash":
				target.write_bytes(b"mutated mid-hash")
		monkeypatch.setattr(b6, "_FAULT_HOOK", mutate)
		with b6.open_instance(config_path) as st:
			with pytest.raises(b6.BatonError, match="changed while being hashed"):
				st.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
				        kind="evidence", body=None, attach={"root_id": "evidence", "path": "e.md"})


class TestRootValidation:
	def test_non_canonical_root_refused(self, tmp_path):
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"bad": str(tmp_path) + "/sub/../sub"}
		(tmp_path / "sub").mkdir()
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		with pytest.raises(b6.BatonError, match="canonical"):
			b6.init_instance(config_path)

	def test_symlink_root_refused(self, tmp_path):
		real = tmp_path / "real"
		real.mkdir()
		os.symlink(real, tmp_path / "link")
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"linked": str(tmp_path / "link")}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		with pytest.raises(b6.BatonError, match="symlink"):
			b6.init_instance(config_path)

	def test_missing_root_fails_at_open(self, tmp_path):
		root = tmp_path / "evidence"
		root.mkdir()
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		root.rmdir()
		with pytest.raises(b6.BatonError, match="openable directory"):
			b6.open_instance(config_path)


class TestRootBindingGenerations:
	@pytest.fixture
	def bound(self, tmp_path):
		root = tmp_path / "evidence"
		root.mkdir()
		(root / "e.md").write_bytes(b"evidence")
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		return config_path, root, tmp_path

	def test_unrelated_regen_does_not_invalidate_attachments(self, bound):
		config_path, root, tmp_path = bound
		with b6.open_instance(config_path) as st:
			mid = st.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			              kind="evidence", body=None, attach={"root_id": "evidence", "path": "e.md"})
			assert st.get_message(mid)["attach_generation"] == 1
		cfg = make_config(generation=2)
		cfg["roots"] = {"evidence": str(root)}
		cfg["participants"]["acme.newcomer"] = {"identity": "agent"}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.regen_instance(config_path, participant="hq.lead", actor="lead", seed=SEED_C)
		with b6.open_instance(config_path) as st:
			binding = st.conn.execute(
				"SELECT binding_generation FROM accepted_roots WHERE root_id='evidence'").fetchone()
			assert binding["binding_generation"] == 1  # unchanged root keeps its binding
			claim = st.claim("acme.implementer", actor="imp1", seed=SEED_B)  # still verifiable
			assert claim["message_id"] == mid

	def test_new_root_gets_current_generation(self, bound):
		config_path, root, tmp_path = bound
		extra = tmp_path / "extra"
		extra.mkdir()
		cfg = make_config(generation=2)
		cfg["roots"] = {"evidence": str(root), "extra": str(extra)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.regen_instance(config_path, participant="hq.lead", actor="lead", seed=SEED_C)
		with b6.open_instance(config_path) as st:
			rows = {r["root_id"]: r["binding_generation"] for r in st.conn.execute(
				"SELECT root_id, binding_generation FROM accepted_roots")}
			assert rows == {"evidence": 1, "extra": 2}

	def test_binding_generation_mismatch_is_damage(self, bound):
		config_path, root, tmp_path = bound
		with b6.open_instance(config_path) as st:
			st.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			        kind="evidence", body=None, attach={"root_id": "evidence", "path": "e.md"})
		_raw_corrupt(config_path, lambda conn: conn.execute(
			"UPDATE accepted_roots SET binding_generation=9 WHERE root_id='evidence'"))
		with b6.open_instance(config_path) as st:
			with pytest.raises(b6.BatonError, match="binding generation") as excinfo:
				st.claim("acme.implementer", actor="imp1", seed=SEED_B)
			assert excinfo.value.exit_code == b6.EXIT_DAMAGE


# ---------------------------------------------------------------------------
# Review round 3 pins: effective-route retry, bidirectional CHECK coupling,
# GC reply chains, component-walk no-follow, seen/recovery guards, snapshot
# ---------------------------------------------------------------------------

class TestEffectiveRouteRetry:
	def test_first_explicit_retry_omitted_fails_closed(self, store):
		send_one(store, thread="t1")  # from acme.reviewer
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
		            body=b"x", recipient="hq.lead", thread_id="t2")
		with pytest.raises(b6.BatonError, match="recipient differs"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"x")

	def test_first_explicit_thread_retry_omitted_fails_closed(self, store):
		send_one(store, thread="t1")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
		            body=b"x", thread_id="t2")
		with pytest.raises(b6.BatonError, match="thread differs"):
			store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"x")

	def test_first_default_retry_omitted_redelivers(self, store):
		send_one(store, thread="t1")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"x")
		retry = store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"x")
		assert retry["already_committed"] is True

	def test_first_default_retry_explicit_same_redelivers(self, store):
		send_one(store, thread="t1")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer", body=b"x")
		retry = store.reply(claim["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
		                    body=b"x", recipient="acme.reviewer", thread_id="t1")
		assert retry["already_committed"] is True


class TestBidirectionalCoupling:
	def test_terminal_transition_without_timestamp_rejected(self, store):
		mid = send_one(store)
		store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store._txn_begin("reply", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError):
				store.conn.execute("UPDATE messages SET state='completed' WHERE id=?", (mid,))
		finally:
			store._txn_rollback()

	def test_claim_terminal_without_timestamp_rejected(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		store._txn_begin("reply", "imp1", SEED_B)
		try:
			with pytest.raises(sqlite3.IntegrityError):
				store.conn.execute("UPDATE claims SET state='completed' WHERE claim_id=?",
				                   (claim["claim_id"],))
		finally:
			store._txn_rollback()

	def test_prefilled_terminal_timestamp_birth_rejected(self, store):
		store._txn_begin("send", "rev1", SEED_A)
		try:
			with pytest.raises(sqlite3.IntegrityError):
				store.conn.execute(
					"INSERT INTO messages(id, from_participant, to_participant, kind, retention, "
					"content_sha256, created_ts, state, completed_ts) "
					"VALUES('prefilled', 'acme.reviewer', 'acme.implementer', 'k', 'durable', "
					"'sha', 'now', 'pending', 'already')")
		finally:
			store._txn_rollback()


class TestGcReplyChains:
	def _chain(self, store, incoming_retention="transient", response_retention=None):
		mid_a = send_one(store, retention=incoming_retention)
		claim_a = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid_a)
		result = store.reply(claim_a["claim_id"], actor="imp1", seed=SEED_B, kind="answer",
		                     body=b"resp", retention=response_retention)
		mid_b = result["response_message_id"]
		claim_b = store.claim("acme.reviewer", actor="rev1", seed=SEED_A, message_id=mid_b)
		store.close_claim(claim_b["claim_id"], actor="rev1", seed=SEED_A)
		return mid_a, mid_b, claim_a["claim_id"]

	def test_all_transient_chain_collected(self, store):
		mid_a, mid_b, _ = self._chain(store)
		result = store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now="2027-01-01T00:00:00Z")
		assert set(result["messages"]) >= {mid_a, mid_b}
		remaining = store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
		assert remaining == 0

	def test_durable_incoming_anchors_transient_response(self, store):
		mid_a, mid_b, _ = self._chain(store, incoming_retention="durable",
		                              response_retention="transient")
		result = store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now="2027-01-01T00:00:00Z")
		assert mid_a not in result["messages"]
		assert mid_b not in result["messages"]  # retained disposition anchors its response metadata
		assert store.conn.execute(
			"SELECT COUNT(*) FROM messages WHERE id IN (?,?)", (mid_a, mid_b)).fetchone()[0] == 2

	def test_transient_incoming_anchored_by_durable_response(self, store):
		mid_a, mid_b, _ = self._chain(store, incoming_retention="transient",
		                              response_retention="durable")
		result = store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now="2027-01-01T00:00:00Z")
		assert mid_a not in result["messages"]  # retained child references its parent
		assert mid_b not in result["messages"]

	def test_gc_never_aborts_and_retry_after_gc_is_clean(self, store):
		mid_a, mid_b, claim_a = self._chain(store)
		ledger_before = store.conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
		result = store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now="2027-01-01T00:00:00Z")
		assert set(result["messages"]) >= {mid_a, mid_b}
		assert store.conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] > ledger_before
		with pytest.raises(b6.BatonError, match="unknown claim"):
			store.reply(claim_a, actor="imp1", seed=SEED_B, kind="answer", body=b"resp")
		again = store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now="2027-01-01T00:00:00Z")
		assert again["messages"] == []


class TestComponentWalkNoFollow:
	def test_intermediate_symlink_root_refused(self, tmp_path):
		base = tmp_path / "base"
		base.mkdir()
		target = tmp_path / "target"
		(target / "leaf").mkdir(parents=True)
		os.symlink(target, base / "link")
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"bad": str(base / "link" / "leaf")}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		with pytest.raises(b6.BatonError, match="symlink"):
			b6.init_instance(config_path)

	def test_intermediate_symlink_instance_dir_refused(self, tmp_path):
		real = tmp_path / "real"
		real.mkdir()
		os.symlink(real, tmp_path / "link")
		config_path = str(tmp_path / "link" / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		with pytest.raises(b6.BatonError, match="symlink"):
			b6.init_instance(config_path)


class TestSeenAndRecoveryGuards:
	def test_notice_seen_immutable_and_delete_guarded(self, store):
		nid = store.send_notice("hq.lead", actor="lead", seed=SEED_C, kind="note", body=b"x")
		store.see("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(sqlite3.IntegrityError, match="immutable"):
			store.conn.execute("UPDATE notice_seen SET seed='forged'")
		with pytest.raises(sqlite3.IntegrityError, match="removable only"):
			store.conn.execute("DELETE FROM notice_seen")
		assert store.conn.execute("SELECT COUNT(*) FROM notice_seen").fetchone()[0] == 1
		removed = store.expire("hq.lead", actor="lead", seed=SEED_C, notice_id=nid)
		assert removed == [nid]
		assert store.conn.execute("SELECT COUNT(*) FROM notice_seen").fetchone()[0] == 0

	def test_uncontextual_recovery_row_rejected(self, store):
		send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B)
		with pytest.raises(sqlite3.IntegrityError, match="context"):
			store.conn.execute(
				"INSERT INTO recoveries(recovery_id, claim_id, participant, actor, seed, reason, "
				"created_ts) VALUES('forged', ?, 'ghost.admin', 'x', 'y', 'because', 'now')",
				(claim["claim_id"],))


class TestSnapshotHardening:
	def test_same_size_restored_mtime_mutation_refused(self, tmp_path, monkeypatch):
		root = tmp_path / "evidence"
		root.mkdir()
		target = root / "e.md"
		target.write_bytes(b"original")
		st = os.stat(target)
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		def mutate(point):
			if point == "attach:post-hash":
				target.write_bytes(b"mutated!")  # same size as b"original"
				os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
		monkeypatch.setattr(b6, "_FAULT_HOOK", mutate)
		with b6.open_instance(config_path) as store:
			with pytest.raises(b6.BatonError, match="changed while being hashed"):
				store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
				           kind="evidence", body=None, attach={"root_id": "evidence", "path": "e.md"})

	def test_fifo_attachment_rejected_without_hanging(self, tmp_path):
		root = tmp_path / "evidence"
		root.mkdir()
		os.mkfifo(root / "pipe")
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		try:
			with b6.open_instance(config_path) as store:
				with pytest.raises(b6.BatonError, match="not a regular file"):
					store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
					           kind="evidence", body=None, attach={"root_id": "evidence", "path": "pipe"})
		finally:
			os.unlink(root / "pipe")  # host tooling that scans tmp trees must never meet a FIFO


class TestDurableCloseAnchor:
	def test_transient_envelope_durable_close_retained(self, store):
		mid = send_one(store, retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
		                  body=b"durable signoff record", outcome="signed_off",
		                  retention="durable")
		result = store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now="2027-01-01T00:00:00Z")
		assert mid not in result["messages"]
		assert store.conn.execute(
			"SELECT COUNT(*) FROM messages WHERE id=?", (mid,)).fetchone()[0] == 1
		assert store.get_claim(claim["claim_id"])["state"] == "completed"
		row = store.conn.execute(
			"SELECT c.body FROM dispositions d JOIN contents c ON c.content_id=d.content_id "
			"WHERE d.claim_id=?", (claim["claim_id"],)).fetchone()
		assert row["body"] == b"durable signoff record"
		retry = store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B,
		                          body=b"durable signoff record", outcome="signed_off",
		                          retention="durable")
		assert retry["already_committed"] is True

	def test_transient_envelope_transient_close_still_collected(self, store):
		mid = send_one(store, retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B, outcome="seen")
		result = store.gc(participant="hq.lead", actor="lead", seed=SEED_C, now="2027-01-01T00:00:00Z")
		assert mid in result["messages"]


# ---------------------------------------------------------------------------
# Maintenance / move / migrate ceremonies (T7, T15, T22-move)
# ---------------------------------------------------------------------------

class TestMaintenance:
	def test_enter_gates_writes_and_exit_clears(self, instance):
		result = b6.maintenance_enter(instance, participant="hq.lead", actor="lead",
		                              seed=SEED_C, reason="planned upkeep")
		assert result == {"maintenance": True, "move_token": None, "destination": None}
		with pytest.raises(b6.BatonError) as excinfo:
			with b6.open_instance(instance) as st:
				send_one(st)
		assert excinfo.value.exit_code == b6.EXIT_GATED
		with b6.open_instance(instance, readonly=True) as ro:
			assert ro.conn.execute(
				"SELECT maintainer_reason FROM instance_meta").fetchone()[0] == "planned upkeep"
		b6.maintenance_exit(instance, participant="hq.lead", actor="lead", seed=SEED_C,
		                    reason="done")
		with b6.open_instance(instance) as st:
			send_one(st)
		with b6.open_instance(instance, readonly=True) as ro:
			kinds = [r[0] for r in ro.conn.execute("SELECT kind FROM ceremonies ORDER BY created_ts")]
			assert kinds == ["maintenance_enter", "maintenance_exit"]

	def test_enter_requires_capability_and_reason(self, instance):
		with pytest.raises(b6.BatonError, match="'config' capability"):
			b6.maintenance_enter(instance, participant="acme.reviewer", actor="rev1",
			                     seed=SEED_A, reason="nope")
		with pytest.raises(b6.BatonError, match="reason"):
			b6.maintenance_enter(instance, participant="hq.lead", actor="lead",
			                     seed=SEED_C, reason=" ")

	def test_double_enter_refused(self, instance):
		b6.maintenance_enter(instance, participant="hq.lead", actor="lead", seed=SEED_C,
		                     reason="first")
		with pytest.raises(b6.BatonError, match="already under maintenance"):
			b6.maintenance_enter(instance, participant="hq.lead", actor="lead", seed=SEED_C,
			                     reason="second")

	def test_ceremony_rows_immutable(self, instance):
		b6.maintenance_enter(instance, participant="hq.lead", actor="lead", seed=SEED_C,
		                     reason="upkeep")
		with b6.open_instance(instance, _for_ceremony=True) as st:
			with pytest.raises(sqlite3.IntegrityError, match="immutable"):
				st.conn.execute("UPDATE ceremonies SET reason='forged'")
			with pytest.raises(sqlite3.IntegrityError, match="immutable"):
				st.conn.execute("DELETE FROM ceremonies")
			with pytest.raises(sqlite3.IntegrityError, match="context"):
				st.conn.execute(
					"INSERT INTO ceremonies(ceremony_id, kind, participant, actor, seed, created_ts) "
					"VALUES('raw', 'migrate', 'p.q', 'a', 'b', 'now')")


class TestCheckpointDrain:
	def test_drain_waits_for_reader_then_converges(self, instance, monkeypatch):
		monkeypatch.setattr(b6, "CHECKPOINT_DRAIN_ATTEMPTS", 3)
		monkeypatch.setattr(b6, "CHECKPOINT_DRAIN_SLEEP_S", 0.05)
		with b6.open_instance(instance) as writer:
			send_one(writer)
			reader = b6.open_instance(instance, readonly=True)
			reader.conn.execute("BEGIN")
			reader.conn.execute("SELECT COUNT(*) FROM messages").fetchone()
			try:
				with pytest.raises(b6.BatonError, match="did not converge"):
					b6.checkpoint_drain(writer)
			finally:
				reader.conn.execute("COMMIT")
				reader.close()
			log, ckpt = b6.checkpoint_drain(writer)
			assert log == ckpt


class TestMoveCeremony:
	def _setup(self, tmp_path):
		src = tmp_path / "src"
		src.mkdir()
		config_path = str(src / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		b6.init_instance(config_path)
		with b6.open_instance(config_path) as st:
			send_one(st)
		dest = tmp_path / "dest"
		dest.mkdir()
		return config_path, str(dest / "baton.json")

	def _enter(self, config_path, dest_config):
		return b6.maintenance_enter(config_path, participant="hq.lead", actor="lead",
		                            seed=SEED_C, reason="relocating", move=True,
		                            destination=dest_config)["move_token"]

	def _lead(self):
		return {"participant": "hq.lead", "actor": "lead", "seed": SEED_C}

	def test_full_move_happy_path_with_idempotent_retries(self, tmp_path):
		config_path, dest_config = self._setup(tmp_path)
		token = self._enter(config_path, dest_config)
		copy1 = b6.move_copy(config_path, **self._lead())
		assert copy1["stage"] == "copied" and copy1["already_committed"] is False
		copy2 = b6.move_copy(config_path, **self._lead())  # pre-bind resume
		assert copy2["already_committed"] is True and copy2["stage"] == "copied"
		bind = b6.move_bind_destination(dest_config, token=token, **self._lead())
		assert bind["already_committed"] is False
		rebind = b6.move_bind_destination(dest_config, token=token, **self._lead())
		assert rebind["already_committed"] is True
		copy3 = b6.move_copy(config_path, **self._lead())  # post-bind stage discovery
		assert copy3["already_committed"] is True and copy3["stage"] == "bound"
		act = b6.move_activate(dest_config, token=token, **self._lead())
		assert act["already_committed"] is False
		react = b6.move_activate(dest_config, token=token, **self._lead())
		assert react["already_committed"] is True
		copy4 = b6.move_copy(config_path, **self._lead())  # post-activation discovery
		assert copy4["already_committed"] is True and copy4["stage"] == "activated"
		with b6.open_instance(dest_config) as st:
			assert len(st.scan("acme.implementer")["pending"]) == 1
			send_one(st)
		dec = b6.move_decommission(config_path, token=token, moved_to=dest_config, **self._lead())
		assert dec["already_committed"] is False
		redec = b6.move_decommission(config_path, token=token, moved_to=dest_config, **self._lead())
		assert redec["already_committed"] is True
		with pytest.raises(b6.BatonError, match="has moved to"):
			with b6.open_instance(config_path) as st:
				send_one(st)
		with b6.open_instance(dest_config, readonly=True) as ro:
			uuid_dest = ro.conn.execute("SELECT uuid FROM instance_meta").fetchone()[0]
		with b6.open_instance(config_path, readonly=True, _for_ceremony=True) as ro:
			uuid_src = ro.conn.execute("SELECT uuid FROM instance_meta").fetchone()[0]
		assert uuid_src == uuid_dest

	def test_three_authority_repro_is_impossible(self, tmp_path):
		"""The reviewer's fork repro: source + two copies must NOT all activate."""
		config_path, dest_config = self._setup(tmp_path)
		token = self._enter(config_path, dest_config)
		b6.move_copy(config_path, **self._lead())
		# The source cannot activate (role='source').
		with pytest.raises(b6.BatonError, match="can never activate"):
			b6.move_activate(config_path, token=token, **self._lead())
		# An unbound copy cannot activate either.
		with pytest.raises(b6.BatonError, match="can never activate"):
			b6.move_activate(dest_config, token=token, **self._lead())
		# A second destination cannot exist: copy only goes to the bound peer,
		# and a copy manually placed elsewhere refuses to bind.
		rogue = os.path.dirname(dest_config) + "-rogue"
		os.mkdir(rogue)
		import shutil
		shutil.copy(dest_config.replace("baton.json", "mailbox.sqlite3"),
		            os.path.join(rogue, "mailbox.sqlite3"))
		shutil.copy(os.path.join(os.path.dirname(dest_config), "baton.json"),
		            os.path.join(rogue, "baton.json"))
		with pytest.raises(b6.BatonError, match="DESTINATION route"):
			b6.move_bind_destination(os.path.join(rogue, "baton.json"), token=token, **self._lead())
		# Only the bound copy can bind + activate; afterwards the source still
		# cannot activate and must decommission.
		b6.move_bind_destination(dest_config, token=token, **self._lead())
		b6.move_activate(dest_config, token=token, **self._lead())
		with pytest.raises(b6.BatonError, match="can never activate"):
			b6.move_activate(config_path, token=token, **self._lead())
		active = 0
		for path in (config_path, dest_config, os.path.join(rogue, "baton.json")):
			try:
				with b6.open_instance(path) as st:
					send_one(st)
				active += 1
			except b6.BatonError:
				pass
		assert active == 1

	def test_generic_clear_refused_on_both_roles(self, tmp_path):
		config_path, dest_config = self._setup(tmp_path)
		token = self._enter(config_path, dest_config)
		b6.move_copy(config_path, **self._lead())
		for path in (config_path, dest_config):
			with pytest.raises(b6.BatonError, match="generic maintenance clear is refused"):
				b6.maintenance_exit(path, participant="hq.lead", actor="lead", seed=SEED_C,
				                    reason="oops")

	def test_abort_is_source_only(self, tmp_path):
		config_path, dest_config = self._setup(tmp_path)
		token = self._enter(config_path, dest_config)
		b6.move_copy(config_path, **self._lead())
		with pytest.raises(b6.BatonError, match="SOURCE route"):
			b6.abort_move(dest_config, token=token, destination_destroyed=True,
			              reason="copy must die instead", **self._lead())
		with pytest.raises(b6.BatonError, match="attestation"):
			b6.abort_move(config_path, token=token, destination_destroyed=False,
			              reason="abort", **self._lead())
		with pytest.raises(b6.BatonError, match="boolean"):
			b6.abort_move(config_path, token=token, destination_destroyed="yes",
			              reason="abort", **self._lead())
		with pytest.raises(b6.BatonError, match="token does not match"):
			b6.abort_move(config_path, token="0" * 32, destination_destroyed=True,
			              reason="abort", **self._lead())
		b6.abort_move(config_path, token=token, destination_destroyed=True,
		              reason="destination destroyed by hand", **self._lead())
		with b6.open_instance(config_path) as st:
			send_one(st)

	def test_decommission_role_and_peer_validation(self, tmp_path):
		config_path, dest_config = self._setup(tmp_path)
		token = self._enter(config_path, dest_config)
		b6.move_copy(config_path, **self._lead())
		with pytest.raises(b6.BatonError, match="SOURCE route"):
			b6.move_decommission(dest_config, token=token, moved_to=dest_config, **self._lead())
		with pytest.raises(b6.BatonError, match="does not match the bound destination"):
			b6.move_decommission(config_path, token=token, moved_to="/somewhere/else", **self._lead())

	def test_copy_requires_move_gate(self, tmp_path):
		config_path, dest_config = self._setup(tmp_path)
		with pytest.raises(b6.BatonError, match="maintenance_enter"):
			b6.move_copy(config_path, **self._lead())
		b6.maintenance_enter(config_path, participant="hq.lead", actor="lead", seed=SEED_C,
		                     reason="plain, not move")
		with pytest.raises(b6.BatonError, match="maintenance_enter"):
			b6.move_copy(config_path, **self._lead())

	def test_mismatching_destination_artifact_fails_closed(self, tmp_path):
		config_path, dest_config = self._setup(tmp_path)
		token = self._enter(config_path, dest_config)
		(tmp_path / "dest" / "mailbox.sqlite3").write_bytes(b"squatter")
		with pytest.raises(b6.BatonError) as excinfo:
			b6.move_copy(config_path, **self._lead())
		assert excinfo.value.exit_code == b6.EXIT_DAMAGE

	def test_enter_validates_destination_shape(self, tmp_path):
		config_path, _ = self._setup(tmp_path)
		for bad in ("relative/baton.json", "/nonexistent-dir/baton.json",
		            str(tmp_path / "dest") + "/", str(tmp_path)):
			with pytest.raises(b6.BatonError):
				b6.maintenance_enter(config_path, participant="hq.lead", actor="lead",
				                     seed=SEED_C, reason="move", move=True, destination=bad)
		with pytest.raises(b6.BatonError, match="boolean"):
			b6.maintenance_enter(config_path, participant="hq.lead", actor="lead",
			                     seed=SEED_C, reason="move", move=1,
			                     destination=str(tmp_path / "dest" / "baton.json"))

	def test_crash_window_zero_active_and_human_recovery(self, tmp_path):
		config_path, dest_config = self._setup(tmp_path)
		token = self._enter(config_path, dest_config)
		b6.move_copy(config_path, **self._lead())
		for path in (config_path, dest_config):
			with pytest.raises(b6.BatonError) as excinfo:
				with b6.open_instance(path) as st:
					send_one(st)
			assert excinfo.value.exit_code == b6.EXIT_GATED
		b6.move_bind_destination(dest_config, token=token, **self._lead())
		b6.move_activate(dest_config, token=token, **self._lead())
		with b6.open_instance(dest_config) as st:
			send_one(st)


def _move_copy_with_fault(config_path, point, queue):
	import baton_v6 as mod
	def hook(p):
		if p == point:
			os._exit(9)
	mod._FAULT_HOOK = hook
	try:
		mod.move_copy(config_path, participant="hq.lead", actor="lead", seed="c" * 32)
		queue.put("completed")
	except mod.BatonError as exc:
		queue.put(f"error:{exc}")


class TestMoveFaultMatrix:
	POINTS = ["move:pre-drain", "move:post-drain", "move:config-copied", "move:db-copied"]

	@pytest.mark.parametrize("point", POINTS)
	def test_kill_then_resume_same_move(self, tmp_path, point):
		src = tmp_path / "src"
		src.mkdir()
		config_path = str(src / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		b6.init_instance(config_path)
		with b6.open_instance(config_path) as st:
			send_one(st)
		dest = tmp_path / "dest"
		dest.mkdir()
		dest_config = str(dest / "baton.json")
		token = b6.maintenance_enter(config_path, participant="hq.lead", actor="lead",
		                             seed=SEED_C, reason="relocating", move=True,
		                             destination=dest_config)["move_token"]
		ctx = multiprocessing.get_context("spawn")
		queue = ctx.Queue()
		proc = ctx.Process(target=_move_copy_with_fault, args=(config_path, point, queue))
		proc.start()
		proc.join(60)
		assert proc.exitcode == 9
		# Fresh-process resume of the SAME move: completes to 'copied'.
		result = b6.move_copy(config_path, participant="hq.lead", actor="lead", seed=SEED_C)
		assert result["stage"] == "copied"
		b6.move_bind_destination(dest_config, token=token, participant="hq.lead",
		                         actor="lead", seed=SEED_C)
		b6.move_activate(dest_config, token=token, participant="hq.lead", actor="lead",
		                 seed=SEED_C)
		with b6.open_instance(dest_config) as st:
			assert len(st.scan("acme.implementer")["pending"]) == 1


class TestMigrateGate:
	def test_migrate_requires_maintenance_and_reports_no_path(self, instance):
		with pytest.raises(b6.BatonError, match="maintenance gate"):
			b6.migrate_instance(instance, participant="hq.lead", actor="lead", seed=SEED_C)
		b6.maintenance_enter(instance, participant="hq.lead", actor="lead", seed=SEED_C,
		                     reason="migration attempt")
		with pytest.raises(b6.BatonError, match="no migration path"):
			b6.migrate_instance(instance, participant="hq.lead", actor="lead", seed=SEED_C)

	def test_migrate_requires_capability(self, instance):
		with pytest.raises(b6.BatonError, match="'config' capability"):
			b6.migrate_instance(instance, participant="acme.reviewer", actor="rev1", seed=SEED_A)


# ---------------------------------------------------------------------------
# Move round-5 pins: post-bind clone, routing history, committed-boundary
# crash matrix, streaming copy
# ---------------------------------------------------------------------------

def _move_setup(tmp_path):
	src = tmp_path / "src"
	src.mkdir()
	config_path = str(src / "baton.json")
	with open(config_path, "w") as handle:
		json.dump(make_config(), handle)
	b6.init_instance(config_path)
	with b6.open_instance(config_path) as st:
		send_one(st)
	dest = tmp_path / "dest"
	dest.mkdir()
	dest_config = str(dest / "baton.json")
	token = b6.maintenance_enter(config_path, participant="hq.lead", actor="lead",
	                             seed=SEED_C, reason="relocating", move=True,
	                             destination=dest_config)["move_token"]
	return config_path, dest_config, token


LEAD = {"participant": "hq.lead", "actor": "lead", "seed": "c" * 32}


class TestPostBindClone:
	def test_post_bind_clone_cannot_activate(self, tmp_path):
		"""Red-first order per review: rogue activation is attempted BEFORE
		the real destination activates."""
		import shutil
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		rogue = tmp_path / "rogue"
		rogue.mkdir()
		shutil.copy(os.path.join(os.path.dirname(dest_config), "mailbox.sqlite3"),
		            rogue / "mailbox.sqlite3")
		shutil.copy(dest_config, rogue / "baton.json")
		with pytest.raises(b6.BatonError, match="DESTINATION route") as excinfo:
			b6.move_activate(str(rogue / "baton.json"), token=token, **LEAD)
		assert excinfo.value.exit_code == b6.EXIT_DAMAGE
		b6.move_activate(dest_config, token=token, **LEAD)
		writable = []
		for path in (dest_config, str(rogue / "baton.json")):
			try:
				with b6.open_instance(path) as st:
					send_one(st)
				writable.append(path)
			except b6.BatonError:
				pass
		assert writable == [dest_config]

	def test_activated_clone_cannot_acknowledge_retry(self, tmp_path):
		import shutil
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		b6.move_activate(dest_config, token=token, **LEAD)
		rogue = tmp_path / "rogue2"
		rogue.mkdir()
		shutil.copy(os.path.join(os.path.dirname(dest_config), "mailbox.sqlite3"),
		            rogue / "mailbox.sqlite3")
		shutil.copy(dest_config, rogue / "baton.json")
		with pytest.raises(b6.BatonError, match="DESTINATION route"):
			b6.move_activate(str(rogue / "baton.json"), token=token, **LEAD)

	def test_bound_clone_cannot_acknowledge_bind_retry(self, tmp_path):
		import shutil
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		rogue = tmp_path / "rogue3"
		rogue.mkdir()
		shutil.copy(os.path.join(os.path.dirname(dest_config), "mailbox.sqlite3"),
		            rogue / "mailbox.sqlite3")
		shutil.copy(dest_config, rogue / "baton.json")
		with pytest.raises(b6.BatonError, match="DESTINATION route"):
			b6.move_bind_destination(str(rogue / "baton.json"), token=token, **LEAD)


class TestRoutingHistory:
	def test_decommission_retry_wrong_route_rejects(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		b6.move_activate(dest_config, token=token, **LEAD)
		b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		retry = b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		assert retry["already_committed"] is True
		with pytest.raises(b6.BatonError, match="differs from the committed route"):
			b6.move_decommission(config_path, token=token,
			                     moved_to="/definitely/wrong/baton.json", **LEAD)

	def test_ceremony_rows_retain_route(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		b6.move_activate(dest_config, token=token, **LEAD)
		with b6.open_instance(dest_config, readonly=True) as ro:
			rows = {r["kind"]: r["peer"] for r in ro.conn.execute(
				"SELECT kind, peer FROM ceremonies WHERE token=?", (token,))}
		assert rows["move_bind_destination"] == dest_config
		assert rows["move_activate"] == dest_config


def _ceremony_with_fault(func_name, args, kwargs, point, queue):
	import baton_v6 as mod
	def hook(p):
		if p == point:
			os._exit(9)
	mod._FAULT_HOOK = hook
	try:
		getattr(mod, func_name)(*args, **kwargs)
		queue.put("completed")
	except mod.BatonError as exc:
		queue.put(f"error:{exc}")


class TestCommittedBoundaryCrashes:
	def _kill(self, func_name, args, kwargs, point):
		ctx = multiprocessing.get_context("spawn")
		queue = ctx.Queue()
		proc = ctx.Process(target=_ceremony_with_fault,
		                   args=(func_name, args, kwargs, point, queue))
		proc.start()
		proc.join(60)
		assert proc.exitcode == 9

	def test_enter_committed_crash_is_discoverable_and_resumable(self, tmp_path):
		src = tmp_path / "src"
		src.mkdir()
		config_path = str(src / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		b6.init_instance(config_path)
		dest = tmp_path / "dest"
		dest.mkdir()
		dest_config = str(dest / "baton.json")
		self._kill("maintenance_enter", (config_path,),
		           dict(reason="relocating", move=True, destination=dest_config, **LEAD),
		           "enter:committed")
		state = b6.move_status_inspect(config_path)
		assert state["move_status"] == "moving"
		assert state["move_peer"] == dest_config
		token = state["move_token"]
		assert token is not None
		b6.move_copy(config_path, **LEAD)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		b6.move_activate(dest_config, token=token, **LEAD)
		with b6.open_instance(dest_config) as st:
			send_one(st)

	def test_bind_activate_decommission_committed_crashes_resume(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		self._kill("move_bind_destination", (dest_config,),
		           dict(token=token, **LEAD), "bind:committed")
		rebind = b6.move_bind_destination(dest_config, token=token, **LEAD)
		assert rebind["already_committed"] is True
		self._kill("move_activate", (dest_config,),
		           dict(token=token, **LEAD), "activate:committed")
		react = b6.move_activate(dest_config, token=token, **LEAD)
		assert react["already_committed"] is True
		self._kill("move_decommission", (config_path,),
		           dict(token=token, moved_to=dest_config, **LEAD), "decommission:committed")
		redec = b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		assert redec["already_committed"] is True
		active = 0
		for path in (config_path, dest_config):
			try:
				with b6.open_instance(path) as st:
					send_one(st)
				active += 1
			except b6.BatonError:
				pass
		assert active == 1


class TestStreamingCopy:
	def test_bounded_chunks_and_resume(self, tmp_path, monkeypatch):
		monkeypatch.setattr(b6, "COPY_CHUNK", 7)  # tiny chunks: bounded by construction
		config_path, dest_config, token = _move_setup(tmp_path)
		result = b6.move_copy(config_path, **LEAD)
		assert result["stage"] == "copied"
		again = b6.move_copy(config_path, **LEAD)
		assert again["already_committed"] is True

	def test_premature_eof_fails_closed(self, tmp_path):
		scratch_dir = tmp_path / "s"
		scratch_dir.mkdir()
		src = tmp_path / "short.bin"
		src.write_bytes(b"abc")
		sfd = os.open(src, os.O_RDONLY)
		dfd = os.open(scratch_dir, os.O_DIRECTORY)
		try:
			with pytest.raises(b6.BatonError, match="premature EOF"):
				b6._stream_publish_from_fd(sfd, 10, dfd, "out.bin", 0o600)
			assert not (scratch_dir / "out.bin").exists()
			leftovers = [p for p in scratch_dir.iterdir()]
			assert leftovers == []  # scratch cleaned
		finally:
			os.close(sfd)
			os.close(dfd)

	def test_fifo_destination_artifact_rejected(self, tmp_path):
		scratch_dir = tmp_path / "s"
		scratch_dir.mkdir()
		os.mkfifo(scratch_dir / "out.bin")
		src = tmp_path / "src.bin"
		src.write_bytes(b"payload")
		sfd = os.open(src, os.O_RDONLY)
		dfd = os.open(scratch_dir, os.O_DIRECTORY)
		try:
			with pytest.raises(b6.BatonError, match="not a regular file"):
				b6._stream_publish_from_fd(sfd, 7, dfd, "out.bin", 0o600)
		finally:
			os.close(sfd)
			os.close(dfd)
			os.unlink(scratch_dir / "out.bin")  # FIFO must not meet tmp scanners

	def test_mismatching_existing_artifact_fails_closed(self, tmp_path):
		scratch_dir = tmp_path / "s"
		scratch_dir.mkdir()
		(scratch_dir / "out.bin").write_bytes(b"different")
		src = tmp_path / "src.bin"
		src.write_bytes(b"payload")
		sfd = os.open(src, os.O_RDONLY)
		dfd = os.open(scratch_dir, os.O_DIRECTORY)
		try:
			with pytest.raises(b6.BatonError, match="MISMATCHING"):
				b6._stream_publish_from_fd(sfd, 7, dfd, "out.bin", 0o600)
		finally:
			os.close(sfd)
			os.close(dfd)


# ---------------------------------------------------------------------------
# Move round-6 pins: symmetric source route, activation-gated decommission,
# nonblocking config artifacts
# ---------------------------------------------------------------------------

class TestSourceRouteBinding:
	def _rogue_from(self, src_dir, tmp_path, name="rogue-src"):
		import shutil
		rogue = tmp_path / name
		rogue.mkdir()
		shutil.copy(os.path.join(src_dir, "mailbox.sqlite3"), rogue / "mailbox.sqlite3")
		shutil.copy(os.path.join(src_dir, "baton.json"), rogue / "baton.json")
		return str(rogue / "baton.json")

	def test_two_active_abort_repro_is_impossible(self, tmp_path):
		"""The round-6 repro: rogue source-role copy + truthful attestation."""
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		rogue_config = self._rogue_from(os.path.dirname(dest_config), tmp_path)
		import shutil
		shutil.rmtree(os.path.dirname(dest_config))  # destination truly destroyed
		with pytest.raises(b6.BatonError, match="SOURCE route"):
			b6.abort_move(rogue_config, token=token, destination_destroyed=True,
			              reason="rogue tries first", **LEAD)
		b6.abort_move(config_path, token=token, destination_destroyed=True,
		              reason="destination destroyed", **LEAD)
		active = []
		for path in (config_path, rogue_config):
			try:
				with b6.open_instance(path) as st:
					send_one(st)
				active.append(path)
			except b6.BatonError:
				pass
		assert active == [config_path]

	def test_rogue_source_copy_cannot_decommission_or_copy(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		rogue_config = self._rogue_from(os.path.dirname(config_path), tmp_path, "rogue-src2")
		with pytest.raises(b6.BatonError, match="SOURCE route"):
			b6.move_decommission(rogue_config, token=token, moved_to=dest_config, **LEAD)
		with pytest.raises(b6.BatonError, match="SOURCE route"):
			b6.move_copy(rogue_config, **LEAD)
		result = b6.move_copy(config_path, **LEAD)  # true source still drives the move
		assert result["already_committed"] is True

	def test_enter_must_run_at_source_route(self, tmp_path):
		src = tmp_path / "src"
		src.mkdir()
		config_path = str(src / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		b6.init_instance(config_path)
		dest = tmp_path / "dest"
		dest.mkdir()
		token = b6.maintenance_enter(config_path, participant="hq.lead", actor="lead",
		                             seed=SEED_C, reason="ok", move=True,
		                             destination=str(dest / "baton.json"))["move_token"]
		state = b6.move_status_inspect(config_path)
		assert state["move_peer"] == str(dest / "baton.json")


class TestActivationGatedDecommission:
	def test_decommission_refused_before_copy_bind_activation(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		with pytest.raises(b6.BatonError, match="does not exist yet"):
			b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		b6.move_copy(config_path, **LEAD)
		with pytest.raises(b6.BatonError, match="not active"):
			b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		with pytest.raises(b6.BatonError, match="not active"):
			b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		b6.move_activate(dest_config, token=token, **LEAD)
		result = b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		assert result["already_committed"] is False
		retry = b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		assert retry["already_committed"] is True


class TestNonblockingConfigArtifacts:
	def test_fifo_destination_config_refuses_without_hanging(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		os.mkfifo(dest_config)
		try:
			with pytest.raises(b6.BatonError):
				b6.move_copy(config_path, **LEAD)
		finally:
			os.unlink(dest_config)

	def test_fifo_instance_config_refuses_without_hanging(self, tmp_path):
		os.mkfifo(tmp_path / "baton.json")
		try:
			with pytest.raises(b6.BatonError, match="regular file"):
				b6.load_config(str(tmp_path / "baton.json"))
		finally:
			os.unlink(tmp_path / "baton.json")


# ---------------------------------------------------------------------------
# Move round-7 pins: symlink-route repro, directory replacement, immutable
# move bindings, inspect completeness
# ---------------------------------------------------------------------------

class TestSymlinkRouteIdentity:
	def test_symlink_route_two_active_repro_impossible(self, tmp_path):
		"""The reviewer's exact five-step repro: symlink the source path at a
		rogue source-role copy; both aborts must NOT succeed."""
		import shutil
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		rogue = tmp_path / "rogue"
		rogue.mkdir()
		shutil.copy(os.path.join(os.path.dirname(dest_config), "mailbox.sqlite3"),
		            rogue / "mailbox.sqlite3")
		shutil.copy(os.path.join(os.path.dirname(dest_config), "baton.json"),
		            rogue / "baton.json")
		shutil.rmtree(os.path.dirname(dest_config))  # attestation true
		src_dir = os.path.dirname(config_path)
		aside = src_dir + "-aside"
		os.rename(src_dir, aside)
		os.symlink(rogue, src_dir)
		try:
			with pytest.raises(b6.BatonError):
				b6.abort_move(config_path, token=token, destination_destroyed=True,
				              reason="rogue via symlinked source path", **LEAD)
		finally:
			os.unlink(src_dir)
			os.rename(aside, src_dir)
		b6.abort_move(config_path, token=token, destination_destroyed=True,
		              reason="true source aborts", **LEAD)
		active = []
		for path in (config_path, str(rogue / "baton.json")):
			try:
				with b6.open_instance(path) as st:
					send_one(st)
				active.append(path)
			except b6.BatonError:
				pass
		assert active == [config_path]

	def test_replaced_source_directory_refuses_source_ceremonies(self, tmp_path):
		"""Rename-aside + fresh directory at the same path: new inode, so the
		bound identity no longer matches even for byte-identical contents."""
		import shutil
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		src_dir = os.path.dirname(config_path)
		aside = src_dir + "-aside"
		os.rename(src_dir, aside)
		shutil.copytree(aside, src_dir)  # same path, DIFFERENT directory inode
		try:
			with pytest.raises(b6.BatonError, match="directory identity|physically reside"):
				b6.abort_move(config_path, token=token, destination_destroyed=True,
				              reason="replaced dir", **LEAD)
			with pytest.raises(b6.BatonError, match="directory identity|physically reside"):
				b6.move_copy(config_path, **LEAD)
		finally:
			shutil.rmtree(src_dir)
			os.rename(aside, src_dir)
		retry = b6.move_copy(config_path, **LEAD)  # true source still works
		assert retry["already_committed"] is True

	def test_replaced_destination_directory_refuses_bind_activate(self, tmp_path):
		import shutil
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		dest_dir = os.path.dirname(dest_config)
		aside = dest_dir + "-aside"
		os.rename(dest_dir, aside)
		shutil.copytree(aside, dest_dir)
		try:
			with pytest.raises(b6.BatonError, match="directory identity|physically reside"):
				b6.move_bind_destination(dest_config, token=token, **LEAD)
		finally:
			shutil.rmtree(dest_dir)
			os.rename(aside, dest_dir)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		b6.move_activate(dest_config, token=token, **LEAD)


class TestMoveBindingAuthority:
	def test_moves_row_created_and_immutable(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		with b6.open_instance(config_path, readonly=True, _for_ceremony=True) as ro:
			row = ro.conn.execute("SELECT * FROM moves WHERE token=?", (token,)).fetchone()
			assert row["source_config"] == config_path
			assert row["destination_config"] == dest_config
			assert row["source_ino"] > 0 and row["destination_ino"] > 0
		with b6.open_instance(config_path, _for_ceremony=True) as st:
			with pytest.raises(sqlite3.IntegrityError, match="immutable"):
				st.conn.execute("UPDATE moves SET source_config='/forged' WHERE token=?", (token,))
			with pytest.raises(sqlite3.IntegrityError, match="immutable"):
				st.conn.execute("DELETE FROM moves")
			with pytest.raises(sqlite3.IntegrityError, match="move entry"):
				st.conn.execute(
					"INSERT INTO moves(token, instance_uuid, source_config, source_dev, source_ino, "
					"destination_config, destination_dev, destination_ino, created_ts) "
					"VALUES('raw', 'u', '/s', 1, 1, '/d', 1, 1, 'now')")

	def test_binding_survives_activation_and_inspect_is_complete(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		state = b6.move_status_inspect(config_path)
		assert state["move_source"] == config_path
		assert state["binding"]["destination_config"] == dest_config
		b6.move_copy(config_path, **LEAD)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		b6.move_activate(dest_config, token=token, **LEAD)
		with b6.open_instance(dest_config, readonly=True) as ro:
			row = ro.conn.execute("SELECT * FROM moves WHERE token=?", (token,)).fetchone()
			assert row is not None
			assert row["source_config"] == config_path
			assert row["destination_config"] == dest_config


# ---------------------------------------------------------------------------
# Move round-8 pins: same-directory rejection, binding as sole authority,
# entry-verb-guarded bindings
# ---------------------------------------------------------------------------

class TestSameDirectoryMove:
	def test_same_config_path_and_same_dir_other_basename_refused(self, tmp_path):
		src = tmp_path / "src"
		src.mkdir()
		config_path = str(src / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		b6.init_instance(config_path)
		for bad_dest in (config_path, str(src / "other-config.json")):
			with pytest.raises(b6.BatonError, match="same directory"):
				b6.maintenance_enter(config_path, participant="hq.lead", actor="lead",
				                     seed=SEED_C, reason="fold", move=True, destination=bad_dest)
			with b6.open_instance(config_path) as st:
				send_one(st)  # source remains active and unchanged after refusal


class TestBindingSoleAuthority:
	def _replaced(self, path):
		import shutil
		aside = path + "-aside"
		os.rename(path, aside)
		shutil.copytree(aside, path)
		return aside

	def test_destination_replacement_fails_stage_discovery(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		dest_dir = os.path.dirname(dest_config)
		aside = self._replaced(dest_dir)
		try:
			with pytest.raises(b6.BatonError, match="directory identity|physically reside"):
				b6.move_copy(config_path, **LEAD)
		finally:
			import shutil
			shutil.rmtree(dest_dir)
			os.rename(aside, dest_dir)
		again = b6.move_copy(config_path, **LEAD)
		assert again["already_committed"] is True

	def test_destination_replacement_fails_decommission(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		b6.move_copy(config_path, **LEAD)
		b6.move_bind_destination(dest_config, token=token, **LEAD)
		b6.move_activate(dest_config, token=token, **LEAD)
		dest_dir = os.path.dirname(dest_config)
		aside = self._replaced(dest_dir)
		try:
			with pytest.raises(b6.BatonError, match="directory identity|physically reside"):
				b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		finally:
			import shutil
			shutil.rmtree(dest_dir)
			os.rename(aside, dest_dir)
		result = b6.move_decommission(config_path, token=token, moved_to=dest_config, **LEAD)
		assert result["already_committed"] is False

	def test_forged_binding_uuid_is_corruption(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		with b6.open_instance(config_path, _for_ceremony=True) as st:
			st._txn_begin("move_enter", "lead", SEED_C, ceremony="move")
			try:
				st.conn.execute(
					"INSERT INTO moves(token, instance_uuid, source_config, source_dev, source_ino, "
					"destination_config, destination_dev, destination_ino, created_ts) "
					"VALUES('ffffffffffffffffffffffffffffffff', 'foreign-uuid', ?, 1, 1, ?, 1, 1, 'now')",
					(config_path, dest_config))
				st._txn_commit()
			except BaseException:
				st._txn_rollback()
				raise
			with pytest.raises(b6.BatonError, match="different instance uuid"):
				st._move_binding("ffffffffffffffffffffffffffffffff")

	def test_noncanonical_caller_spelling_refused(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		alias = os.path.dirname(config_path) + "/./baton.json"
		with pytest.raises(b6.BatonError):
			b6.move_copy(alias, **LEAD)
		result = b6.move_copy(config_path, **LEAD)  # exact spelling proceeds
		assert result["stage"] == "copied"

	def test_binding_insert_requires_entry_verb(self, tmp_path):
		config_path, dest_config, token = _move_setup(tmp_path)
		with b6.open_instance(config_path, _for_ceremony=True) as st:
			st._txn_begin("move", "lead", SEED_C, ceremony="move")
			try:
				with pytest.raises(sqlite3.IntegrityError, match="move entry"):
					st.conn.execute(
						"INSERT INTO moves(token, instance_uuid, source_config, source_dev, "
						"source_ino, destination_config, destination_dev, destination_ino, "
						"created_ts) VALUES('deadbeefdeadbeefdeadbeefdeadbeef', 'u', '/s', 1, 1, "
						"'/d', 1, 1, 'now')")
			finally:
				st._txn_rollback()


# ---------------------------------------------------------------------------
# Move round-9 pins: post-publication identity, non-regular source config
# ---------------------------------------------------------------------------

class TestPostPublicationValidation:
	def test_destination_substitution_after_publication_fails(self, tmp_path, monkeypatch):
		import shutil
		config_path, dest_config, token = _move_setup(tmp_path)
		dest_dir = os.path.dirname(dest_config)
		state = {}
		def substitute(point):
			if point == "move:db-copied":
				aside = dest_dir + "-aside"
				os.rename(dest_dir, aside)
				shutil.copytree(aside, dest_dir)
				state["aside"] = aside
		monkeypatch.setattr(b6, "_FAULT_HOOK", substitute)
		with pytest.raises(b6.BatonError, match="directory identity|physically reside"):
			b6.move_copy(config_path, **LEAD)
		monkeypatch.setattr(b6, "_FAULT_HOOK", None)
		shutil.rmtree(dest_dir)
		os.rename(state["aside"], dest_dir)
		result = b6.move_copy(config_path, **LEAD)  # restored original resumes
		assert result["stage"] == "copied"

	def test_non_regular_source_config_rejected_promptly(self, tmp_path, monkeypatch):
		config_path, dest_config, token = _move_setup(tmp_path)
		def replace_with_fifo(point):
			if point == "move:post-drain":
				os.unlink(config_path)
				os.mkfifo(config_path)
		monkeypatch.setattr(b6, "_FAULT_HOOK", replace_with_fifo)
		try:
			with pytest.raises(b6.BatonError, match="regular file"):
				b6.move_copy(config_path, **LEAD)
		finally:
			monkeypatch.setattr(b6, "_FAULT_HOOK", None)
			os.unlink(config_path)
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		state = b6.move_status_inspect(config_path)
		assert state["move_status"] == "moving"  # move stays gated and resumable
		result = b6.move_copy(config_path, **LEAD)
		assert result["stage"] == "copied"


# ---------------------------------------------------------------------------
# wait/eventing + CLI + observability phase (T8/T19 core + CLI matrix)
# ---------------------------------------------------------------------------
import threading
import time as _time


class TestWait:
	def test_wait_returns_existing_immediately(self, instance):
		with b6.open_instance(instance) as st:
			send_one(st)
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                            timeout_s=5)
		assert result["claim"]["state"] == "active"
		assert result["message"]["body"]["utf8"] == "hello"

	def test_wait_wakes_on_late_send(self, instance):
		def sender():
			_time.sleep(0.5)
			with b6.open_instance(instance) as st:
				send_one(st)
		thread = threading.Thread(target=sender)
		thread.start()
		start = _time.monotonic()
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                            timeout_s=30, rescan_interval_s=20)
		elapsed = _time.monotonic() - start
		thread.join()
		assert result["claim"]["state"] == "active"
		assert elapsed < 15  # woken by the watch, not the 20s rescan

	def test_wait_timeout_is_clean_none(self, instance):
		with pytest.raises(b6.BatonError) as excinfo:
			b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
			                    timeout_s=0.3, rescan_interval_s=0.1)
		assert excinfo.value.exit_code == b6.EXIT_NONE

	def test_degraded_polling_parity(self, instance, monkeypatch):
		class Broken:
			def __init__(self, _dir):
				raise OSError("inotify unavailable")
		monkeypatch.setattr(b6, "_InotifyWatch", Broken)
		def sender():
			_time.sleep(0.4)
			with b6.open_instance(instance) as st:
				send_one(st)
		thread = threading.Thread(target=sender)
		thread.start()
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                            timeout_s=30, rescan_interval_s=0.2)
		thread.join()
		assert result["claim"]["state"] == "active"

	def test_wait_stands_down_when_gated(self, instance):
		b6.maintenance_enter(instance, participant="hq.lead", actor="lead", seed=SEED_C,
		                     reason="gate")
		with pytest.raises(b6.BatonError) as excinfo:
			b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
			                    timeout_s=5)
		assert excinfo.value.exit_code == b6.EXIT_GATED


class TestObservability:
	def test_doctor_healthy_and_scratch_report(self, instance, tmp_path):
		with b6.open_instance(instance) as st:
			send_one(st)
			st.claim("acme.implementer", actor="imp1", seed=SEED_B)
		report = b6.doctor(instance)
		assert report["ok"] is True
		assert report["messages_by_state"] == {"claimed": 1}
		assert len(report["active_claims"]) == 1
		(tmp_path / ".init-stale.sqlite3").write_bytes(b"x")
		(tmp_path / "surprise.txt").write_bytes(b"x")
		report = b6.doctor(instance)
		assert report["stale_scratch"] == [".init-stale.sqlite3"]
		assert report["unrecognized_files"] == ["surprise.txt"]
		assert report["ok"] is True  # residue is a warning, never a problem
		assert len(report["warnings"]) == 2

	def test_dump_redacts_bodies(self, instance):
		with b6.open_instance(instance) as st:
			send_one(st, body=b"secret payload")
		snapshot = b6.dump(instance)
		assert snapshot["messages"][0]["state"] == "pending"
		assert "bytes>" in snapshot["contents"][0]["body"]

	def test_materialize_byte_exact_and_idempotent(self, instance, tmp_path):
		with b6.open_instance(instance) as st:
			mid = send_one(st, body=b"# durable record\\n")
		out = tmp_path / "projections"
		out.mkdir()
		path1 = b6.materialize(instance, mid, str(out))
		assert open(path1, "rb").read() == b"# durable record\\n"
		path2 = b6.materialize(instance, mid, str(out))
		assert path2 == path1  # idempotent re-emit

	def test_materialize_refuses_scrubbed(self, instance, tmp_path):
		with b6.open_instance(instance) as st:
			mid = send_one(st, body=b"gone", retention="transient")
			claim = st.claim("acme.implementer", actor="imp1", seed=SEED_B)
			st.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		with pytest.raises(b6.BatonError, match="transient"):
			b6.materialize(instance, mid, str(tmp_path))


class TestCli:
	def _run(self, *argv):
		import io, contextlib
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			code = b6.main(list(argv))
		return code, out.getvalue()

	def test_cli_roundtrip(self, tmp_path, monkeypatch):
		config_path = str(tmp_path / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		code, _ = self._run("--config", config_path, "init")
		assert code == 0
		monkeypatch.setattr("sys.stdin", type("S", (), {"buffer": __import__("io").BytesIO(b"question body")})())
		code, out = self._run("--config", config_path, "send",
		                      "--participant", "acme.reviewer", "--actor", "rev1",
		                      "--seed", SEED_A, "--to", "acme.implementer",
		                      "--kind", "question", "--thread", "t1")
		assert code == 0
		code, out = self._run("--config", config_path, "claim",
		                      "--participant", "acme.implementer", "--actor", "imp1",
		                      "--seed", SEED_B)
		assert code == 0
		delivery = json.loads(out)
		claim_id = delivery["claim"]["claim_id"]
		assert delivery["message"]["body"]["utf8"] == "question body"
		assert delivery["message"]["attachment"] is None
		monkeypatch.setattr("sys.stdin", type("S", (), {"buffer": __import__("io").BytesIO(b"answer body")})())
		code, out = self._run("--config", config_path, "reply", claim_id,
		                      "--participant", "acme.implementer", "--actor", "imp1",
		                      "--seed", SEED_B, "--kind", "answer", "--outcome", "done")
		assert code == 0
		assert json.loads(out)["already_committed"] is False
		code, out = self._run("--config", config_path, "scan")
		assert code == 0
		assert len(json.loads(out)["pending"]) == 1  # the reply

	def test_cli_exit_codes(self, tmp_path):
		config_path = str(tmp_path / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		self._run("--config", config_path, "init")
		code, _ = self._run("--config", config_path, "claim",
		                    "--participant", "acme.implementer", "--actor", "imp1",
		                    "--seed", SEED_B)
		assert code == b6.EXIT_NONE
		code, _ = self._run("--config", config_path, "maintenance-enter",
		                    "--participant", "hq.lead", "--actor", "lead",
		                    "--seed", SEED_C, "--reason", "gate")
		assert code == 0
		code, _ = self._run("--config", config_path, "send",
		                    "--participant", "acme.reviewer", "--actor", "rev1",
		                    "--seed", SEED_A, "--to", "acme.implementer",
		                    "--kind", "k", "--body", "/dev/null")
		assert code == b6.EXIT_GATED
		code, _ = self._run("--config", config_path, "doctor")
		assert code == 0

	def test_cli_missing_config(self):
		code, _ = self._run("scan")
		assert code == b6.EXIT_PROTOCOL


# ---------------------------------------------------------------------------
# wait/CLI round-2 pins: lossless delivery, CLI totality, event matrix,
# doctor logical checks, durable-only materialize
# ---------------------------------------------------------------------------



def _raw_corrupt(config_path, mutate):
	"""EXPLICIT test-only corruption construction: drop all guard triggers,
	apply the mutation, restore the exact schema triggers. Production
	mutation paths stay guarded; this simulates offline tampering."""
	db = os.path.join(os.path.dirname(config_path), "mailbox.sqlite3")
	conn = sqlite3.connect(db)
	try:
		for (name,) in conn.execute(
				"SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
			conn.execute(f"DROP TRIGGER {name}")
		mutate(conn)
		for sql in b6._TRIGGERS.values():
			conn.execute(sql)
		conn.commit()
	finally:
		conn.close()

class TestLosslessDelivery:
	def test_non_utf8_and_empty_bodies(self, store):
		import base64
		raw = bytes(range(256))
		mid = store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
		                 kind="blob", body=raw)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		delivery = b6._delivery(store, claim)
		body = delivery["message"]["body"]
		assert base64.b64decode(body["base64"]) == raw
		assert body["size"] == 256
		assert "utf8" not in body
		mid2 = store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
		                  kind="empty", body=b"")
		claim2 = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid2)
		body2 = b6._delivery(store, claim2)["message"]["body"]
		assert body2["size"] == 0 and body2["utf8"] == ""

	def test_transient_body_readable_after_claim_until_consumed(self, store):
		mid = store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
		                 kind="t", body=b"still here", retention="transient")
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		delivery = b6._delivery(store, claim)
		assert delivery["message"]["body"]["utf8"] == "still here"
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		post = b6._delivery(store, dict(claim))
		assert post["message"]["body"] is None
		assert post["message"]["content_sha256"] is not None

	def test_attachment_delivery_tuple(self, tmp_path):
		root = tmp_path / "evidence"
		root.mkdir()
		(root / "e.md").write_bytes(b"evidence")
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		with b6.open_instance(config_path) as store:
			mid = store.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			                 kind="ev", body=None, attach={"root_id": "evidence", "path": "e.md"})
			claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
			delivery = b6._delivery(store, claim)
			assert delivery["message"]["body"] is None
			att = delivery["message"]["attachment"]
			assert att["root_id"] == "evidence" and att["path"] == "e.md"
			assert att["sha256"] and att["size"] == 8 and att["generation"] == 1


class TestCliTotality:
	def _run(self, *argv, stdin=b""):
		import io, contextlib
		out = io.StringIO()
		err = io.StringIO()
		import sys as _sys
		old_stdin = _sys.stdin
		_sys.stdin = type("S", (), {"buffer": __import__("io").BytesIO(stdin)})()
		try:
			with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
				code = b6.main(list(argv))
		finally:
			_sys.stdin = old_stdin
		return code, out.getvalue(), err.getvalue()

	def _instance(self, tmp_path):
		config_path = str(tmp_path / "baton.json")
		with open(config_path, "w") as handle:
			json.dump(make_config(), handle)
		b6.main(["--config", config_path, "init"])
		return config_path

	def test_usage_error_is_validation_code(self, tmp_path):
		code, _, _ = self._run("no-such-command")
		assert code == b6.EXIT_PROTOCOL
		code, _, _ = self._run("--help")
		assert code == 0

	def test_body_attach_mutually_exclusive_at_parser(self, tmp_path):
		config_path = self._instance(tmp_path)
		code, _, _ = self._run("--config", config_path, "send",
		                       "--participant", "acme.reviewer", "--actor", "rev1",
		                       "--seed", SEED_A, "--to", "acme.implementer", "--kind", "k",
		                       "--body", "/dev/null", "--attach", "r:p")
		assert code == b6.EXIT_PROTOCOL  # argparse mutual exclusion -> 4

	def test_missing_body_file_clean(self, tmp_path):
		config_path = self._instance(tmp_path)
		code, _, err = self._run("--config", config_path, "send",
		                         "--participant", "acme.reviewer", "--actor", "rev1",
		                         "--seed", SEED_A, "--to", "acme.implementer", "--kind", "k",
		                         "--body", "/nonexistent/body.txt")
		assert code == b6.EXIT_PROTOCOL
		assert "unreadable" in err and "Traceback" not in err

	def test_bad_attach_syntax_clean(self, tmp_path):
		config_path = self._instance(tmp_path)
		code, _, err = self._run("--config", config_path, "send",
		                         "--participant", "acme.reviewer", "--actor", "rev1",
		                         "--seed", SEED_A, "--to", "acme.implementer", "--kind", "k",
		                         "--attach", "no-colon")
		assert code == b6.EXIT_PROTOCOL
		assert "ROOT_ID" in err

	def test_malformed_utf8_config_clean(self, tmp_path):
		config_path = str(tmp_path / "baton.json")
		with open(config_path, "wb") as handle:
			handle.write(bytes([0xFF, 0xFE]) + b" not json")
		code, _, err = self._run("--config", config_path, "scan")
		assert code == b6.EXIT_PROTOCOL
		assert "UTF-8" in err and "Traceback" not in err

	def test_bad_wait_numerics_clean(self, tmp_path):
		config_path = self._instance(tmp_path)
		for bad in ("nan", "inf", "-1"):
			code, _, err = self._run("--config", config_path, "wait",
			                         "--participant", "acme.implementer", "--actor", "imp1",
			                         "--seed", SEED_B, "--timeout", bad)
			assert code == b6.EXIT_PROTOCOL
		code, _, _ = self._run("--config", config_path, "wait",
		                       "--participant", "acme.implementer", "--actor", "imp1",
		                       "--seed", SEED_B, "--timeout", "0.1", "--interval", "0")
		assert code == b6.EXIT_PROTOCOL

	def test_cli_attachment_path(self, tmp_path):
		root = tmp_path / "evidence"
		root.mkdir()
		(root / "e.md").write_bytes(b"evidence")
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.main(["--config", config_path, "init"])
		code, out, _ = self._run("--config", config_path, "send",
		                         "--participant", "acme.reviewer", "--actor", "rev1",
		                         "--seed", SEED_A, "--to", "acme.implementer",
		                         "--kind", "ev", "--attach", "evidence:e.md")
		assert code == 0
		code, out, _ = self._run("--config", config_path, "claim",
		                         "--participant", "acme.implementer", "--actor", "imp1",
		                         "--seed", SEED_B)
		assert code == 0
		delivery = json.loads(out)
		assert delivery["message"]["attachment"]["path"] == "e.md"
		assert delivery["message"]["body"] is None


class TestEventMatrix:
	def test_arm_race_closed_by_requery(self, instance, monkeypatch):
		sent = {}
		def publish_during_arm(point):
			if point == "wait:armed" and not sent:
				sent["done"] = True
				with b6.open_instance(instance) as st:
					send_one(st)
		monkeypatch.setattr(b6, "_FAULT_HOOK", publish_during_arm)
		start = _time.monotonic()
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                            timeout_s=30, rescan_interval_s=25)
		assert result["claim"]["state"] == "active"
		assert _time.monotonic() - start < 10  # requery caught it; no event needed

	def test_wal_checkpoint_reset_still_wakes(self, instance):
		with b6.open_instance(instance) as st:
			send_one(st)
			claim = st.claim("acme.implementer", actor="imp1", seed=SEED_B)
			st.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
			st.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
		def sender():
			_time.sleep(0.4)
			with b6.open_instance(instance) as st:
				send_one(st)
		thread = threading.Thread(target=sender)
		thread.start()
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                            timeout_s=30, rescan_interval_s=20)
		thread.join()
		assert result["claim"]["state"] == "active"

	@pytest.mark.parametrize("flag_name,mask", [
		("overflow", 0x00004000), ("ignored", 0x00008000),
		("move_self", 0x00000800), ("delete_self", 0x00000400),
		("unmount", 0x00002000)])
	def test_decoder_classifies_each_disruption(self, flag_name, mask):
		import struct
		record = struct.pack("iIII", 1, mask, 0, 0)
		flags = b6._decode_inotify(record)
		assert flags["revalidate"] is True
		named = struct.pack("iIII", 1, 0x00000002, 0, 16) + b"mailbox.sqlite3\x00"
		flags = b6._decode_inotify(named)
		assert flags["revalidate"] is False and flags["relevant"] is True
		other = struct.pack("iIII", 1, 0x00000002, 0, 16) + b"unrelated.file\x00\x00"
		assert b6._decode_inotify(other)["relevant"] is False

	def test_armed_mask_contains_required_bits(self):
		for bit in (b6._IN_CREATE, b6._IN_DELETE, b6._IN_MODIFY, b6._IN_MOVED_TO,
		            b6._IN_MOVE_SELF, b6._IN_DELETE_SELF, b6._IN_UNMOUNT):
			assert b6._WATCH_MASK & bit

	@pytest.mark.parametrize("flag_name,mask", [
		("overflow", 0x00004000), ("ignored", 0x00008000),
		("move_self", 0x00000800), ("unmount", 0x00002000)])
	def test_decoded_disruption_forces_validated_reopen(self, instance, monkeypatch,
	                                                    flag_name, mask):
		import struct
		record = struct.pack("iIII", 1, mask, 0, 0)
		decoded = b6._decode_inotify(record)
		assert decoded["revalidate"] is True
		opens = {"count": 0}
		real_open = b6.open_instance
		def counting_open(*args, **kwargs):
			opens["count"] += 1
			return real_open(*args, **kwargs)
		monkeypatch.setattr(b6, "open_instance", counting_open)
		class FakeWatch:
			calls = 0
			def __init__(self, _dir):
				pass
			def close(self):
				pass
			def poll(self, timeout_s):
				FakeWatch.calls += 1
				if FakeWatch.calls == 1:
					return dict(decoded)  # the REAL decoder's verdict for this mask
				return {"revalidate": False, "relevant": False}
		monkeypatch.setattr(b6, "_InotifyWatch", FakeWatch)
		def sender():
			_time.sleep(0.5)
			with real_open(instance) as st:
				send_one(st)
		thread = threading.Thread(target=sender)
		thread.start()
		before = opens["count"]
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                             timeout_s=30, rescan_interval_s=0.2)
		thread.join()
		assert result["claim"]["state"] == "active"
		assert opens["count"] > before + 1  # requery reopened with full validation

	def test_wal_reset_while_blocked_still_wakes(self, instance, monkeypatch):
		armed = threading.Event()
		def on_arm(point):
			if point == "wait:armed":
				armed.set()
		monkeypatch.setattr(b6, "_FAULT_HOOK", on_arm)
		def churn_and_send():
			assert armed.wait(20)  # the waiter is DEMONSTRABLY armed and blocking
			_time.sleep(0.2)  # let it enter poll()
			with b6.open_instance(instance) as st:
				st.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # resets -wal under the waiter
			with b6.open_instance(instance) as st:
				send_one(st)
		thread = threading.Thread(target=churn_and_send)
		thread.start()
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                             timeout_s=30, rescan_interval_s=20)
		thread.join()
		assert result["claim"]["state"] == "active"

	def test_degraded_sleep_receives_configured_interval(self, instance, monkeypatch):
		class Broken:
			def __init__(self, _dir):
				raise OSError("no inotify")
		monkeypatch.setattr(b6, "_InotifyWatch", Broken)
		sleeps = []
		import time as _t
		real_sleep = _t.sleep
		def recording_sleep(seconds):
			sleeps.append(seconds)
			if len(sleeps) == 1:
				with b6.open_instance(instance) as st:
					send_one(st)  # published during the first degraded sleep
			real_sleep(0.01)
		monkeypatch.setattr(_t, "sleep", recording_sleep)
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                             timeout_s=None, rescan_interval_s=3.0)
		assert result["claim"]["state"] == "active"
		assert sleeps[0] == 3.0  # the CONFIGURED interval reached sleep() exactly

	def test_gate_while_blocked_stands_down(self, instance):
		def gater():
			_time.sleep(0.4)
			b6.maintenance_enter(instance, participant="hq.lead", actor="lead", seed=SEED_C,
			                     reason="mid-wait gate")
		thread = threading.Thread(target=gater)
		thread.start()
		with pytest.raises(b6.BatonError) as excinfo:
			b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
			                    timeout_s=30, rescan_interval_s=0.2)
		thread.join()
		assert excinfo.value.exit_code == b6.EXIT_GATED

	def test_invalid_wait_inputs_rejected(self, instance):
		import math
		for bad_timeout in (float("nan"), float("inf"), -1, True):
			with pytest.raises(b6.BatonError, match="finite|timeout"):
				b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
				                    timeout_s=bad_timeout, rescan_interval_s=1)
		for bad_interval in (0, -5, float("nan"), True):
			with pytest.raises(b6.BatonError, match="rescan"):
				b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
				                    timeout_s=0.1, rescan_interval_s=bad_interval)


class TestDoctorLogical:
	def test_orphan_content_detected(self, store):
		send_one(store, body=b"x")
		import hashlib as _h
		_raw_corrupt(store.config_path, lambda conn: conn.execute(
			"INSERT INTO contents(content_id, body, sha256, size, created_ts) "
			"VALUES('orphan01', X'00', ?, 1, 'now')", (_h.sha256(b"\x00").hexdigest(),)))
		report = b6.doctor(store.config_path)
		assert any("owners" in p or "orphan" in p for p in report["problems"])
		assert report["ok"] is False

	def test_accepted_roots_config_mismatch_detected(self, tmp_path):
		root = tmp_path / "evidence"
		root.mkdir()
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		_raw_corrupt(config_path, lambda conn: conn.execute("DELETE FROM accepted_roots"))
		report = b6.doctor(config_path)
		assert any("accepted_roots" in p for p in report["problems"])

	def test_materialize_refuses_pending_and_claimed_transient(self, instance, tmp_path):
		with b6.open_instance(instance) as st:
			mid = send_one(st, body=b"t", retention="transient")
			with pytest.raises(b6.BatonError, match="transient"):
				b6.materialize(instance, mid, str(tmp_path))
			st.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
			with pytest.raises(b6.BatonError, match="transient"):
				b6.materialize(instance, mid, str(tmp_path))


# ---------------------------------------------------------------------------
# Round-3 additions: atomic wait delivery, root guards, audit-chain doctor
# ---------------------------------------------------------------------------

class TestAtomicWaitDelivery:
	def test_gate_after_claim_still_delivers(self, instance, monkeypatch):
		with b6.open_instance(instance) as st:
			send_one(st, body=b"already owned")
		def gate_after_claim(point):
			if point == "wait:claimed":
				b6._FAULT_HOOK = None
				b6.maintenance_enter(instance, participant="hq.lead", actor="lead",
				                     seed=SEED_C, reason="post-claim gate")
		monkeypatch.setattr(b6, "_FAULT_HOOK", gate_after_claim)
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                             timeout_s=10)
		assert result["message"]["body"]["utf8"] == "already owned"

	def test_content_hash_mismatch_is_damage_not_delivery(self, instance):
		with b6.open_instance(instance) as st:
			mid = send_one(st, body=b"real bytes")
		_raw_corrupt(instance, lambda conn: conn.execute(
			"UPDATE contents SET body=X'6861636b6564'"))  # 'hacked'
		with b6.open_instance(instance) as st:
			claim = st.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
			with pytest.raises(b6.BatonError, match="recorded sha256") as excinfo:
				b6._delivery(st, claim)
			assert excinfo.value.exit_code == b6.EXIT_DAMAGE


class TestAcceptedRootsGuards:
	def test_uncontextual_and_wrong_verb_mutations_refused(self, tmp_path):
		root = tmp_path / "evidence"
		root.mkdir()
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		with b6.open_instance(config_path) as st:
			with pytest.raises(sqlite3.IntegrityError, match="regen"):
				st.conn.execute("DELETE FROM accepted_roots")
			with pytest.raises(sqlite3.IntegrityError, match="regen"):
				st.conn.execute("UPDATE accepted_roots SET binding_generation=9")
			st._txn_begin("move", "lead", SEED_C, ceremony=None)
			try:
				with pytest.raises(sqlite3.IntegrityError, match="regen"):
					st.conn.execute(
						"INSERT INTO accepted_roots(root_id, path, binding_generation) "
						"VALUES('forged', '/f', 1)")
			finally:
				st._txn_rollback()
		# The public regen path still succeeds (the only authorized writer).
		extra = tmp_path / "extra"
		extra.mkdir()
		cfg2 = make_config(generation=2)
		cfg2["roots"] = {"evidence": str(root), "extra": str(extra)}
		with open(config_path, "w") as handle:
			json.dump(cfg2, handle)
		result = b6.regen_instance(config_path, participant="hq.lead", actor="lead", seed=SEED_C)
		assert result["accepted_generation"] == 2


class TestAuditChainDoctor:
	def test_duplicate_birth_detected(self, store):
		mid = send_one(store)
		_raw_corrupt(store.config_path, lambda conn: conn.execute(
			"INSERT INTO transitions(entity, entity_id, from_state, to_state, op_id, "
			"participant, actor, seed, verb, at_ts) VALUES('message', ?, NULL, 'pending', "
			"'forged0000000000forged0000000000', 'p.q', 'a', 'b', 'send', 'now')", (mid,)))
		report = b6.doctor(store.config_path)
		assert any("birth" in p for p in report["problems"])

	def test_broken_chain_detected(self, store):
		mid = send_one(store)
		_raw_corrupt(store.config_path, lambda conn: conn.execute(
			"INSERT INTO transitions(entity, entity_id, from_state, to_state, op_id, "
			"participant, actor, seed, verb, at_ts) VALUES('message', ?, 'completed', 'closed', "
			"'forged0000000000forged0000000000', 'p.q', 'a', 'b', 'close', 'now')", (mid,)))
		report = b6.doctor(store.config_path)
		assert any("breaks" in p or "illegal edge" in p or "disagrees" in p
		           for p in report["problems"])

	def test_wrong_tail_detected(self, store):
		mid = send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		_raw_corrupt(store.config_path, lambda conn: conn.execute(
			"DELETE FROM transitions WHERE entity='message' AND to_state='closed'"))
		report = b6.doctor(store.config_path)
		assert any("disagrees" in p for p in report["problems"])

	def test_attachment_mutation_detected_by_doctor(self, tmp_path):
		root = tmp_path / "evidence"
		root.mkdir()
		(root / "e.md").write_bytes(b"original")
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["roots"] = {"evidence": str(root)}
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		with b6.open_instance(config_path) as st:
			st.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			        kind="ev", body=None, attach={"root_id": "evidence", "path": "e.md"})
		assert b6.doctor(config_path)["ok"] is True
		(root / "e.md").write_bytes(b"tampered")
		report = b6.doctor(config_path)
		assert any("attachment" in p for p in report["problems"])
		assert report["ok"] is False

	def test_content_byte_mismatch_detected(self, store):
		send_one(store, body=b"real")
		_raw_corrupt(store.config_path, lambda conn: conn.execute(
			"UPDATE contents SET body=X'00'"))
		report = b6.doctor(store.config_path)
		assert any("disagree with recorded" in p for p in report["problems"])

	def test_projection_inventory(self, tmp_path):
		proj = tmp_path / "proj"
		proj.mkdir()
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["participants"]["acme.reviewer"]["projection_dir"] = str(proj)
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		with b6.open_instance(config_path) as st:
			mid = st.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			              kind="q", body=b"# record\\n")
		path = b6.materialize(config_path, mid, str(proj))
		report = b6.doctor(config_path)
		assert report["projections"]["checked"] == 1
		assert report["projections"]["orphans"] == []
		(proj / "message-2020-01-01T00-00-00Z-deadbeefdeadbeefdeadbeefdeadbeef.md").write_bytes(b"x")
		report = b6.doctor(config_path)
		assert len(report["projections"]["orphans"]) == 1
		assert report["ok"] is True  # orphan projections warn, never fail


class TestStrictJsonOutput:
	def test_non_string_keys_and_nonfinite_rejected(self):
		with pytest.raises(b6.BatonError, match="non-string"):
			b6._to_jsonable({1: "x"})
		with pytest.raises(b6.BatonError, match="non-finite"):
			b6._to_jsonable({"x": float("inf")})


class TestRound4Additions:
	def test_forged_attribution_detected(self, store):
		mid = send_one(store)
		claim = store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		store.close_claim(claim["claim_id"], actor="imp1", seed=SEED_B)
		# Valid chain/edge/verb; ONLY the attribution is malformed.
		_raw_corrupt(store.config_path, lambda conn: conn.execute(
			"UPDATE transitions SET seed='not-a-seed' WHERE entity='claim'"))
		report = b6.doctor(store.config_path)
		assert any("malformed seed" in p for p in report["problems"])
		assert not any("birth" in p or "breaks" in p or "illegal" in p or "disagrees" in p
		               for p in report["problems"])

	def test_configured_projection_prefix_used(self, tmp_path):
		proj = tmp_path / "proj"
		proj.mkdir()
		config_path = str(tmp_path / "baton.json")
		cfg = make_config()
		cfg["participants"]["acme.reviewer"]["projection_dir"] = str(proj)
		cfg["participants"]["acme.reviewer"]["projection_prefix"] = "review"
		with open(config_path, "w") as handle:
			json.dump(cfg, handle)
		b6.init_instance(config_path)
		with b6.open_instance(config_path) as st:
			mid = st.send("acme.reviewer", "acme.implementer", actor="rev1", seed=SEED_A,
			              kind="q", body=b"# record\n")
		path = b6.materialize(config_path, mid, str(proj), prefix="review")
		assert os.path.basename(path).startswith("review-")
		report = b6.doctor(config_path)
		assert report["projections"]["checked"] == 1  # configured prefix inventoried
		assert report["projections"]["orphans"] == []
		default_named = b6.materialize(config_path, mid, str(proj))  # default prefix ignored here
		report = b6.doctor(config_path)
		assert report["projections"]["checked"] == 1  # only the configured prefix counts
		with pytest.raises(b6.BatonError, match="invalid projection prefix"):
			b6.materialize(config_path, mid, str(proj), prefix="Bad Prefix!")

	def test_gate_between_claim_and_fetch_still_delivers(self, instance, monkeypatch):
		"""The seam now fires BEFORE _delivery: content is fetched through the
		already-open store after the instance has been gated."""
		with b6.open_instance(instance) as st:
			send_one(st, body=b"claimed then gated")
		def gate_at_seam(point):
			if point == "wait:claimed":
				b6._FAULT_HOOK = None
				b6.maintenance_enter(instance, participant="hq.lead", actor="lead",
				                     seed=SEED_C, reason="between claim and fetch")
		monkeypatch.setattr(b6, "_FAULT_HOOK", gate_at_seam)
		result = b6.wait_for_message(instance, "acme.implementer", actor="imp1", seed=SEED_B,
		                             timeout_s=10)
		assert result["message"]["body"]["utf8"] == "claimed then gated"


class TestAttributionCoherence:
	def test_impossible_edge_verb_pairing_detected(self, store):
		mid = send_one(store)
		store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		# Chain and tail stay valid; ONLY the verb becomes impossible for
		# the pending->claimed edge.
		_raw_corrupt(store.config_path, lambda conn: conn.execute(
			"UPDATE transitions SET verb='migrate' WHERE entity='message' "
			"AND from_state='pending' AND to_state='claimed'"))
		report = b6.doctor(store.config_path)
		assert any("cannot be produced by verb" in p for p in report["problems"])
		assert not any("birth" in p or "breaks" in p or "disagrees" in p
		               for p in report["problems"])

	def test_same_op_attribution_split_detected(self, store):
		mid = send_one(store)
		store.claim("acme.implementer", actor="imp1", seed=SEED_B, message_id=mid)
		# The claim transaction emits two rows under one op_id; split the
		# actor on exactly one of them, keeping every field lexically valid.
		def split(conn):
			op = conn.execute(
				"SELECT op_id FROM transitions WHERE verb='claim' LIMIT 1").fetchone()[0]
			seq = conn.execute(
				"SELECT seq FROM transitions WHERE op_id=? ORDER BY seq LIMIT 1", (op,)).fetchone()[0]
			conn.execute("UPDATE transitions SET actor='other' WHERE seq=?", (seq,))
		_raw_corrupt(store.config_path, split)
		report = b6.doctor(store.config_path)
		assert any("distinct attribution tuples" in p for p in report["problems"])
		assert not any("cannot be produced" in p for p in report["problems"])

	def test_oversized_participant_detected(self, store):
		send_one(store)
		long_addr = "a." + "b" * 80
		_raw_corrupt(store.config_path, lambda conn: conn.execute(
			"UPDATE transitions SET participant=? WHERE participant IS NOT NULL",
			(long_addr,)))
		report = b6.doctor(store.config_path)
		assert any("malformed participant" in p for p in report["problems"])


# ---------------------------------------------------------------------------
# Phase 5: packaging, distribution, extraction purity
# ---------------------------------------------------------------------------

class TestPackaging:
	def _builder(self):
		import importlib.util
		spec = importlib.util.spec_from_file_location(
			"build_zipapp", os.path.join(os.path.dirname(__file__), "build_zipapp.py"))
		builder = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(builder)
		return builder

	def test_zipapp_deterministic_and_runnable(self, tmp_path):
		import subprocess, sys as _sys
		builder = self._builder()
		m1 = builder.build(str(tmp_path / "a"))
		m2 = builder.build(str(tmp_path / "b"))
		assert m1["artifact_sha256"] == m2["artifact_sha256"]
		assert open(tmp_path / "a" / "bin" / "baton", "rb").read() == \
			open(tmp_path / "b" / "bin" / "baton", "rb").read()
		proc = subprocess.run([_sys.executable, str(tmp_path / "a" / "bin" / "baton"),
		                       "--version"], capture_output=True, text=True)
		assert proc.returncode == 0
		assert f"protocol {b6.PROTOCOL_VERSION}" in proc.stdout

	def test_distribution_root_contract(self, tmp_path):
		"""The manifest's artifact path RESOLVES from the root it sits in and
		hashes to the recorded value."""
		import hashlib as _h
		builder = self._builder()
		root = tmp_path / "dist"
		manifest = builder.build(str(root))
		assert (root / "DISTRIBUTION.json").is_file()
		artifact = root / manifest["artifact"]
		assert artifact.is_file(), "manifest artifact must resolve from the distribution root"
		assert _h.sha256(artifact.read_bytes()).hexdigest() == manifest["artifact_sha256"]
		committed = json.loads(open(os.path.join(os.path.dirname(__file__),
		                                          "DISTRIBUTION.json")).read())
		committed_artifact = os.path.join(os.path.dirname(__file__), committed["artifact"])
		assert os.path.isfile(committed_artifact), "checked-in bin/baton must exist"
		assert _h.sha256(open(committed_artifact, "rb").read()).hexdigest() == \
			committed["artifact_sha256"]
		here_src = open(os.path.join(os.path.dirname(__file__), "baton_v6.py"), "rb").read()
		assert committed["source_sha256"] == _h.sha256(here_src).hexdigest(), \
			"committed manifest is stale against baton_v6.py — rerun build_zipapp.py"
		# The generic protocol doc ships in the distribution root and is
		# hash-pinned by the manifest.
		proto_built = root / manifest["protocol_doc"]
		assert proto_built.is_file()
		assert _h.sha256(proto_built.read_bytes()).hexdigest() == manifest["protocol_doc_sha256"]
		proto_committed = os.path.join(os.path.dirname(__file__), committed["protocol_doc"])
		assert os.path.isfile(proto_committed)
		assert _h.sha256(open(proto_committed, "rb").read()).hexdigest() == \
			committed["protocol_doc_sha256"], "committed manifest stale against the protocol doc"

	def test_bootstrap_floor_syntax_and_logic(self, tmp_path):
		builder = self._builder()
		import ast
		tree = ast.parse(builder.BOOTSTRAP)
		for node in ast.walk(tree):
			assert not isinstance(node, ast.NamedExpr)
			assert type(node).__name__ != "Match"
		lines = builder.BOOTSTRAP.splitlines()
		floor_idx = next(i for i, l in enumerate(lines) if "version_info < (3, 11)" in l)
		import_idx = next(i for i, l in enumerate(lines) if "from baton_v6" in l)
		assert floor_idx < import_idx

	def test_zipapp_imports_own_module_under_poisoned_cwd(self, tmp_path):
		import subprocess, sys as _sys
		builder = self._builder()
		root = tmp_path / "dist"
		builder.build(str(root))
		poison = tmp_path / "poison"
		poison.mkdir()
		(poison / "baton_v6.py").write_text("raise RuntimeError('poisoned import')\n")
		proc = subprocess.run([_sys.executable, str(root / "bin" / "baton"), "--version"],
		                      capture_output=True, text=True, cwd=str(poison),
		                      env={"PATH": os.environ["PATH"], "PYTHONPATH": str(poison)})
		assert proc.returncode == 0, proc.stderr
		assert "poisoned" not in proc.stderr

	REUSABLE_ASSETS = ["baton_v6.py", "test_baton_v6.py", "build_zipapp.py",
	                   "example-baton.json", "config-schema.json", "README.md",
	                   "baton", "DISTRIBUTION.json", "AGENTS-MAILBOX-PROTO.md"]

	@pytest.mark.skipif(os.environ.get("BATON_ISOLATED") == "1",
	                    reason="already inside the isolated run")
	def test_isolated_checkout_runs_full_reusable_suite(self, tmp_path):
		"""T26: the ENTIRE reusable set — including this test file — passes
		from a bare copied tree whose cwd/PYTHONPATH exclude the host."""
		import shutil, subprocess, sys as _sys
		iso = tmp_path / "iso"
		iso.mkdir()
		here = os.path.dirname(__file__)
		for asset in self.REUSABLE_ASSETS:
			src = os.path.join(here, asset)
			shutil.copy(src, iso / asset)
		(iso / "bin").mkdir()
		shutil.copy(os.path.join(here, "bin", "baton"), iso / "bin" / "baton")
		env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(iso),
		       "BATON_ISOLATED": "1", "HOME": str(tmp_path)}
		proc = subprocess.run(
			[_sys.executable, "-m", "pytest", "test_baton_v6.py", "-q", "-x",
			 "-p", "no:cacheprovider"],
			capture_output=True, text=True, cwd=str(iso), env=env, timeout=420)
		assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
		assert " passed" in proc.stdout

	def test_distribution_roundtrip_under_poisoned_env(self, tmp_path):
		"""The PACKED distribution (bin/baton) drives send/claim/reply/doctor
		end-to-end with poisoned CWD and PYTHONPATH — the archive must prefer
		its own module over hostile ones for the full workflow, not just
		--version."""
		import shutil, subprocess, sys as _sys
		builder = self._builder()
		root = tmp_path / "dist"
		builder.build(str(root))
		poison = tmp_path / "poison"
		poison.mkdir()
		(poison / "baton_v6.py").write_text("raise RuntimeError('poisoned import')\n")
		inst = tmp_path / "inst"
		inst.mkdir()
		config_path = str(inst / "baton.json")
		shutil.copy(os.path.join(os.path.dirname(__file__), "example-baton.json"), config_path)
		env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(poison), "HOME": str(tmp_path)}
		def run(*args, stdin=b""):
			return subprocess.run(
				[_sys.executable, str(root / "bin" / "baton"), "--config", config_path, *args],
				input=stdin, capture_output=True, cwd=str(poison), env=env, timeout=60)
		assert run("init").returncode == 0
		proc = run("send", "--participant", "team.reviewer", "--actor", "r1",
		           "--seed", "a" * 32, "--to", "team.implementer", "--kind", "q",
		           stdin=b"distribution body")
		assert proc.returncode == 0, proc.stderr
		proc = run("claim", "--participant", "team.implementer", "--actor", "i1",
		           "--seed", "b" * 32)
		assert proc.returncode == 0, proc.stderr
		delivery = json.loads(proc.stdout)
		assert delivery["message"]["body"]["utf8"] == "distribution body"
		proc = run("reply", delivery["claim"]["claim_id"], "--participant",
		           "team.implementer", "--actor", "i1", "--seed", "b" * 32,
		           "--kind", "a", stdin=b"distribution answer")
		assert proc.returncode == 0, proc.stderr
		assert json.loads(proc.stdout)["already_committed"] is False
		proc = run("doctor")
		assert proc.returncode == 0, proc.stderr
		assert json.loads(proc.stdout)["ok"] is True

	def test_extraction_purity_grep_gate(self):
		"""Project-specific needles across EVERY reusable asset, including
		the packed archive bytes."""
		here = os.path.dirname(__file__)
		# Needles are split-constructed so this tuple (and this comment) can
		# never match itself: host policy-file references are banned, while
		# the distribution's own protocol document is a legitimate
		# self-reference.
		banned = ("dri" + "ft-lang", "dri" + "ft.", "/wo" + "rk/",
		          "fin" + "ding-", "AGE" + "NTS.md")
		for asset in self.REUSABLE_ASSETS + [os.path.join("bin", "baton")]:
			path = os.path.join(here, asset)
			data = open(path, "rb").read()
			for needle in banned:
				assert needle.encode() not in data, \
					f"{needle!r} found in reusable asset {asset}"

	def test_schema_asset_matches_validator_and_example(self):
		here = os.path.dirname(__file__)
		schema = json.loads(open(os.path.join(here, "config-schema.json")).read())
		assert set(schema["fields"]) == set(b6._CONFIG_FIELDS)
		assert set(schema["fields"]["participants"]["value_fields"]) == \
			set(b6._PARTICIPANT_FIELDS)
		example = b6.loads_strict(open(os.path.join(here, "example-baton.json")).read())
		b6.validate_config(example)
