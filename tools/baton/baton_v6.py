"""Protocol-6 Baton: one logical transactional coordination authority.

All protocol state lives in a single SQLite database (`mailbox.sqlite3`
beside the explicitly passed config); there are no filename-state
transitions. This module is semantically independent of any host project:
it knows nothing about repositories, work trees, review workflows, or any
particular participant names. See PLAN (the consolidated v6 design) for
the contract this implements.

Exit-code table (documented contract):
  0 success
  2 environment floor (Python / sqlite3 module / SQLite library)
  3 nothing eligible
  4 protocol / validation error
  5 race / busy / locked
  6 integrity damage (fail closed)
  7 gated (maintenance / moved instance)
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
from typing import Any

EXIT_FLOOR = 2
EXIT_NONE = 3
EXIT_PROTOCOL = 4
EXIT_RACE = 5
EXIT_DAMAGE = 6
EXIT_GATED = 7

PROTOCOL_VERSION = 6
TOOL_VERSION = "1.0.0"
SQLITE_MIN = (3, 37, 0)  # STRICT tables
BUSY_TIMEOUT_MS = 10_000
TRANSIENT_BODY_MAX_BYTES = 64 * 1024
DEFAULT_RETENTION_DAYS = 90
DEFAULT_NOTICE_TTL_SECONDS = 86_400

RETENTION_DURABLE = "durable"
RETENTION_TRANSIENT = "transient"
RETENTIONS = frozenset((RETENTION_DURABLE, RETENTION_TRANSIENT))

ACTOR_MAX = 32
ADDRESS_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
ACTOR_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
SEED_RE = re.compile(r"^[a-f0-9]{32}$")
HEX32_RE = re.compile(r"^[a-f0-9]{32}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
THREAD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
ROOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")

DB_NAME = "mailbox.sqlite3"

# Deleting verbs authorized to remove content rows (retention deletion is
# not mutation): the consuming reply/close transaction scrubs the incoming
# transient body; gc removes aged terminal metadata.
CONTENT_DELETE_VERBS = ("reply", "close", "gc")

# statfs f_type allowlist: known-good local filesystems (fail closed).
LOCAL_FS_MAGICS = {
	0xEF53,        # ext2/3/4
	0x9123683E,    # btrfs
	0x58465342,    # xfs
	0x01021994,    # tmpfs
	0xF2F52010,    # f2fs
}


# Test-only fault-injection seam (PLAN: injected storage-layer hooks, no
# ambient production switch). Production leaves this None.
_FAULT_HOOK = None


def _fault(point: str) -> None:
	if _FAULT_HOOK is not None:
		_FAULT_HOOK(point)


class BatonError(RuntimeError):
	def __init__(self, message: str, exit_code: int = EXIT_PROTOCOL) -> None:
		super().__init__(message)
		self.exit_code = exit_code


def _utc_now_iso() -> str:
	return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
	return secrets.token_hex(16)


_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_ts(ts: str) -> dt.datetime:
	return dt.datetime.strptime(ts, _TS_FMT).replace(tzinfo=dt.timezone.utc)


def _notice_expired(created_ts: str, ttl_seconds: int | None, now_ts: str) -> bool:
	if ttl_seconds is None:
		return False
	return _parse_ts(created_ts) + dt.timedelta(seconds=ttl_seconds) <= _parse_ts(now_ts)


def _iso_minus_days(now_ts: str, days: int) -> str:
	return (_parse_ts(now_ts) - dt.timedelta(days=days)).strftime(_TS_FMT)


# ---------------------------------------------------------------------------
# Strict JSON
# ---------------------------------------------------------------------------

def _reject_dup_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	obj: dict[str, Any] = {}
	for key, value in pairs:
		if key in obj:
			raise BatonError(f"strict JSON: duplicate object key {key!r}")
		obj[key] = value
	return obj


def _reject_constant(name: str) -> Any:
	raise BatonError(f"strict JSON: non-finite constant {name!r} rejected")


def loads_strict(text: str) -> Any:
	try:
		return json.loads(text, object_pairs_hook=_reject_dup_pairs, parse_constant=_reject_constant)
	except BatonError:
		raise
	except json.JSONDecodeError as exc:
		raise BatonError(f"strict JSON: parse error: {exc}") from exc


def canonical_dumps(obj: Any) -> str:
	return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_sha256(obj: Any) -> str:
	return hashlib.sha256(canonical_dumps(obj).encode("utf-8")).hexdigest()


def _expect_str(obj: dict, key: str, where: str, pattern: re.Pattern | None = None, maxlen: int | None = None) -> str:
	value = obj.get(key)
	if type(value) is not str:
		raise BatonError(f"{where}: {key!r} must be a string")
	if maxlen is not None and len(value) > maxlen:
		raise BatonError(f"{where}: {key!r} exceeds maximum length {maxlen}")
	if pattern is not None and not pattern.match(value):
		raise BatonError(f"{where}: {key!r} value {value!r} violates grammar")
	return value


def _expect_int(obj: dict, key: str, where: str, minimum: int | None = None) -> int:
	value = obj.get(key)
	if type(value) is not int:  # bool is not int here, by exact-type check
		raise BatonError(f"{where}: {key!r} must be an integer")
	if minimum is not None and value < minimum:
		raise BatonError(f"{where}: {key!r} must be >= {minimum}")
	return value


def _reject_unknown(obj: dict, allowed: frozenset[str], where: str) -> None:
	unknown = set(obj) - allowed
	if unknown:
		raise BatonError(f"{where}: unknown field(s) {sorted(unknown)!r} rejected")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_FIELDS = frozenset(("config_version", "protocol_version", "generation", "mailbox", "participants", "roots", "retention_days"))
_MAILBOX_FIELDS = frozenset(("name",))
_PARTICIPANT_FIELDS = frozenset(("identity", "singleton_actor", "projection_prefix", "projection_dir", "capabilities"))
_CAPABILITIES = frozenset(("recovery", "config"))


def validate_config(obj: Any) -> dict:
	if type(obj) is not dict:
		raise BatonError("config: top level must be an object")
	_reject_unknown(obj, _CONFIG_FIELDS, "config")
	if _expect_int(obj, "config_version", "config") != 1:
		raise BatonError("config: unsupported config_version")
	if _expect_int(obj, "protocol_version", "config") != PROTOCOL_VERSION:
		raise BatonError(f"config: protocol_version must be {PROTOCOL_VERSION}")
	_expect_int(obj, "generation", "config", minimum=1)
	mailbox = obj.get("mailbox")
	if type(mailbox) is not dict:
		raise BatonError("config: 'mailbox' must be an object")
	_reject_unknown(mailbox, _MAILBOX_FIELDS, "config.mailbox")
	_expect_str(mailbox, "name", "config.mailbox", pattern=re.compile(r"^[a-z0-9][a-z0-9_-]*$"), maxlen=64)
	participants = obj.get("participants")
	if type(participants) is not dict or not participants:
		raise BatonError("config: 'participants' must be a non-empty object")
	for address, spec in participants.items():
		where = f"config.participants[{address!r}]"
		if not ADDRESS_RE.match(address) or len(address) > 64:
			raise BatonError(f"{where}: invalid participant address")
		if type(spec) is not dict:
			raise BatonError(f"{where}: must be an object")
		_reject_unknown(spec, _PARTICIPANT_FIELDS, where)
		identity = _expect_str(spec, "identity", where)
		if identity not in ("agent", "singleton"):
			raise BatonError(f"{where}: identity must be 'agent' or 'singleton'")
		if identity == "singleton":
			_expect_str(spec, "singleton_actor", where, pattern=ACTOR_RE, maxlen=ACTOR_MAX)
		elif "singleton_actor" in spec:
			raise BatonError(f"{where}: singleton_actor is only valid for identity 'singleton'")
		if "capabilities" in spec:
			caps = spec["capabilities"]
			if type(caps) is not list or any(type(c) is not str for c in caps):
				raise BatonError(f"{where}: capabilities must be a list of strings")
			unknown_caps = set(caps) - _CAPABILITIES
			if unknown_caps:
				raise BatonError(f"{where}: unknown capabilities {sorted(unknown_caps)!r}")
			if len(set(caps)) != len(caps):
				raise BatonError(f"{where}: duplicate capabilities")
		if "projection_prefix" in spec:
			_expect_str(spec, "projection_prefix", where, pattern=KIND_RE, maxlen=64)
		if "projection_dir" in spec:
			_expect_str(spec, "projection_dir", where, maxlen=4096)
	roots = obj.get("roots", {})
	if type(roots) is not dict:
		raise BatonError("config: 'roots' must be an object")
	for root_id, path in roots.items():
		if not ROOT_ID_RE.match(root_id) or len(root_id) > 64:
			raise BatonError(f"config.roots: invalid root id {root_id!r}")
		if type(path) is not str or not path.startswith("/"):
			raise BatonError(f"config.roots[{root_id!r}]: must be an absolute path string")
	if "retention_days" in obj:
		_expect_int(obj, "retention_days", "config", minimum=1)
	return obj


def _read_config_at(dirfd: int, name: str) -> tuple[dict, str]:
	"""Open the config existing-only/no-follow RELATIVE to the held instance
	dirfd and read through the fd — no re-resolution window exists between
	validation and read, and the config binds to the same directory identity
	the DB is opened under."""
	try:
		fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dirfd)
	except FileNotFoundError:
		raise BatonError(f"config not found: {name}") from None
	except OSError as exc:
		if exc.errno == errno.ELOOP:
			raise BatonError("config must not be a symlink") from exc
		raise BatonError(f"config unreadable: {exc}") from exc
	try:
		st = os.fstat(fd)
		if not stat.S_ISREG(st.st_mode):
			raise BatonError("config must be a regular file")
		with os.fdopen(fd, "rb", closefd=False) as handle:
			raw = handle.read()
	finally:
		os.close(fd)
	try:
		text = raw.decode("utf-8")
	except UnicodeDecodeError as exc:
		raise BatonError(f"config is not valid UTF-8: {exc}") from exc
	config = validate_config(loads_strict(text))
	return config, canonical_sha256(config)


def load_config(config_path: str) -> tuple[dict, str]:
	"""Validate and load the explicit config; returns (config, canonical digest)."""
	if not os.path.isabs(config_path):
		raise BatonError("config path must be absolute")
	dirfd = open_instance_dir(config_path)
	try:
		return _read_config_at(dirfd, os.path.basename(config_path))
	finally:
		os.close(dirfd)


# ---------------------------------------------------------------------------
# Filesystem anchoring
# ---------------------------------------------------------------------------

def _statfs_ftype(fd: int) -> int:
	import ctypes
	class StatFS(ctypes.Structure):
		_fields_ = [
			("f_type", ctypes.c_long), ("f_bsize", ctypes.c_long),
			("f_blocks", ctypes.c_ulong), ("f_bfree", ctypes.c_ulong),
			("f_bavail", ctypes.c_ulong), ("f_files", ctypes.c_ulong),
			("f_ffree", ctypes.c_ulong), ("f_fsid", ctypes.c_long * 2),
			("f_namelen", ctypes.c_long), ("f_frsize", ctypes.c_long),
			("f_flags", ctypes.c_long), ("f_spare", ctypes.c_long * 4),
		]
	libc = ctypes.CDLL(None, use_errno=True)
	buf = StatFS()
	if libc.fstatfs(fd, ctypes.byref(buf)) != 0:
		raise BatonError("fstatfs failed; cannot verify filesystem", EXIT_DAMAGE)
	return buf.f_type & 0xFFFFFFFF


def _open_dir_no_follow(path: str, what: str) -> int:
	"""Open a canonical absolute directory by walking EVERY component from an
	opened "/" dirfd with O_DIRECTORY|O_NOFOLLOW — no ancestor may be a
	symlink (final-component-only no-follow is not the approved boundary)."""
	if not os.path.isabs(path) or path != os.path.normpath(path):
		raise BatonError(f"{what} path {path!r} must be a canonical absolute path")
	flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
	fd = os.open("/", flags)
	try:
		for component in [c for c in path.split("/") if c]:
			try:
				next_fd = os.open(component, flags, dir_fd=fd)
			except OSError as exc:
				if exc.errno in (errno.ELOOP, errno.ENOTDIR):
					raise BatonError(
						f"{what} {path!r}: component {component!r} is a symlink or not a "
						"directory; refusing", EXIT_DAMAGE) from exc
				raise BatonError(f"{what} {path!r} is not an openable directory: {exc}") from exc
			os.close(fd)
			fd = next_fd
		result, fd = fd, -1
		return result
	finally:
		if fd >= 0:
			os.close(fd)


def _open_root_dir(path: str) -> int:
	"""Configured roots are trust anchors opened via the component-walk
	no-follow authority."""
	return _open_dir_no_follow(path, "root")


def _validate_roots(config: dict) -> None:
	for root_id, path in config.get("roots", {}).items():
		os.close(_open_root_dir(path))


def open_instance_dir(config_path: str) -> int:
	if not os.path.isabs(config_path):
		raise BatonError("config path must be absolute")
	instance_dir = os.path.dirname(config_path) or "/"
	dirfd = _open_dir_no_follow(os.path.normpath(instance_dir), "instance directory")
	ftype = _statfs_ftype(dirfd)
	if ftype not in LOCAL_FS_MAGICS:
		os.close(dirfd)
		raise BatonError(f"instance directory filesystem (statfs f_type 0x{ftype:X}) is not a supported local filesystem", EXIT_DAMAGE)
	return dirfd


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_TABLES: dict[str, str] = {
	"instance_meta": (
		"CREATE TABLE instance_meta(one_row INTEGER PRIMARY KEY CHECK(one_row=1), "
		"uuid TEXT NOT NULL, protocol INTEGER NOT NULL, "
		"accepted_generation INTEGER NOT NULL CHECK(accepted_generation>=1), "
		"config_sha256 TEXT NOT NULL, "
		"maintenance INTEGER NOT NULL DEFAULT 0 CHECK(maintenance IN (0,1)), "
		"maintainer_actor TEXT, maintainer_reason TEXT, "
		"move_status TEXT NOT NULL DEFAULT 'none' CHECK(move_status IN ('none','moving','moved')), "
		"move_token TEXT, move_role TEXT CHECK(move_role IN ('source','destination')), "
		"move_peer TEXT, move_source TEXT, moved_to TEXT, created_ts TEXT NOT NULL, "
		"CHECK(NOT (move_status IN ('moving','moved') AND maintenance = 0)), "
		"CHECK((move_status = 'none') = (move_token IS NULL)), "
		"CHECK((move_status = 'none') = (move_role IS NULL)), "
		"CHECK((move_status = 'none') = (move_peer IS NULL)), "
		"CHECK((move_status = 'none') = (move_source IS NULL)), "
		"CHECK(NOT (move_status = 'moving' AND moved_to IS NOT NULL)), "
		"CHECK((move_status = 'moved') = (moved_to IS NOT NULL))) STRICT"
	),
	"op_context": (
		"CREATE TABLE op_context(one_row INTEGER PRIMARY KEY CHECK(one_row=1), "
		"op_id TEXT, participant TEXT, actor TEXT, seed TEXT, verb TEXT, ts TEXT) STRICT"
	),
	"contents": (
		"CREATE TABLE contents(content_id TEXT PRIMARY KEY, body BLOB NOT NULL, "
		"sha256 TEXT NOT NULL, size INTEGER NOT NULL, created_ts TEXT NOT NULL) STRICT"
	),
	"messages": (
		"CREATE TABLE messages(id TEXT PRIMARY KEY, "
		"from_participant TEXT NOT NULL, to_participant TEXT NOT NULL, "
		"kind TEXT NOT NULL, thread_id TEXT, "
		"retention TEXT NOT NULL CHECK(retention IN ('durable','transient')), "
		"content_id TEXT REFERENCES contents(content_id), content_sha256 TEXT, "
		"attach_root_id TEXT, attach_path TEXT, attach_sha256 TEXT, "
		"attach_size INTEGER, attach_generation INTEGER, "
		"outcome TEXT, created_ts TEXT NOT NULL, "
		"state TEXT NOT NULL CHECK(state IN ('pending','claimed','completed','closed','expired')), "
		"responds_to TEXT REFERENCES messages(id), completed_ts TEXT, "
		"CHECK((state IN ('pending','claimed')) = (completed_ts IS NULL)), "
		"CHECK((content_sha256 IS NOT NULL) + (attach_root_id IS NOT NULL) = 1), "
		"CHECK((attach_root_id IS NULL) = (attach_path IS NULL) "
		"AND (attach_root_id IS NULL) = (attach_sha256 IS NULL) "
		"AND (attach_root_id IS NULL) = (attach_size IS NULL) "
		"AND (attach_root_id IS NULL) = (attach_generation IS NULL))) STRICT"
	),
	"claims": (
		"CREATE TABLE claims(claim_id TEXT PRIMARY KEY, "
		"message_id TEXT NOT NULL REFERENCES messages(id), "
		"actor TEXT NOT NULL, seed TEXT NOT NULL, claimed_ts TEXT NOT NULL, "
		"state TEXT NOT NULL CHECK(state IN ('active','completed','recovered')), "
		"terminal_ts TEXT, "
		"CHECK((state = 'active') = (terminal_ts IS NULL))) STRICT"
	),
	"dispositions": (
		"CREATE TABLE dispositions(claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id), "
		"kind TEXT NOT NULL CHECK(kind IN ('reply','close')), outcome TEXT, "
		"retention TEXT NOT NULL CHECK(retention IN ('durable','transient')), "
		"content_id TEXT REFERENCES contents(content_id), content_sha256 TEXT, "
		"response_message_id TEXT REFERENCES messages(id), created_ts TEXT NOT NULL) STRICT"
	),
	"notices": (
		"CREATE TABLE notices(id TEXT PRIMARY KEY, from_participant TEXT NOT NULL, "
		"author_actor TEXT NOT NULL, author_seed TEXT NOT NULL, "
		"kind TEXT NOT NULL, content_id TEXT REFERENCES contents(content_id), "
		"content_sha256 TEXT, created_ts TEXT NOT NULL, "
		"ttl_seconds INTEGER NOT NULL CHECK(ttl_seconds >= 1)) STRICT"
	),
	"notice_seen": (
		"CREATE TABLE notice_seen(notice_id TEXT NOT NULL REFERENCES notices(id) ON DELETE CASCADE, "
		"participant TEXT NOT NULL, actor TEXT NOT NULL, seed TEXT NOT NULL, "
		"seen_ts TEXT NOT NULL, PRIMARY KEY(notice_id, participant, actor)) STRICT"
	),
	"recoveries": (
		"CREATE TABLE recoveries(recovery_id TEXT PRIMARY KEY, "
		"claim_id TEXT NOT NULL REFERENCES claims(claim_id), "
		"participant TEXT NOT NULL, actor TEXT NOT NULL, seed TEXT NOT NULL, "
		"reason TEXT NOT NULL, created_ts TEXT NOT NULL) STRICT"
	),
	"ceremonies": (
		"CREATE TABLE ceremonies(ceremony_id TEXT PRIMARY KEY, "
		"kind TEXT NOT NULL CHECK(kind IN ('maintenance_enter','maintenance_exit',"
		"'move_bind_destination','move_activate','move_decommission','abort_move','migrate')), "
		"participant TEXT NOT NULL, actor TEXT NOT NULL, seed TEXT NOT NULL, "
		"reason TEXT, token TEXT, peer TEXT, created_ts TEXT NOT NULL) STRICT"
	),
	"moves": (
		"CREATE TABLE moves(token TEXT PRIMARY KEY, instance_uuid TEXT NOT NULL, "
		"source_config TEXT NOT NULL, source_dev INTEGER NOT NULL, source_ino INTEGER NOT NULL, "
		"destination_config TEXT NOT NULL, destination_dev INTEGER NOT NULL, "
		"destination_ino INTEGER NOT NULL, created_ts TEXT NOT NULL) STRICT"
	),
	"accepted_roots": (
		"CREATE TABLE accepted_roots(root_id TEXT PRIMARY KEY, path TEXT NOT NULL, "
		"binding_generation INTEGER NOT NULL CHECK(binding_generation>=1)) STRICT"
	),
	"transitions": (
		"CREATE TABLE transitions(seq INTEGER PRIMARY KEY AUTOINCREMENT, "
		"entity TEXT NOT NULL CHECK(entity IN ('message','claim')), "
		"entity_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, "
		"op_id TEXT NOT NULL, participant TEXT, actor TEXT NOT NULL, seed TEXT NOT NULL, "
		"verb TEXT NOT NULL, at_ts TEXT NOT NULL) STRICT"
	),
}

_INDEXES: dict[str, str] = {
	"contents_sha_idx": "CREATE INDEX contents_sha_idx ON contents(sha256)",
	"messages_dest_idx": "CREATE INDEX messages_dest_idx ON messages(to_participant, state)",
	"messages_thread_idx": "CREATE INDEX messages_thread_idx ON messages(thread_id)",
	"claims_one_active_idx": "CREATE UNIQUE INDEX claims_one_active_idx ON claims(message_id) WHERE state='active'",
	"claims_message_idx": "CREATE INDEX claims_message_idx ON claims(message_id)",
}

_CTX = "(SELECT op_id FROM op_context WHERE one_row=1)"
_CTX_ACTOR = "(SELECT actor FROM op_context WHERE one_row=1)"
_CTX_PART = "(SELECT participant FROM op_context WHERE one_row=1)"
_CTX_SEED = "(SELECT seed FROM op_context WHERE one_row=1)"
_CTX_VERB = "(SELECT verb FROM op_context WHERE one_row=1)"
_CTX_TS = "(SELECT ts FROM op_context WHERE one_row=1)"

_TRIGGERS: dict[str, str] = {
	"trg_msg_insert_guard": (
		f"CREATE TRIGGER trg_msg_insert_guard BEFORE INSERT ON messages "
		f"WHEN {_CTX} IS NULL OR new.state <> 'pending' "
		f"BEGIN SELECT RAISE(ABORT, 'message insert requires operation context and pending birth'); END"
	),
	"trg_msg_birth": (
		f"CREATE TRIGGER trg_msg_birth AFTER INSERT ON messages "
		f"BEGIN INSERT INTO transitions(entity, entity_id, from_state, to_state, op_id, participant, actor, seed, verb, at_ts) "
		f"VALUES('message', new.id, NULL, new.state, {_CTX}, {_CTX_PART}, {_CTX_ACTOR}, {_CTX_SEED}, {_CTX_VERB}, {_CTX_TS}); END"
	),
	"trg_msg_update_guard": (
		f"CREATE TRIGGER trg_msg_update_guard BEFORE UPDATE OF state ON messages "
		f"WHEN {_CTX} IS NULL "
		f"BEGIN SELECT RAISE(ABORT, 'uncontextual message state mutation'); END"
	),
	"trg_msg_edge": (
		"CREATE TRIGGER trg_msg_edge BEFORE UPDATE OF state ON messages "
		"WHEN NOT ((old.state='pending' AND new.state='claimed') "
		"OR (old.state='claimed' AND new.state IN ('completed','closed','pending'))) "
		"BEGIN SELECT RAISE(ABORT, 'illegal message state edge'); END"
	),
	"trg_msg_transition": (
		f"CREATE TRIGGER trg_msg_transition AFTER UPDATE OF state ON messages "
		f"BEGIN INSERT INTO transitions(entity, entity_id, from_state, to_state, op_id, participant, actor, seed, verb, at_ts) "
		f"VALUES('message', new.id, old.state, new.state, {_CTX}, {_CTX_PART}, {_CTX_ACTOR}, {_CTX_SEED}, {_CTX_VERB}, {_CTX_TS}); END"
	),
	"trg_msg_frozen_cols": (
		"CREATE TRIGGER trg_msg_frozen_cols BEFORE UPDATE OF id, from_participant, to_participant, kind, "
		"thread_id, retention, content_sha256, attach_root_id, attach_path, attach_sha256, attach_size, "
		"attach_generation, outcome, created_ts, responds_to ON messages "
		"BEGIN SELECT RAISE(ABORT, 'immutable message column'); END"
	),
	"trg_msg_content_scrub_only": (
		f"CREATE TRIGGER trg_msg_content_scrub_only BEFORE UPDATE OF content_id ON messages "
		f"WHEN new.content_id IS NOT NULL OR old.content_id IS NULL "
		f"OR old.retention IS NOT 'transient' "
		f"OR (old.state NOT IN ('completed','closed') AND new.state NOT IN ('completed','closed')) "
		f"OR {_CTX} IS NULL OR {_CTX_VERB} NOT IN ('reply','close','gc') "
		f"BEGIN SELECT RAISE(ABORT, 'scrub is restricted to a terminal transient message in a consuming operation'); END"
	),
	"trg_msg_completed_ts_guard": (
		"CREATE TRIGGER trg_msg_completed_ts_guard BEFORE UPDATE OF completed_ts ON messages "
		"WHEN NOT ((old.state='claimed' AND new.state IN ('completed','closed') AND new.completed_ts IS NOT NULL) "
		"OR (old.state='claimed' AND new.state='pending' AND new.completed_ts IS NULL)) "
		"BEGIN SELECT RAISE(ABORT, 'completed_ts changes only with its own terminal transition'); END"
	),
	"trg_claim_terminal_ts_guard": (
		"CREATE TRIGGER trg_claim_terminal_ts_guard BEFORE UPDATE OF terminal_ts ON claims "
		"WHEN NOT (old.state='active' AND new.state IN ('completed','recovered') AND new.terminal_ts IS NOT NULL) "
		"BEGIN SELECT RAISE(ABORT, 'terminal_ts changes only with its own terminal transition'); END"
	),
	"trg_claim_insert_guard": (
		f"CREATE TRIGGER trg_claim_insert_guard BEFORE INSERT ON claims "
		f"WHEN {_CTX} IS NULL OR new.state <> 'active' "
		f"BEGIN SELECT RAISE(ABORT, 'claim insert requires operation context and active birth'); END"
	),
	"trg_claim_birth": (
		f"CREATE TRIGGER trg_claim_birth AFTER INSERT ON claims "
		f"BEGIN INSERT INTO transitions(entity, entity_id, from_state, to_state, op_id, participant, actor, seed, verb, at_ts) "
		f"VALUES('claim', new.claim_id, NULL, new.state, {_CTX}, {_CTX_PART}, {_CTX_ACTOR}, {_CTX_SEED}, {_CTX_VERB}, {_CTX_TS}); END"
	),
	"trg_claim_update_guard": (
		f"CREATE TRIGGER trg_claim_update_guard BEFORE UPDATE OF state ON claims "
		f"WHEN {_CTX} IS NULL "
		f"BEGIN SELECT RAISE(ABORT, 'uncontextual claim state mutation'); END"
	),
	"trg_claim_edge": (
		"CREATE TRIGGER trg_claim_edge BEFORE UPDATE OF state ON claims "
		"WHEN NOT (old.state='active' AND new.state IN ('completed','recovered')) "
		"BEGIN SELECT RAISE(ABORT, 'illegal claim state edge'); END"
	),
	"trg_claim_transition": (
		f"CREATE TRIGGER trg_claim_transition AFTER UPDATE OF state ON claims "
		f"BEGIN INSERT INTO transitions(entity, entity_id, from_state, to_state, op_id, participant, actor, seed, verb, at_ts) "
		f"VALUES('claim', new.claim_id, old.state, new.state, {_CTX}, {_CTX_PART}, {_CTX_ACTOR}, {_CTX_SEED}, {_CTX_VERB}, {_CTX_TS}); END"
	),
	"trg_claim_frozen_cols": (
		"CREATE TRIGGER trg_claim_frozen_cols BEFORE UPDATE OF claim_id, message_id, actor, seed, claimed_ts ON claims "
		"BEGIN SELECT RAISE(ABORT, 'immutable claim column'); END"
	),
	"trg_disp_insert_guard": (
		f"CREATE TRIGGER trg_disp_insert_guard BEFORE INSERT ON dispositions "
		f"WHEN {_CTX} IS NULL "
		f"BEGIN SELECT RAISE(ABORT, 'disposition insert requires operation context'); END"
	),
	"trg_disp_reply_hash": (
		"CREATE TRIGGER trg_disp_reply_hash BEFORE INSERT ON dispositions "
		"WHEN new.kind='reply' AND (new.response_message_id IS NULL "
		"OR new.content_sha256 IS NOT (SELECT content_sha256 FROM messages WHERE id=new.response_message_id)) "
		"BEGIN SELECT RAISE(ABORT, 'reply disposition content hash mismatch'); END"
	),
	"trg_disp_update": (
		"CREATE TRIGGER trg_disp_update BEFORE UPDATE ON dispositions "
		"BEGIN SELECT RAISE(ABORT, 'dispositions are immutable'); END"
	),
	"trg_disp_delete": (
		f"CREATE TRIGGER trg_disp_delete BEFORE DELETE ON dispositions "
		f"WHEN {_CTX_VERB} IS NOT 'gc' "
		f"BEGIN SELECT RAISE(ABORT, 'dispositions are removable only by gc'); END"
	),
	"trg_content_update": (
		"CREATE TRIGGER trg_content_update BEFORE UPDATE ON contents "
		"BEGIN SELECT RAISE(ABORT, 'contents are immutable'); END"
	),
	"trg_content_delete": (
		f"CREATE TRIGGER trg_content_delete BEFORE DELETE ON contents "
		f"WHEN {_CTX_VERB} IS NULL OR {_CTX_VERB} NOT IN ('reply','close','gc','expire') "
		f"BEGIN SELECT RAISE(ABORT, 'content deletion restricted to retention operations'); END"
	),
	"trg_msg_delete_guard": (
		f"CREATE TRIGGER trg_msg_delete_guard BEFORE DELETE ON messages "
		f"WHEN {_CTX_VERB} IS NOT 'gc' "
		f"BEGIN SELECT RAISE(ABORT, 'messages are removable only by gc'); END"
	),
	"trg_msg_gc_ledger": (
		f"CREATE TRIGGER trg_msg_gc_ledger AFTER DELETE ON messages "
		f"BEGIN INSERT INTO transitions(entity, entity_id, from_state, to_state, op_id, participant, actor, seed, verb, at_ts) "
		f"VALUES('message', old.id, old.state, 'gc', {_CTX}, {_CTX_PART}, {_CTX_ACTOR}, {_CTX_SEED}, {_CTX_VERB}, {_CTX_TS}); END"
	),
	"trg_claim_delete_guard": (
		f"CREATE TRIGGER trg_claim_delete_guard BEFORE DELETE ON claims "
		f"WHEN {_CTX_VERB} IS NOT 'gc' "
		f"BEGIN SELECT RAISE(ABORT, 'claims are removable only by gc'); END"
	),
	"trg_claim_gc_ledger": (
		f"CREATE TRIGGER trg_claim_gc_ledger AFTER DELETE ON claims "
		f"BEGIN INSERT INTO transitions(entity, entity_id, from_state, to_state, op_id, participant, actor, seed, verb, at_ts) "
		f"VALUES('claim', old.claim_id, old.state, 'gc', {_CTX}, {_CTX_PART}, {_CTX_ACTOR}, {_CTX_SEED}, {_CTX_VERB}, {_CTX_TS}); END"
	),
	"trg_notice_insert_guard": (
		f"CREATE TRIGGER trg_notice_insert_guard BEFORE INSERT ON notices "
		f"WHEN {_CTX} IS NULL "
		f"BEGIN SELECT RAISE(ABORT, 'notice insert requires operation context'); END"
	),
	"trg_notice_frozen": (
		"CREATE TRIGGER trg_notice_frozen BEFORE UPDATE ON notices "
		"BEGIN SELECT RAISE(ABORT, 'notices are immutable'); END"
	),
	"trg_notice_seen_guard": (
		f"CREATE TRIGGER trg_notice_seen_guard BEFORE INSERT ON notice_seen "
		f"WHEN {_CTX} IS NULL "
		f"BEGIN SELECT RAISE(ABORT, 'notice_seen insert requires operation context'); END"
	),
	"trg_notice_seen_update": (
		"CREATE TRIGGER trg_notice_seen_update BEFORE UPDATE ON notice_seen "
		"BEGIN SELECT RAISE(ABORT, 'notice_seen receipts are immutable'); END"
	),
	"trg_notice_seen_delete": (
		f"CREATE TRIGGER trg_notice_seen_delete BEFORE DELETE ON notice_seen "
		f"WHEN {_CTX_VERB} IS NULL OR {_CTX_VERB} NOT IN ('expire','gc') "
		f"BEGIN SELECT RAISE(ABORT, 'notice_seen receipts are removable only by expire or gc'); END"
	),
	"trg_recoveries_insert_guard": (
		f"CREATE TRIGGER trg_recoveries_insert_guard BEFORE INSERT ON recoveries "
		f"WHEN {_CTX} IS NULL "
		f"BEGIN SELECT RAISE(ABORT, 'recovery insert requires operation context'); END"
	),
	"trg_notice_delete_guard": (
		f"CREATE TRIGGER trg_notice_delete_guard BEFORE DELETE ON notices "
		f"WHEN {_CTX_VERB} IS NULL OR {_CTX_VERB} NOT IN ('expire','gc') "
		f"BEGIN SELECT RAISE(ABORT, 'notices are removable only by expire or gc'); END"
	),
	"trg_transitions_update": (
		"CREATE TRIGGER trg_transitions_update BEFORE UPDATE ON transitions "
		"BEGIN SELECT RAISE(ABORT, 'transition ledger is append-only'); END"
	),
	"trg_transitions_delete": (
		"CREATE TRIGGER trg_transitions_delete BEFORE DELETE ON transitions "
		"BEGIN SELECT RAISE(ABORT, 'transition ledger is append-only'); END"
	),
	"trg_accepted_roots_guard_ins": (
		f"CREATE TRIGGER trg_accepted_roots_guard_ins BEFORE INSERT ON accepted_roots "
		f"WHEN {_CTX} IS NULL OR {_CTX_VERB} IS NOT 'regen' "
		f"BEGIN SELECT RAISE(ABORT, 'accepted roots change only under regen'); END"
	),
	"trg_accepted_roots_guard_upd": (
		f"CREATE TRIGGER trg_accepted_roots_guard_upd BEFORE UPDATE ON accepted_roots "
		f"WHEN {_CTX} IS NULL OR {_CTX_VERB} IS NOT 'regen' "
		f"BEGIN SELECT RAISE(ABORT, 'accepted roots change only under regen'); END"
	),
	"trg_accepted_roots_guard_del": (
		f"CREATE TRIGGER trg_accepted_roots_guard_del BEFORE DELETE ON accepted_roots "
		f"WHEN {_CTX} IS NULL OR {_CTX_VERB} IS NOT 'regen' "
		f"BEGIN SELECT RAISE(ABORT, 'accepted roots change only under regen'); END"
	),
	"trg_moves_insert_guard": (
		f"CREATE TRIGGER trg_moves_insert_guard BEFORE INSERT ON moves "
		f"WHEN {_CTX} IS NULL OR {_CTX_VERB} IS NOT 'move_enter' "
		f"BEGIN SELECT RAISE(ABORT, 'move bindings are created only by move entry'); END"
	),
	"trg_moves_update": (
		"CREATE TRIGGER trg_moves_update BEFORE UPDATE ON moves "
		"BEGIN SELECT RAISE(ABORT, 'move bindings are immutable'); END"
	),
	"trg_moves_delete": (
		"CREATE TRIGGER trg_moves_delete BEFORE DELETE ON moves "
		"BEGIN SELECT RAISE(ABORT, 'move bindings are immutable'); END"
	),
	"trg_meta_frozen": (
		"CREATE TRIGGER trg_meta_frozen BEFORE UPDATE OF one_row, uuid, protocol, created_ts "
		"ON instance_meta BEGIN SELECT RAISE(ABORT, 'instance identity is immutable'); END"
	),
	"trg_meta_config_guard": (
		f"CREATE TRIGGER trg_meta_config_guard BEFORE UPDATE OF accepted_generation, config_sha256 "
		f"ON instance_meta WHEN {_CTX} IS NULL OR {_CTX_VERB} NOT IN ('regen','migrate') "
		f"BEGIN SELECT RAISE(ABORT, 'config acceptance changes only under regen/migrate'); END"
	),
	"trg_meta_gate_guard": (
		f"CREATE TRIGGER trg_meta_gate_guard BEFORE UPDATE OF maintenance, move_status, move_token, "
		f"move_role, move_peer, move_source, moved_to, maintainer_actor, maintainer_reason ON instance_meta "
		f"WHEN {_CTX} IS NULL OR {_CTX_VERB} NOT IN ('maintenance','move','move_enter') "
		f"BEGIN SELECT RAISE(ABORT, 'gate/move state changes only under an authorized ceremony'); END"
	),
	"trg_meta_move_edge": (
		"CREATE TRIGGER trg_meta_move_edge BEFORE UPDATE OF move_status ON instance_meta "
		"WHEN old.move_status IS NOT new.move_status AND NOT ("
		"(old.move_status='none' AND new.move_status='moving') "
		"OR (old.move_status='moving' AND new.move_status IN ('none','moved'))) "
		"BEGIN SELECT RAISE(ABORT, 'illegal move_status edge'); END"
	),
	"trg_ceremonies_insert_guard": (
		f"CREATE TRIGGER trg_ceremonies_insert_guard BEFORE INSERT ON ceremonies "
		f"WHEN {_CTX} IS NULL "
		f"BEGIN SELECT RAISE(ABORT, 'ceremony insert requires operation context'); END"
	),
	"trg_ceremonies_update": (
		"CREATE TRIGGER trg_ceremonies_update BEFORE UPDATE ON ceremonies "
		"BEGIN SELECT RAISE(ABORT, 'ceremony records are immutable'); END"
	),
	"trg_ceremonies_delete": (
		"CREATE TRIGGER trg_ceremonies_delete BEFORE DELETE ON ceremonies "
		"BEGIN SELECT RAISE(ABORT, 'ceremony records are immutable'); END"
	),
	"trg_recoveries_update": (
		"CREATE TRIGGER trg_recoveries_update BEFORE UPDATE ON recoveries "
		"BEGIN SELECT RAISE(ABORT, 'recovery records are immutable'); END"
	),
	"trg_recoveries_delete": (
		"CREATE TRIGGER trg_recoveries_delete BEFORE DELETE ON recoveries "
		"BEGIN SELECT RAISE(ABORT, 'recovery records are immutable'); END"
	),
}


def _expected_schema() -> dict[tuple[str, str], str]:
	expected: dict[tuple[str, str], str] = {}
	for name, sql in _TABLES.items():
		expected[("table", name)] = sql
	for name, sql in _INDEXES.items():
		expected[("index", name)] = sql
	for name, sql in _TRIGGERS.items():
		expected[("trigger", name)] = sql
	return expected


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
	"""One open handle on a protocol-6 instance. Not thread-safe."""

	def __init__(self, config_path: str, config: dict, config_digest: str,
	             dirfd: int, dbfd: int, conn: sqlite3.Connection, readonly: bool) -> None:
		self.config_path = config_path
		self.config = config
		self.config_digest = config_digest
		self.dirfd = dirfd
		self.dbfd = dbfd
		self.conn = conn
		self.readonly = readonly

	def close(self) -> None:
		try:
			self.conn.close()
		finally:
			for fd in (self.dbfd, self.dirfd):
				try:
					os.close(fd)
				except OSError:
					pass

	def __enter__(self) -> "Store":
		return self

	def __exit__(self, *exc: Any) -> None:
		self.close()

	# -- transaction discipline --------------------------------------------

	def _txn_begin(self, verb: str, actor: str, seed: str, *, participant: str | None = None,
	               ceremony: str | None = None) -> str:
		"""Open a write transaction: BEGIN IMMEDIATE, then re-read and enforce
		the instance gates against THIS handle's config (open-time checks are
		not sufficient — another process may have set maintenance, moved the
		instance, or accepted a new config since open), then set the operation
		context. Any failure after BEGIN rolls back — a Store never strands an
		open transaction."""
		if self.readonly:
			raise BatonError("read-only store cannot execute write operations")
		if not ACTOR_RE.match(actor) or len(actor) > ACTOR_MAX:
			raise BatonError(f"invalid actor {actor!r} (grammar [a-z][a-z0-9_-]*, max {ACTOR_MAX})")
		if not SEED_RE.match(seed):
			raise BatonError("invalid seed (expect 32 lowercase hex)")
		op_id = new_id()
		try:
			self.conn.execute("BEGIN IMMEDIATE")
		except sqlite3.OperationalError as exc:
			raise BatonError(f"store is busy: {exc}", EXIT_RACE) from exc
		try:
			self._enforce_gates_in_txn(ceremony)
			self.conn.execute(
				"UPDATE op_context SET op_id=?, participant=?, actor=?, seed=?, verb=?, ts=? WHERE one_row=1",
				(op_id, participant, actor, seed, verb, _utc_now_iso()))
			return op_id
		except BaseException as exc:
			self._txn_rollback()
			if isinstance(exc, BatonError):
				raise
			if isinstance(exc, sqlite3.OperationalError):
				raise BatonError(f"store is busy: {exc}", EXIT_RACE) from exc
			if isinstance(exc, sqlite3.Error):
				raise BatonError(f"transaction begin failed: {exc}", EXIT_DAMAGE) from exc
			raise

	def _enforce_gates_in_txn(self, ceremony: str | None) -> None:
		row = self.conn.execute(
			"SELECT protocol, accepted_generation, config_sha256, maintenance, move_status, moved_to "
			"FROM instance_meta WHERE one_row=1").fetchone()
		if row is None:
			raise BatonError("instance_meta row is missing", EXIT_DAMAGE)
		if row["protocol"] != PROTOCOL_VERSION:
			raise BatonError(f"instance protocol {row['protocol']} unsupported", EXIT_PROTOCOL)
		if row["move_status"] == "moved" and ceremony != "move":
			raise BatonError(f"instance has moved to {row['moved_to']!r}; refusing", EXIT_GATED)
		if row["maintenance"] == 1 and ceremony not in ("move", "migrate", "maintenance"):
			raise BatonError("instance is under maintenance; write operations are gated", EXIT_GATED)
		if ceremony == "regen":
			if row["accepted_generation"] != self.config["generation"] - 1:
				raise BatonError(
					f"regen race: accepted generation is now {row['accepted_generation']}, "
					f"offered {self.config['generation']}", EXIT_RACE)
		elif (row["accepted_generation"] != self.config["generation"]
				or row["config_sha256"] != self.config_digest):
			raise BatonError(
				"this handle's config is stale (the instance accepted a newer config); reopen",
				EXIT_GATED)

	def _txn_commit(self) -> None:
		self.conn.execute(
			"UPDATE op_context SET op_id=NULL, participant=NULL, actor=NULL, seed=NULL, verb=NULL, ts=NULL "
			"WHERE one_row=1")
		self.conn.execute("COMMIT")

	def _txn_rollback(self) -> None:
		try:
			self.conn.execute("ROLLBACK")
		except sqlite3.OperationalError:
			pass

	# -- participant identity ----------------------------------------------

	def _check_participant(self, address: str, where: str) -> dict:
		spec = self.config["participants"].get(address)
		if spec is None:
			raise BatonError(f"{where}: participant {address!r} is not declared in the config")
		return spec

	def _validate_route_identity(self, route_config: str, bound_dev: int, bound_ino: int,
	                             what: str) -> None:
		"""The ONLY residence predicate: a route passes when (a) it is the
		canonical committed path, (b) opening its parent via the component
		walk (never following a symlink) yields exactly the directory
		identity bound at maintenance_enter, (c) THIS Store's held dirfd has
		that same identity, and (d) the config basename matches. Pathname
		stats that follow symlinks are banned from this class of check."""
		if not os.path.isabs(route_config) or route_config != os.path.normpath(route_config):
			raise BatonError(f"{what}: committed route is not canonical", EXIT_DAMAGE)
		if self.config_path != route_config:
			raise BatonError(
				f"{what}: this handle's config path {self.config_path!r} is not the exact "
				f"committed route {route_config!r}; alternate spellings are refused, never "
				"normalized", EXIT_DAMAGE)
		fd = _open_dir_no_follow(os.path.dirname(route_config), what)
		try:
			opened = os.fstat(fd)
		finally:
			os.close(fd)
		if (opened.st_dev, opened.st_ino) != (bound_dev, bound_ino):
			raise BatonError(
				f"{what}: the route's directory identity does not match the identity bound "
				"at maintenance_enter (replaced, renamed, or symlinked)", EXIT_DAMAGE)
		own = os.fstat(self.dirfd)
		if (own.st_dev, own.st_ino) != (bound_dev, bound_ino):
			raise BatonError(
				f"{what}: this instance does not physically reside at the bound directory",
				EXIT_DAMAGE)
		if os.path.basename(route_config) != os.path.basename(self.config_path):
			raise BatonError(f"{what}: config basename does not match the bound route", EXIT_DAMAGE)

	def _move_binding(self, token: str) -> sqlite3.Row:
		row = self.conn.execute("SELECT * FROM moves WHERE token=?", (token,)).fetchone()
		if row is None:
			raise BatonError("no immutable move binding exists for this token", EXIT_DAMAGE)
		uuid = self.conn.execute("SELECT uuid FROM instance_meta WHERE one_row=1").fetchone()[0]
		if row["instance_uuid"] != uuid:
			raise BatonError(
				"move binding names a different instance uuid; refusing (corruption)", EXIT_DAMAGE)
		return row

	def _require_capability(self, address: str, actor: str, seed: str,
	                        capability: str, what: str) -> None:
		"""Administrative authority is an EXPLICIT config declaration, never
		inferred from endpoint cardinality: the participant must carry the
		named capability in addition to ordinary identity validation. The
		host deployment decides which endpoint holds it."""
		self._check_actor_for(address, actor, seed)
		caps = self.config["participants"][address].get("capabilities", [])
		if capability not in caps:
			raise BatonError(
				f"{what} requires the {capability!r} capability, which {address!r} does not hold")

	def _check_actor_for(self, address: str, actor: str, seed: str) -> None:
		if not ACTOR_RE.match(actor) or len(actor) > ACTOR_MAX:
			raise BatonError(f"invalid actor {actor!r} (grammar [a-z][a-z0-9_-]*, max {ACTOR_MAX})")
		if not SEED_RE.match(seed):
			raise BatonError("invalid seed (expect 32 lowercase hex)")
		spec = self._check_participant(address, "actor check")
		if spec["identity"] == "singleton" and spec["singleton_actor"] != actor:
			raise BatonError(f"participant {address!r} is a singleton bound to actor {spec['singleton_actor']!r}")

	# -- operations ---------------------------------------------------------

	def _resolve_attachment(self, attach: Any) -> tuple[str, str, str, int]:
		"""Resolve and hash-pin a separately authored evidence file under a
		configured root with component-wise no-follow containment. Returns
		(root_id, rel_path, sha256, size)."""
		if type(attach) is not dict or set(attach) != {"root_id", "path"}:
			raise BatonError("attachment must be {'root_id': ..., 'path': ...}")
		root_id = attach["root_id"]
		rel_path = attach["path"]
		if type(root_id) is not str or type(rel_path) is not str:
			raise BatonError("attachment root_id and path must be strings")
		root = self.config.get("roots", {}).get(root_id)
		if root is None:
			raise BatonError(f"attachment root {root_id!r} is not declared in the config")
		parts = rel_path.split("/")
		if rel_path.startswith("/") or any(p in ("", ".", "..") for p in parts):
			raise BatonError(f"attachment path {rel_path!r} must be a clean relative path")
		fd = _open_root_dir(root)
		try:
			for component in parts[:-1]:
				next_fd = os.open(component, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
				os.close(fd)
				fd = next_fd
			try:
				leaf = os.open(parts[-1],
				               os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
			except OSError as exc:
				if exc.errno == errno.ELOOP:
					raise BatonError(f"attachment {rel_path!r} is a symlink; refusing", EXIT_DAMAGE) from exc
				raise BatonError(f"attachment {rel_path!r} unreadable: {exc}") from exc
			try:
				st_before = os.fstat(leaf)
				if not stat.S_ISREG(st_before.st_mode):
					raise BatonError(f"attachment {rel_path!r} is not a regular file")
				hasher = hashlib.sha256()
				with os.fdopen(leaf, "rb", closefd=False) as handle:
					for chunk in iter(lambda: handle.read(1 << 20), b""):
						hasher.update(chunk)
				_fault("attach:post-hash")
				st_after = os.fstat(leaf)
				before_id = (st_before.st_dev, st_before.st_ino, st_before.st_mode,
				             st_before.st_size, st_before.st_mtime_ns, st_before.st_ctime_ns)
				after_id = (st_after.st_dev, st_after.st_ino, st_after.st_mode,
				            st_after.st_size, st_after.st_mtime_ns, st_after.st_ctime_ns)
				if before_id != after_id:
					raise BatonError(
						f"attachment {rel_path!r} changed while being hashed; refusing the "
						"ambiguous snapshot", EXIT_DAMAGE)
				return root_id, rel_path, hasher.hexdigest(), st_before.st_size
			finally:
				os.close(leaf)
		except OSError as exc:
			if exc.errno == errno.ELOOP:
				raise BatonError(f"attachment path {rel_path!r} crosses a symlink; refusing", EXIT_DAMAGE) from exc
			raise BatonError(f"attachment path {rel_path!r} unresolvable: {exc}") from exc
		finally:
			try:
				os.close(fd)
			except OSError:
				pass

	def verify_attachment(self, message_id: str) -> None:
		"""Re-resolve a message's pinned attachment; mutation fails closed."""
		msg = self.conn.execute(
			"SELECT attach_root_id, attach_path, attach_sha256, attach_size, attach_generation "
			"FROM messages WHERE id=?", (message_id,)).fetchone()
		if msg is None:
			raise BatonError(f"unknown message {message_id!r}", EXIT_NONE)
		if msg["attach_root_id"] is None:
			return
		# attach_generation identifies the ROOT BINDING it was resolved
		# under (not the global config generation): verification requires the
		# CURRENT accepted binding to match both root id and binding
		# generation, so unrelated config edits never invalidate attachments
		# while remap/removal of a referenced binding stays refused by regen.
		accepted = self.conn.execute(
			"SELECT path, binding_generation FROM accepted_roots WHERE root_id=?",
			(msg["attach_root_id"],)).fetchone()
		if accepted is None or accepted["path"] != self.config.get("roots", {}).get(msg["attach_root_id"]):
			raise BatonError(
				f"attachment root {msg['attach_root_id']!r} is no longer the accepted mapping", EXIT_DAMAGE)
		if accepted["binding_generation"] != msg["attach_generation"]:
			raise BatonError(
				f"attachment root {msg['attach_root_id']!r} binding generation "
				f"{accepted['binding_generation']} does not match the pinned "
				f"{msg['attach_generation']}", EXIT_DAMAGE)
		_, _, sha, size = self._resolve_attachment(
			{"root_id": msg["attach_root_id"], "path": msg["attach_path"]})
		if sha != msg["attach_sha256"] or size != msg["attach_size"]:
			raise BatonError(
				f"attachment {msg['attach_path']!r} no longer matches its pinned hash; refusing", EXIT_DAMAGE)

	def send(self, sender: str, recipient: str, *, actor: str, seed: str, kind: str,
	         body: bytes | None, thread_id: str | None = None,
	         retention: str = RETENTION_DURABLE, outcome: str | None = None,
	         responds_to: str | None = None, attach: Any = None) -> str:
		if (body is None) == (attach is None):
			raise BatonError("a message requires exactly one of body or attachment (XOR)")
		attach_cols = self._resolve_attachment(attach) if attach is not None else None
		self._check_actor_for(sender, actor, seed)
		self._check_participant(recipient, "send")
		if not KIND_RE.match(kind):
			raise BatonError(f"invalid kind {kind!r}")
		if thread_id is not None and not THREAD_RE.match(thread_id):
			raise BatonError(f"invalid thread id {thread_id!r}")
		if retention not in RETENTIONS:
			raise BatonError(f"invalid retention {retention!r}")
		if retention == RETENTION_TRANSIENT and body is not None and len(body) > TRANSIENT_BODY_MAX_BYTES:
			raise BatonError(f"transient body exceeds {TRANSIENT_BODY_MAX_BYTES} bytes")
		self._txn_begin("send", actor, seed, participant=sender)
		try:
			message_id = self._insert_message(
				sender, recipient, kind=kind, body=body, thread_id=thread_id,
				retention=retention, outcome=outcome, responds_to=responds_to,
				attach_cols=attach_cols)
			self._txn_commit()
			return message_id
		except BaseException:
			self._txn_rollback()
			raise

	def _insert_message(self, sender: str, recipient: str, *, kind: str, body: bytes | None,
	                    thread_id: str | None, retention: str, outcome: str | None,
	                    responds_to: str | None, attach_cols: tuple | None = None) -> str:
		now = _utc_now_iso()
		message_id = new_id()
		content_id = None
		sha = None
		if body is not None:
			content_id = new_id()
			sha = hashlib.sha256(body).hexdigest()
			self.conn.execute(
				"INSERT INTO contents(content_id, body, sha256, size, created_ts) VALUES(?,?,?,?,?)",
				(content_id, body, sha, len(body), now))
		a_root, a_path, a_sha, a_size = attach_cols if attach_cols is not None else (None, None, None, None)
		a_gen = None
		if attach_cols is not None:
			binding = self.conn.execute(
				"SELECT binding_generation FROM accepted_roots WHERE root_id=?", (a_root,)).fetchone()
			if binding is None:
				raise BatonError(f"root {a_root!r} has no accepted binding", EXIT_DAMAGE)
			a_gen = binding["binding_generation"]
		self.conn.execute(
			"INSERT INTO messages(id, from_participant, to_participant, kind, thread_id, retention, "
			"content_id, content_sha256, attach_root_id, attach_path, attach_sha256, attach_size, "
			"attach_generation, outcome, created_ts, state, responds_to) "
			"VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
			(message_id, sender, recipient, kind, thread_id, retention, content_id, sha,
			 a_root, a_path, a_sha, a_size, a_gen, outcome, now, responds_to))
		return message_id

	def claim(self, participant: str, *, actor: str, seed: str, message_id: str | None = None) -> dict:
		self._check_actor_for(participant, actor, seed)
		if message_id is None:
			row = self.conn.execute(
				"SELECT id FROM messages WHERE to_participant=? AND state='pending' "
				"ORDER BY created_ts, id LIMIT 1", (participant,)).fetchone()
			if row is None:
				raise BatonError(f"no message addressed to {participant!r} is pending", EXIT_NONE)
			message_id = row[0]
		# Attachment pins are enforced at claim: post-publication mutation
		# fails closed before the claim transaction begins (file IO stays
		# outside the write lock).
		self.verify_attachment(message_id)
		self._txn_begin("claim", actor, seed, participant=participant)
		try:
			claim_id = new_id()
			now = _utc_now_iso()
			self.conn.execute(
				"INSERT INTO claims(claim_id, message_id, actor, seed, claimed_ts, state) "
				"VALUES(?,?,?,?,?, 'active')", (claim_id, message_id, actor, seed, now))
			cur = self.conn.execute(
				"UPDATE messages SET state='claimed' WHERE id=? AND state='pending' AND to_participant=?",
				(message_id, participant))
			if cur.rowcount != 1:
				raise BatonError(f"message {message_id!r} is not pending for {participant!r}", EXIT_NONE)
			self._txn_commit()
			return self.get_claim(claim_id)
		except sqlite3.IntegrityError as exc:
			self._txn_rollback()
			raise BatonError(f"claim lost a race: {exc}", EXIT_RACE) from exc
		except BaseException:
			self._txn_rollback()
			raise

	def _load_active_claim(self, claim_id: str, actor: str, seed: str) -> sqlite3.Row:
		row = self.conn.execute(
			"SELECT c.claim_id, c.message_id, c.actor, c.seed, c.state AS claim_state, "
			"m.from_participant, m.to_participant, m.kind, m.thread_id, m.retention, "
			"m.content_id, m.content_sha256, m.state AS message_state "
			"FROM claims c JOIN messages m ON m.id = c.message_id WHERE c.claim_id=?",
			(claim_id,)).fetchone()
		if row is None:
			raise BatonError(f"unknown claim {claim_id!r}")
		if row["actor"] != actor or row["seed"] != seed:
			raise BatonError(f"claim {claim_id!r} is owned by actor {row['actor']!r}, not {actor!r}")
		return row

	def _existing_disposition(self, claim_id: str) -> sqlite3.Row | None:
		return self.conn.execute(
			"SELECT d.claim_id, d.kind, d.outcome, d.retention, d.content_id, d.content_sha256, "
			"d.response_message_id, d.created_ts FROM dispositions d WHERE d.claim_id=?",
			(claim_id,)).fetchone()

	def _verify_retry(self, existing: sqlite3.Row, *, op: str, message_kind: str | None,
	                  outcome: str | None, body: bytes | None, recipient: str | None,
	                  thread_id: str | None = None, retention: str | None = None) -> dict:
		"""Round-12 retry idempotence: validate the retried operation against
		the committed disposition; matching retries redeliver, mismatches fail
		closed. Transient bodies may already be scrubbed — compare hashes."""
		if existing["kind"] != op:
			raise BatonError(
				f"claim already has a committed {existing['kind']} disposition; retried {op} mismatches", EXIT_PROTOCOL)
		if existing["outcome"] != outcome:
			raise BatonError("retried outcome differs from the committed disposition", EXIT_PROTOCOL)
		if retention is not None and existing["retention"] != retention:
			raise BatonError("retried retention differs from the committed disposition", EXIT_PROTOCOL)
		committed_sha = existing["content_sha256"]
		retry_sha = hashlib.sha256(body).hexdigest() if body is not None else None
		if committed_sha != retry_sha:
			raise BatonError("retried content differs from the committed disposition", EXIT_PROTOCOL)
		response_id = existing["response_message_id"]
		if response_id is not None:
			row = self.conn.execute(
				"SELECT to_participant, kind, thread_id FROM messages WHERE id=?", (response_id,)).fetchone()
			if row is None:
				raise BatonError(
					"committed disposition references a missing response message", EXIT_DAMAGE)
			if row["to_participant"] != recipient:
				raise BatonError("retried recipient differs from the committed disposition", EXIT_PROTOCOL)
			if message_kind is not None and row["kind"] != message_kind:
				raise BatonError("retried message kind differs from the committed disposition", EXIT_PROTOCOL)
			if row["thread_id"] != thread_id:
				raise BatonError("retried thread differs from the committed disposition", EXIT_PROTOCOL)
		return {
			"already_committed": True,
			"claim_id": existing["claim_id"],
			"kind": existing["kind"],
			"outcome": existing["outcome"],
			"content_sha256": committed_sha,
			"retention": existing["retention"],
			"response_message_id": response_id,
			"created_ts": existing["created_ts"],
		}

	def _scrub_transient_incoming(self, row: sqlite3.Row) -> None:
		if row["retention"] == RETENTION_TRANSIENT and row["content_id"] is not None:
			self.conn.execute("UPDATE messages SET content_id=NULL WHERE id=?", (row["message_id"],))
			self.conn.execute("DELETE FROM contents WHERE content_id=?", (row["content_id"],))

	def reply(self, claim_id: str, *, actor: str, seed: str, kind: str,
	          body: bytes | None, outcome: str | None = None,
	          recipient: str | None = None, thread_id: str | None = None,
	          retention: str | None = None) -> dict:
		if not KIND_RE.match(kind):
			raise BatonError(f"invalid kind {kind!r}")
		if body is None:
			raise BatonError("reply requires a body (a close is the bodyless disposition)")
		if retention is not None and retention not in RETENTIONS:
			raise BatonError(f"invalid retention {retention!r}")
		self._txn_begin("reply", actor, seed)
		try:
			row = self._load_active_claim(claim_id, actor, seed)
			self.conn.execute("UPDATE op_context SET participant=? WHERE one_row=1", (row["to_participant"],))
			effective_retention = retention if retention is not None else row["retention"]
			# None means INHERIT on both first publication and retry — the
			# effective route is normalized before disposition lookup so a
			# retry can never wildcard-match a differently routed commit.
			effective_recipient = recipient if recipient is not None else row["from_participant"]
			effective_thread = thread_id if thread_id is not None else row["thread_id"]
			existing = self._existing_disposition(claim_id)
			if existing is not None:
				result = self._verify_retry(existing, op='reply', message_kind=kind, outcome=outcome,
				                            body=body, recipient=effective_recipient,
				                            thread_id=effective_thread, retention=effective_retention)
				self._txn_rollback()
				return result
			if row["claim_state"] != "active":
				raise BatonError(f"claim {claim_id!r} is {row['claim_state']}, not active", EXIT_PROTOCOL)
			to = effective_recipient
			self._check_participant(to, "reply")
			thread = effective_thread
			# v5-preserved surface: explicit override permitted, default inherit
			# (v5 respond(): response_retention = retention or envelope retention).
			if effective_retention == RETENTION_TRANSIENT and len(body) > TRANSIENT_BODY_MAX_BYTES:
				raise BatonError(f"transient body exceeds {TRANSIENT_BODY_MAX_BYTES} bytes")
			response_id = self._insert_message(
				row["to_participant"], to, kind=kind, body=body, thread_id=thread,
				retention=effective_retention, outcome=outcome, responds_to=row["message_id"])
			now = _utc_now_iso()
			sha = hashlib.sha256(body).hexdigest() if body is not None else None
			self.conn.execute(
				"INSERT INTO dispositions(claim_id, kind, outcome, retention, content_id, content_sha256, "
				"response_message_id, created_ts) VALUES(?, 'reply', ?, ?, NULL, ?, ?, ?)",
				(claim_id, outcome, effective_retention, sha, response_id, now))
			self.conn.execute(
				"UPDATE claims SET state='completed', terminal_ts=? WHERE claim_id=?", (now, claim_id))
			self.conn.execute(
				"UPDATE messages SET state='completed', completed_ts=? WHERE id=?", (now, row["message_id"]))
			self._scrub_transient_incoming(row)
			self._txn_commit()
			return {
				"already_committed": False,
				"claim_id": claim_id,
				"kind": kind,
				"outcome": outcome,
				"content_sha256": sha,
				"retention": effective_retention,
				"response_message_id": response_id,
				"created_ts": now,
			}
		except BaseException:
			self._txn_rollback()
			raise

	def close_claim(self, claim_id: str, *, actor: str, seed: str,
	                body: bytes | None = None, outcome: str | None = None,
	                retention: str | None = None) -> dict:
		if retention is not None and retention not in RETENTIONS:
			raise BatonError(f"invalid retention {retention!r}")
		self._txn_begin("close", actor, seed)
		try:
			row = self._load_active_claim(claim_id, actor, seed)
			self.conn.execute("UPDATE op_context SET participant=? WHERE one_row=1", (row["to_participant"],))
			effective_retention = retention if retention is not None else row["retention"]
			existing = self._existing_disposition(claim_id)
			if existing is not None:
				result = self._verify_retry(existing, op='close', message_kind=None, outcome=outcome,
				                            body=body, recipient=None, retention=effective_retention)
				self._txn_rollback()
				return result
			if row["claim_state"] != "active":
				raise BatonError(f"claim {claim_id!r} is {row['claim_state']}, not active", EXIT_PROTOCOL)
			now = _utc_now_iso()
			content_id = None
			sha = None
			if body is not None:
				# The EFFECTIVE disposition retention (override or inherit)
				# decides retained body vs hash-only identity (T16).
				sha = hashlib.sha256(body).hexdigest()
				if effective_retention == RETENTION_TRANSIENT:
					if len(body) > TRANSIENT_BODY_MAX_BYTES:
						raise BatonError(f"transient body exceeds {TRANSIENT_BODY_MAX_BYTES} bytes")
				else:
					content_id = new_id()
					self.conn.execute(
						"INSERT INTO contents(content_id, body, sha256, size, created_ts) VALUES(?,?,?,?,?)",
						(content_id, body, sha, len(body), now))
			self.conn.execute(
				"INSERT INTO dispositions(claim_id, kind, outcome, retention, content_id, content_sha256, "
				"response_message_id, created_ts) VALUES(?, 'close', ?, ?, ?, ?, NULL, ?)",
				(claim_id, outcome, effective_retention, content_id, sha, now))
			self.conn.execute(
				"UPDATE claims SET state='completed', terminal_ts=? WHERE claim_id=?", (now, claim_id))
			self.conn.execute(
				"UPDATE messages SET state='closed', completed_ts=? WHERE id=?", (now, row["message_id"]))
			self._scrub_transient_incoming(row)
			self._txn_commit()
			return {
				"already_committed": False,
				"claim_id": claim_id,
				"kind": "close",
				"outcome": outcome,
				"content_sha256": sha,
				"retention": effective_retention,
				"response_message_id": None,
				"created_ts": now,
			}
		except BaseException:
			self._txn_rollback()
			raise

	# -- notices ------------------------------------------------------------

	def send_notice(self, sender: str, *, actor: str, seed: str, kind: str,
	                body: bytes, ttl_seconds: int | None = None) -> str:
		"""Broadcast a notice with a FINITE lifetime (default 86400s, the v5
		protocol TTL). Immortal notices are not constructible. The exact
		author instance (participant+actor+seed) is recorded immutably and
		is the only identity permitted to expire the notice early."""
		self._check_actor_for(sender, actor, seed)
		if not KIND_RE.match(kind):
			raise BatonError(f"invalid kind {kind!r}")
		if ttl_seconds is None:
			ttl_seconds = DEFAULT_NOTICE_TTL_SECONDS
		if type(ttl_seconds) is not int or ttl_seconds < 1:
			raise BatonError("ttl_seconds must be a positive integer")
		if len(body) > TRANSIENT_BODY_MAX_BYTES:
			raise BatonError(f"notice body exceeds {TRANSIENT_BODY_MAX_BYTES} bytes")
		self._txn_begin("send", actor, seed, participant=sender)
		try:
			now = _utc_now_iso()
			notice_id = new_id()
			content_id = new_id()
			sha = hashlib.sha256(body).hexdigest()
			self.conn.execute(
				"INSERT INTO contents(content_id, body, sha256, size, created_ts) VALUES(?,?,?,?,?)",
				(content_id, body, sha, len(body), now))
			self.conn.execute(
				"INSERT INTO notices(id, from_participant, author_actor, author_seed, kind, "
				"content_id, content_sha256, created_ts, ttl_seconds) VALUES(?,?,?,?,?,?,?,?,?)",
				(notice_id, sender, actor, seed, kind, content_id, sha, now, ttl_seconds))
			self._txn_commit()
			return notice_id
		except BaseException:
			self._txn_rollback()
			raise

	def see(self, participant: str, *, actor: str, seed: str) -> list[dict]:
		"""Mark every not-yet-seen live notice seen for (participant, actor)
		and return them. One transaction; broadcast, never claimable."""
		self._check_actor_for(participant, actor, seed)
		self._txn_begin("see", actor, seed, participant=participant)
		try:
			now = _utc_now_iso()
			rows = self.conn.execute(
				"SELECT n.id, n.from_participant, n.kind, n.content_sha256, n.created_ts, "
				"n.ttl_seconds, c.body FROM notices n LEFT JOIN contents c ON c.content_id=n.content_id "
				"WHERE NOT EXISTS (SELECT 1 FROM notice_seen s "
				"WHERE s.notice_id=n.id AND s.participant=? AND s.actor=?) ORDER BY n.created_ts",
				(participant, actor)).fetchall()
			unseen = []
			for row in rows:
				if _notice_expired(row["created_ts"], row["ttl_seconds"], now):
					continue
				self.conn.execute(
					"INSERT INTO notice_seen(notice_id, participant, actor, seed, seen_ts) "
					"VALUES(?,?,?,?,?)", (row["id"], participant, actor, seed, now))
				unseen.append(dict(row))
			self._txn_commit()
			return unseen
		except BaseException:
			self._txn_rollback()
			raise

	def expire(self, participant: str, *, actor: str, seed: str,
	           notice_id: str | None = None) -> list[str]:
		"""Delete expired notices (and, via CASCADE, their seen rows) plus
		their content rows in ONE transaction. An explicit id may also be
		expired early by its author."""
		self._check_actor_for(participant, actor, seed)
		self._txn_begin("expire", actor, seed, participant=participant)
		try:
			now = _utc_now_iso()
			if notice_id is not None:
				rows = self.conn.execute(
					"SELECT id, from_participant, author_actor, author_seed, content_id, created_ts, "
					"ttl_seconds FROM notices WHERE id=?", (notice_id,)).fetchall()
				if not rows:
					raise BatonError(f"unknown notice {notice_id!r}", EXIT_NONE)
			else:
				rows = self.conn.execute(
					"SELECT id, from_participant, author_actor, author_seed, content_id, created_ts, "
					"ttl_seconds FROM notices").fetchall()
			removed = []
			for row in rows:
				elapsed = _notice_expired(row["created_ts"], row["ttl_seconds"], now)
				exact_author = (row["from_participant"] == participant
				                and row["author_actor"] == actor and row["author_seed"] == seed)
				if not elapsed and not (notice_id is not None and exact_author):
					if notice_id is not None:
						raise BatonError(
							f"notice {notice_id!r} is not expired and the caller is not its exact "
							f"author instance (participant+actor+seed); a dead author's notice is "
							f"swept when its TTL elapses")
					continue
				self.conn.execute("DELETE FROM notices WHERE id=?", (row["id"],))
				if row["content_id"] is not None:
					self.conn.execute("DELETE FROM contents WHERE content_id=?", (row["content_id"],))
				removed.append(row["id"])
			self._txn_commit()
			return removed
		except BaseException:
			self._txn_rollback()
			raise

	# -- recovery -----------------------------------------------------------

	def recover_claim(self, claim_id: str, *, participant: str, actor: str, seed: str,
	                  reason: str) -> dict:
		"""Capability-authorized dead-seed recovery: the recovering identity
		must hold the config-declared 'recovery' capability. Closes the exact
		immutable claim attempt as recovered, records the audit row with the
		full participant+actor+seed identity, and re-pends the message — one
		transaction; history is never rewritten."""
		if type(reason) is not str or not reason.strip():
			raise BatonError("recovery requires a non-empty --reason")
		self._require_capability(participant, actor, seed, "recovery", "claim recovery")
		self._txn_begin("recover", actor, seed, participant=participant)
		try:
			row = self.conn.execute(
				"SELECT c.state, c.message_id, m.state AS message_state FROM claims c "
				"JOIN messages m ON m.id=c.message_id WHERE c.claim_id=?", (claim_id,)).fetchone()
			if row is None:
				raise BatonError(f"unknown claim {claim_id!r}", EXIT_NONE)
			if row["state"] != "active":
				raise BatonError(f"claim {claim_id!r} is {row['state']}, not active; nothing to recover")
			now = _utc_now_iso()
			recovery_id = new_id()
			self.conn.execute(
				"UPDATE claims SET state='recovered', terminal_ts=? WHERE claim_id=?", (now, claim_id))
			self.conn.execute(
				"INSERT INTO recoveries(recovery_id, claim_id, participant, actor, seed, reason, created_ts) "
				"VALUES(?,?,?,?,?,?,?)", (recovery_id, claim_id, participant, actor, seed, reason, now))
			cur = self.conn.execute(
				"UPDATE messages SET state='pending', completed_ts=NULL WHERE id=? AND state='claimed'",
				(row["message_id"],))
			if cur.rowcount != 1:
				raise BatonError(
					f"message for claim {claim_id!r} is {row['message_state']!r}, not claimed", EXIT_DAMAGE)
			self._txn_commit()
			return {"recovery_id": recovery_id, "claim_id": claim_id, "message_id": row["message_id"]}
		except BaseException:
			self._txn_rollback()
			raise

	# -- gc ------------------------------------------------------------------

	def gc(self, *, participant: str, actor: str, seed: str, now: str | None = None) -> dict:
		"""Bounded deletion of TRANSIENT terminal message metadata older than
		retention_days, plus expired-notice sweep. Durable messages and the
		transitions/recoveries audit trail are permanent. Every deletion
		emits a final ledger event via the gc triggers."""
		self._check_actor_for(participant, actor, seed)
		retention_days = self.config.get("retention_days", DEFAULT_RETENTION_DAYS)
		now_ts = now if now is not None else _utc_now_iso()
		cutoff = _iso_minus_days(now_ts, retention_days)
		self._txn_begin("gc", actor, seed, participant=participant)
		try:
			# Retention-graph fixpoint (reply links form deletion dependencies
			# in BOTH directions): start from aged transient terminal messages
			# with no recovery-referenced claim, then iteratively remove any
			# candidate anchored by retained protocol state —
			#   (a) a responds_to child OUTSIDE the candidate set (the child
			#       row references its parent), or
			#   (b) a disposition belonging to a claim on a message OUTSIDE
			#       the set whose response_message_id names the candidate
			#       (a RETAINED disposition is the immutable retry-identity
			#       authority, so its transient response stays retained as
			#       metadata — the pinned contract), or
			#   (c) one of the candidate's OWN dispositions is durable — a
			#       durable close on a transient envelope is a retained
			#       record; the delivery envelope's retention never deletes
			#       a durable disposition body.
			# The surviving component deletes cleanly: dispositions → claims →
			# messages children-first in an order derived from the actual
			# responds_to graph → contents. One call always makes its bounded
			# progress or returns empty; it can never abort on a valid graph
			# (a corrupted/self-referential graph fails closed instead).
			candidates = {row[0] for row in self.conn.execute(
				"SELECT m.id FROM messages m WHERE m.retention='transient' "
				"AND m.state IN ('completed','closed') AND m.completed_ts < ? "
				"AND NOT EXISTS (SELECT 1 FROM claims c JOIN recoveries rec ON rec.claim_id=c.claim_id "
				"WHERE c.message_id = m.id)", (cutoff,))}
			while True:
				anchored = set()
				for mid in candidates:
					children = [r[0] for r in self.conn.execute(
						"SELECT id FROM messages WHERE responds_to=?", (mid,))]
					if any(child not in candidates for child in children):
						anchored.add(mid)
						continue
					holders = [r[0] for r in self.conn.execute(
						"SELECT c.message_id FROM dispositions d JOIN claims c ON c.claim_id=d.claim_id "
						"WHERE d.response_message_id=?", (mid,))]
					if any(holder not in candidates for holder in holders):
						anchored.add(mid)
						continue
					durable_own = self.conn.execute(
						"SELECT 1 FROM dispositions d JOIN claims c ON c.claim_id=d.claim_id "
						"WHERE c.message_id=? AND d.retention='durable' LIMIT 1", (mid,)).fetchone()
					if durable_own is not None:
						anchored.add(mid)
				if not anchored:
					break
				candidates -= anchored
			removed_messages = []
			if candidates:
				# Children-first order derived from the responds_to references
				# themselves (timestamps can tie within one second): a message
				# is deletable once no REMAINING component member references
				# it as a parent.
				parents = {mid: self.conn.execute(
					"SELECT responds_to FROM messages WHERE id=?", (mid,)).fetchone()[0]
					for mid in candidates}
				remaining = set(candidates)
				ordered = []
				while remaining:
					referenced = {parents[m] for m in remaining if parents[m] in remaining}
					leaves = sorted(remaining - referenced)
					if not leaves:
						raise BatonError("gc: responds_to cycle in candidate component", EXIT_DAMAGE)
					ordered.extend(leaves)
					remaining -= set(leaves)
				for mid in ordered:
					for (cid,) in self.conn.execute(
							"SELECT claim_id FROM claims WHERE message_id=?", (mid,)).fetchall():
						disp = self.conn.execute(
							"SELECT content_id FROM dispositions WHERE claim_id=?", (cid,)).fetchone()
						if disp is not None:
							self.conn.execute("DELETE FROM dispositions WHERE claim_id=?", (cid,))
							if disp["content_id"] is not None:
								self.conn.execute(
									"DELETE FROM contents WHERE content_id=?", (disp["content_id"],))
						self.conn.execute("DELETE FROM claims WHERE claim_id=?", (cid,))
				for mid in ordered:
					content = self.conn.execute(
						"SELECT content_id FROM messages WHERE id=?", (mid,)).fetchone()
					self.conn.execute("DELETE FROM messages WHERE id=?", (mid,))
					if content is not None and content["content_id"] is not None:
						self.conn.execute("DELETE FROM contents WHERE content_id=?", (content["content_id"],))
					removed_messages.append(mid)
			expired = []
			for row in self.conn.execute(
					"SELECT id, content_id, created_ts, ttl_seconds FROM notices").fetchall():
				if _notice_expired(row["created_ts"], row["ttl_seconds"], now_ts):
					self.conn.execute("DELETE FROM notices WHERE id=?", (row["id"],))
					if row["content_id"] is not None:
						self.conn.execute("DELETE FROM contents WHERE content_id=?", (row["content_id"],))
					expired.append(row["id"])
			self._txn_commit()
			return {"messages": removed_messages, "notices": expired, "cutoff": cutoff}
		except BaseException:
			self._txn_rollback()
			raise

	# -- reads --------------------------------------------------------------

	def get_message(self, message_id: str) -> dict:
		row = self.conn.execute(
			"SELECT m.*, c.body, c.size AS content_size "
			"FROM messages m LEFT JOIN contents c ON c.content_id = m.content_id "
			"WHERE m.id=?", (message_id,)).fetchone()
		if row is None:
			raise BatonError(f"unknown message {message_id!r}", EXIT_NONE)
		return dict(row)

	def get_claim(self, claim_id: str) -> dict:
		row = self.conn.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
		if row is None:
			raise BatonError(f"unknown claim {claim_id!r}", EXIT_NONE)
		return dict(row)

	def scan(self, participant: str | None = None) -> dict:
		where = "WHERE to_participant=?" if participant else ""
		args = (participant,) if participant else ()
		pending = [dict(r) for r in self.conn.execute(
			f"SELECT id, from_participant, to_participant, kind, thread_id, created_ts "
			f"FROM messages {where} {'AND' if where else 'WHERE'} state='pending' ORDER BY created_ts", args)]
		claimed = [dict(r) for r in self.conn.execute(
			f"SELECT m.id, m.from_participant, m.to_participant, c.claim_id, c.actor, c.claimed_ts "
			f"FROM messages m JOIN claims c ON c.message_id=m.id AND c.state='active' "
			f"{where.replace('to_participant', 'm.to_participant')} {'AND' if where else 'WHERE'} m.state='claimed' "
			f"ORDER BY c.claimed_ts", args)]
		return {"pending": pending, "claimed": claimed}


