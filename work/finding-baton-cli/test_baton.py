from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


BATON = Path(__file__).resolve().parents[2] / "tools" / "baton" / "baton"
SEED_A = "a" * 32
SEED_B = "b" * 32


class BatonTests(unittest.TestCase):
	def setUp(self) -> None:
		self._tmp = tempfile.TemporaryDirectory(prefix="baton-test-")
		self.root = Path(self._tmp.name)
		self.work = self.root / "work"
		self.finding = self.work / "finding-case"
		self.finding.mkdir(parents=True)
		self.child_finding = self.finding / "findings" / "finding-child"
		self.child_finding.mkdir(parents=True)

	def tearDown(self) -> None:
		self._tmp.cleanup()

	def run_baton(self, *args: str, body: str | None = None) -> subprocess.CompletedProcess[str]:
		env = os.environ.copy()
		env["MAILBOX_REPO_ROOT"] = str(self.root)
		return subprocess.run(
			[str(BATON), *args, "--json"],
			input=body,
			text=True,
			capture_output=True,
			env=env,
			check=False,
		)

	def publish(self, kind: str, timestamp: str, target_name: str = "snapshot.md") -> tuple[Path, Path]:
		target = self.finding / target_name
		target.write_text("frozen snapshot\n", encoding="utf-8")
		token = self.work / f"{kind}-PENDING-{timestamp}"
		token.write_text(f"finding-case/{target_name}\n", encoding="utf-8")
		return token, target

	def claim(self, kind: str = "IMPL", timestamp: str = "2026-08-04T10-00-00Z") -> dict:
		token, _ = self.publish(kind, timestamp)
		role = {"IMPL": "reviewer", "REVIEW": "implementer", "APPROVAL": "human"}[kind]
		args = [role, "claim", token.name]
		if role != "human":
			args.extend(["--actor", f"{role}-one", "--seed", SEED_A])
		result = self.run_baton(*args)
		self.assertEqual(result.returncode, 0, result.stderr)
		return json.loads(result.stdout)

	def test_reply_publishes_response_before_popping_claim(self) -> None:
		claimed = self.claim()
		result = self.run_baton("reviewer", "reply", claimed["claim"], "--actor", "reviewer-one", "--seed", SEED_A, body="Please address the boundary.\n")
		self.assertEqual(result.returncode, 0, result.stderr)
		response = json.loads(result.stdout)
		self.assertFalse((self.work / claimed["claim"]).exists())
		self.assertTrue((self.root / response["detail"]).is_file())
		self.assertTrue((self.root / response["outgoing_token"]).is_file())
		self.assertIn("Please address the boundary.", (self.root / response["detail"]).read_text(encoding="utf-8"))

	def test_announce_atomically_publishes_detail_then_token(self) -> None:
		result = self.run_baton("reviewer", "announce", "finding-case", "--actor", "reviewer-one", "--seed", SEED_A, body="Baton status update.\n")
		self.assertEqual(result.returncode, 0, result.stderr)
		published = json.loads(result.stdout)
		detail = self.root / published["detail"]
		token = self.root / published["outgoing_token"]
		self.assertTrue(detail.is_file())
		self.assertTrue(token.is_file())
		self.assertEqual(token.read_text(encoding="utf-8"), f"{detail.relative_to(self.work).as_posix()}\n")
		self.assertIn("Baton status update.", detail.read_text(encoding="utf-8"))

	def test_adopt_migrates_valid_manual_claim_then_allows_response(self) -> None:
		_, target = self.publish("IMPL", "2026-08-04T10-30-00Z")
		pending = self.work / "IMPL-PENDING-2026-08-04T10-30-00Z"
		claim_name = f"CLAIMED--{pending.name}--BY-reviewer-one--SEED-{SEED_A}--AT-2026-08-04T10-31-00Z"
		claim = self.work / claim_name
		pending.rename(claim)
		adopted = self.run_baton("reviewer", "adopt", claim_name, "--actor", "reviewer-one", "--seed", SEED_A)
		self.assertEqual(adopted.returncode, 0, adopted.stderr)
		adoption = json.loads(adopted.stdout)
		self.assertEqual(adoption["status"], "adopted")
		self.assertEqual(adoption["target_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
		response = self.run_baton("reviewer", "reply", claim_name, "--actor", "reviewer-one", "--seed", SEED_A, body="Adopted response.\n")
		self.assertEqual(response.returncode, 0, response.stderr)
		self.assertFalse(claim.exists())

	def test_adopt_rejects_wrong_actor_and_preserves_manual_claim(self) -> None:
		pending, _ = self.publish("IMPL", "2026-08-04T10-40-00Z")
		claim_name = f"CLAIMED--{pending.name}--BY-reviewer-one--SEED-{SEED_A}--AT-2026-08-04T10-41-00Z"
		claim = self.work / claim_name
		pending.rename(claim)
		result = self.run_baton("reviewer", "adopt", claim_name, "--actor", "reviewer-other", "--seed", SEED_B)
		self.assertEqual(result.returncode, 4)
		self.assertIn("exact actor instance", result.stderr)
		self.assertTrue(claim.is_file())

	def test_adopt_rejects_second_claim_for_same_original(self) -> None:
		pending, _ = self.publish("IMPL", "2026-08-04T10-50-00Z")
		claim_name = f"CLAIMED--{pending.name}--BY-reviewer-one--SEED-{SEED_A}--AT-2026-08-04T10-51-00Z"
		claim = self.work / claim_name
		pending.rename(claim)
		sibling_name = f"CLAIMED--{pending.name}--BY-reviewer-other--SEED-{SEED_B}--AT-2026-08-04T10-52-00Z"
		sibling = self.work / sibling_name
		sibling.write_bytes(claim.read_bytes())
		result = self.run_baton("reviewer", "adopt", claim_name, "--actor", "reviewer-one", "--seed", SEED_A)
		self.assertEqual(result.returncode, 4)
		self.assertIn("another claim exists", result.stderr)
		self.assertTrue(claim.is_file())
		self.assertTrue(sibling.is_file())

	def test_signoff_publishes_detail_without_outgoing_token(self) -> None:
		claimed = self.claim()
		result = self.run_baton("reviewer", "signoff", claimed["claim"], "--actor", "reviewer-one", "--seed", SEED_A, body="No blocking findings.\n")
		self.assertEqual(result.returncode, 0, result.stderr)
		response = json.loads(result.stdout)
		self.assertIsNone(response["outgoing_token"])
		self.assertFalse((self.work / claimed["claim"]).exists())
		self.assertTrue((self.root / response["detail"]).is_file())

	def test_ack_publishes_nonterminal_detail_without_outgoing_token(self) -> None:
		claimed = self.claim()
		result = self.run_baton("reviewer", "ack", claimed["claim"], "--actor", "reviewer-one", "--seed", SEED_A, body="Checkpoint recorded; finding remains open.\n")
		self.assertEqual(result.returncode, 0, result.stderr)
		response = json.loads(result.stdout)
		self.assertIsNone(response["outgoing_token"])
		self.assertFalse((self.work / claimed["claim"]).exists())
		detail = (self.root / response["detail"]).read_text(encoding="utf-8")
		self.assertIn("# Reviewer acknowledgment", detail)
		self.assertIn("finding remains open", detail)

	def test_response_destination_can_select_child_but_not_another_finding(self) -> None:
		claimed = self.claim()
		result = self.run_baton("reviewer", "ack", claimed["claim"], "--destination", "finding-case/findings/finding-child", "--actor", "reviewer-one", "--seed", SEED_A, body="Child checkpoint.\n")
		self.assertEqual(result.returncode, 0, result.stderr)
		response = json.loads(result.stdout)
		self.assertEqual((self.root / response["detail"]).parent, self.child_finding)

		other = self.work / "finding-other"
		other.mkdir()
		claimed = self.claim(timestamp="2026-08-04T10-01-00Z")
		result = self.run_baton("reviewer", "ack", claimed["claim"], "--destination", "finding-other", "--actor", "reviewer-one", "--seed", SEED_A, body="Wrong finding.\n")
		self.assertEqual(result.returncode, 4)
		self.assertIn("top-level finding", result.stderr)
		self.assertTrue((self.work / claimed["claim"]).is_file())

	def test_changed_target_blocks_response_and_preserves_claim(self) -> None:
		claimed = self.claim()
		target = self.work / claimed["target"]
		target.write_text("changed after claim\n", encoding="utf-8")
		result = self.run_baton("reviewer", "reply", claimed["claim"], "--actor", "reviewer-one", "--seed", SEED_A, body="This must not publish.\n")
		self.assertEqual(result.returncode, 4)
		self.assertIn("changed since claim", result.stderr)
		self.assertTrue((self.work / claimed["claim"]).is_file())
		self.assertEqual(list(self.work.glob("REVIEW-PENDING-*")), [])

	def test_only_exact_claiming_agent_instance_can_respond(self) -> None:
		claimed = self.claim()
		result = self.run_baton("reviewer", "reply", claimed["claim"], "--actor", "reviewer-other", "--seed", SEED_B, body="Wrong owner.\n")
		self.assertEqual(result.returncode, 4)
		self.assertIn("exact claiming actor instance", result.stderr)
		self.assertTrue((self.work / claimed["claim"]).is_file())
		self.assertEqual(list(self.work.glob("REVIEW-PENDING-*")), [])

	def test_concurrent_claim_has_one_winner(self) -> None:
		token, _ = self.publish("IMPL", "2026-08-04T11-00-00Z")
		def attempt(actor: str, seed: str) -> subprocess.CompletedProcess[str]:
			return self.run_baton("reviewer", "claim", token.name, "--actor", actor, "--seed", seed)
		with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
			results = list(pool.map(lambda pair: attempt(*pair), (("reviewer-a", SEED_A), ("reviewer-b", SEED_B))))
		self.assertEqual(sum(result.returncode == 0 for result in results), 1, [(r.returncode, r.stderr) for r in results])
		self.assertFalse(token.exists())
		self.assertEqual(len(list(self.work.glob("CLAIMED--IMPL-PENDING-*"))), 1)

	def test_wait_next_uses_interval_then_exits_after_one_claim(self) -> None:
		env = os.environ.copy()
		env["MAILBOX_REPO_ROOT"] = str(self.root)
		proc = subprocess.Popen(
			[str(BATON), "reviewer", "wait-next", "--actor", "reviewer-wait", "--seed", SEED_A, "--interval", "2", "--json"],
			text=True,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			env=env,
		)
		time.sleep(0.08)
		published_at = time.monotonic()
		token, _ = self.publish("IMPL", "2026-08-04T11-30-00Z")
		stdout, stderr = proc.communicate(timeout=3)
		self.assertEqual(proc.returncode, 0, stderr)
		claimed = json.loads(stdout)
		self.assertEqual(claimed["status"], "claimed")
		self.assertLess(time.monotonic() - published_at, 1.0, "inotify wake should not wait for the 2-second safety interval")
		self.assertFalse(token.exists())
		self.assertTrue((self.work / claimed["claim"]).is_file())

	def test_role_mismatch_is_rejected_without_mutation(self) -> None:
		token, _ = self.publish("REVIEW", "2026-08-04T12-00-00Z")
		result = self.run_baton("reviewer", "claim", token.name, "--actor", "reviewer-one", "--seed", SEED_A)
		self.assertEqual(result.returncode, 4)
		self.assertTrue(token.is_file())

	def test_human_claim_and_stdin_approval_need_no_seed(self) -> None:
		claimed = self.claim("APPROVAL", "2026-08-04T13-00-00Z")
		self.assertIn("--BY-slawomir--AT-", claimed["claim"])
		result = self.run_baton("human", "approve", claimed["claim"], body="Approved as proposed.\n")
		self.assertEqual(result.returncode, 0, result.stderr)
		response = json.loads(result.stdout)
		self.assertIn("Decision: approved", (self.root / response["detail"]).read_text(encoding="utf-8"))
		self.assertTrue((self.root / response["outgoing_token"]).is_file())

	def test_malformed_target_is_not_read_or_claimed(self) -> None:
		token = self.work / "IMPL-PENDING-2026-08-04T14-00-00Z"
		token.write_text("../escape.md\n", encoding="utf-8")
		result = self.run_baton("reviewer", "claim", token.name, "--actor", "reviewer-one", "--seed", SEED_A)
		self.assertEqual(result.returncode, 4)
		self.assertFalse(token.exists())
		self.assertEqual(len(list(self.work.glob("CLAIMED--IMPL-PENDING-*"))), 1)


if __name__ == "__main__":
	unittest.main()
