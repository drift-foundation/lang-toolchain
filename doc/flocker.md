# flocker — host-local slot wrapper for arbitrary commands

`flocker` is a small bash utility that caps how many processes sharing a
named key can run concurrently on a single host. It is generic — no
coupling to any specific consumer — and ships with the Drift toolchain
because Drift's certification gates need a global concurrency cap across
multiple test lanes. Other workflows that need the same primitive can
use it equally well.

## When to reach for it

You have N independent processes that all want to run, but the host
budget (RAM, file descriptors, GPU, license seats, anything finite) only
admits K of them concurrently. The processes may be started from
unrelated parents — different shells, different lanes of a CI matrix,
different orchestration scripts — so an in-shell `xargs -P` or per-shell
job queue won't do, because each shell only sees its own queue depth.

Wrap each invocation with `flocker -k SOMEKEY -j K -- CMD…`. All
wrappers naming the same key cooperate on the same K-slot pool.
Wrappers naming different keys are independent.

Concrete example: a certification gate runs three test lanes (plain
build, ASAN build, valgrind build) in parallel, each spawning compiler
invocations with `-j $(nproc)/2`. The 16-job-per-lane budget multiplies
to 48 in-flight compiles across lanes, and the compiler's per-invocation
memory pushes the host over its physical RAM. The fix is to cap *across
lanes*, not within each lane. `flocker -k toolchain-jobs -j 16 -- $CC …`
in every lane's compile launcher gives all three lanes a shared 16-slot
pool, no matter what their per-lane parallelism setting is.

## Synopsis

```text
flocker --key KEY -j N -- COMMAND [ARGS...]
flocker -k KEY -j N -- COMMAND [ARGS...]
flocker --help
flocker --version
```

`--` is required when `COMMAND` or its arguments could be mistaken for
`flocker` options.

## Options

- `-k`, `--key KEY` — pool name. Must match `[A-Za-z0-9_.-]+` so it can
  be used as a path component. Processes naming the same key share a
  pool on the same host.
- `-j N` — pool size. The first caller for a given key fixes N; later
  callers passing a different N get a warning on stderr and use the
  size the first caller established. Persistent across processes
  because the size is recorded in a sentinel file inside the pool dir.
- `--help`, `--version` — informational; do not acquire a slot.

## Semantics

- **Host-local.** flocker uses `flock(2)` on regular files in a per-host
  directory. There is no cross-host coordination. Two hosts running
  independent flocker invocations against the same key see two
  independent N-slot pools.
- **Key-scoped.** Each key gets its own pool directory. Keys are
  isolated from each other.
- **First `-j` wins.** Concurrent first-callers serialize through an
  init lock; whichever holds it first writes the pool size. Later
  callers requesting a different N receive a stderr warning and run
  against the existing pool size.
- **Blocking acquisition.** A caller blocks until a slot is free, then
  `exec`s into `COMMAND`, replacing the wrapper process. The kernel
  releases the slot when `COMMAND` exits, regardless of cause: clean
  exit, crash, signal, OOM kill. There is no release logic that can be
  skipped by abnormal termination.
- **Exit code preserved.** Because the wrapper `exec`s, `COMMAND`'s
  exit status is the wrapper's exit status. No wrapping, no rewriting.
- **stdin/stdout/stderr transparent.** The wrapper does not interpose
  on streams; `COMMAND` inherits them directly from the wrapper's
  caller.

## Pool state on disk

By default, pool state lives under `$TMPDIR/flocker` or `/tmp/flocker`
when `$TMPDIR` is unset. Override with `FLOCKER_DIR`.

```text
$FLOCKER_DIR/
├── <key>.init.lock     # provisioning serialization (per key)
└── <key>/              # pool directory
    ├── .size           # persisted N (the first writer's pool size)
    ├── 1               # token files (one per slot)
    ├── 2
    └── ...
```

- The init lock file lives **outside** the pool directory so token
  cleanup during recovery cannot touch it.
- Token files are empty regular files. The slot is held by a `flock`
  on the file's open file description, not by file contents.
- `.size` is the "provisioning completed" sentinel. Its absence means
  no successful provision has happened yet (fresh or crashed
  initializer).

Pool state is **not** auto-cleaned. On Linux with a tmpfs `/tmp`, reboot
clears it. If you use a persistent `FLOCKER_DIR`, clean it yourself when
you no longer care about the state — `rm -rf $FLOCKER_DIR/<key>` while
no callers are active is safe.

