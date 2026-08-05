"""Protocol-v4 implementation for the Baton filesystem handoff tool."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import select
import stat
import sys
from typing import Any


EXIT_NONE = 3
EXIT_PROTOCOL = 4
EXIT_RACE = 5

TIMESTAMP_RE = r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z"
ROLE_TEXT_RE = r"[a-z][a-z0-9_]*"
ACTOR_TEXT_RE = r"[a-z0-9][a-z0-9_-]*?"
MESSAGE_ID_RE = r"[a-f0-9]{12}"
ROLE_RE = re.compile(rf"^{ROLE_TEXT_RE}$")
ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SEED_RE = re.compile(r"^[a-f0-9]{32,}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
THREAD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")

PENDING_RE = re.compile(rf"^PENDING-FROM-(?P<from>{ROLE_TEXT_RE})-TO-(?P<to>{ROLE_TEXT_RE})-(?P<created>{TIMESTAMP_RE})-(?P<id>{MESSAGE_ID_RE})$")
NOTICE_RE = re.compile(rf"^NOTICE-FROM-(?P<from>{ROLE_TEXT_RE})-TO-ALL-(?P<created>{TIMESTAMP_RE})-(?P<id>{MESSAGE_ID_RE})$")
CLAIMED_RE = re.compile(
	rf"^CLAIMED-FROM-(?P<from>{ROLE_TEXT_RE})-TO-(?P<to>{ROLE_TEXT_RE})-(?P<created>{TIMESTAMP_RE})-(?P<id>{MESSAGE_ID_RE})"
	rf"-BY-(?P<actor>{ACTOR_TEXT_RE})(?:-SEED-(?P<seed>[a-f0-9]{{32,}}))?-AT-(?P<claimed>{TIMESTAMP_RE})$"
)

OBSOLETE_V3_RE = re.compile(rf"^(?:REVIEW|IMPL|APPROVAL)-PENDING-{TIMESTAMP_RE}$|^CLAIMED--(?:REVIEW|IMPL|APPROVAL)-PENDING-{TIMESTAMP_RE}--BY-")


class MailboxError(RuntimeError):
	def __init__(self, message: str, exit_code: int = EXIT_PROTOCOL) -> None:
		super().__init__(message)
		self.exit_code = exit_code


def _utc_now() -> dt.datetime:
	return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _format_timestamp(value: dt.datetime) -> str:
	return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _utc_timestamp() -> str:
	return _format_timestamp(_utc_now())


def _parse_timestamp(value: str) -> dt.datetime:
	try:
		return dt.datetime.strptime(value, "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=dt.timezone.utc)
	except ValueError as exc:
		raise MailboxError(f"invalid UTC timestamp: {value!r}") from exc


def _sha256_bytes(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
	h = hashlib.sha256()
	with path.open("rb") as f:
		for chunk in iter(lambda: f.read(1024 * 1024), b""):
			h.update(chunk)
	return h.hexdigest()


def _fsync_dir(path: Path) -> None:
	fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
	try:
		os.fsync(fd)
	finally:
		os.close(fd)


_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = getattr(_libc, "renameat2", None)
if _renameat2 is not None:
	_renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
	_renameat2.restype = ctypes.c_int

_inotify_init1 = getattr(_libc, "inotify_init1", None)
_inotify_add_watch = getattr(_libc, "inotify_add_watch", None)
if _inotify_init1 is not None:
	_inotify_init1.argtypes = [ctypes.c_int]
	_inotify_init1.restype = ctypes.c_int
if _inotify_add_watch is not None:
	_inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
	_inotify_add_watch.restype = ctypes.c_int

IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_IGNORED = 0x00008000
INOTIFY_MASK = IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE | IN_DELETE_SELF | IN_MOVE_SELF


def _rename_noreplace(src: Path, dst: Path) -> None:
	if _renameat2 is None:
		raise MailboxError("atomic renameat2(RENAME_NOREPLACE) is unavailable; refusing mailbox mutation")
	rc = _renameat2(-100, os.fsencode(src), -100, os.fsencode(dst), 1)
	if rc == 0:
		return
	err = ctypes.get_errno()
	if err in (errno.EEXIST, errno.ENOENT):
		raise MailboxError(f"atomic rename lost a race: {src.name} -> {dst.name}", EXIT_RACE)
	raise MailboxError(f"atomic rename failed for {src} -> {dst}: {os.strerror(err)}")


def _open_work_watch(path: Path) -> int:
	if _inotify_init1 is None or _inotify_add_watch is None:
		raise MailboxError("Linux inotify is unavailable; refusing a wait operation")
	fd = _inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
	if fd < 0:
		err = ctypes.get_errno()
		raise MailboxError(f"inotify_init1 failed: {os.strerror(err)}")
	wd = _inotify_add_watch(fd, os.fsencode(path), INOTIFY_MASK)
	if wd >= 0:
		return fd
	err = ctypes.get_errno()
	os.close(fd)
	raise MailboxError(f"inotify_add_watch failed for {path}: {os.strerror(err)}")


def _wait_for_work_change(fd: int, interval: float) -> None:
	ready, _, _ = select.select([fd], [], [], interval)
	if not ready:
		return
	while True:
		try:
			data = os.read(fd, 65536)
		except BlockingIOError:
			return
		if not data:
			raise MailboxError("inotify watch closed while waiting")
		offset = 0
		while offset + 16 <= len(data):
			mask = int.from_bytes(data[offset + 4:offset + 8], byteorder=sys.byteorder)
			name_len = int.from_bytes(data[offset + 12:offset + 16], byteorder=sys.byteorder)
			offset += 16 + name_len
			if mask & IN_IGNORED:
				raise MailboxError("inotify watch was invalidated while waiting")


def _regular_nonsymlink(path: Path, *, label: str) -> os.stat_result:
	try:
		st = path.lstat()
	except FileNotFoundError as exc:
		raise MailboxError(f"{label} does not exist: {path}") from exc
	if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
		raise MailboxError(f"{label} must be a regular non-symlink file: {path}")
	return st


def _atomic_publish_bytes(final: Path, data: bytes) -> None:
	if final.exists() or final.is_symlink():
		raise MailboxError(f"refusing to replace existing published path: {final}", EXIT_RACE)
	tmp = final.parent / f".{final.name}.tmp-baton-{os.getpid()}-{secrets.token_hex(8)}"
	fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
	try:
		with os.fdopen(fd, "wb", closefd=False) as f:
			f.write(data)
			f.flush()
			os.fsync(f.fileno())
	finally:
		os.close(fd)
	try:
		before = _regular_nonsymlink(tmp, label="publication temporary")
		_rename_noreplace(tmp, final)
		after = _regular_nonsymlink(final, label="published file")
		if tmp.exists() or tmp.is_symlink() or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
			raise MailboxError(f"atomic publication verification failed: {final}")
		if _sha256_file(final) != _sha256_bytes(data):
			raise MailboxError(f"published content verification failed: {final}")
		_fsync_dir(final.parent)
	except BaseException:
		if tmp.exists() or tmp.is_symlink():
			tmp.unlink()
		raise


def _json_bytes(value: dict[str, Any]) -> bytes:
	return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


class Mailbox:
	def __init__(self, repo_root: Path) -> None:
		self.repo_root = repo_root.resolve(strict=True)
		self.work = self.repo_root / "work"
		if not self.work.is_dir() or self.work.is_symlink():
			raise MailboxError(f"mailbox root must be a real directory: {self.work}")
		self.roles = self._load_roles()
		root_key = hashlib.sha256(os.fsencode(self.repo_root)).hexdigest()[:24]
		self.receipts = Path("/tmp") / f"drift-baton-{os.getuid()}" / root_key
		self.receipts.mkdir(mode=0o700, parents=True, exist_ok=True)

	def _load_roles(self) -> dict[str, dict[str, str]]:
		path = Path(__file__).with_name("roles.json")
		_regular_nonsymlink(path, label="Baton role configuration")
		try:
			data = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, ValueError) as exc:
			raise MailboxError(f"invalid Baton role configuration: {path}") from exc
		if set(data) != {"protocol_version", "roles"} or data["protocol_version"] != 4 or not isinstance(data["roles"], dict):
			raise MailboxError("Baton role configuration must declare protocol_version 4 and roles")
		roles: dict[str, dict[str, str]] = {}
		for role, config in data["roles"].items():
			if ROLE_RE.fullmatch(role) is None or role == "all" or not isinstance(config, dict):
				raise MailboxError(f"invalid configured role: {role!r}")
			identity = config.get("identity")
			prefix = config.get("detail_prefix")
			if identity not in ("agent", "singleton") or not isinstance(prefix, str) or KIND_RE.fullmatch(prefix) is None:
				raise MailboxError(f"invalid configuration for role {role!r}")
			allowed = {"identity", "detail_prefix"}
			if identity == "singleton":
				allowed.add("singleton_actor")
				actor = config.get("singleton_actor")
				if not isinstance(actor, str) or ACTOR_RE.fullmatch(actor) is None:
					raise MailboxError(f"singleton role {role!r} requires singleton_actor")
			if set(config) != allowed:
				raise MailboxError(f"unknown or missing configuration fields for role {role!r}")
			roles[role] = config
		if not roles:
			raise MailboxError("Baton role configuration contains no roles")
		return roles

	def _identity(self, role: str, actor: str | None, seed: str | None) -> tuple[str, str | None]:
		if role not in self.roles:
			raise MailboxError(f"unknown role {role!r}; add it to tools/baton/roles.json")
		config = self.roles[role]
		if config["identity"] == "singleton":
			if actor is not None or seed is not None:
				raise MailboxError(f"singleton role {role!r} accepts neither --actor nor --seed")
			return config["singleton_actor"], None
		if actor is None or ACTOR_RE.fullmatch(actor) is None:
			raise MailboxError("agent roles require --actor with lowercase letters, digits, hyphens, or underscores")
		if seed is None or SEED_RE.fullmatch(seed) is None:
			raise MailboxError("agent roles require --seed with at least 128 bits of lowercase hexadecimal")
		return actor, seed

	def _detail_dir(self, rel: str) -> Path:
		if rel in ("", "."):
			return self.work
		if "\x00" in rel or "\\" in rel:
			raise MailboxError(f"invalid detail destination: {rel!r}")
		pure = PurePosixPath(rel)
		if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
			raise MailboxError(f"detail destination must be normalized beneath work/: {rel!r}")
		path = self.work.joinpath(*pure.parts)
		try:
			st = path.lstat()
		except FileNotFoundError as exc:
			raise MailboxError(f"detail destination does not exist: {rel!r}") from exc
		if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
			raise MailboxError(f"detail destination must be a real directory: {rel!r}")
		resolved = path.resolve(strict=True)
		resolved_work = self.work.resolve(strict=True)
		if resolved != resolved_work and resolved_work not in resolved.parents:
			raise MailboxError(f"detail destination escapes work/: {rel!r}")
		return path

	def _target(self, rel: str) -> Path:
		if not rel or "\x00" in rel or "\\" in rel:
			raise MailboxError(f"invalid target path: {rel!r}")
		pure = PurePosixPath(rel)
		if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
			raise MailboxError(f"target must be a normalized relative path beneath work/: {rel!r}")
		target = self.work.joinpath(*pure.parts)
		_regular_nonsymlink(target, label="message target")
		resolved = target.resolve(strict=True)
		resolved_work = self.work.resolve(strict=True)
		if resolved.parent != resolved_work and resolved_work not in resolved.parents:
			raise MailboxError(f"target escapes work/: {rel!r}")
		return target

	def _validate_author(self, envelope: dict[str, Any]) -> None:
		role = envelope["from_role"]
		if role not in self.roles:
			raise MailboxError(f"message names unknown sender role {role!r}")
		actor = envelope["author_actor"]
		seed = envelope["author_seed"]
		if not isinstance(actor, str) or ACTOR_RE.fullmatch(actor) is None:
			raise MailboxError("message contains an invalid author actor")
		config = self.roles[role]
		if config["identity"] == "singleton":
			if actor != config["singleton_actor"] or seed is not None:
				raise MailboxError("message author does not match its singleton role")
		elif not isinstance(seed, str) or SEED_RE.fullmatch(seed) is None:
			raise MailboxError("message contains an invalid agent seed")

	def _validate_v4(self, path: Path) -> tuple[dict[str, Any], Path]:
		_regular_nonsymlink(path, label="mailbox message")
		pending = PENDING_RE.fullmatch(path.name)
		notice = NOTICE_RE.fullmatch(path.name)
		claimed = CLAIMED_RE.fullmatch(path.name)
		match = pending or notice or claimed
		if match is None:
			raise MailboxError(f"not a protocol-v4 mailbox name: {path.name!r}")
		try:
			envelope = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, ValueError) as exc:
			raise MailboxError(f"invalid JSON envelope: {path.name}") from exc
		if not isinstance(envelope, dict):
			raise MailboxError(f"message envelope must be an object: {path.name}")
		message_type = "notice" if notice is not None else "handoff"
		required = {"protocol_version", "message_type", "message_id", "from_role", "to_role", "created_at", "target", "kind", "thread_id", "author_actor", "author_seed"}
		if message_type == "notice":
			required.add("expires_at")
		if set(envelope) != required:
			raise MailboxError(f"message envelope fields do not match the v4 {message_type} contract: {path.name}")
		if envelope["protocol_version"] != 4 or envelope["message_type"] != message_type:
			raise MailboxError(f"message envelope protocol/type mismatch: {path.name}")
		to_role = "ALL" if notice is not None else match.group("to")
		if envelope["message_id"] != match.group("id") or envelope["from_role"] != match.group("from") or envelope["to_role"] != to_role or envelope["created_at"] != match.group("created"):
			raise MailboxError(f"message envelope does not match its filename: {path.name}")
		if envelope["from_role"] not in self.roles or (message_type == "handoff" and envelope["to_role"] not in self.roles):
			raise MailboxError(f"message names an unconfigured role: {path.name}")
		if not isinstance(envelope["kind"], str) or KIND_RE.fullmatch(envelope["kind"]) is None:
			raise MailboxError(f"message contains an invalid kind: {path.name}")
		if not isinstance(envelope["thread_id"], str) or THREAD_RE.fullmatch(envelope["thread_id"]) is None:
			raise MailboxError(f"message contains an invalid thread_id: {path.name}")
		_parse_timestamp(envelope["created_at"])
		if message_type == "notice":
			expires = _parse_timestamp(envelope["expires_at"])
			if expires <= _parse_timestamp(envelope["created_at"]):
				raise MailboxError(f"notice expiration must follow creation: {path.name}")
		self._validate_author(envelope)
		return envelope, self._target(envelope["target"])

	def _validate_message(self, path: Path) -> tuple[dict[str, Any], Path]:
		return self._validate_v4(path)

	def _pending(self, role: str) -> list[Path]:
		out: list[Path] = []
		for path in self.work.iterdir():
			match = PENDING_RE.fullmatch(path.name)
			if match is not None and match.group("to") == role:
				self._validate_v4(path)
				out.append(path)
				continue
		return sorted(out, key=lambda item: item.name)

	def _claims(self) -> list[Path]:
		out: list[Path] = []
		for path in self.work.iterdir():
			if CLAIMED_RE.fullmatch(path.name):
				_regular_nonsymlink(path, label="claimed token")
				out.append(path)
		return sorted(out, key=lambda item: item.name)

	def _notices(self) -> list[Path]:
		out: list[Path] = []
		for path in self.work.iterdir():
			if NOTICE_RE.fullmatch(path.name):
				self._validate_v4(path)
				out.append(path)
		return sorted(out, key=lambda item: item.name)

	def _claim_identity(self, path: Path) -> tuple[str, str | None] | None:
		match = CLAIMED_RE.fullmatch(path.name)
		if match is not None:
			return match.group("actor"), match.group("seed")
		return None

	def _ensure_no_existing_claim(self, actor: str, seed: str | None) -> None:
		for existing in self._claims():
			if self._claim_identity(existing) == (actor, seed):
				raise MailboxError(f"this actor instance already owns a claim: {existing.name}")

	def _claim_receipt_path(self, claim_name: str) -> Path:
		return self.receipts / f"claim-{hashlib.sha256(claim_name.encode()).hexdigest()}.json"

	def _notice_receipt_path(self, notice_name: str, role: str, actor: str, seed: str | None, receipt_kind: str) -> Path:
		identity = f"{role}\0{actor}\0{seed or ''}"
		return self.receipts / f"notice-{receipt_kind}-{hashlib.sha256(notice_name.encode()).hexdigest()}-{hashlib.sha256(identity.encode()).hexdigest()}.json"

	def _write_claim_receipt(self, claim: Path, *, role: str, actor: str, seed: str | None, original: str, envelope: dict[str, Any], target: Path) -> Path:
		receipt = self._claim_receipt_path(claim.name)
		data = {
			"version": 2,
			"repo_root": str(self.repo_root),
			"role": role,
			"actor": actor,
			"seed": seed,
			"original_token": original,
			"claim": claim.name,
			"target_rel": envelope["target"],
			"claim_sha256": _sha256_file(claim),
			"target_sha256": _sha256_file(target),
			"claimed_at": _utc_timestamp(),
		}
		_atomic_publish_bytes(receipt, _json_bytes(data))
		return receipt

	def _notice_seen(self, notice: Path, role: str, actor: str, seed: str | None) -> bool:
		receipt = self._notice_receipt_path(notice.name, role, actor, seed, "seen")
		if not receipt.exists():
			return False
		_regular_nonsymlink(receipt, label="notice seen receipt")
		try:
			data = json.loads(receipt.read_text(encoding="utf-8"))
		except (OSError, ValueError) as exc:
			raise MailboxError(f"invalid notice seen receipt: {receipt}") from exc
		envelope, target = self._validate_v4(notice)
		if data.get("notice") != notice.name or data.get("role") != role or data.get("actor") != actor or data.get("seed") != seed or data.get("notice_sha256") != _sha256_file(notice) or data.get("target_sha256") != _sha256_file(target):
			raise MailboxError(f"notice seen receipt does not match its immutable snapshot: {receipt}")
		return True

	def _unseen_notices(self, role: str, actor: str, seed: str | None) -> list[Path]:
		out: list[Path] = []
		now = _utc_now()
		for notice in self._notices():
			envelope, _ = self._validate_v4(notice)
			if _parse_timestamp(envelope["expires_at"]) <= now:
				continue
			if envelope["from_role"] == role and envelope["author_actor"] == actor and envelope["author_seed"] == seed:
				continue
			if not self._notice_seen(notice, role, actor, seed):
				out.append(notice)
		return out

	def scan(self, role: str, *, actor: str | None, seed: str | None) -> dict[str, Any]:
		identity, live_seed = self._identity(role, actor, seed)
		expired_owned: list[str] = []
		now = _utc_now()
		for notice in self._notices():
			envelope, _ = self._validate_v4(notice)
			if envelope["from_role"] == role and envelope["author_actor"] == identity and envelope["author_seed"] == live_seed and _parse_timestamp(envelope["expires_at"]) <= now:
				expired_owned.append(notice.name)
		return {
			"role": role,
			"eligible_pending": [path.name for path in self._pending(role)],
			"unseen_notices": [path.name for path in self._unseen_notices(role, identity, live_seed)],
			"expired_owned_notices": expired_owned,
			"claims": [path.name for path in self._claims()],
		}

	def claim(self, role: str, token_name: str, *, actor: str | None, seed: str | None) -> dict[str, Any]:
		identity, live_seed = self._identity(role, actor, seed)
		pending = PENDING_RE.fullmatch(token_name)
		if pending is None:
			if NOTICE_RE.fullmatch(token_name):
				raise MailboxError("broadcast notices cannot be claimed")
			raise MailboxError(f"not a pending-token basename: {token_name!r}")
		to_role = pending.group("to")
		if to_role != role:
			raise MailboxError(f"pending handoff is addressed to {to_role!r}, not {role!r}")
		self._ensure_no_existing_claim(identity, live_seed)
		source = self.work / token_name
		try:
			before = _regular_nonsymlink(source, label="pending token")
		except MailboxError:
			if not source.exists() and not source.is_symlink():
				raise MailboxError(f"pending token disappeared before claim: {token_name}", EXIT_RACE)
			raise
		at = _utc_timestamp()
		identity_part = f"-BY-{identity}"
		if live_seed is not None:
			identity_part += f"-SEED-{live_seed}"
		claim_name = f"CLAIMED-FROM-{pending.group('from')}-TO-{pending.group('to')}-{pending.group('created')}-{pending.group('id')}{identity_part}-AT-{at}"
		claim = self.work / claim_name
		_rename_noreplace(source, claim)
		if source.exists() or source.is_symlink():
			raise MailboxError(f"claim source still exists after rename: {source}")
		after = _regular_nonsymlink(claim, label="claimed token")
		if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
			raise MailboxError("claim rename changed token identity")
		_fsync_dir(self.work)
		envelope, target = self._validate_message(claim)
		receipt = self._write_claim_receipt(claim, role=role, actor=identity, seed=live_seed, original=token_name, envelope=envelope, target=target)
		return {"status": "claimed", "claim": claim.name, "target": envelope["target"], "from": envelope["from_role"], "to": envelope["to_role"], "kind": envelope["kind"], "receipt": str(receipt)}

	def claim_next(self, role: str, *, actor: str | None, seed: str | None) -> dict[str, Any]:
		pending = self._pending(role)
		if not pending:
			raise MailboxError(f"no handoff addressed to {role!r} is pending", EXIT_NONE)
		last_race: MailboxError | None = None
		for path in pending:
			try:
				return self.claim(role, path.name, actor=actor, seed=seed)
			except MailboxError as exc:
				if exc.exit_code != EXIT_RACE:
					raise
				last_race = exc
		if last_race is not None:
			raise last_race
		raise MailboxError("no eligible handoff could be claimed", EXIT_NONE)

	def see(self, role: str, notice_name: str, *, actor: str | None, seed: str | None) -> dict[str, Any]:
		identity, live_seed = self._identity(role, actor, seed)
		if NOTICE_RE.fullmatch(notice_name) is None:
			raise MailboxError(f"not a broadcast-notice basename: {notice_name!r}")
		notice = self.work / notice_name
		envelope, target = self._validate_v4(notice)
		if _parse_timestamp(envelope["expires_at"]) <= _utc_now():
			raise MailboxError(f"notice has expired: {notice_name}", EXIT_NONE)
		receipt = self._notice_receipt_path(notice_name, role, identity, live_seed, "seen")
		if not self._notice_seen(notice, role, identity, live_seed):
			data = {"version": 1, "notice": notice_name, "role": role, "actor": identity, "seed": live_seed, "notice_sha256": _sha256_file(notice), "target_sha256": _sha256_file(target), "seen_at": _utc_timestamp()}
			_atomic_publish_bytes(receipt, _json_bytes(data))
		return {"status": "notice", "notice": notice_name, "target": envelope["target"], "from": envelope["from_role"], "expires_at": envelope["expires_at"], "receipt": str(receipt)}

	def wait(self, role: str, *, actor: str | None, seed: str | None, interval: float) -> dict[str, Any]:
		if not 0 < interval <= 86400:
			raise MailboxError("--interval must be greater than 0 and no more than 86400 seconds")
		identity, live_seed = self._identity(role, actor, seed)
		self._ensure_no_existing_claim(identity, live_seed)
		fd = _open_work_watch(self.work)
		try:
			while True:
				try:
					return self.claim_next(role, actor=actor, seed=seed)
				except MailboxError as exc:
					if exc.exit_code not in (EXIT_NONE, EXIT_RACE):
						raise
				for notice in self._unseen_notices(role, identity, live_seed):
					try:
						return self.see(role, notice.name, actor=actor, seed=seed)
					except MailboxError as exc:
						if exc.exit_code not in (EXIT_NONE, EXIT_RACE):
							raise
				_wait_for_work_change(fd, interval)
		finally:
			os.close(fd)

	def _load_claim(self, role: str, claim_name: str, *, actor: str | None, seed: str | None) -> tuple[dict[str, Any], Path, Path, Path]:
		identity, live_seed = self._identity(role, actor, seed)
		claim = self.work / claim_name
		envelope, target = self._validate_message(claim)
		if envelope["to_role"] != role:
			raise MailboxError(f"claim is addressed to {envelope['to_role']!r}, not {role!r}")
		claim_identity = self._claim_identity(claim)
		if claim_identity != (identity, live_seed):
			raise MailboxError("only the exact claiming actor instance may answer or pop this claim")
		receipt_path = self._claim_receipt_path(claim_name)
		_regular_nonsymlink(receipt_path, label="claim receipt")
		try:
			receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
		except (OSError, ValueError) as exc:
			raise MailboxError(f"invalid claim receipt: {receipt_path}") from exc
		if receipt.get("claim") != claim_name or receipt.get("role") != role or receipt.get("repo_root") != str(self.repo_root) or receipt.get("actor") != identity or receipt.get("seed") != live_seed:
			raise MailboxError("claim receipt identity does not match this operation")
		if receipt.get("target_rel") != envelope["target"] or receipt.get("claim_sha256") != _sha256_file(claim) or receipt.get("target_sha256") != _sha256_file(target):
			raise MailboxError("claim or authoritative target changed since claim; leaving claim intact")
		return envelope, target, claim, receipt_path

	def _publish_detail(self, role: str, to_role: str, destination: Path, body: bytes, *, actor: str, kind: str, thread_id: str, outcome: str | None, responds_to: str | None) -> tuple[Path, str, str]:
		if not body.strip():
			raise MailboxError("message body is empty; refusing to publish")
		if KIND_RE.fullmatch(kind) is None:
			raise MailboxError(f"invalid message kind: {kind!r}")
		if THREAD_RE.fullmatch(thread_id) is None:
			raise MailboxError(f"invalid thread id: {thread_id!r}")
		timestamp = _utc_timestamp()
		message_id = secrets.token_hex(6)
		detail = destination / f"{self.roles[role]['detail_prefix']}-{timestamp}.md"
		header = ["# Baton message", "", f"Timestamp: {timestamp}", f"From role: {role}", f"Actor: {actor}", f"To role: {to_role}", f"Kind: {kind}", f"Thread: {thread_id}"]
		if outcome is not None:
			header.append(f"Outcome: {outcome}")
		if responds_to is not None:
			header.extend(["", "Responds to the exact incoming claim:", "", f"`{responds_to}`"])
		header.append("")
		payload = "\n".join(header).encode("utf-8") + body
		if not payload.endswith(b"\n"):
			payload += b"\n"
		_atomic_publish_bytes(detail, payload)
		return detail, timestamp, message_id

	def _publish_envelope(self, *, role: str, to_role: str, actor: str, seed: str | None, target: Path, timestamp: str, message_id: str, kind: str, thread_id: str, ttl: float) -> Path:
		envelope: dict[str, Any] = {
			"protocol_version": 4,
			"message_type": "notice" if to_role == "ALL" else "handoff",
			"message_id": message_id,
			"from_role": role,
			"to_role": to_role,
			"created_at": timestamp,
			"target": target.relative_to(self.work).as_posix(),
			"kind": kind,
			"thread_id": thread_id,
			"author_actor": actor,
			"author_seed": seed,
		}
		if to_role == "ALL":
			if not 1 <= ttl <= 31536000:
				raise MailboxError("broadcast --ttl must be from 1 second through 365 days")
			envelope["expires_at"] = _format_timestamp(_parse_timestamp(timestamp) + dt.timedelta(seconds=ttl))
			name = f"NOTICE-FROM-{role}-TO-ALL-{timestamp}-{message_id}"
		else:
			name = f"PENDING-FROM-{role}-TO-{to_role}-{timestamp}-{message_id}"
		token = self.work / name
		_atomic_publish_bytes(token, _json_bytes(envelope))
		if to_role == "ALL":
			receipt = self._notice_receipt_path(name, role, actor, seed, "author")
			_atomic_publish_bytes(receipt, _json_bytes({"version": 1, "notice": name, "role": role, "actor": actor, "seed": seed, "notice_sha256": _sha256_file(token), "target_sha256": _sha256_file(target), "published_at": _utc_timestamp()}))
		return token

	def send(self, role: str, to_role: str, destination_rel: str, body: bytes, *, actor: str | None, seed: str | None, kind: str, thread_id: str | None, ttl: float) -> dict[str, Any]:
		identity, live_seed = self._identity(role, actor, seed)
		if to_role == "all":
			envelope_to = "ALL"
		elif to_role in self.roles:
			envelope_to = to_role
		else:
			raise MailboxError(f"unknown recipient role {to_role!r}; add it to tools/baton/roles.json")
		destination = self._detail_dir(destination_rel)
		thread = thread_id or secrets.token_hex(6)
		detail, timestamp, message_id = self._publish_detail(role, envelope_to, destination, body, actor=identity, kind=kind, thread_id=thread, outcome=None, responds_to=None)
		token = self._publish_envelope(role=role, to_role=envelope_to, actor=identity, seed=live_seed, target=detail, timestamp=timestamp, message_id=message_id, kind=kind, thread_id=thread, ttl=ttl)
		return {"status": "notice-published" if envelope_to == "ALL" else "handoff-published", "detail": detail.relative_to(self.repo_root).as_posix(), "message": token.relative_to(self.repo_root).as_posix(), "from": role, "to": envelope_to, "thread_id": thread}

	def _response_destination(self, target: Path, destination_rel: str | None) -> Path:
		if destination_rel is None:
			return target.parent
		destination = self._detail_dir(destination_rel)
		target_rel = target.relative_to(self.work)
		destination_rel_path = destination.relative_to(self.work)
		if target_rel.parts and target_rel.parts[0].startswith("finding-") and (not destination_rel_path.parts or destination_rel_path.parts[0] != target_rel.parts[0]):
			raise MailboxError("response destination must remain inside the incoming target's top-level finding")
		return destination

	def respond(self, role: str, claim_name: str, body: bytes, *, actor: str | None, seed: str | None, close: bool, to_role: str | None, destination_rel: str | None, kind: str, thread_id: str | None, outcome: str | None, ttl: float) -> dict[str, Any]:
		identity, live_seed = self._identity(role, actor, seed)
		envelope, target, claim, receipt_path = self._load_claim(role, claim_name, actor=actor, seed=seed)
		if close and to_role is not None:
			raise MailboxError("close cannot specify --to because it publishes no outgoing message")
		response_to = envelope["from_role"] if to_role is None else to_role
		if not close and response_to != "all" and response_to not in self.roles:
			raise MailboxError(f"unknown response role {response_to!r}")
		envelope_to = "ALL" if response_to == "all" else response_to
		thread = thread_id or envelope["thread_id"]
		destination = self._response_destination(target, destination_rel)
		detail, timestamp, message_id = self._publish_detail(role, envelope_to if not close else "CLOSED", destination, body, actor=identity, kind=kind, thread_id=thread, outcome=outcome, responds_to=claim_name)
		outgoing: Path | None = None
		if not close:
			outgoing = self._publish_envelope(role=role, to_role=envelope_to, actor=identity, seed=live_seed, target=detail, timestamp=timestamp, message_id=message_id, kind=kind, thread_id=thread, ttl=ttl)
		self._load_claim(role, claim_name, actor=actor, seed=seed)
		claim.unlink()
		_fsync_dir(self.work)
		receipt_path.unlink()
		_fsync_dir(receipt_path.parent)
		return {"status": "closed" if close else "replied", "detail": detail.relative_to(self.repo_root).as_posix(), "outgoing_message": outgoing.relative_to(self.repo_root).as_posix() if outgoing is not None else None, "popped_claim": claim_name}

	def expire(self, role: str, notice_name: str, *, actor: str | None, seed: str | None) -> dict[str, Any]:
		identity, live_seed = self._identity(role, actor, seed)
		if NOTICE_RE.fullmatch(notice_name) is None:
			raise MailboxError(f"not a broadcast-notice basename: {notice_name!r}")
		notice = self.work / notice_name
		envelope, target = self._validate_v4(notice)
		if envelope["from_role"] != role or envelope["author_actor"] != identity or envelope["author_seed"] != live_seed:
			raise MailboxError("only the exact original author instance may expire a broadcast notice")
		if _parse_timestamp(envelope["expires_at"]) > _utc_now():
			raise MailboxError(f"notice has not expired; expires at {envelope['expires_at']}")
		receipt = self._notice_receipt_path(notice_name, role, identity, live_seed, "author")
		_regular_nonsymlink(receipt, label="notice author receipt")
		try:
			data = json.loads(receipt.read_text(encoding="utf-8"))
		except (OSError, ValueError) as exc:
			raise MailboxError(f"invalid notice author receipt: {receipt}") from exc
		if data.get("notice") != notice_name or data.get("role") != role or data.get("actor") != identity or data.get("seed") != live_seed or data.get("notice_sha256") != _sha256_file(notice) or data.get("target_sha256") != _sha256_file(target):
			raise MailboxError("notice or target changed since publication; refusing cleanup")
		try:
			notice.unlink()
		except FileNotFoundError as exc:
			raise MailboxError(f"notice disappeared during expiration: {notice_name}", EXIT_RACE) from exc
		_fsync_dir(self.work)
		receipt.unlink()
		_fsync_dir(receipt.parent)
		return {"status": "expired", "notice": notice_name, "target_retained": envelope["target"]}

	def doctor(self) -> dict[str, Any]:
		checked: list[str] = []
		errors: list[str] = []
		for path in sorted(self.work.iterdir(), key=lambda item: item.name):
			if OBSOLETE_V3_RE.match(path.name):
				checked.append(path.name)
				errors.append(f"{path.name}: obsolete protocol-v3 mailbox state requires explicit human recovery")
				continue
			if not (PENDING_RE.fullmatch(path.name) or NOTICE_RE.fullmatch(path.name) or CLAIMED_RE.fullmatch(path.name)):
				continue
			checked.append(path.name)
			try:
				self._validate_message(path)
			except MailboxError as exc:
				errors.append(f"{path.name}: {exc}")
		return {"checked": checked, "errors": errors}


def _read_body(path_arg: str | None) -> bytes:
	if path_arg is None or path_arg == "-":
		if sys.stdin.isatty():
			raise MailboxError("message requires stdin or a file argument; refusing to prompt or block")
		return sys.stdin.buffer.read()
	path = Path(path_arg)
	_regular_nonsymlink(path, label="message input")
	return path.read_bytes()


def _print_result(result: dict[str, Any], *, as_json: bool) -> None:
	if as_json:
		print(json.dumps(result, sort_keys=True))
		return
	for key, value in result.items():
		if isinstance(value, list):
			print(f"{key}:")
			for item in value:
				print(f"  {item}")
		else:
			print(f"{key}: {value}")


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(prog="baton", description="Atomic role-addressed repository handoffs")
	parser.add_argument("role")
	parser.add_argument("action", help="send, scan, claim, wait, see, reply, close, expire, or doctor")
	parser.add_argument("args", nargs="*")
	parser.add_argument("--actor")
	parser.add_argument("--seed")
	parser.add_argument("--interval", type=float, default=60.0, help="wait safety-rescan interval in seconds (default: 60)")
	parser.add_argument("--destination", help="response detail directory relative to work/")
	parser.add_argument("--to", help="override reply recipient role")
	parser.add_argument("--kind", default="message")
	parser.add_argument("--thread")
	parser.add_argument("--outcome")
	parser.add_argument("--ttl", type=float, default=86400.0, help="broadcast lifetime in seconds (default: 86400)")
	parser.add_argument("--json", action="store_true")
	return parser


def main(argv: list[str] | None = None) -> int:
	ns = _parser().parse_args(argv)
	try:
		root = Path(os.environ.get("BATON_REPO_ROOT", os.environ.get("MAILBOX_REPO_ROOT", Path(__file__).resolve().parents[2])))
		box = Mailbox(root)
		if ns.action == "scan":
			if ns.args:
				raise MailboxError("scan takes no positional arguments")
			result = box.scan(ns.role, actor=ns.actor, seed=ns.seed)
		elif ns.action == "doctor":
			if ns.args:
				raise MailboxError("doctor takes no positional arguments")
			result = box.doctor()
			if result["errors"]:
				_print_result(result, as_json=ns.json)
				return EXIT_PROTOCOL
		elif ns.action == "send":
			if len(ns.args) not in (2, 3):
				raise MailboxError("send requires TO_ROLE DESTINATION [BODY_FILE|-]; stdin is the default")
			body = _read_body(ns.args[2] if len(ns.args) == 3 else None)
			result = box.send(ns.role, ns.args[0], ns.args[1], body, actor=ns.actor, seed=ns.seed, kind=ns.kind, thread_id=ns.thread, ttl=ns.ttl)
		elif ns.action == "claim":
			if len(ns.args) > 1:
				raise MailboxError("claim takes at most one pending-token basename")
			result = box.claim(ns.role, ns.args[0], actor=ns.actor, seed=ns.seed) if ns.args else box.claim_next(ns.role, actor=ns.actor, seed=ns.seed)
		elif ns.action == "wait":
			if ns.args:
				raise MailboxError("wait takes no positional arguments")
			result = box.wait(ns.role, actor=ns.actor, seed=ns.seed, interval=ns.interval)
		elif ns.action == "see":
			if len(ns.args) != 1:
				raise MailboxError("see requires exactly one broadcast-notice basename")
			result = box.see(ns.role, ns.args[0], actor=ns.actor, seed=ns.seed)
		elif ns.action in ("reply", "close"):
			if len(ns.args) not in (1, 2):
				raise MailboxError(f"{ns.action} requires CLAIM [BODY_FILE|-]; stdin is the default")
			body = _read_body(ns.args[1] if len(ns.args) == 2 else None)
			result = box.respond(ns.role, ns.args[0], body, actor=ns.actor, seed=ns.seed, close=ns.action == "close", to_role=ns.to, destination_rel=ns.destination, kind=ns.kind, thread_id=ns.thread, outcome=ns.outcome, ttl=ns.ttl)
		elif ns.action == "expire":
			if len(ns.args) != 1:
				raise MailboxError("expire requires exactly one broadcast-notice basename")
			result = box.expire(ns.role, ns.args[0], actor=ns.actor, seed=ns.seed)
		else:
			raise MailboxError(f"unknown action: {ns.action!r}")
		_print_result(result, as_json=ns.json)
		return 0
	except MailboxError as exc:
		print(f"baton: {exc}", file=sys.stderr)
		return exc.exit_code
