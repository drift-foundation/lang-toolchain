# Baton v6 — consolidated implementation plan (central transactional authority)

Status: **implementation authorized by Slawomir (review-2026-08-06T18-23-03Z) and in progress.** Storage/notices/recovery/GC slice signed off (review-2026-08-06T19-24-44Z); maintenance/move/migrate slice signed off (review-2026-08-06T20-26-11Z). Remaining: CLI, wait/eventing, doctor/scan/dump/materialize, packaging/cutover. This document is the
single current plan, consolidated per review-2026-08-06T15-57-39Z. It
supersedes reconstruction from the fourteen design rounds recorded in
PROGRESS.md; the rounds remain as history, and the filename-state-machine
design (rounds 7–10) is a superseded contingent record. Prior phased text of
this file is replaced; the round-6 packaging decision (deterministic Python
zipapp; PEX/scie rejected) is carried forward unchanged.

## Architecture and invariants

- ONE logical authoritative coordination resource: a SQLite database
  (`mailbox.sqlite3`, with SQLite-owned WAL/SHM siblings forming one
  transactional authority). No filename-as-state transitions. SQLite is the
  accepted leading implementation of Slawomir's central-authority invariant;
  flocked whole-file JSON is the fallback only if a falsifiable SQLite
  requirement fails (double-win claims, lost synchronous=FULL commits,
  provably missed WAL-watch wakeups, sqlite3-less target Pythons).
- Semantic invariants carried over from the filename design: effectively-once
  logical publication; redelivery-never-recreation on retry/recovery;
  immutable per-attempt claim identity, never rewritten by recovery;
  human-authorized (never age/timeout-based) recovery; fail-closed
  everywhere; notification is never authority.
- Instance dir = directory of the explicitly passed `--config` (baton.json).
  Contents: `baton.json` (external strict-JSON policy INPUT), the DB, and
  nothing else authoritative. **There is no instance.json** — the DB owns all
  mutable instance state (uuid, protocol, accepted config generation+digest,
  maintenance, move status). Finding-side response `.md` files are idempotent
  post-commit projections, never protocol state.
- Linux-only, local filesystems only (statfs check; SQLite-on-network-fs
  refused). dirfd/no-follow discipline for config, roots, attachments, and
  projection writes. Strict JSON (dup-key/comment/NaN/unknown-field/
  Bool-vs-Int rejection; canonical sorted-compact digest) for baton.json and
  attachment references.
- Versioning: protocol integer 6 == PRAGMA user_version == schema contract;
  tool SemVer independent, starting 1.0.0. Floors, fail-closed with clean
  diagnostics and one documented exit-code table: Python >= 3.11 (bootstrap
  parseable on ~3.6, exit 2), sqlite3 module present, SQLite library >=
  3.37.0 (STRICT tables).
- **Extraction purity (user requirement)**: everything under `tools/baton/`
  — implementation, tests, schemas, example configs, README, zipapp
  metadata, DISTRIBUTION manifest — is semantically independent of Drift:
  no dependence on drift-lang, `work/`, `finding-*`, compiler releases,
  AGENTS workflow, or Drift participant names. Tests use neutral
  multi-workspace fixtures/addresses. Drift's active config, finding
  projection policy, participant declarations, cutover instructions, and
  AGENTS integration live OUTSIDE the reusable package (repo-level docs/
  config only). Extraction to a standalone repository must require
  path/build changes, not semantic edits.

## Schema (STRICT tables; abort-trigger immutability)

- `instance_meta` — typed singleton (one_row CHECK): uuid, protocol,
  accepted config generation + canonical sha256, maintenance flag +
  maintainer identity/reason, move_status ('none','moving','moved') +
  move_token + moved_to.
- `messages` — id (32-hex), from/to participant, kind, thread_id, retention
  ('durable','transient'), content_id → contents (NULL after scrub/bodyless),
  content_sha256 (always populated; survives scrub), attachment reference
  columns ({root_id, path, sha256, size, generation}, NULL-group), created,
  state CHECK ('pending','claimed','completed','closed','expired'),
  responds_to → messages, completed_at. Owns DELIVERY STATE ONLY.
- `claims` — claim_id (32-hex) PK, message_id, actor, seed, claimed_at,
  state ('active','completed','recovered'), terminal_at. Immutable
  per-attempt rows; PARTIAL UNIQUE INDEX on (message_id) WHERE
  state='active'.
