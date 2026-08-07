# baton — portable coordination over one transactional authority

Baton is a standalone, stdlib-only coordination tool: role-addressed
handoffs, broadcast notices, and audited administrative ceremonies over a
single SQLite database. It has no dependency on any host project; every
instance is defined entirely by one explicitly passed strict-JSON config.

Floors (fail-closed, documented exit code 2): Python >= 3.11, the sqlite3
module, SQLite library >= 3.37.0. Linux, local filesystems only.

## Instance

An instance is a directory holding `baton.json` (the config, always passed
explicitly via `--config`) and `mailbox.sqlite3` (the single authority; its
WAL/SHM siblings belong to SQLite). There is no other authoritative file.
Create one with:

    baton --config /abs/path/instance/baton.json init

See `example-baton.json` for the config shape: participants are dotted
addresses with `identity` `agent` (any actor) or `singleton` (one bound
actor); administrative authority is granted ONLY by an explicit
`capabilities` list (`recovery`, `config`) — never inferred from identity.
`roots` name the directories attachments may reference. `retention_days`
bounds transient-metadata garbage collection.

## Core commands

`send` (body from stdin/file XOR `--attach ROOT:REL/PATH`), `send-notice`
(finite TTL, default 86400s), `claim` / `wait` (one lossless delivery shape:
claim metadata plus envelope with base64+sha256 body or pinned attachment
tuple), `reply` / `close` (effectively-once: retries redeliver the committed
disposition and mismatches fail closed), `see` / `expire` (notices),
`recover-claim` (requires the `recovery` capability and a reason), `gc`,
`regen` (accept a generation+1 config; requires `config`), `scan`,
`doctor`, `dump`, `inspect`, `materialize`.

Projections: `materialize --dir DIR --prefix P` re-emits a durable body as a
byte-exact `P-<created>-<id>.md` file. The prefix is an EXPLICIT caller
choice; participants' configured `projection_prefix`/`projection_dir` define
which files `doctor` owns and inventories (orphans are warnings).

## Maintenance and moves

`maintenance-enter/exit` gate the instance (exit refuses during a move).
A move binds one source and one destination config path plus their directory
identities immutably at entry; `move-copy`, `move-bind`, `move-activate`,
`move-decommission`, and `abort-move` are exact-token audited ceremonies —
at most one instance with a given UUID can ever be active through the API.

## Exit codes

0 success · 2 environment floor · 3 nothing eligible · 4 validation/usage ·
5 race/busy · 6 integrity damage · 7 gated (maintenance/moved).

## Distribution

`python3 build_zipapp.py [outdir]` builds a deterministic executable
`baton` zipapp and writes `DISTRIBUTION.json` (tool/protocol versions,
floors, artifact hash). Same inputs, same bytes. A complete deployment also
ships the generic `AGENTS-MAILBOX-PROTO.md` beside the executable; consumer
projects keep only their local participant bindings and discover paths from
the deployment rather than hard-coding a checkout or host layout.