# ---------------------------------------------------------------------------
# init / open
# ---------------------------------------------------------------------------

def _connect_fd(dbfd: int, readonly: bool) -> sqlite3.Connection:
	mode = "ro" if readonly else "rw"
	conn = sqlite3.connect(f"file:/proc/self/fd/{dbfd}?mode={mode}", uri=True,
	                       isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000.0)
	conn.row_factory = sqlite3.Row
	return conn


def _verify_db_identity(conn: sqlite3.Connection, dbfd: int, dirfd: int) -> None:
	rows = conn.execute("PRAGMA database_list").fetchall()
	main = [r for r in rows if r[1] == "main"]
	if len(main) != 1 or not main[0][2]:
		raise BatonError("cannot resolve the opened database path", EXIT_DAMAGE)
	canonical = main[0][2]
	try:
		st_path = os.stat(canonical)
	except OSError as exc:
		raise BatonError(f"opened database path vanished: {exc}", EXIT_DAMAGE) from exc
	st_fd = os.fstat(dbfd)
	if (st_path.st_dev, st_path.st_ino) != (st_fd.st_dev, st_fd.st_ino):
		raise BatonError("database identity mismatch (dev/inode)", EXIT_DAMAGE)
	st_parent = os.stat(os.path.dirname(canonical))
	st_dir = os.fstat(dirfd)
	if (st_parent.st_dev, st_parent.st_ino) != (st_dir.st_dev, st_dir.st_ino):
		raise BatonError("database parent directory mismatch", EXIT_DAMAGE)


