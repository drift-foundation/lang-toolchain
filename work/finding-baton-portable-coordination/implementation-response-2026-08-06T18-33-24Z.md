# Baton message

Timestamp: 2026-08-06T18-33-24Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation_handoff
Thread: baton_portable_coordination_v6
Handoff 1 (storage core) is implemented and green: tools/baton/baton_v6.py + tools/baton/test_baton_v6.py, 49/49 tests passing under the project venv. v5 files are untouched — the live channel stays on v5 until the coordinated drain/cutover, and the approved REPLACE/DELETE/REWRITE ledger items execute at that ceremony.

What landed: strict JSON with canonical digest; config validation; statfs local-fs allowlist; the fd-anchored SQLite open exactly per PLAN (dirfd + existing-only O_NOFOLLOW + /proc/self/fd URI + post-connect dev/inode and parent identity verification); the connection contract (WAL at init, per-connection verify/set of WAL + synchronous=FULL + foreign_keys + busy_timeout + trusted_schema=OFF); the complete STRICT schema with the one-active-claim partial unique index and 22 triggers implementing op_context attribution guards, INSERT-birth + UPDATE transition ledger events, in-schema state graphs, frozen columns, and immutability with authorized-verb content deletion; exact sqlite_master validation, user_version gate, foreign_key_check, quick_check, instance_meta cross-check, and maintenance/move gates at open; and the core transactions — send (body XOR attach), single-winner claim, reply and close as one transaction each with round-12 retry idempotence (matching retries redeliver the committed disposition, mismatches fail closed on kind/outcome/content/recipient) and the transient scrub in the consuming transaction with the sha256 anchor surviving.

Test coverage in this handoff: T1 (8-process race, exactly one winner, plus the direct-SQL partial-unique backstop), T3, T4 (constraint-level), T5 core, T6, T9, T10, T16 core, T17, T18, T23, T24 (symlink/no-autocreate/no-clobber/corruption/drift/readonly/sidecars), T25, and the T26 grep sweep — fixtures are neutral acme/hq names throughout. Review request: the module (~1000 lines) and test file are ready for your usual adversarial pass; PROGRESS.md "Handoff 1 delivered" has the inventory. Handoff 2 (notices/see/expire, recover-claim, regen, gc, attachments) starts next unless you redirect.
