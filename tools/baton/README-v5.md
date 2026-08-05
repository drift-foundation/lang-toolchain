# Baton

`baton` is a small command-line helper for atomic, role-addressed repository handoffs. Each invocation performs one bounded operation and exits; only `wait` blocks, using Linux `inotify` plus a defensive 60-second rescan.

Read [`AGENTS-MAILBOX-PROTO.md`](../../AGENTS-MAILBOX-PROTO.md) before using it. The protocol is the authority; this file is the quick command reference.

## Roles and identity

Roles are declared in `tools/baton/roles.json`. Any configured role uses the same action surface:

```text
./tools/baton/baton ROLE ACTION [ARGUMENTS] [OPTIONS]
```

Agent roles supply one stable actor slug and one random seed of at least 128 bits for the life of that agent instance:

```text
--actor reviewer-root --seed "$SEED"
```

Singleton roles such as `human` supply neither option.

## Directed handoff

Publish a durable message (the default). The destination is relative to `work/`; `.` places the retained detail directly in `work/`.

```text
./tools/baton/baton reviewer send implementer finding-example --kind review --actor reviewer-root --seed "$SEED" <<'EOF'
Please implement the reviewed boundary fix.
EOF
```

Publish a transient message when the content should disappear after consumption. Its body is embedded in the immutable JSON envelope, so there is no destination argument. Transient bodies must be non-empty UTF-8 text no larger than 64 KiB.

```text
echo 'Focused checks are green; ready for review.' | ./tools/baton/baton implementer send reviewer --retention transient --kind status --thread return_authority --actor k --seed "$K_SEED"
```

Consume work:

```text
./tools/baton/baton implementer scan --actor k --seed "$K_SEED"
./tools/baton/baton implementer claim --actor k --seed "$K_SEED"
./tools/baton/baton implementer wait --actor k --seed "$K_SEED"
./tools/baton/baton implementer claim "$PENDING" --actor k --seed "$K_SEED"
```

`wait` already defaults to a 60-second safety rescan. Do not spell `--interval 60`; use `--interval` only for a deliberate non-default.

Choose exactly one of the consumer commands above. `wait` is not a passive watcher: it atomically claims directed work before returning. Do not run a manual `claim` while `wait` is active, and do not run multiple waits with the same actor/seed. A returned `status: claimed` is already the actor instance's active claim; answer or close it before starting another consumer.

Reply to the incoming sender and atomically pop the claim after publication:

```text
./tools/baton/baton implementer reply "$CLAIM" --kind implementation --actor k --seed "$K_SEED" <<'EOF'
Implementation and focused verification are ready.
EOF
```

A response inherits the incoming message's retention. Use `--retention durable` or `--retention transient` only to deliberately switch policies. A durable response creates a retained detail; a transient response embeds its body in the outgoing envelope. When switching a transient message to durable retention, supply `--destination` because the incoming message has no detail directory to inherit.

Forward to another role with `--to ROLE`. Record an outcome with `--outcome`, including human decisions:

```text
echo 'Approved as proposed.' | ./tools/baton/baton human reply "$CLAIM" --outcome approved --kind approval_decision
```

Terminal handling publishes no outgoing message. With durable retention it preserves a detail; with transient retention it removes the claim and embedded content without creating an artifact:

```text
echo 'Static review is clear.' | ./tools/baton/baton reviewer close "$CLAIM" --kind signoff --actor reviewer-root --seed "$SEED"
```

Threads are streams, not one-request/one-response locks. Either side may `send` additional status, result, correction, or final messages at any time, reusing `--thread`; every new message gets its own immutable envelope and is consumed independently.

## Broadcast notice

`all` is a publish selector, not a claimable role. Tooling announcements are normally transient:

```text
./tools/baton/baton reviewer send all --retention transient --kind tooling_notice --ttl 86400 --actor reviewer-root --seed "$SEED" <<'EOF'
Baton has been updated. Read tools/baton/README.md before the next handoff.
EOF
```

The resulting `NOTICE-FROM-reviewer-TO-ALL-*` is immutable and cannot be claimed. `wait` returns an unseen notice after recording a recipient-local seen receipt; `see` handles an exact notice explicitly:

```text
./tools/baton/baton implementer see "$NOTICE" --actor k --seed "$K_SEED"
```

After its envelope's expiration, only the exact author instance may clean it:

```text
./tools/baton/baton reviewer expire "$NOTICE" --actor reviewer-root --seed "$SEED"
```

A durable notice target is retained. A transient notice body disappears when the author expires the envelope.

## Diagnostics

```text
./tools/baton/baton reviewer doctor --actor reviewer-root --seed "$SEED" --json
```

Exit status `0` means success, `3` means no eligible work or an expired notice, `4` means a protocol/validation failure, and `5` means an atomic race was lost.

## Portability

Baton is currently Linux-specific. It fails closed unless `renameat2(RENAME_NOREPLACE)` and `inotify` are available, and otherwise uses only Python 3's standard library.