def _apply_connection_contract(conn: sqlite3.Connection, readonly: bool) -> None:
	if sqlite3.sqlite_version_info < SQLITE_MIN:
		raise BatonError(
			f"SQLite library {sqlite3.sqlite_version} is below the required "
			f"{'.'.join(map(str, SQLITE_MIN))} (STRICT tables)", EXIT_FLOOR)
	mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
	if mode != "wal":
		raise BatonError(f"database journal_mode is {mode!r}, not WAL; refusing", EXIT_DAMAGE)
	conn.execute("PRAGMA trusted_schema=OFF")
	conn.execute("PRAGMA foreign_keys=ON")
	conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
	if not readonly:
		conn.execute("PRAGMA synchronous=FULL")


def _validate_schema(conn: sqlite3.Connection) -> None:
	user_version = conn.execute("PRAGMA user_version").fetchone()[0]
	if user_version != PROTOCOL_VERSION:
		raise BatonError(
			f"database protocol {user_version} does not match supported protocol {PROTOCOL_VERSION}", EXIT_PROTOCOL)
	actual: dict[tuple[str, str], str] = {}
	for typ, name, sql in conn.execute(
			"SELECT type, name, sql FROM sqlite_master "
			"WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"):
		actual[(typ, name)] = sql
	expected = _expected_schema()
	if actual != expected:
		missing = sorted(set(expected) - set(actual))
		extra = sorted(set(actual) - set(expected))
		changed = sorted(k for k in set(actual) & set(expected) if actual[k] != expected[k])
		raise BatonError(
			f"schema validation failed (missing={missing!r} extra={extra!r} changed={changed!r})", EXIT_DAMAGE)
	fk = conn.execute("PRAGMA foreign_key_check").fetchall()
	if fk:
		raise BatonError(f"foreign_key_check reported {len(fk)} violation(s)", EXIT_DAMAGE)
	quick = [r[0] for r in conn.execute("PRAGMA quick_check")]
	if quick != ["ok"]:
		raise BatonError(f"quick_check failed: {quick!r}", EXIT_DAMAGE)


