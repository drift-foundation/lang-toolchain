# Baton

`baton` is a command-line helper for the repository's agent handoff protocol.
Ordinary actions perform exactly one filesystem transition and exit. The
explicit `wait-next` action polls until it atomically claims one eligible
handoff, then exits. Baton never presents a blocking dashboard, prompts,
steals, repairs, or requeues work.

The repository filesystem remains authoritative. `baton` validates roles and
paths, performs no-clobber atomic publication and claims, records an immutable
claim receipt, publishes a response before popping its incoming claim, and
refuses to continue if a claimed snapshot changes.

## Command shape

```text
./tools/baton/baton ROLE ACTION [ARGUMENTS] [--actor SLUG --seed HEX] [--json]
```

Agent roles use one stable actor slug and a random seed of at least 128 bits
for the lifetime of that agent instance. The singular `human` role is
Slawomir and deliberately accepts neither option.

Common operations:

```text
./tools/baton/baton reviewer scan
./tools/baton/baton reviewer claim-next --actor reviewer-root --seed "$SEED"
./tools/baton/baton reviewer wait-next --actor reviewer-root --seed "$SEED" --interval 60
./tools/baton/baton reviewer claim IMPL-PENDING-2026-08-04T22-23-12Z --actor reviewer-root --seed "$SEED"
./tools/baton/baton reviewer adopt "$PRE_BATON_CLAIM" --actor reviewer-root --seed "$SEED"
./tools/baton/baton reviewer announce finding-name announcement.md --actor reviewer-root --seed "$SEED"

./tools/baton/baton reviewer reply "$CLAIM" response.md --actor reviewer-root --seed "$SEED"
./tools/baton/baton reviewer ack "$CLAIM" acknowledgment.md --actor reviewer-root --seed "$SEED"
./tools/baton/baton reviewer signoff "$CLAIM" response.md --actor reviewer-root --seed "$SEED"
./tools/baton/baton reviewer signoff "$CLAIM" response.md --destination finding-parent/findings/finding-child --actor reviewer-root --seed "$SEED"

./tools/baton/baton implementer handoff "$CLAIM" response.md --actor k --seed "$SEED"
./tools/baton/baton implementer request-approval "$CLAIM" request.md --actor k --seed "$SEED"
```

Response text defaults to standard input, so human decisions remain quick:

```text
echo 'Approved as proposed.' | ./tools/baton/baton human approve "$CLAIM"

./tools/baton/baton human reject "$CLAIM" <<'EOF'
Rejected pending the stated boundary test.
EOF
```

If standard input is a terminal and no response file is supplied, `baton`
fails immediately instead of opening an interactive prompt.

Agent response actions repeat the claiming actor and seed. Baton compares them
with the immutable claim receipt so another same-role agent cannot accidentally
answer or pop work it did not claim. Human responses remain seedless.

Reviewer outcomes are deliberately distinct: `reply` requests another
implementer turn and publishes `REVIEW-PENDING`; `ack` records a status or
informational handoff without requesting work and may leave its finding open;
`signoff` is the terminal finding review. Both `ack` and `signoff` publish their
immutable review before popping the claim, but neither publishes a pending
token.

Response details normally stay beside the incoming target. For a handoff that
delegates work to a nested child finding, `--destination` may select another
directory inside the same top-level `finding-*` tree. Cross-finding reroutes
are rejected. The outgoing token, if any, points to the selected immutable
detail.

`announce` publishes a new immutable detail and role-appropriate pending token
without consuming an incoming claim. Its destination is a path relative to
`work/` inside an existing `finding-*` tree. Use it for initial work orders,
corrections, and independent status announcements; never use it to bypass the
response-before-pop operation for a claim already being processed.

`adopt` is the explicit migration boundary for a protocol-valid claim made
manually before Baton was introduced. It verifies that the claim names the
invoking role/actor/seed, that the original pending token is absent, and that
the claim and target satisfy the protocol, then snapshots Baton's immutable
receipt. It also rejects a surviving original pending token or any second claim
for the same original handoff. It never runs implicitly from a response action. Once adopted, the
claim uses the ordinary reply/handoff/signoff rules. Do not use `adopt` to
recover, steal, or requeue a malformed or abandoned claim.

Exit status `0` means success, `3` means no eligible pending work, `4` means
a protocol or validation failure, and `5` means an atomic race was lost.

`wait-next` uses Linux `inotify` to react to `work/` changes immediately. It
also rescans after 60 seconds without an event by default; override that
safety interval with `--interval SECONDS`. It claims at most one token and
returns its target rather than remaining resident as a dashboard.

## Portability boundary

This first implementation is Linux-specific because mutations fail closed
unless `renameat2(RENAME_NOREPLACE)` is available and `wait-next` uses
`inotify`. It uses only the Python 3 standard library and keeps
repository-specific policy in this directory so the tool can later move to a
standalone repository.