- `dispositions` — claim_id UNIQUE (≤1 terminal disposition per claim, as a
  constraint), kind ('reply','close'), outcome, content linkage
  (reply → response_message_id, trigger equates content_sha256 with the
  outgoing message; close → own content_id), created.
- `contents` — content_id PK, body BLOB, sha256 (non-unique INDEX), size,
  created. PER-OWNER rows — no deduplication; BEFORE UPDATE always aborts;
  BEFORE DELETE aborts unless op_context.verb ∈ authorized deleting verbs
  ('consume_transient','gc').
- `notices` / `notice_seen` — broadcast + seen receipts (FK CASCADE);
  expire+seen-cleanup is one transaction.
- `recoveries` — recovery_id, claim_id, actor, seed, reason or pinned reason
  reference, created. Permanent.
- `transitions` — append-only ledger (seq, entity, entity_id, from_state,
  to_state, op_id, actor, seed, at). Populated by AFTER-INSERT triggers
  (birth events: NULL→'pending' for messages, NULL→'active' for claims) AND
  AFTER-UPDATE triggers on state — every row's first state is explained and
  no code path can skip either; attribution from the same op_context;
  uncontextual INSERT fails closed exactly like uncontextual UPDATE (BEFORE
  triggers abort on NULL op_id); a context-bearing direct INSERT is logged
  attributed. GC of transient terminal metadata emits a final ledger event
  (to_state='gc') in the deleting transaction before the rows disappear.
  UPDATE/DELETE on transitions abort; permanent (soft entity references so
  the ledger outlives GC'd subjects).
- `op_context` — strict singleton set as the FIRST statement of every write
  transaction (fresh op_id + actor/seed/verb) and cleared as the LAST;
  BEFORE-UPDATE state triggers abort on NULL op_id (uncontextual direct SQL
  fails closed) and validate the old→new edge against the legal state graph
  in-schema.

## Transactions (each ONE `BEGIN IMMEDIATE`; writers serialize — documented)

- **claim**: INSERT claims(active) + UPDATE messages pending→claimed
  (conditional predicate, rowcount=1 single winner; partial unique index
  backstop).
- **reply**: verify active claim (claim_id+actor+seed); INSERT contents +
  outgoing message; INSERT disposition; claim→completed; incoming→completed.
  Canonical body lives IN the DB in this same transaction.
- **close** (terminal disposition, incl. empty-text with outcome): same shape
  without an outgoing message. Literally-nothing close = claim completed with
  disposition kind 'close', no content.
- **Retry idempotence**: reply/close first SELECTs dispositions by claim_id.
  Found ⇒ validate supplied routing/kind/outcome/content-hash against the
  committed row: match ⇒ report already-committed (+re-materialize durable
  projection); mismatch ⇒ fail closed. Transient-after-consumption ⇒ report
  identity/hash only (bytes deliberately erased). Not found ⇒ pre-COMMIT
  crash ⇒ execute normally.
- **Transient scrub**: in the consuming transaction — clear the sole owning
  content reference, DELETE that content row. content_sha256 + routing +
  claim/disposition identity survive as the idempotence/audit anchor.
- **recover-claim**: claims active→recovered (exact claim_id) + INSERT
  recoveries + messages claimed→pending. Later claimants get new claim rows;
  history never rewritten; pre-COMMIT death leaves nothing (no takeover
  recursion needed).
- **regen**: validate new strict-JSON config; require generation ==
  accepted+1; update instance_meta digest/generation. One transaction.
- **gc**: bounded deletion of transient/expired TERMINAL metadata older than
  config retention_days; durable messages+contents and
  transitions/recoveries are permanent.
- **Attachments** (separately authored evidence): committed/fsynced BEFORE
  the transaction, hash-pinned {root_id, path, sha256, size, generation};
  attachment, never message authority. CLI: body (stdin/--body) XOR
  --attach — both is an error; expanding requires Slawomir's approval.

## Ceremonies

- **maintenance/move** (maintenance exists ONLY for move/migrate):
  (1) txn: maintenance=1 + move_status='moving' + fresh move_token;
  (2) cooperative refusal (every op checks first; waiters stand down on
  requery); (3) drain: `wal_checkpoint(TRUNCATE)` with NO open transaction,
  bounded backoff until busy==0 AND log==checkpointed (verified live:
  in-txn checkpoint raises; active reader ⇒ (1,1,0); drained ⇒ (0,0,0));
  (4) verify WAL truncated + stable, close own connection; (5) dirfd copy of
  DB + baton.json; copy starts gated; (6) DESTINATION activation txn after
  full open validation: move_status='none', maintenance=0, audited with
  token; (7) SOURCE decommission txn: move_status='moved', moved_to
  recorded, maintenance stays set forever. At-most-one-ACTIVE at every crash
  point; stale-flag clear on a 'moving' store default-refuses and requires
  --abort-move + exact token + destruction attestation, audited.
- **stale maintenance clear** (non-move): human-authorized, audited.
- **migrate**: under the maintenance gate; only path that may restructure
  schema/ledger; user_version stepped explicitly.

## SQLite open boundary (dirfd/no-follow covers the authority itself)

`sqlite3.connect()` exposes no SQLITE_OPEN_NOFOLLOW, so the DB is opened
fd-anchored: open the instance directory `O_DIRECTORY|O_NOFOLLOW`; open the
DB relative to that dirfd with `O_NOFOLLOW` — operational open is
EXISTING-ONLY (`O_RDWR`), init is `O_CREAT|O_EXCL`; connect via URI
`file:/proc/self/fd/<fd>?mode=rw` (mode=rw never auto-creates); then verify
identity: `PRAGMA database_list` canonical path must fstat to the SAME
(st_dev, st_ino) as the held fd, and its parent must be the held dirfd —
closing the /proc round-trip. Probed live on this host: canonicalization
lands sidecars (-wal/-shm) beside the real DB; triggers function under
`trusted_schema=OFF`; a missing DB refuses at the OS open (autocreate
impossible); a symlink refuses with ELOOP. Residual risk pinned: SQLite
re-derives sidecar paths from the canonical name — the post-connect
inode/parent verification is the guard; if any platform breaks that
invariant, STOP for Slawomir rather than falling back to pathname
validation.

## Connection/durability contract (every connection, before protocol work)

Init deliberately establishes `journal_mode=WAL`. Every connection then
sets/verifies: WAL (refuse if the persistent mode differs), 
`synchronous=FULL`, `foreign_keys=ON`, bounded `busy_timeout`,
`trusted_schema=OFF` (compatible with the static triggers — probed).
Operational opens are existing-only (a typo can never create an empty DB).
Read-only connections (doctor/scan/dump/wait queries) open
`mode=ro`-equivalent and never take the write path; schema/integrity
validation precedes any mutation; reopen verification refuses when
persistent mode or schema invariants differ. Clarified: "transient scrub"
is PROTOCOL retention (rows/bytes removed from the logical store), not
guaranteed forensic erasure from SQLite pages/WAL — that stronger promise
is out of scope unless intentionally implemented and tested
(secure_delete/VACUUM would be that follow-up).

## Open validation (before any mutation)

Config regular/absolute/non-symlink + strict JSON + digest/generation match
against instance_meta; the fd-anchored open + identity verification above;
SQLite library capability probe (>= 3.37.0, STRICT);
user_version == 6; schema-object validation; foreign_key_check; quick_check
(full integrity_check in doctor); local-fs check; move_status/maintenance
gates; actor grammar `[a-z][a-z0-9_-]*` (config addresses validated at
init/regen; the invocation actor at open).

## wait / eventing (no daemon)

Eligibility query → arm inotify on the INSTANCE DIRECTORY (filtered to
mailbox.sqlite3/-wal/-shm; the -wal inode is created/deleted/reset by
checkpoints — never watch one inode) → REQUERY (closes the arm race) → block;
every event is only a prompt to requery. IN_Q_OVERFLOW ⇒ requery+rearm;
IN_IGNORED ⇒ rearm or degrade; DELETE_SELF/MOVE_SELF/UNMOUNT ⇒ full re-open
validation first. 60s safety rescan always; degraded mode = pure polling
(v5 parity). Waiters observe maintenance/move gates each requery.

## Observability

`scan` (mailbox state), `dump` (human inspection; 3.11 lacks
`python -m sqlite3`), `doctor` (read-only: integrity_check,
foreign_key_check, unreferenced projections/attachments, ledger/attribution
cross-checks, gate states), `materialize <id>` (byte-exact projection
re-emit).

## Cutover (one-way) and distribution

Drain/close every v5 claim/pending/notice (v5 doctor --assert-empty) and
stop every v5 waiter BEFORE protocol-6 init; no dual reader/importer absent
Slawomir's approval. Edit ledger (approved, adapted): REPLACE
baton_v5.py/test_baton_v5.py with the v6 implementation/tests; DELETE
roles.json + stale v4 artifacts; REWRITE AGENTS-MAILBOX-PROTO.md as v6 (
finding-* policy moves to AGENTS.md); REWRITE tools/baton/README.md; ADD
config schema/example + DISTRIBUTION manifest (tool_version,
protocol_version, python_min, sqlite_min, schema version); retire the
work/mailbox .gitignore entry when the instance moves external. Packaging:
deterministic stdlib-only zipapp `bin/baton` (round-6 decision), bootstrap
floor probes for Python and sqlite3.

## Test matrix (consolidated; supersedes all earlier matrices)

Core: T1 concurrent-claim single winner (partial-unique backstop).
T2 immutable claim history across recover→reclaim. T3 reply/close retry
idempotence (post-commit discovery/validation/redelivery; mismatch
fail-closed; pre-commit clean re-run). T4 disposition uniqueness at
constraint level. T5 body-in-DB authority (works with projection absent;
byte-exact materialize; attachment pinning + mutation detection).
T6 ledger inseparability + transitions immutability. T7 maintenance gate +
checkpoint drain proof + stale-flag ceremonies. T8 eventing (arm race, WAL
reset, overflow, invalidation, unmount; degraded parity; 60s rescan).
T9 writer serialization + busy_timeout bounded diagnostics. T10 open
validation matrix + corrupted-db fail-closed. T11 recover-claim atomicity +
audit. T12 expire+seen one txn. T13/T20 floors (python, sqlite3 module,
SQLite >= 3.37.0). T14 unsupported-fs refusal. T15 move ceremony crash
matrix: at-most-one-active PLUS restart/resume at every source/destination
boundary; destination activation and source decommission idempotent by
exact token; abort-move refused unless the named destination is still gated
or attestably destroyed; a copied same-UUID DB is NEVER clearable through
the generic stale-maintenance ceremony ('moving' stores require the
token-bearing move ceremonies); 'moved' store refusal. T16 transient retention (scrub in consuming txn; identity-only
retry; gc bounds; permanent ledger). T17 content normalization (no dedup;
hash-drift unconstructible). T18 attribution (NULL op_id aborts; state-graph
edges enforced). T19 event edges. T21 one-way cutover (init refused over
undrained v5). T22 single-authority instance lifecycle (no instance.json;
one-txn regen; move boundaries). T23 per-owner content + CLI body-XOR-attach.
T24 fd-anchored DB open: symlink-swap refused (ELOOP); missing DB never
auto-created (OS open + mode=rw); wrong-inode/parent identity mismatch
fails closed; sidecars land beside the verified DB; init no-clobber
(O_CREAT|O_EXCL); connection-contract reopen verification (persistent
mode/schema mismatch refused); read-only connections cannot mutate.
T25 ledger birth/GC events: INSERT-born rows have explained first states;
uncontextual INSERT aborts; context-bearing direct INSERT logged; gc
emits final ledger events before deletion.
T26 extraction purity: the tools/baton/ package + tests run green in an
isolated checkout with no Drift repo present (neutral fixtures only);
grep-clean of drift/work/finding/AGENTS references in the reusable set.
Retained: config/init strict-JSON validation, roots/containment
(dirfd/no-follow, symlink escape), notices/transient semantics, zipapp
packaging determinism + probes.

## Execution phases (upon authorization)

1. Schema + storage layer with fault-injection seams; open validation.
2. Core transactions (claim/reply/close/retry/scrub) + CLI; projections.
3. Notices, recovery, gc, maintenance/move, migrate.
4. wait/eventing; doctor/scan/dump/materialize.
5. Packaging (zipapp), DISTRIBUTION manifest, cutover of the Drift
   instance; protocol doc rewrite.
Red-first throughout; every T-item lands with its phase.