def _check_meta(conn: sqlite3.Connection, config: dict, config_digest: str, readonly: bool,
                for_regen: bool = False, for_ceremony: bool = False) -> None:
	row = conn.execute("SELECT * FROM instance_meta WHERE one_row=1").fetchone()
	if row is None:
		raise BatonError("instance_meta row is missing", EXIT_DAMAGE)
	if row["protocol"] != PROTOCOL_VERSION:
		raise BatonError(f"instance protocol {row['protocol']} unsupported", EXIT_PROTOCOL)
	if for_regen:
		if config["generation"] != row["accepted_generation"] + 1:
			raise BatonError(
				f"regen requires config generation {row['accepted_generation'] + 1} "
				f"(accepted {row['accepted_generation']}, offered {config['generation']})")
	elif row["accepted_generation"] != config["generation"] or row["config_sha256"] != config_digest:
		raise BatonError(
			"config digest/generation does not match the accepted instance state "
			f"(accepted generation {row['accepted_generation']}; run regen for config changes)", EXIT_PROTOCOL)
	if row["move_status"] == "moved" and not for_ceremony:
		raise BatonError(f"instance has moved to {row['moved_to']!r}; refusing", EXIT_GATED)
	if not readonly and not for_ceremony and row["maintenance"] == 1:
		raise BatonError("instance is under maintenance; write operations are gated", EXIT_GATED)


