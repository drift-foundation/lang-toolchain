# Agent Mailbox Protocol

Protocol version: **1**  
Status: **active**

## Purpose and scope

This protocol is the repository-wide notification contract for review findings under `work/finding-*`. Finding details remain in their own top-level or nested finding folders, while every ready-for-handoff notice appears in one flat mailbox: the repository's `work/` directory.

The mailbox avoids recursive scans, makes transitions between findings visible, and permits independent handoffs to coexist without overwriting one another.

## Authorities and ownership

- `review-YYYY-MM-DDTHH-MM-SSZ.md` is immutable reviewer input and lives in the relevant finding or child-finding root.
- `PROGRESS.md` is implementer-owned and lives in the relevant finding or child-finding root.
- A pending token is only a notification pointer. Its target review or `PROGRESS.md` remains the content authority.
- The reviewer never edits `PROGRESS.md`; the implementer never edits an existing `review-*.md`.

## Mailbox location and token names

Pending tokens are direct children of `work/`, never children of a finding folder:

- `work/REVIEW-PENDING-<UTC-timestamp>`
- `work/IMPL-PENDING-<UTC-timestamp>`
- `work/APPROVAL-PENDING-<UTC-timestamp>`

Use UTC timestamps in `YYYY-MM-DDTHH-MM-SSZ` form. Every token name must be unique; never reuse a timestamp. Singleton names such as `REVIEW-PENDING` are forbidden because concurrent handoffs could be mistaken for one another.

Everyone monitors only the top level of `work/` for pending tokens. Do not rely on recursive scans or out-of-band chat to discover a handoff.

## Token payload contract

A token is not empty. Its complete contents are exactly one newline-terminated relative path, resolved from `work/`, to the authoritative detail file.

The target must:

- already exist before the token is published;
- be a regular file inside a top-level `work/finding-*/` tree, including a valid nested child finding;
- remain inside `work/` after path and symlink resolution.

Absolute paths, `..` components, extra lines, directories, symlink escapes, missing targets, and malformed payloads are protocol violations. Raise an alarm rather than silently ignoring or guessing at an invalid notice.

Example:

```text
work/REVIEW-PENDING-2026-08-04T14-00-00Z
```

contains exactly:

```text
finding-parent/findings/finding-child/review-2026-08-04T14-00-00Z.md
```

## Publishing handoffs

Always publish the authoritative detail file first and its token second.

### Reviewer to implementer

After publishing a changes-requested `review-<timestamp>.md`, create `work/REVIEW-PENDING-<same-timestamp>` pointing to that review. The implementer removes that exact token only after addressing the review in the relevant `PROGRESS.md`.

To hand off the next serial finding, publish an initial `review-<timestamp>.md` in that finding describing its readiness and requested next action, then create the matching work-level review token. This is how finding transitions enter the mailbox.

### Implementer to reviewer

When implementation and the relevant `PROGRESS.md` are ready for review, create a uniquely timestamped `work/IMPL-PENDING-<timestamp>` pointing to that `PROGRESS.md`. The reviewer removes that exact token only after publishing the corresponding review.

### Human approval gate

When only Slawomir can resolve a decision—such as permission to edit an existing test, a language-contract or specification ruling, or another explicit approval gate—the implementer records the complete proposal and evidence in the relevant `PROGRESS.md`, then creates a uniquely timestamped `work/APPROVAL-PENDING-<timestamp>` pointing to that file.

No gated implementation proceeds while the approval token stands. Slawomir supplies the decision in his own words. The implementer then removes that exact approval token and resumes the normal implementation-review handshake. A blocked pass consumes its incoming review token after recording the block in `PROGRESS.md`.

## Consuming handoffs safely

At the start of a pass:

1. Snapshot the exact incoming token names being consumed.
2. Read and validate each token payload and target.
3. Read the authoritative detail file.
4. Perform the corresponding review, implementation, or approval response.
5. Publish any outgoing detail file first and outgoing token second.
6. Remove only the exact incoming token set captured at step 1.

Never glob or bulk-delete pending tokens. A token created while a pass is in flight belongs to a later pass and must remain.

Multiple token types and findings may coexist. Handle each notice by its exact filename and target; coexistence is not itself an error.

## Terminal reviews and lifecycle

- A review requesting more changes creates a new work-level review token.
- A signing-off review consumes the reviewed implementation token and creates no review token. Creating one would cause a deadlock or notification spin.
- On terminal signoff, the reviewer ends the user-facing handoff with exactly `finished`.
- Intermediate work that is not ready for the other role creates no token.
- Never delete, archive, or relocate a finding while any work-level pending token points into it.

## Versioning and distribution

This file is the complete mailbox protocol. Semantic changes require incrementing the protocol version and distributing the updated file byte-for-byte to every participating repository. Each repository's `AGENTS.md` should reference this file instead of duplicating its rules.