**Do not manually delete `.size` or token files while jobs are running.**
flocker can recover from a crashed initializer, but it cannot defend
against operator deletion of live state. If you blast `.size` while
jobs hold tokens, the next caller will re-provision and you will end up
with a duplicated pool — old jobs holding orphaned-inode locks, new
jobs running against fresh tokens with no interlock between them.

## Recovery from a dead initializer

If the process that holds the init lock dies before writing `.size`
(crash, SIGKILL, host reboot during init), the kernel releases the
init lock automatically. The next caller acquires the lock blockingly,
sees `.size` is absent, clears any half-created token files, and
re-provisions. No timeouts, no PID heuristics, no stale-lock detection.

This recovery path is exercised by the test suite (`bin/flocker_test.sh`
case 8).

## Corrupt completed pool (fail closed)

A **completed** pool (`.size` present) is validated under the init lock
on every invocation: `.size` must be a positive integer, and every
token `1..N` must exist as a regular file. If validation fails — for
example, a token file was deleted while the pool was live — flocker
**fails closed**: it exits `2` with a diagnostic naming the key, the
pool directory, and the offending token, and it does **not** run
`COMMAND`.

flocker deliberately refuses to repair such a pool. Recreating a
missing token would be unsafe: a running caller may still hold the
unlinked token's inode via its inherited file descriptor, so a fresh
file at the same path would be a *second*, independently lockable
slot — the pool would silently oversubscribe.

Safe recovery, once **no callers for that key are active**:

```bash
rm -rf "$FLOCKER_DIR/<key>"     # next caller re-provisions from scratch
# — or —
export FLOCKER_DIR=/some/fresh/path
```

This behavior is exercised by `bin/flocker_test.sh` case 14. It is
distinct from dead-initializer recovery above: an *incomplete* pool
(no `.size`) is safely re-provisioned because no token can be legally
held before provisioning completes; a *completed* pool's tokens may be
held at any moment, so nothing may be mutated.

## Multi-process correctness example

```bash
# Five callers, two slots.
for i in 1 2 3 4 5; do
    flocker -k example -j 2 -- bash -c "echo started \$\$ ; sleep 2 ; echo done \$\$" &
done
wait
```

Expected output (PIDs vary): exactly two `started` lines appear
immediately, then `done` lines and new `started` lines interleave as
slots free up, ending with five `done` lines total. Peak concurrency
is two.

## Caveats and non-features

- **The wrapped command must not close inherited file descriptors
  numbered ≥ 3.** The slot is held by an fd inherited across `exec`;
  closing it would release the lock while `COMMAND` continues to run,
  letting a second caller in. Standard tools (compilers, valgrind,
  test runners) do not do this. Custom programs that explicitly close
  high-numbered fds — `closefrom(3)`, `for fd in range(3, MAX): os.close(fd)`,
  etc. — break this contract.
- **No fairness guarantees.** Acquisition order is random (each waiter
  picks a random start offset across the token files), so low-numbered
  tokens are not preferred. There is no FIFO queue.
- **No barrier mode.** A `--wait`-style "wait for all jobs to drain"
  mode is intentionally absent in this version. The naive
  implementation (sequentially acquiring blocking flocks on each
  token) does not give a true barrier under active enqueue, only a
  "wait for everyone who was running when I started" semantics, which
  is easy to misuse. If you need barrier behavior, stop producing new
  jobs first, then `wait` on your job PIDs in the producing shell.
- **No cross-host coordination.** Use a real distributed lock manager
  if that is what you need.
- **No timeouts.** Wrap `flocker` with `timeout(1)` if you need a
  bound on total acquire-plus-run time.
- **Polling interval.** When all tokens are held, the acquire loop
  polls every 50ms. This is fine for the target workload (compiles
  that take seconds to minutes). Sub-second jobs in a heavily
  contended pool will see modest scheduling jitter.

## Dependencies

- bash 4.1 or newer (for `{fd}`-style automatic descriptor assignment).
- `flock(1)` from util-linux.
- GNU coreutils `sleep` (for fractional-second arguments).

All three are present on stock Linux distributions; nothing has to be
installed on top.

## Exit codes

- The exit code of `COMMAND` is propagated unchanged on successful
  acquisition.
- `2` — usage error (missing or invalid `--key` / `-j`, bad option,
  no `COMMAND`), or a corrupt completed pool (invalid `.size`, or a
  token missing / not a regular file — see "Corrupt completed pool").

## License

flocker is distributed under GPL-3.0-or-later. The wrapped command
runs in a separate process via `exec`, so its license is not affected
by flocker's.
