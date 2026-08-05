# Finding: manual mailbox operations are error-prone

Date: 2026-08-04

Observed failures during the protocol-v3 trial included duplicate detached
polling loops, a missed pending handoff, and one target read before atomic
claim. The protocol itself was explicit; repeatedly recreating its filesystem
operations by hand was the weak boundary.

The proposed support tool is the repository-local `tools/baton/baton`
executable. It is a
non-blocking, standard-library Python CLI with the grammar:

```text
./tools/baton/baton <role> <action> [action arguments]
```

It centralizes role eligibility, token/target validation, atomic no-clobber
claims and publication, immutable claim receipts, response-before-pop ordering,
and terminal signoff. Its explicit `wait-next` action uses inotify plus a
60-second defensive rescan and exits after one successful claim. It never
presents a dashboard, prompts, repairs, requeues, or steals.

This is initially a protocol-v3-compatible trial helper, not yet a mandatory
replacement or a peer-project distribution. The singular human role requires
no seed in the tool; formalizing that exception belongs to the next protocol
revision after the trial.