def init_instance(config_path: str) -> None:
	"""Crash-atomic initialization: build a uniquely named scratch DB, create
	the schema transactionally, checkpoint/validate/fsync it, then no-clobber
	publish (hardlink) to the final name and fsync the directory. A crash can
	leave recognizable `.init-*` scratch, never a partial final authority."""
	dirfd = open_instance_dir(config_path)
	scratch = None
	sfd = -1
	try:
		config, digest = _read_config_at(dirfd, os.path.basename(config_path))
		_validate_roots(config)
		scratch = f".init-{new_id()}.sqlite3"
		sfd = os.open(scratch, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
		              0o600, dir_fd=dirfd)
		conn = _connect_fd(sfd, readonly=False)
		try:
			if sqlite3.sqlite_version_info < SQLITE_MIN:
				raise BatonError(
					f"SQLite library {sqlite3.sqlite_version} is below the required "
					f"{'.'.join(map(str, SQLITE_MIN))}", EXIT_FLOOR)
			conn.execute("PRAGMA journal_mode=WAL")
			conn.execute("PRAGMA synchronous=FULL")
			conn.execute("PRAGMA trusted_schema=OFF")
			conn.execute("PRAGMA foreign_keys=ON")
			conn.execute("BEGIN IMMEDIATE")
			conn.execute(f"PRAGMA user_version={PROTOCOL_VERSION}")
			for sql in _TABLES.values():
				conn.execute(sql)
			for sql in _INDEXES.values():
				conn.execute(sql)
			# Seed rows BEFORE the guard triggers exist: bootstrap inserts are
			# part of instance creation, not protocol operations.
			conn.execute("INSERT INTO op_context(one_row) VALUES(1)")
			conn.execute(
				"INSERT INTO instance_meta(one_row, uuid, protocol, accepted_generation, "
				"config_sha256, created_ts) VALUES(1, ?, ?, ?, ?, ?)",
				(new_id(), PROTOCOL_VERSION, config["generation"], digest, _utc_now_iso()))
			for root_id, path in config.get("roots", {}).items():
				conn.execute(
					"INSERT INTO accepted_roots(root_id, path, binding_generation) VALUES(?, ?, ?)",
					(root_id, path, config["generation"]))
			for sql in _TRIGGERS.values():
				conn.execute(sql)
			conn.execute("COMMIT")
			_fault("init:post-commit")
			busy, log, ckpt = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
			if busy != 0 or log != ckpt:
				raise BatonError(
					f"init checkpoint incomplete (busy={busy}, log={log}, checkpointed={ckpt})", EXIT_DAMAGE)
			_fault("init:post-checkpoint")
			_validate_schema(conn)
		finally:
			conn.close()
		os.fsync(sfd)
		_fault("init:pre-link")
		try:
			os.link(scratch, DB_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
		except FileExistsError:
			raise BatonError(f"refusing to initialize over existing {DB_NAME}") from None
		_fault("init:post-link")
		os.unlink(scratch, dir_fd=dirfd)
		scratch = None
		_fault("init:post-unlink")
		os.fsync(dirfd)
	except BaseException:
		if scratch is not None:
			try:
				os.unlink(scratch, dir_fd=dirfd)
			except OSError:
				pass
		raise
	finally:
		if sfd >= 0:
			os.close(sfd)
		os.close(dirfd)


def open_instance(config_path: str, *, readonly: bool = False, _for_regen: bool = False,
                  _for_ceremony: bool = False) -> Store:
	dirfd = open_instance_dir(config_path)
	dbfd = -1
	conn = None
	try:
		config, digest = _read_config_at(dirfd, os.path.basename(config_path))
		flags = (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NOFOLLOW | os.O_CLOEXEC
		try:
			dbfd = os.open(DB_NAME, flags, dir_fd=dirfd)
		except FileNotFoundError:
			raise BatonError(f"no {DB_NAME} beside the config (run init)", EXIT_PROTOCOL) from None
		except OSError as exc:
			if exc.errno == errno.ELOOP:
				raise BatonError(f"{DB_NAME} is a symlink; refusing", EXIT_DAMAGE) from exc
			raise
		_validate_roots(config)
		conn = _connect_fd(dbfd, readonly)
		try:
			_verify_db_identity(conn, dbfd, dirfd)
			_apply_connection_contract(conn, readonly)
			_validate_schema(conn)
			_check_meta(conn, config, digest, readonly, for_regen=_for_regen,
			            for_ceremony=_for_ceremony)
		except sqlite3.DatabaseError as exc:
			raise BatonError(f"database failed open validation: {exc}", EXIT_DAMAGE) from exc
		return Store(config_path, config, digest, dirfd, dbfd, conn, readonly)
	except BaseException:
		if conn is not None:
			conn.close()
		if dbfd >= 0:
			os.close(dbfd)
		os.close(dirfd)
		raise


def regen_instance(config_path: str, *, participant: str, actor: str, seed: str) -> dict:
	"""Accept a new config in ONE transaction. Requirements enforced inside
	the transaction: capability authority; offered generation exactly
	accepted+1; NO participant named by a live (pending/claimed) message or
	live notice may be removed; NO root referenced by any retained attachment
	may be removed or remapped (referenced mappings are preserved immutably —
	the accepted_roots table is the publication-time authority). Additive
	changes are always safe."""
	with open_instance(config_path, _for_regen=True) as store:
		store._require_capability(participant, actor, seed, "config", "regen")
		store._txn_begin("regen", actor, seed, participant=participant, ceremony="regen")
		try:
			new_participants = set(store.config["participants"])
			live = store.conn.execute(
				"SELECT DISTINCT from_participant AS p FROM messages WHERE state IN ('pending','claimed') "
				"UNION SELECT DISTINCT to_participant FROM messages WHERE state IN ('pending','claimed') "
				"UNION SELECT DISTINCT from_participant FROM notices").fetchall()
			missing = sorted({r["p"] for r in live} - new_participants)
			if missing:
				raise BatonError(
					f"regen refused: participant(s) {missing!r} are named by live messages/notices "
					"and absent from the offered config")
			new_roots = store.config.get("roots", {})
			for row in store.conn.execute(
					"SELECT DISTINCT m.attach_root_id AS root_id, a.path FROM messages m "
					"JOIN accepted_roots a ON a.root_id = m.attach_root_id "
					"WHERE m.attach_root_id IS NOT NULL").fetchall():
				if new_roots.get(row["root_id"]) != row["path"]:
					raise BatonError(
						f"regen refused: root {row['root_id']!r} is referenced by retained "
						f"attachments and must keep its accepted mapping {row['path']!r}")
			previous = {row["root_id"]: (row["path"], row["binding_generation"])
			            for row in store.conn.execute(
			                "SELECT root_id, path, binding_generation FROM accepted_roots")}
			store.conn.execute("DELETE FROM accepted_roots")
			for root_id, path in new_roots.items():
				prior = previous.get(root_id)
				binding_gen = prior[1] if prior is not None and prior[0] == path else store.config["generation"]
				store.conn.execute(
					"INSERT INTO accepted_roots(root_id, path, binding_generation) VALUES(?, ?, ?)",
					(root_id, path, binding_gen))
			store.conn.execute(
				"UPDATE instance_meta SET accepted_generation=?, config_sha256=? WHERE one_row=1",
				(store.config["generation"], store.config_digest))
			store._txn_commit()
			return {"accepted_generation": store.config["generation"], "config_sha256": store.config_digest}
		except BaseException:
			store._txn_rollback()
			raise


# ---------------------------------------------------------------------------
# Maintenance / move / migrate ceremonies
# ---------------------------------------------------------------------------

CHECKPOINT_DRAIN_ATTEMPTS = 50
CHECKPOINT_DRAIN_SLEEP_S = 0.1


def _audit_ceremony(store: Store, kind: str, participant: str, actor: str, seed: str,
                    reason: str | None, token: str | None, peer: str | None = None) -> str:
	ceremony_id = new_id()
	store.conn.execute(
		"INSERT INTO ceremonies(ceremony_id, kind, participant, actor, seed, reason, token, peer, created_ts) "
		"VALUES(?,?,?,?,?,?,?,?,?)",
		(ceremony_id, kind, participant, actor, seed, reason, token, peer, _utc_now_iso()))
	return ceremony_id


def _committed_ceremony(store: Store, kind: str, token: str) -> sqlite3.Row | None:
	return store.conn.execute(
		"SELECT * FROM ceremonies WHERE kind=? AND token=?", (kind, token)).fetchone()


def _meta(store: Store) -> sqlite3.Row:
	return store.conn.execute("SELECT * FROM instance_meta WHERE one_row=1").fetchone()


def maintenance_enter(config_path: str, *, participant: str, actor: str, seed: str,
                      reason: str, move: bool = False, destination: str | None = None) -> dict:
	"""Set the maintenance gate. For a move, the ONE canonical destination
	directory is bound atomically with the token BEFORE any copy exists, and
	this instance becomes the move SOURCE; a copied database inherits that
	role and therefore can never activate itself."""
	if type(move) is not bool:
		raise BatonError("move must be a boolean")
	if type(reason) is not str or not reason.strip():
		raise BatonError("maintenance requires a non-empty --reason")
	if move:
		if type(destination) is not str or not os.path.isabs(destination) \
				or destination != os.path.normpath(destination) or destination.endswith("/"):
			raise BatonError(
				"a move requires an explicit canonical absolute DESTINATION CONFIG PATH")
		dest_dirfd = _open_dir_no_follow(os.path.dirname(destination), "move destination")
		try:
			ftype = _statfs_ftype(dest_dirfd)
			if ftype not in LOCAL_FS_MAGICS:
				raise BatonError(
					f"move destination filesystem (statfs f_type 0x{ftype:X}) is not a "
					"supported local filesystem", EXIT_DAMAGE)
			dest_identity = os.fstat(dest_dirfd)
			try:
				probe = os.open(os.path.basename(destination),
				                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
				                dir_fd=dest_dirfd)
				try:
					if not stat.S_ISREG(os.fstat(probe).st_mode):
						raise BatonError(
							"destination config path exists and is not a regular file; refusing")
				finally:
					os.close(probe)
			except FileNotFoundError:
				pass
			except OSError as exc:
				if exc.errno == errno.ELOOP:
					raise BatonError("destination config path is a symlink; refusing", EXIT_DAMAGE) from exc
				raise
		finally:
			os.close(dest_dirfd)
	elif destination is not None:
		raise BatonError("destination is only valid with move=True")
	with open_instance(config_path, _for_ceremony=True) as store:
		store._require_capability(participant, actor, seed, "config", "maintenance")
		verb = "move_enter" if move else "maintenance"
		store._txn_begin(verb, actor, seed, participant=participant,
		                 ceremony="move" if move else "maintenance")
		try:
			row = _meta(store)
			if row["maintenance"] == 1:
				raise BatonError("instance is already under maintenance")
			token = new_id() if move else None
			if move:
				source_route = config_path
				if source_route != os.path.normpath(source_route):
					raise BatonError("source config path must be canonical for a move binding")
				source_identity = os.fstat(store.dirfd)
				if os.path.basename(source_route) != os.path.basename(store.config_path):
					raise BatonError("move must be entered at the source's own config path")
				if (source_identity.st_dev, source_identity.st_ino) == \
						(dest_identity.st_dev, dest_identity.st_ino):
					raise BatonError(
						"move source and destination are the same directory; a move must "
						"change the instance's directory identity")
				store.conn.execute(
					"UPDATE instance_meta SET maintenance=1, maintainer_actor=?, maintainer_reason=?, "
					"move_status='moving', move_token=?, move_role='source', move_peer=?, "
					"move_source=? WHERE one_row=1",
					(actor, reason, token, destination, source_route))
				store.conn.execute(
					"INSERT INTO moves(token, instance_uuid, source_config, source_dev, source_ino, "
					"destination_config, destination_dev, destination_ino, created_ts) "
					"VALUES(?,?,?,?,?,?,?,?,?)",
					(token, row["uuid"], source_route, source_identity.st_dev, source_identity.st_ino,
					 destination, dest_identity.st_dev, dest_identity.st_ino, _utc_now_iso()))
			else:
				store.conn.execute(
					"UPDATE instance_meta SET maintenance=1, maintainer_actor=?, maintainer_reason=? "
					"WHERE one_row=1", (actor, reason))
			_audit_ceremony(store, "maintenance_enter", participant, actor, seed, reason, token,
			                peer=destination)
			store._txn_commit()
			_fault("enter:committed")
			return {"maintenance": True, "move_token": token, "destination": destination}
		except BaseException:
			store._txn_rollback()
			raise


def maintenance_exit(config_path: str, *, participant: str, actor: str, seed: str,
                     reason: str) -> dict:
	"""Clear a plain maintenance gate. Any instance that is part of a move
	(source OR copied destination) DEFAULT-REFUSES this generic clear —
	completing or aborting the move are the only exits, so a same-UUID copy
	can never be forked back to active by a routine stale-flag clear."""
	if type(reason) is not str or not reason.strip():
		raise BatonError("maintenance exit requires a non-empty --reason")
	with open_instance(config_path, _for_ceremony=True) as store:
		store._require_capability(participant, actor, seed, "config", "maintenance")
		store._txn_begin("maintenance", actor, seed, participant=participant, ceremony="maintenance")
		try:
			row = _meta(store)
			if row["maintenance"] == 0:
				raise BatonError("instance is not under maintenance")
			if row["move_status"] != "none":
				raise BatonError(
					"instance is part of a move; the generic maintenance clear is refused — "
					"complete the move (bind/activate/decommission) or, on the SOURCE only, "
					"use abort-move with the exact token and a destination-destroyed "
					"attestation", EXIT_GATED)
			store.conn.execute(
				"UPDATE instance_meta SET maintenance=0, maintainer_actor=NULL, "
				"maintainer_reason=NULL WHERE one_row=1")
			_audit_ceremony(store, "maintenance_exit", participant, actor, seed, reason, None)
			store._txn_commit()
			return {"maintenance": False}
		except BaseException:
			store._txn_rollback()
			raise


def checkpoint_drain(store: Store) -> tuple[int, int]:
	"""Run wal_checkpoint(TRUNCATE) with NO open transaction until it reports
	busy==0 AND log==checkpointed, with bounded backoff. Returns the final
	(log, checkpointed) tuple; raises on timeout with the flag left set."""
	import time
	for _ in range(CHECKPOINT_DRAIN_ATTEMPTS):
		busy, log, ckpt = store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
		if busy == 0 and log == ckpt:
			return (log, ckpt)
		time.sleep(CHECKPOINT_DRAIN_SLEEP_S)
	raise BatonError(
		"checkpoint drain did not converge (readers/writers still active); the maintenance "
		"flag remains set — retry after the instance quiesces", EXIT_RACE)


def _write_all(dfd: int, data: bytes) -> None:
	view = memoryview(data)
	while view:
		written = os.write(dfd, view)
		if written == 0:
			raise BatonError("zero-byte write while publishing; refusing", EXIT_DAMAGE)
		view = view[written:]


COPY_CHUNK = 1 << 20


def _sha256_fd_pread(fd: int) -> str:
	hasher = hashlib.sha256()
	offset = 0
	while True:
		chunk = os.pread(fd, COPY_CHUNK, offset)
		if not chunk:
			return hasher.hexdigest()
		hasher.update(chunk)
		offset += len(chunk)


def _hash_existing_regular(dst_dirfd: int, name: str) -> str | None:
	"""Stream-hash an existing destination artifact; absent returns None. The
	artifact must be a regular file (nonblocking/no-follow open) so a FIFO or
	device can neither hang the resume nor impersonate a copied file."""
	try:
		fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
		             dir_fd=dst_dirfd)
	except FileNotFoundError:
		return None
	except OSError as exc:
		if exc.errno == errno.ELOOP:
			raise BatonError(f"destination {name!r} is a symlink; refusing", EXIT_DAMAGE) from exc
		raise
	try:
		if not stat.S_ISREG(os.fstat(fd).st_mode):
			raise BatonError(f"destination {name!r} is not a regular file; refusing", EXIT_DAMAGE)
		return _sha256_fd_pread(fd)
	finally:
		os.close(fd)


def _stream_publish_from_fd(src_fd: int, expected_size: int, dst_dirfd: int, dst_name: str,
                            mode: int) -> str:
	"""Stream the held source fd into a scratch destination in bounded chunks
	while hashing, fsync, then no-clobber publish. An EXISTING regular
	artifact is accepted only when its streamed hash equals the source's
	streamed hash (resume); mismatch fails closed. Bounded memory by
	construction; premature EOF and zero-byte writes fail closed. Returns the
	source hash."""
	source_sha = _sha256_fd_pread(src_fd)
	existing_sha = _hash_existing_regular(dst_dirfd, dst_name)
	if existing_sha is not None:
		if existing_sha != source_sha:
			raise BatonError(
				f"destination already contains a MISMATCHING {dst_name!r}; refusing", EXIT_DAMAGE)
		return source_sha
	scratch = f".copy-{new_id()}"
	dfd = os.open(scratch, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
	              mode, dir_fd=dst_dirfd)
	try:
		verify = hashlib.sha256()
		offset = 0
		while offset < expected_size:
			chunk = os.pread(src_fd, min(COPY_CHUNK, expected_size - offset), offset)
			if not chunk:
				raise BatonError(
					f"premature EOF streaming {dst_name!r} at offset {offset}; refusing", EXIT_DAMAGE)
			verify.update(chunk)
			view = memoryview(chunk)
			while view:
				written = os.write(dfd, view)
				if written == 0:
					raise BatonError(
						f"zero-byte write streaming {dst_name!r}; refusing", EXIT_DAMAGE)
				view = view[written:]
			offset += len(chunk)
		if verify.hexdigest() != source_sha:
			raise BatonError(
				f"source changed while streaming {dst_name!r}; refusing the ambiguous copy",
				EXIT_DAMAGE)
		os.fsync(dfd)
	except BaseException:
		os.close(dfd)
		try:
			os.unlink(scratch, dir_fd=dst_dirfd)
		except OSError:
			pass
		raise
	os.close(dfd)
	try:
		os.link(scratch, dst_name, src_dir_fd=dst_dirfd, dst_dir_fd=dst_dirfd)
	except FileExistsError:
		os.unlink(scratch, dir_fd=dst_dirfd)
		raise BatonError(f"destination race on {dst_name!r}; rerun to verify/resume", EXIT_RACE) from None
	os.unlink(scratch, dir_fd=dst_dirfd)
	os.fsync(dst_dirfd)
	return source_sha


def _publish_bytes_at(dst_dirfd: int, dst_name: str, data: bytes, mode: int,
                      expect_sha: str) -> None:
	"""Publish exact bytes at dst_name (scratch → fsync → no-clobber hardlink
	→ dirfsync). An EXISTING artifact is accepted only if its bytes hash to
	expect_sha (resume); a mismatch fails closed."""
	try:
		existing = os.open(dst_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
		                   dir_fd=dst_dirfd)
	except FileNotFoundError:
		existing = -1
	except OSError as exc:
		if exc.errno == errno.ELOOP:
			raise BatonError(f"destination {dst_name!r} is a symlink; refusing", EXIT_DAMAGE) from exc
		raise BatonError(f"destination {dst_name!r} unreadable: {exc}", EXIT_DAMAGE) from exc
	if existing >= 0:
		try:
			if not stat.S_ISREG(os.fstat(existing).st_mode):
				raise BatonError(f"destination {dst_name!r} is not a regular file; refusing", EXIT_DAMAGE)
			if _sha256_fd_pread(existing) != expect_sha:
				raise BatonError(
					f"destination already contains a MISMATCHING {dst_name!r}; refusing", EXIT_DAMAGE)
			return  # exact artifact already published — resume
		finally:
			os.close(existing)
	scratch = f".copy-{new_id()}"
	dfd = os.open(scratch, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
	              mode, dir_fd=dst_dirfd)
	try:
		_write_all(dfd, data)
		os.fsync(dfd)
	finally:
		os.close(dfd)
	try:
		os.link(scratch, dst_name, src_dir_fd=dst_dirfd, dst_dir_fd=dst_dirfd)
	except FileExistsError:
		os.unlink(scratch, dir_fd=dst_dirfd)
		raise BatonError(f"destination race on {dst_name!r}; rerun to verify/resume", EXIT_RACE) from None
	os.unlink(scratch, dir_fd=dst_dirfd)
	os.fsync(dst_dirfd)


def move_copy(config_path: str, *, participant: str, actor: str, seed: str) -> dict:
	"""Copy the drained, move-gated SOURCE to its BOUND destination config
	path (set at maintenance_enter — never a call-site argument). The DB
	bytes are read from the HELD, identity-verified descriptor after drain;
	the config bytes are re-read through the held instance dirfd and must
	still hash to the accepted canonical digest. Publication is per-file
	resumable BEFORE destination binding (byte/digest equality required);
	after the bind/activate ceremonies the destination legitimately differs,
	so a retry discovers the committed stage from the destination's immutable
	ceremony/token/UUID history and reports it instead of demanding byte
	equality. Unexplained artifacts fail closed."""
	with open_instance(config_path, _for_ceremony=True) as store:
		store._require_capability(participant, actor, seed, "config", "move copy")
		row = _meta(store)
		if row["move_status"] != "moving" or row["move_role"] != "source":
			raise BatonError("move copy requires the move-gated SOURCE (maintenance_enter(move=True))")
		token = row["move_token"]
		source_uuid = row["uuid"]
		binding = store._move_binding(token)
		store._validate_route_identity(binding["source_config"], binding["source_dev"],
		                               binding["source_ino"], "move_copy SOURCE route")
		dest_config = binding["destination_config"]
		if row["move_peer"] != dest_config or row["move_source"] != binding["source_config"]:
			raise BatonError(
				"live move fields disagree with the immutable binding; refusing (corruption)",
				EXIT_DAMAGE)
		dest_dir = os.path.dirname(dest_config)
		dest_name = os.path.basename(dest_config)
		# Stage discovery: a valid destination pair means a committed stage.
		try:
			with open_instance(dest_config, readonly=True, _for_ceremony=True) as peer:
				pm = peer.conn.execute(
					"SELECT uuid, move_status, move_token, move_role FROM instance_meta "
					"WHERE one_row=1").fetchone()
				if pm["uuid"] != source_uuid:
					raise BatonError(
						"destination holds a DIFFERENT instance uuid; refusing", EXIT_DAMAGE)
				# NO stage may be reported for a peer that is not physically at
				# the bound destination identity.
				peer._validate_route_identity(binding["destination_config"],
				                              binding["destination_dev"],
				                              binding["destination_ino"],
				                              "stage discovery DESTINATION route")
				if pm["move_token"] == token and pm["move_role"] == "source":
					return {"move_token": token, "destination": dest_config, "stage": "copied",
					        "already_committed": True}
				if pm["move_token"] == token and pm["move_role"] == "destination":
					return {"move_token": token, "destination": dest_config, "stage": "bound",
					        "already_committed": True}
				activated = _committed_ceremony(peer, "move_activate", token)
				if activated is not None:
					if activated["peer"] != dest_config:
						raise BatonError(
							"activation history names a different route than the bound "
							"destination; refusing", EXIT_DAMAGE)
					return {"move_token": token, "destination": dest_config, "stage": "activated",
					        "already_committed": True}
				raise BatonError(
					"destination pair exists but its move history does not explain this token; "
					"refusing", EXIT_DAMAGE)
		except BatonError as exc:
			# Recovery classification is NARROW: only the two expected absence
			# shapes mean "publish/resume below"; anything else from an
			# existing pair keeps its own reason.
			message = str(exc)
			if not ("run init" in message or "config not found" in message):
				raise
		dest_dirfd = _open_dir_no_follow(dest_dir, "move destination")
		try:
			ftype = _statfs_ftype(dest_dirfd)
			if ftype not in LOCAL_FS_MAGICS:
				raise BatonError(
					f"move destination filesystem (statfs f_type 0x{ftype:X}) is not a "
					"supported local filesystem", EXIT_DAMAGE)
			dest_now = os.fstat(dest_dirfd)
			if (dest_now.st_dev, dest_now.st_ino) != (binding["destination_dev"], binding["destination_ino"]):
				raise BatonError(
					"destination directory identity does not match the identity bound at "
					"maintenance_enter; refusing to publish", EXIT_DAMAGE)
			_fault("move:pre-drain")
			log, ckpt = checkpoint_drain(store)
			_fault("move:post-drain")
			config_name = os.path.basename(store.config_path)
			cfd = os.open(config_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
			              dir_fd=store.dirfd)
			try:
				if not stat.S_ISREG(os.fstat(cfd).st_mode):
					raise BatonError(
						f"source config {config_name!r} is no longer a regular file; refusing",
						EXIT_DAMAGE)
				config_bytes = b""
				while True:
					chunk = os.read(cfd, 1 << 20)
					if not chunk:
						break
					config_bytes += chunk
			finally:
				os.close(cfd)
			try:
				config_text = config_bytes.decode("utf-8")
			except UnicodeDecodeError as exc:
				raise BatonError(f"source config is not valid UTF-8: {exc}", EXIT_DAMAGE) from exc
			reparsed = validate_config(loads_strict(config_text))
			if canonical_sha256(reparsed) != store.config_digest:
				raise BatonError(
					"config bytes no longer match the accepted canonical digest; refusing to copy",
					EXIT_DAMAGE)
			config_sha = hashlib.sha256(config_bytes).hexdigest()
			_publish_bytes_at(dest_dirfd, dest_name, config_bytes, 0o644, config_sha)
			_fault("move:config-copied")
			db_size = os.fstat(store.dbfd).st_size
			_stream_publish_from_fd(store.dbfd, db_size, dest_dirfd, DB_NAME, 0o600)
			_fault("move:db-copied")
		finally:
			os.close(dest_dirfd)
	# Full validation of the gated destination pair before reporting success —
	# including the bound directory identity: a substitution between
	# publication and this open must fail, not report 'copied'.
	with open_instance(dest_config, readonly=True, _for_ceremony=True) as check:
		check._validate_route_identity(binding["destination_config"],
		                               binding["destination_dev"], binding["destination_ino"],
		                               "post-publication DESTINATION route")
		peer_meta = check.conn.execute(
			"SELECT uuid, move_token, move_role, move_peer, move_source FROM instance_meta "
			"WHERE one_row=1").fetchone()
		if peer_meta["uuid"] != source_uuid or peer_meta["move_token"] != token:
			raise BatonError("copied destination failed identity validation; refusing", EXIT_DAMAGE)
		if (peer_meta["move_role"] != "source"
				or peer_meta["move_peer"] != binding["destination_config"]
				or peer_meta["move_source"] != binding["source_config"]):
			raise BatonError(
				"copied destination's live move mirrors disagree with the binding; refusing",
				EXIT_DAMAGE)
	return {"move_token": token, "destination": dest_config, "stage": "copied",
	        "already_committed": False, "checkpoint": (log, ckpt)}


def move_bind_destination(dest_config_path: str, *, participant: str, actor: str, seed: str,
                          token: str) -> dict:
	"""After both files are durably present and the copy validates, flip ONLY
	the copy to role='destination' (audited, exact token). The ceremony
	verifies the copy physically resides at the destination directory bound
	by the source — a copy placed anywhere else refuses — and records its
	peer. Idempotent by committed ceremony."""
	with open_instance(dest_config_path, _for_ceremony=True) as store:
		store._require_capability(participant, actor, seed, "config", "move destination binding")
		store._txn_begin("move", actor, seed, participant=participant, ceremony="move")
		try:
			row = _meta(store)
			committed = _committed_ceremony(store, "move_bind_destination", token)
			if committed is not None and row["move_role"] == "destination":
				binding = store._move_binding(token)
				store._validate_route_identity(binding["destination_config"],
				                               binding["destination_dev"], binding["destination_ino"],
				                               "bind retry DESTINATION route")
				store._txn_rollback()
				return {"already_committed": True, "bound": True}
			if row["move_status"] != "moving" or row["move_role"] != "source":
				raise BatonError(
					f"destination binding requires a moving source-role copy "
					f"(status {row['move_status']!r}, role {row['move_role']!r})")
			if row["move_token"] != token:
				raise BatonError("move token does not match; refusing destination binding")
			binding = store._move_binding(token)
			store._validate_route_identity(binding["destination_config"],
			                               binding["destination_dev"], binding["destination_ino"],
			                               "bind DESTINATION route")
			bound_config = binding["destination_config"]
			store.conn.execute(
				"UPDATE instance_meta SET move_role='destination' WHERE one_row=1")
			_audit_ceremony(store, "move_bind_destination", participant, actor, seed, None, token,
			                peer=bound_config)
			store._txn_commit()
			_fault("bind:committed")
			return {"already_committed": False, "bound": True}
		except BaseException:
			store._txn_rollback()
			raise


def move_activate(dest_config_path: str, *, participant: str, actor: str, seed: str,
                  token: str) -> dict:
	"""Activate the BOUND destination: requires moving + role='destination'
	+ exact token. Retries discover the committed ceremony and return
	already_committed after validating the token."""
	with open_instance(dest_config_path, _for_ceremony=True) as store:
		store._require_capability(participant, actor, seed, "config", "move activation")
		store._txn_begin("move", actor, seed, participant=participant, ceremony="move")
		try:
			row = _meta(store)
			committed = _committed_ceremony(store, "move_activate", token)
			if committed is not None and row["move_status"] == "none":
				binding = store._move_binding(token)
				store._validate_route_identity(binding["destination_config"],
				                               binding["destination_dev"], binding["destination_ino"],
				                               "activation retry DESTINATION route")
				store._txn_rollback()
				return {"already_committed": True, "activated": True}
			if row["move_status"] != "moving":
				raise BatonError(f"destination is not part of a move (status {row['move_status']!r})")
			if row["move_role"] != "destination":
				raise BatonError(
					"activation requires the BOUND destination role; a source (or unbound copy) "
					"can never activate", EXIT_GATED)
			if row["move_token"] != token:
				raise BatonError("move token does not match; refusing activation")
			# A post-bind clone carries the destination role in its BYTES;
			# only the bound directory identity may activate.
			binding = store._move_binding(token)
			store._validate_route_identity(binding["destination_config"],
			                               binding["destination_dev"], binding["destination_ino"],
			                               "activation DESTINATION route")
			bound_route = binding["destination_config"]
			store.conn.execute(
				"UPDATE instance_meta SET maintenance=0, maintainer_actor=NULL, maintainer_reason=NULL, "
				"move_status='none', move_token=NULL, move_role=NULL, move_peer=NULL, "
				"move_source=NULL WHERE one_row=1")
			_audit_ceremony(store, "move_activate", participant, actor, seed, None, token,
			                peer=bound_route)
			store._txn_commit()
			_fault("activate:committed")
			return {"already_committed": False, "activated": True}
		except BaseException:
			store._txn_rollback()
			raise


def move_decommission(source_config_path: str, *, participant: str, actor: str, seed: str,
                      token: str, moved_to: str) -> dict:
	"""Mark the SOURCE 'moved' forever: requires moving + role='source' +
	exact token, and moved_to must equal the bound destination. Retries
	discover the committed ceremony and return already_committed."""
	if type(moved_to) is not str or not os.path.isabs(moved_to) or moved_to != os.path.normpath(moved_to):
		raise BatonError("moved_to must be a canonical absolute path")
	with open_instance(source_config_path, _for_ceremony=True) as store:
		store._require_capability(participant, actor, seed, "config", "move decommission")
		store._txn_begin("move", actor, seed, participant=participant, ceremony="move")
		try:
			row = _meta(store)
			committed = _committed_ceremony(store, "move_decommission", token)
			if committed is not None and row["move_status"] == "moved":
				if committed["peer"] != moved_to:
					raise BatonError(
						f"retried moved_to {moved_to!r} differs from the committed route "
						f"{committed['peer']!r}; refusing", EXIT_PROTOCOL)
				binding = store._move_binding(token)
				store._validate_route_identity(binding["source_config"], binding["source_dev"],
				                               binding["source_ino"], "decommission retry SOURCE route")
				store._txn_rollback()
				return {"already_committed": True, "moved_to": row["moved_to"]}
			if row["move_status"] != "moving":
				raise BatonError(f"source is not part of a move (status {row['move_status']!r})")
			if row["move_role"] != "source":
				raise BatonError("decommission requires the SOURCE role", EXIT_DAMAGE)
			if row["move_token"] != token:
				raise BatonError("move token does not match; refusing decommission")
			binding = store._move_binding(token)
			store._validate_route_identity(binding["source_config"], binding["source_dev"],
			                               binding["source_ino"], "decommission SOURCE route")
			if binding["destination_config"] != moved_to:
				raise BatonError(
					f"moved_to {moved_to!r} does not match the bound destination "
					f"{binding['destination_config']!r}; refusing")
			# PLAN sequence: destination activation precedes source
			# decommission — the bound destination must exist, carry the same
			# immutable UUID, hold the committed activation for this token and
			# route, and be active (ungated) at the exact destination route.
			try:
				with open_instance(moved_to, readonly=True) as dest:
					dest._validate_route_identity(binding["destination_config"],
					                              binding["destination_dev"],
					                              binding["destination_ino"],
					                              "decommission DESTINATION route")
					dest_meta = dest.conn.execute(
						"SELECT uuid, maintenance, move_status FROM instance_meta "
						"WHERE one_row=1").fetchone()
					if dest_meta["uuid"] != row["uuid"]:
						raise BatonError(
							"bound destination carries a different instance uuid; refusing "
							"decommission", EXIT_DAMAGE)
					if dest_meta["maintenance"] != 0 or dest_meta["move_status"] != "none":
						raise BatonError(
							"bound destination is not active; activate it before source "
							"decommission")
					activated = _committed_ceremony(dest, "move_activate", token)
					if activated is None or activated["peer"] != moved_to:
						raise BatonError(
							"bound destination has no committed activation for this token and "
							"route; activate it before source decommission")
			except BatonError as exc:
				if "run init" in str(exc) or "config not found" in str(exc):
					raise BatonError(
						"bound destination does not exist yet; copy/bind/activate before "
						"source decommission") from exc
				raise
			store.conn.execute(
				"UPDATE instance_meta SET move_status='moved', moved_to=? WHERE one_row=1",
				(moved_to,))
			_audit_ceremony(store, "move_decommission", participant, actor, seed, None, token,
			                peer=moved_to)
			store._txn_commit()
			_fault("decommission:committed")
			return {"already_committed": False, "moved_to": moved_to}
		except BaseException:
			store._txn_rollback()
			raise


def abort_move(config_path: str, *, participant: str, actor: str, seed: str, token: str,
               destination_destroyed: bool, reason: str) -> dict:
	"""Abort an in-flight move — SOURCE ONLY. Requires the exact token plus
	an explicit attestation that the destination copy is destroyed (or was
	never created). A destination copy REFUSES abort outright: destroying it
	is the only disposal; it can never interpret the attestation as
	permission to ungate itself."""
	if type(destination_destroyed) is not bool:
		raise BatonError("destination_destroyed must be a boolean")
	if not destination_destroyed:
		raise BatonError(
			"abort-move requires the destination-destroyed attestation; without it the same "
			"mailbox UUID could fork into two active authorities")
	if type(reason) is not str or not reason.strip():
		raise BatonError("abort-move requires a non-empty --reason")
	with open_instance(config_path, _for_ceremony=True) as store:
		store._require_capability(participant, actor, seed, "config", "abort-move")
		store._txn_begin("move", actor, seed, participant=participant, ceremony="move")
		try:
			row = _meta(store)
			if row["move_status"] != "moving":
				raise BatonError(f"instance is not part of a move (status {row['move_status']!r})")
			if row["move_role"] != "source":
				raise BatonError(
					"abort-move requires the SOURCE role; any copy must be destroyed, never "
					"ungated", EXIT_GATED)
			if row["move_token"] != token:
				raise BatonError("move token does not match; refusing abort")
			binding = store._move_binding(token)
			store._validate_route_identity(binding["source_config"], binding["source_dev"],
			                               binding["source_ino"], "abort SOURCE route")
			store.conn.execute(
				"UPDATE instance_meta SET maintenance=0, maintainer_actor=NULL, maintainer_reason=NULL, "
				"move_status='none', move_token=NULL, move_role=NULL, move_peer=NULL, "
				"move_source=NULL WHERE one_row=1")
			_audit_ceremony(store, "abort_move", participant, actor, seed, reason, token)
			store._txn_commit()
			return {"aborted": True}
		except BaseException:
			store._txn_rollback()
			raise


def migrate_instance(config_path: str, *, participant: str, actor: str, seed: str) -> dict:
	"""Schema migration gate. Protocol 6 is the only schema this tool knows.
	The authorized ATTEMPT is durably audited before the unsupported result
	is reported, so the gate's audit claim is true today."""
	with open_instance(config_path, _for_ceremony=True) as store:
		store._require_capability(participant, actor, seed, "config", "migrate")
		row = store.conn.execute(
			"SELECT maintenance FROM instance_meta WHERE one_row=1").fetchone()
		if row["maintenance"] != 1:
			raise BatonError("migrate requires the maintenance gate to be set first")
		store._txn_begin("migrate", actor, seed, participant=participant, ceremony="migrate")
		try:
			_audit_ceremony(store, "migrate", participant, actor, seed,
			                "attempted migration; no path from protocol 6", None)
			store._txn_commit()
		except BaseException:
			store._txn_rollback()
			raise
		raise BatonError(
			f"no migration path exists from protocol {PROTOCOL_VERSION}; this tool only "
			"gains one alongside a protocol bump", EXIT_PROTOCOL)


def move_status_inspect(config_path: str) -> dict:
	"""Read-only inspection of the move/maintenance state — the discovery
	path for a lost maintenance_enter(move=True) response: the committed
	token, role, and bound route are all durably readable."""
	with open_instance(config_path, readonly=True, _for_ceremony=True) as store:
		row = _meta(store)
		binding = None
		if row["move_token"] is not None:
			b = store.conn.execute(
				"SELECT * FROM moves WHERE token=?", (row["move_token"],)).fetchone()
			if b is not None:
				binding = dict(b)
		return {
			"maintenance": bool(row["maintenance"]),
			"maintainer_actor": row["maintainer_actor"],
			"maintainer_reason": row["maintainer_reason"],
			"move_status": row["move_status"],
			"move_token": row["move_token"],
			"move_role": row["move_role"],
			"move_peer": row["move_peer"],
			"move_source": row["move_source"],
			"moved_to": row["moved_to"],
			"binding": binding,
		}


# ---------------------------------------------------------------------------
# wait / eventing (notification is never authority)
# ---------------------------------------------------------------------------

WAIT_RESCAN_INTERVAL_S = 60.0

_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_MODIFY = 0x00000002
_IN_MOVED_TO = 0x00000080
_IN_MOVE_SELF = 0x00000800
_IN_DELETE_SELF = 0x00000400
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_UNMOUNT = 0x00002000
_IN_NONBLOCK = 0x00000800
_IN_CLOEXEC = 0x00080000


_WATCH_MASK = (_IN_CREATE | _IN_DELETE | _IN_MODIFY | _IN_MOVED_TO
               | _IN_MOVE_SELF | _IN_DELETE_SELF | _IN_UNMOUNT)


def _decode_inotify(data: bytes) -> dict:
	"""Decode a raw inotify buffer into the waiter's decision flags:
	`revalidate` (overflow / watch invalidation / directory replaced or
	unmounted → full re-open validation before rearm) and `relevant`
	(a mailbox.sqlite3* name changed)."""
	revalidate = False
	relevant = False
	offset = 0
	while offset + 16 <= len(data):
		_wd, mask, _cookie, name_len = _struct_unpack_from(data, offset)
		name = data[offset + 16: offset + 16 + name_len].split(b"\x00", 1)[0].decode(
			"utf-8", "replace")
		offset += 16 + name_len
		if mask & (_IN_Q_OVERFLOW | _IN_IGNORED | _IN_MOVE_SELF
		           | _IN_DELETE_SELF | _IN_UNMOUNT):
			revalidate = True
		if name.startswith(DB_NAME):
			relevant = True
	return {"revalidate": revalidate, "relevant": relevant}


class _InotifyWatch:
	"""Best-effort inotify watch on the instance DIRECTORY (never a single
	WAL inode — checkpoints create/delete/reset it). Every event is only a
	prompt to requery; failure to arm degrades to pure polling."""

	def __init__(self, instance_dir: str) -> None:
		import ctypes
		self._libc = ctypes.CDLL(None, use_errno=True)
		self.fd = self._libc.inotify_init1(_IN_NONBLOCK | _IN_CLOEXEC)
		if self.fd < 0:
			raise OSError("inotify_init1 failed")
		wd = self._libc.inotify_add_watch(self.fd, instance_dir.encode(), _WATCH_MASK)
		if wd < 0:
			os.close(self.fd)
			raise OSError("inotify_add_watch failed")

	def close(self) -> None:
		try:
			os.close(self.fd)
		except OSError:
			pass

	def poll(self, timeout_s: float) -> dict:
		"""Block up to timeout_s; drain events. Returns flags describing what
		must happen next: {'revalidate': bool} — dir replaced/unmounted/
		overflowed watches require full re-open validation before rearming."""
		import select
		readable, _, _ = select.select([self.fd], [], [], max(timeout_s, 0.0))
		if not readable:
			return {"revalidate": False, "relevant": False}
		try:
			data = os.read(self.fd, 65536)
		except BlockingIOError:
			data = b""
		# The waiter requeries after EVERY poll return (events are hints,
		# never authority), so the decoder's verdict is returned untouched —
		# `relevant` is informational; only `revalidate` alters control flow.
		return _decode_inotify(data)


def _struct_unpack_from(data: bytes, offset: int) -> tuple:
	import struct
	return struct.unpack_from("iIII", data, offset)


def wait_for_message(config_path: str, participant: str, *, actor: str, seed: str,
                     timeout_s: float | None = None,
                     rescan_interval_s: float = WAIT_RESCAN_INTERVAL_S) -> dict:
	"""Query → arm directory watch → REQUERY → block; every event is only a
	prompt to requery the transactional store. The 60s safety rescan always
	applies; without inotify this degrades to pure interval polling. A gated
	(maintenance/moved) instance makes the waiter stand down with the gate's
	own diagnostic rather than spinning."""
	import math
	import time
	if timeout_s is not None and (type(timeout_s) not in (int, float)
			or not math.isfinite(timeout_s) or timeout_s < 0):
		raise BatonError("timeout must be a finite nonnegative number")
	if (type(rescan_interval_s) not in (int, float)
			or not math.isfinite(rescan_interval_s) or rescan_interval_s <= 0):
		raise BatonError("rescan interval must be a finite positive number")
	deadline = (time.monotonic() + timeout_s) if timeout_s is not None else None

	def try_claim() -> dict | None:
		try:
			with open_instance(config_path) as store:
				claim = store.claim(participant, actor=actor, seed=seed)
				# Deterministic seam BETWEEN claim commit and content fetch:
				# an instance transition here must not strand the claim — the
				# delivery below reads through this SAME open, validated
				# Store, never a second open.
				_fault("wait:claimed")
				return _delivery(store, claim)
		except BatonError as exc:
			if exc.exit_code == EXIT_NONE:
				return None
			raise  # gates (EXIT_GATED) and real errors stand the waiter down

	claim = try_claim()
	if claim is not None:
		return claim
	instance_dir = os.path.dirname(config_path)
	while True:
		watch = None
		try:
			try:
				watch = _InotifyWatch(instance_dir)
			except OSError:
				watch = None  # degraded: pure polling
			_fault("wait:armed")
			claim = try_claim()  # requery closes the query→arm race
			if claim is not None:
				return claim
			remaining = None if deadline is None else deadline - time.monotonic()
			if remaining is not None and remaining <= 0:
				raise BatonError(
					f"no message addressed to {participant!r} arrived within the timeout",
					EXIT_NONE)
			slice_s = rescan_interval_s if remaining is None else min(rescan_interval_s, remaining)
			if watch is not None:
				flags = watch.poll(slice_s)
				if flags["revalidate"]:
					watch.close()
					watch = None  # full re-open validation happens in try_claim
			else:
				time.sleep(slice_s)  # degraded polling honors the configured interval
			claim = try_claim()
			if claim is not None:
				return claim
		finally:
			if watch is not None:
				watch.close()


def _body_repr(body: bytes | None, expected_sha256: str | None,
               expected_size: int | None = None) -> dict | None:
	"""Deterministic lossless JSON representation of a byte body: base64 +
	size + sha256 (RECOMPUTED — a stored-metadata mismatch is damage, never
	delivered), plus an exact utf8 field when the bytes decode cleanly."""
	if body is None:
		return None
	import base64
	actual_sha = hashlib.sha256(body).hexdigest()
	if expected_sha256 is not None and actual_sha != expected_sha256:
		raise BatonError(
			"content bytes do not match their recorded sha256; refusing to deliver "
			"contradictory metadata", EXIT_DAMAGE)
	if expected_size is not None and len(body) != expected_size:
		raise BatonError(
			"content bytes do not match their recorded size; refusing to deliver", EXIT_DAMAGE)
	rep = {"base64": base64.b64encode(body).decode("ascii"), "size": len(body),
	       "sha256": actual_sha}
	try:
		rep["utf8"] = body.decode("utf-8")
	except UnicodeDecodeError:
		pass
	return rep


def _delivery(store: Store, claim: dict) -> dict:
	"""The ONE lossless delivery shape shared by claim and wait: claim
	metadata plus the immutable message envelope with its body (lossless) or
	pinned attachment tuple."""
	msg = store.get_message(claim["message_id"])
	body = msg.pop("body", None)
	envelope = {k: msg[k] for k in (
		"id", "from_participant", "to_participant", "kind", "thread_id", "retention",
		"content_sha256", "outcome", "created_ts", "state", "responds_to")}
	envelope["body"] = _body_repr(body, msg["content_sha256"], msg.get("content_size"))
	if msg["attach_root_id"] is not None:
		envelope["attachment"] = {
			"root_id": msg["attach_root_id"], "path": msg["attach_path"],
			"sha256": msg["attach_sha256"], "size": msg["attach_size"],
			"generation": msg["attach_generation"]}
	else:
		envelope["attachment"] = None
	return {"claim": claim, "message": envelope}


# ---------------------------------------------------------------------------
# Observability: doctor / dump / materialize
# ---------------------------------------------------------------------------

_KNOWN_INSTANCE_FILES = ("baton.json", DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm")


def doctor(config_path: str) -> dict:
	"""Read-only diagnosis. `problems` are integrity/logical violations and
	drive ok/exit status; `warnings` are recoverable residue (stale scratch,
	unrecognized files) that never fail the instance."""
	report: dict = {"ok": True, "problems": [], "warnings": []}
	with open_instance(config_path, readonly=True, _for_ceremony=True) as store:
		integrity = [r[0] for r in store.conn.execute("PRAGMA integrity_check")]
		if integrity != ["ok"]:
			report["problems"].append(f"integrity_check: {integrity!r}")
		fk = store.conn.execute("PRAGMA foreign_key_check").fetchall()
		if fk:
			report["problems"].append(f"foreign_key_check: {len(fk)} violation(s)")
		meta = store.conn.execute("SELECT * FROM instance_meta WHERE one_row=1").fetchone()
		report["instance"] = {
			"uuid": meta["uuid"], "protocol": meta["protocol"],
			"accepted_generation": meta["accepted_generation"],
			"maintenance": bool(meta["maintenance"]), "move_status": meta["move_status"],
		}
		report["messages_by_state"] = {
			r[0]: r[1] for r in store.conn.execute(
				"SELECT state, COUNT(*) FROM messages GROUP BY state")}
		report["active_claims"] = [dict(r) for r in store.conn.execute(
			"SELECT claim_id, message_id, actor, claimed_ts FROM claims WHERE state='active'")]
		report["notices"] = store.conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
		ctx = store.conn.execute("SELECT op_id FROM op_context WHERE one_row=1").fetchone()
		if ctx["op_id"] is not None:
			report["problems"].append("op_context is non-NULL outside any transaction")
		# Full audit-chain validation per entity: exactly one birth,
		# contiguous from->to order by seq, legal edges, tail equal to the
		# live row; ledger groups WITHOUT a live row must close in 'gc'
		# (GC'd subjects keep their permanent history).
		_MSG_EDGES = {(None, "pending"), ("pending", "claimed"), ("claimed", "completed"),
		              ("claimed", "closed"), ("claimed", "pending"),
		              ("completed", "gc"), ("closed", "gc")}
		_CLAIM_EDGES = {(None, "active"), ("active", "completed"), ("active", "recovered"),
		                ("completed", "gc"), ("recovered", "gc"), ("active", "gc")}
		_KNOWN_VERBS = {"send", "claim", "reply", "close", "see", "expire", "recover",
		                "regen", "gc", "maintenance", "move", "move_enter", "migrate"}
		# The finite edge -> producing-verb table: an edge outside its verb
		# set is an unexplained audit record even with valid syntax.
		_EDGE_VERBS = {
			("message", None, "pending"): {"send", "reply"},
			("message", "pending", "claimed"): {"claim"},
			("message", "claimed", "completed"): {"reply"},
			("message", "claimed", "closed"): {"close"},
			("message", "claimed", "pending"): {"recover"},
			("message", "completed", "gc"): {"gc"},
			("message", "closed", "gc"): {"gc"},
			("claim", None, "active"): {"claim"},
			("claim", "active", "completed"): {"reply", "close"},
			("claim", "active", "recovered"): {"recover"},
			("claim", "completed", "gc"): {"gc"},
			("claim", "recovered", "gc"): {"gc"},
			("claim", "active", "gc"): {"gc"},
		}
		live_state = {}
		for r in store.conn.execute("SELECT id, state FROM messages"):
			live_state[("message", r["id"])] = r["state"]
		for r in store.conn.execute("SELECT claim_id, state FROM claims"):
			live_state[("claim", r["claim_id"])] = r["state"]
		chains: dict = {}
		op_groups: dict = {}
		for r in store.conn.execute(
				"SELECT seq, entity, entity_id, from_state, to_state, op_id, participant, "
				"actor, seed, verb, at_ts FROM transitions ORDER BY seq"):
			chains.setdefault((r["entity"], r["entity_id"]), []).append(
				(r["from_state"], r["to_state"]))
			if r["verb"] not in _KNOWN_VERBS:
				report["problems"].append(
					f"transition {r['seq']} has unknown verb {r['verb']!r}")
			if not HEX32_RE.match(r["op_id"] or ""):
				report["problems"].append(f"transition {r['seq']} has malformed op_id")
			if not ACTOR_RE.match(r["actor"] or "") or len(r["actor"] or "") > ACTOR_MAX:
				report["problems"].append(f"transition {r['seq']} has malformed actor")
			if not SEED_RE.match(r["seed"] or ""):
				report["problems"].append(f"transition {r['seq']} has malformed seed")
			if r["participant"] is not None and (not ADDRESS_RE.match(r["participant"])
					or len(r["participant"]) > 64):
				report["problems"].append(f"transition {r['seq']} has malformed participant")
			try:
				_parse_ts(r["at_ts"])
			except (ValueError, TypeError):
				report["problems"].append(f"transition {r['seq']} has malformed timestamp")
			allowed_verbs = _EDGE_VERBS.get((r["entity"], r["from_state"], r["to_state"]))
			if allowed_verbs is not None and r["verb"] not in allowed_verbs:
				report["problems"].append(
					f"transition {r['seq']}: edge {r['from_state']!r}->{r['to_state']!r} "
					f"cannot be produced by verb {r['verb']!r}")
			op_groups.setdefault(r["op_id"], set()).add(
				(r["participant"], r["actor"], r["seed"], r["verb"], r["at_ts"]))
		for key, chain in chains.items():
			entity, entity_id = key
			edges = _MSG_EDGES if entity == "message" else _CLAIM_EDGES
			births = sum(1 for f, _t in chain if f is None)
			if births != 1 or chain[0][0] is not None:
				report["problems"].append(
					f"{entity} {entity_id}: {births} birth event(s) or birth not first")
				continue
			broken = False
			for i in range(1, len(chain)):
				if chain[i][0] != chain[i - 1][1]:
					report["problems"].append(
						f"{entity} {entity_id}: transition chain breaks at step {i}")
					broken = True
					break
			if broken:
				continue
			for edge in chain:
				if edge not in edges:
					report["problems"].append(f"{entity} {entity_id}: illegal edge {edge!r}")
					broken = True
					break
			if broken:
				continue
			tail = chain[-1][1]
			live = live_state.get(key)
			if live is None:
				if tail != "gc":
					report["problems"].append(
						f"{entity} {entity_id}: ledger has no live row and does not close in gc")
			elif tail != live:
				report["problems"].append(
					f"{entity} {entity_id}: ledger tail {tail!r} disagrees with live state {live!r}")
		for key in live_state:
			if key not in chains:
				report["problems"].append(f"{key[0]} {key[1]} has no ledger history at all")
		# Every row sharing an op_id was emitted by ONE transaction and must
		# carry one coherent attribution tuple.
		for op_id, tuples in op_groups.items():
			if len(tuples) > 1:
				report["problems"].append(
					f"op {op_id} has {len(tuples)} distinct attribution tuples "
					"(one transaction, one identity)")
		# Content bytes: recorded size/sha must describe the stored bytes,
		# and every content row must have EXACTLY ONE owner.
		for r in store.conn.execute("SELECT content_id, body, sha256, size FROM contents"):
			if len(r["body"]) != r["size"] or hashlib.sha256(r["body"]).hexdigest() != r["sha256"]:
				report["problems"].append(
					f"content {r['content_id']} bytes disagree with recorded size/sha256")
			owners = store.conn.execute(
				"SELECT (SELECT COUNT(*) FROM messages WHERE content_id=?) + "
				"(SELECT COUNT(*) FROM dispositions WHERE content_id=?) + "
				"(SELECT COUNT(*) FROM notices WHERE content_id=?)",
				(r["content_id"], r["content_id"], r["content_id"])).fetchone()[0]
			if owners != 1:
				report["problems"].append(
					f"content {r['content_id']} has {owners} owners (exactly one required)")
		# Retained attachments: verify pinned path/size/hash through the
		# existing no-follow authority — a mutated or unreadable attachment
		# is a problem, not a healthy report.
		for r in store.conn.execute(
				"SELECT id FROM messages WHERE attach_root_id IS NOT NULL"):
			try:
				store.verify_attachment(r["id"])
			except BatonError as exc:
				report["problems"].append(f"attachment of message {r['id']}: {exc}")
		# Attachment pins: pinned root must be the accepted binding at the
		# pinned generation and match the live config mapping.
		config_roots = store.config.get("roots", {})
		for row in store.conn.execute(
				"SELECT id, attach_root_id, attach_generation FROM messages "
				"WHERE attach_root_id IS NOT NULL"):
			accepted = store.conn.execute(
				"SELECT path, binding_generation FROM accepted_roots WHERE root_id=?",
				(row["attach_root_id"],)).fetchone()
			if accepted is None:
				report["problems"].append(
					f"message {row['id']} pins root {row['attach_root_id']!r} with no "
					"accepted binding")
			elif accepted["binding_generation"] != row["attach_generation"]:
				report["problems"].append(
					f"message {row['id']} pins binding generation {row['attach_generation']} "
					f"but the accepted binding is {accepted['binding_generation']}")
			elif config_roots.get(row["attach_root_id"]) != accepted["path"]:
				report["problems"].append(
					f"root {row['attach_root_id']!r} accepted path disagrees with the config")
		# accepted_roots / config coherence.
		accepted_map = {r["root_id"]: r["path"] for r in store.conn.execute(
			"SELECT root_id, path FROM accepted_roots")}
		if accepted_map != dict(config_roots):
			report["problems"].append("accepted_roots does not match the config roots mapping")
		# Instance-dir inventory via the HELD dirfd (never a re-resolved path).
		unrecognized = []
		scratch = []
		for name in sorted(os.listdir(store.dirfd)):
			if name in _KNOWN_INSTANCE_FILES or name == os.path.basename(config_path):
				continue
			if name.startswith(".init-") or name.startswith(".copy-"):
				scratch.append(name)
			else:
				unrecognized.append(name)
		report["stale_scratch"] = scratch
		report["unrecognized_files"] = unrecognized
		if scratch:
			report["warnings"].append(
				f"{len(scratch)} stale scratch file(s) (crash residue; removable)")
		if unrecognized:
			report["warnings"].append(
				f"{len(unrecognized)} unrecognized file(s) in the instance directory")
		# Projection inventory: reconcile configured projection directories
		# against durable messages — projections are caches, so orphans are
		# warnings, but the PLAN requires them inventoried, never ignored.
		projections = {"orphans": [], "checked": 0}
		durable_ids = {r[0] for r in store.conn.execute(
			"SELECT id FROM messages WHERE retention='durable'")}
		# Shared directories accumulate every declaring participant's
		# configured prefix (default "message").
		dir_prefixes: dict = {}
		for spec in store.config["participants"].values():
			proj_dir = spec.get("projection_dir")
			if proj_dir is not None:
				dir_prefixes.setdefault(proj_dir, set()).add(
					spec.get("projection_prefix", "message"))
		for proj_dir, prefixes in dir_prefixes.items():
			try:
				dfd = _open_dir_no_follow(proj_dir, "projection directory")
			except BatonError as exc:
				report["warnings"].append(f"projection directory {proj_dir!r}: {exc}")
				continue
			try:
				for name in sorted(os.listdir(dfd)):
					if not name.endswith(".md") or not any(
							name.startswith(prefix + "-") for prefix in prefixes):
						continue
					projections["checked"] += 1
					stem = name[:-3]
					mid = stem.rsplit("-", 1)[-1]
					if mid not in durable_ids:
						projections["orphans"].append(os.path.join(proj_dir, name))
			finally:
				os.close(dfd)
		report["projections"] = projections
		if projections["orphans"]:
			report["warnings"].append(
				f"{len(projections['orphans'])} projection file(s) reference no durable message")
	report["ok"] = not report["problems"]
	return report


def dump(config_path: str) -> dict:
	"""Human-inspection snapshot of every protocol table (read-only)."""
	out: dict = {}
	with open_instance(config_path, readonly=True, _for_ceremony=True) as store:
		for table in ("instance_meta", "op_context", "messages", "claims", "dispositions",
		              "contents", "notices", "notice_seen", "recoveries", "ceremonies",
		              "moves", "accepted_roots"):
			rows = []
			for r in store.conn.execute(f"SELECT * FROM {table}"):
				row = dict(r)
				for key, value in row.items():
					if isinstance(value, bytes):
						row[key] = f"<{len(value)} bytes>"
				rows.append(row)
			out[table] = rows
		out["transitions_tail"] = [dict(r) for r in store.conn.execute(
			"SELECT * FROM transitions ORDER BY seq DESC LIMIT 50")]
		out["transitions_tail_truncated_to"] = 50
		out["transitions_total"] = store.conn.execute(
			"SELECT COUNT(*) FROM transitions").fetchone()[0]
	return out


def materialize(config_path: str, message_id: str, target_dir: str,
                prefix: str = "message") -> str:
	"""Re-emit a durable message body as a byte-exact projection file in
	target_dir (idempotent: an existing exact file is accepted). Projections
	are caches, never protocol state."""
	with open_instance(config_path, readonly=True) as store:
		msg = store.get_message(message_id)
		if msg["retention"] != RETENTION_DURABLE:
			raise BatonError(
				f"message {message_id!r} is transient; materializing it would create a "
				"durable copy that defeats the retention contract")
		if msg["body"] is None:
			raise BatonError(
				f"message {message_id!r} has no retained body (attachment-only); nothing "
				"to materialize")
		body = msg["body"]
	dirfd = _open_dir_no_follow(target_dir, "projection directory")
	try:
		if not KIND_RE.match(prefix):
			raise BatonError(f"invalid projection prefix {prefix!r}")
		name = f"{prefix}-{msg['created_ts'].replace(':', '-')}-{message_id}.md"
		_publish_bytes_at(dirfd, name, body, 0o644, hashlib.sha256(body).hexdigest())
		return os.path.join(target_dir, name)
	finally:
		os.close(dirfd)


# ---------------------------------------------------------------------------
# CLI (thin layer over the transaction APIs; exit codes per module table)
# ---------------------------------------------------------------------------

def _to_jsonable(value):
	"""Explicit fail-closed protocol encoding: only JSON-native types pass;
	anything unexpected is a bug surfaced as damage, never silently
	stringified."""
	if type(value) is float:
		import math
		if not math.isfinite(value):
			raise BatonError("non-finite float in protocol output", EXIT_DAMAGE)
		return value
	if value is None or type(value) in (str, int, bool):
		return value
	if isinstance(value, dict):
		out = {}
		for k, v in value.items():
			if type(k) is not str:
				raise BatonError(
					f"non-string dict key {k!r} in protocol output", EXIT_DAMAGE)
			out[k] = _to_jsonable(v)
		return out
	if isinstance(value, (list, tuple)):
		return [_to_jsonable(v) for v in value]
	raise BatonError(f"unexpected type {type(value).__name__} in protocol output", EXIT_DAMAGE)


def _print_result(obj) -> None:
	print(json.dumps(_to_jsonable(obj), indent=2, sort_keys=True))





def _read_body(spec: str | None) -> bytes | None:
	if spec is None:
		return None
	if spec == "-":
		return sys.stdin.buffer.read()
	try:
		with open(spec, "rb") as handle:
			return handle.read()
	except OSError as exc:
		raise BatonError(f"body file unreadable: {exc}") from exc


def _parse_attach(spec: str | None):
	if spec is None:
		return None
	root_id, sep, rel = spec.partition(":")
	if not sep or not root_id or not rel:
		raise BatonError("--attach expects ROOT_ID:RELATIVE/PATH")
	return {"root_id": root_id, "path": rel}


def _build_parser():
	import argparse
	parser = argparse.ArgumentParser(
		prog="baton", description="Portable coordination over one transactional authority")
	parser.add_argument("--config", help="absolute path to the instance baton.json")
	parser.add_argument("--version", action="version",
	                    version=f"baton {TOOL_VERSION} (protocol {PROTOCOL_VERSION})")
	sub = parser.add_subparsers(dest="command", required=True)

	def cmd(name, **kwargs):
		c = sub.add_parser(name, **kwargs)
		return c

	def ident(c, singleton_ok=True):
		c.add_argument("--participant", required=True)
		c.add_argument("--actor", required=True)
		c.add_argument("--seed", required=True)

	cmd("init", help="create a new instance beside --config")
	c = cmd("regen", help="accept a generation+1 config")
	ident(c)
	c = cmd("send", help="send a directed message")
	ident(c)
	c.add_argument("--to", required=True)
	c.add_argument("--kind", required=True)
	c.add_argument("--thread")
	c.add_argument("--retention", choices=sorted(RETENTIONS), default=RETENTION_DURABLE)
	c.add_argument("--outcome")
	group = c.add_mutually_exclusive_group()
	group.add_argument("--body", help="body file or - for stdin (default: stdin)")
	group.add_argument("--attach", help="ROOT_ID:REL/PATH — attachment-only message")
	c = cmd("send-notice", help="broadcast a notice (finite TTL)")
	ident(c)
	c.add_argument("--kind", required=True)
	c.add_argument("--ttl-seconds", type=int)
	c.add_argument("--body", default="-")
	c = cmd("claim", help="claim one pending message")
	ident(c)
	c.add_argument("--message-id")
	c = cmd("wait", help="claim, blocking until a message arrives")
	ident(c)
	c.add_argument("--timeout", type=float)
	c.add_argument("--interval", type=float, default=WAIT_RESCAN_INTERVAL_S)
	c = cmd("see", help="mark unseen notices seen and print them")
	ident(c)
	c = cmd("expire", help="expire notices (author-early or TTL-elapsed)")
	ident(c)
	c.add_argument("--notice-id")
	c = cmd("reply", help="reply to a held claim (effectively-once)")
	ident(c)
	c.add_argument("claim_id")
	c.add_argument("--kind", required=True)
	c.add_argument("--to")
	c.add_argument("--thread")
	c.add_argument("--retention", choices=sorted(RETENTIONS))
	c.add_argument("--outcome")
	c.add_argument("--body", default="-")
	c = cmd("close", help="close a held claim (terminal disposition)")
	ident(c)
	c.add_argument("claim_id")
	c.add_argument("--outcome")
	c.add_argument("--retention", choices=sorted(RETENTIONS))
	c.add_argument("--body")
	c = cmd("recover-claim", help="capability-authorized dead-seed recovery")
	ident(c)
	c.add_argument("claim_id")
	c.add_argument("--reason", required=True)
	c = cmd("gc", help="bounded retention garbage collection")
	ident(c)
	c = cmd("scan", help="pending/claimed inventory")
	c.add_argument("--participant")
	cmd("doctor", help="read-only diagnosis")
	cmd("dump", help="read-only table snapshot")
	cmd("inspect", help="move/maintenance state (read-only)")
	c = cmd("materialize", help="re-emit a durable body as a projection file")
	c.add_argument("message_id")
	c.add_argument("--dir", required=True)
	c.add_argument("--prefix", default="message")
	c = cmd("maintenance-enter", help="set the maintenance gate")
	ident(c)
	c.add_argument("--reason", required=True)
	c.add_argument("--move", action="store_true")
	c.add_argument("--destination", help="destination CONFIG path (with --move)")
	c = cmd("maintenance-exit", help="clear a plain maintenance gate")
	ident(c)
	c.add_argument("--reason", required=True)
	c = cmd("move-copy", help="copy the drained source to its bound destination")
	ident(c)
	c = cmd("move-bind", help="flip the copied pair to the destination role")
	ident(c)
	c.add_argument("--token", required=True)
	c = cmd("move-activate", help="activate the bound destination")
	ident(c)
	c.add_argument("--token", required=True)
	c = cmd("move-decommission", help="mark the source moved forever")
	ident(c)
	c.add_argument("--token", required=True)
	c.add_argument("--moved-to", required=True)
	c = cmd("abort-move", help="source-only move abort (attestation required)")
	ident(c)
	c.add_argument("--token", required=True)
	c.add_argument("--destination-destroyed", action="store_true")
	c.add_argument("--reason", required=True)
	c = cmd("migrate", help="audited migration gate")
	ident(c)
	return parser


def main(argv: list[str] | None = None) -> int:
	parser = _build_parser()
	try:
		ns = parser.parse_args(argv)
	except SystemExit as exc:
		# Usage/parse failures are VALIDATION errors (4); --help/--version exit
		# 0. Exit 2 is reserved for the environment-floor bootstrap.
		code = exc.code if isinstance(exc.code, int) else EXIT_PROTOCOL
		return 0 if code == 0 else EXIT_PROTOCOL
	try:
		if ns.command != "init" and ns.config is None:
			raise BatonError("--config is required")
		if ns.command == "init":
			if ns.config is None:
				raise BatonError("--config is required")
			init_instance(ns.config)
			_print_result({"initialized": True, "config": ns.config})
		elif ns.command == "regen":
			_print_result(regen_instance(ns.config, participant=ns.participant,
			                             actor=ns.actor, seed=ns.seed))
		elif ns.command == "send":
			body = None if ns.attach is not None else _read_body(ns.body if ns.body is not None else "-")
			with open_instance(ns.config) as store:
				message_id = store.send(
					ns.participant, ns.to, actor=ns.actor, seed=ns.seed, kind=ns.kind,
					body=body, thread_id=ns.thread, retention=ns.retention,
					outcome=ns.outcome, attach=_parse_attach(ns.attach))
			_print_result({"message_id": message_id})
		elif ns.command == "send-notice":
			with open_instance(ns.config) as store:
				notice_id = store.send_notice(
					ns.participant, actor=ns.actor, seed=ns.seed, kind=ns.kind,
					body=_read_body(ns.body) or b"", ttl_seconds=ns.ttl_seconds)
			_print_result({"notice_id": notice_id})
		elif ns.command == "claim":
			with open_instance(ns.config) as store:
				claim = store.claim(ns.participant, actor=ns.actor, seed=ns.seed,
				                    message_id=ns.message_id)
				result = _delivery(store, claim)
			_print_result(result)
		elif ns.command == "wait":
			result = wait_for_message(ns.config, ns.participant, actor=ns.actor,
			                          seed=ns.seed, timeout_s=ns.timeout,
			                          rescan_interval_s=ns.interval)
			_print_result(result)
		elif ns.command == "see":
			with open_instance(ns.config) as store:
				seen = store.see(ns.participant, actor=ns.actor, seed=ns.seed)
			for notice in seen:
				notice["body"] = _body_repr(notice.get("body"), notice.get("content_sha256"))
			_print_result({"notices": seen})
		elif ns.command == "expire":
			with open_instance(ns.config) as store:
				removed = store.expire(ns.participant, actor=ns.actor, seed=ns.seed,
				                       notice_id=ns.notice_id)
			_print_result({"expired": removed})
		elif ns.command == "reply":
			with open_instance(ns.config) as store:
				result = store.reply(ns.claim_id, actor=ns.actor, seed=ns.seed,
				                     kind=ns.kind, body=_read_body(ns.body),
				                     outcome=ns.outcome, recipient=ns.to,
				                     thread_id=ns.thread, retention=ns.retention)
			_print_result(result)
		elif ns.command == "close":
			with open_instance(ns.config) as store:
				result = store.close_claim(ns.claim_id, actor=ns.actor, seed=ns.seed,
				                           body=_read_body(ns.body), outcome=ns.outcome,
				                           retention=ns.retention)
			_print_result(result)
		elif ns.command == "recover-claim":
			with open_instance(ns.config) as store:
				result = store.recover_claim(ns.claim_id, participant=ns.participant,
				                             actor=ns.actor, seed=ns.seed, reason=ns.reason)
			_print_result(result)
		elif ns.command == "gc":
			with open_instance(ns.config) as store:
				result = store.gc(participant=ns.participant, actor=ns.actor, seed=ns.seed)
			_print_result(result)
		elif ns.command == "scan":
			with open_instance(ns.config, readonly=True, _for_ceremony=True) as store:
				_print_result(store.scan(ns.participant))
		elif ns.command == "doctor":
			report = doctor(ns.config)
			_print_result(report)
			return 0 if report["ok"] else EXIT_DAMAGE
		elif ns.command == "dump":
			_print_result(dump(ns.config))
		elif ns.command == "inspect":
			_print_result(move_status_inspect(ns.config))
		elif ns.command == "materialize":
			path = materialize(ns.config, ns.message_id, ns.dir, prefix=ns.prefix)
			_print_result({"projection": path})
		elif ns.command == "maintenance-enter":
			_print_result(maintenance_enter(
				ns.config, participant=ns.participant, actor=ns.actor, seed=ns.seed,
				reason=ns.reason, move=ns.move, destination=ns.destination))
		elif ns.command == "maintenance-exit":
			_print_result(maintenance_exit(
				ns.config, participant=ns.participant, actor=ns.actor, seed=ns.seed,
				reason=ns.reason))
		elif ns.command == "move-copy":
			_print_result(move_copy(ns.config, participant=ns.participant,
			                        actor=ns.actor, seed=ns.seed))
		elif ns.command == "move-bind":
			_print_result(move_bind_destination(
				ns.config, participant=ns.participant, actor=ns.actor, seed=ns.seed,
				token=ns.token))
		elif ns.command == "move-activate":
			_print_result(move_activate(
				ns.config, participant=ns.participant, actor=ns.actor, seed=ns.seed,
				token=ns.token))
		elif ns.command == "move-decommission":
			_print_result(move_decommission(
				ns.config, participant=ns.participant, actor=ns.actor, seed=ns.seed,
				token=ns.token, moved_to=ns.moved_to))
		elif ns.command == "abort-move":
			_print_result(abort_move(
				ns.config, participant=ns.participant, actor=ns.actor, seed=ns.seed,
				token=ns.token, destination_destroyed=ns.destination_destroyed,
				reason=ns.reason))
		elif ns.command == "migrate":
			_print_result(migrate_instance(ns.config, participant=ns.participant,
			                               actor=ns.actor, seed=ns.seed))
		else:  # pragma: no cover
			raise BatonError(f"unknown command {ns.command!r}")
		return 0
	except BatonError as exc:
		print(f"baton: {exc}", file=sys.stderr)
		return exc.exit_code


if __name__ == "__main__":  # pragma: no cover
	raise SystemExit(main())
