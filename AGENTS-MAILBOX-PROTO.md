# Agent Mailbox Protocol

Protocol version: **3**
Status: **active local trial in `drift-lang`**

## Purpose and scope

This protocol is the repository-wide notification and exclusive-claim contract for review findings under `work/finding-*`. Finding details remain in their top-level or nested finding folders. Every ready handoff and every active claim appears in the repository's flat `work/` mailbox.

The mailbox provides four properties under concurrent agents:

- atomic publication, so no reader sees a partial handoff;
- immutable evidence, so published meaning cannot change underneath a reader;
- an atomic single-winner claim, so only one eligible agent works a token;
- visible ownership, so observers can answer whether work is unclaimed or active and who claimed it.

## Authorities and ownership

- `review-YYYY-MM-DDTHH-MM-SSZ.md` is permanently immutable reviewer input in the relevant finding or child-finding root.
- `PROGRESS.md` is implementer-owned. It may evolve between handoffs, but its contents are frozen while any pending or claimed `IMPL`/`APPROVAL` token points to it.
- A mailbox token is a notification pointer. Its target remains the content authority.
- The reviewer never edits `PROGRESS.md`; the implementer never edits an existing `review-*.md`.
- A consumed token is removed; its review, response, progress snapshot, and evidence are not deleted merely because the notification was consumed.

## Actor identity and agent seed

Every agent that can claim work has:

- an `actor-slug`: a stable, human-readable identity answering **who** claimed it, such as `k`, `reviewer-root`, or `slawomir`; use lowercase ASCII letters, digits, and hyphens;
- an `agent-seed`: a collision-resistant identity for that live agent instance, generated once per session and reused for all its claims. Use at least 128 random bits rendered as lowercase hexadecimal. Do not use a timestamp, PID, hostname, or role name alone.

The actor slug is attribution; the seed distinguishes simultaneous instances of the same actor. This is a coordination identity, not an authentication or security credential.

## Mailbox token names

Unclaimed handoffs are direct children of `work/`:

- `work/REVIEW-PENDING-<UTC-timestamp>`
- `work/IMPL-PENDING-<UTC-timestamp>`
- `work/APPROVAL-PENDING-<UTC-timestamp>`

Use UTC timestamps in `YYYY-MM-DDTHH-MM-SSZ` form. Every pending-token name is unique and never reused.

An active claim is the same token atomically renamed in place:

```text
work/CLAIMED--<original-token-basename>--BY-<actor-slug>--SEED-<agent-seed>--AT-<UTC-timestamp>
```

Example:

```text
work/CLAIMED--REVIEW-PENDING-2026-08-04T21-48-05Z--BY-k--SEED-a3f91c2e8d4b47f1a902bc77d63e1054--AT-2026-08-04T22-01-00Z
```

The original token basename identifies the work. `BY-*` answers who claimed it. `SEED-*` identifies the exact concurrent agent instance. `AT-*` records claim time.

Everyone monitors the top level of `work/` for both `*-PENDING-*` and `CLAIMED--*`. Do not rely on recursive scans or out-of-band chat to discover ownership.

## Token payload contract

Pending and claimed tokens have identical immutable contents: exactly one newline-terminated relative path, resolved from `work/`, to the authoritative detail file.

The target must:

- already exist before the pending token is published;
- be a regular non-symlink file inside a top-level `work/finding-*/` tree, including a valid nested child finding;
- remain inside `work/` after path and symlink resolution.

Absolute paths, `..` components, extra lines, directories, symlink escapes, missing targets, and malformed payloads are protocol violations. Raise an alarm rather than silently ignoring or guessing.

## Published-handoff immutability

Publication creates an immutable handoff snapshot:

- The sender never edits, replaces, retargets, truncates, renames, removes, or recreates a published token.
- The sender never edits its frozen target or a document to which that target delegates material handoff content.
- The only ordinary rename permitted after publication is the target recipient's atomic `PENDING -> CLAIMED` transition.
- After claiming, no party edits, replaces, retargets, or renames the claimed token. Only its successful claimant may pop it, after completing the response protocol.
- Never fix a typo, omitted constraint, changed authorization, stale path, or other mistake in place.

Corrections and addenda always use a new immutable detail and a new uniquely timestamped pending token. The new detail states what it supersedes or augments. It does not make earlier tokens or evidence disappear.

If ongoing research needs mutable notes, a published review must contain the complete actionable snapshot itself or delegate to a dedicated immutable evidence snapshot. Do not publish a pointer whose meaning can change underneath concurrent readers.

## Atomic publication

Publish the authoritative detail first and the pending token second. Publish each atomically:

1. Write the complete file under a unique hidden temporary name in the same directory as its final name. A token temporary name must not match pending/claimed scanner patterns.
2. Flush/close and validate the temporary file, its complete contents, delegated paths, and intended final destination.
3. Confirm the final name is absent. Atomically rename the temporary file to the final name without clobbering an existing path, then verify the temporary path disappeared and final contents match exactly. If publication loses a race, leave the existing path untouched, choose a new timestamp, and republish.
4. Publish the token only after the authoritative target has reached its final immutable name.

Never create or rewrite a final token with direct redirection, an in-place editor, `touch`, or a non-atomic copy. The same atomic-snapshot rule applies to review targets and to `PROGRESS.md` immediately before its pending token is published.

After a token appears, further material information belongs in a new timestamped detail and token even if the receiver has probably not scanned yet. “Probably unread” is not synchronization.

## Atomic exclusive claim

An eligible recipient must claim a pending token before reading its authoritative target or beginning work. Merely observing or opening the mailbox filename grants no ownership.

1. Choose the claim destination using the exact original basename, canonical actor slug, this agent instance's seed, and current UTC claim timestamp.
2. Keep source and destination directly under the same `work/` directory so the operation is a same-filesystem atomic rename, never a copy/delete fallback.
3. Confirm the destination is absent. Atomically rename the original pending token to the claim destination.
4. Verify the rename succeeded: the original source is absent, this agent's exact claim path exists as a regular non-symlink file, and its payload/target satisfy the token contract.
5. Only after that verification may the winner read the authoritative target or perform work.

The original source can be renamed only once. Concurrent agents may observe it, but only one rename succeeds; every loser must stop immediately and rescan the mailbox. Never infer success solely from a permissive `mv --no-clobber` exit status—verify the exact source and claimant-specific destination state.

Role eligibility is exact:

- implementer claims `REVIEW-PENDING-*`;
- reviewer claims `IMPL-PENDING-*`;
- Slawomir, or an agent explicitly carrying out his recorded decision, claims `APPROVAL-PENDING-*`.

A wrong-role claim is a protocol violation, not a way to reserve work.

## Strict PUSH -> CLAIM -> PUSH -> POP lifecycle

Every handoff follows this state machine:

1. **PUSH detail:** sender atomically publishes the immutable detail.
2. **PUSH pending:** sender atomically publishes the immutable pending token.
3. **CLAIM:** one eligible recipient wins the atomic rename and becomes the visible exclusive claimant.
4. **WORK:** only that claimant processes the frozen snapshot. Other agents do not duplicate the pass.
5. **PUSH response:** claimant atomically publishes its immutable response detail and outgoing pending token before consuming the incoming claim. A terminal signoff publishes an immutable signoff review detail but deliberately no outgoing pending token.
6. **POP claim:** only the successful claimant removes its exact claimed token, and only after the response/signoff or other requested outcome is safely published. Popping never deletes the target/evidence.

There is no sender-side withdraw, replace, cleanup, acknowledgement, or token removal. If a sender notices a malformed/stale handoff, it pushes a correction and alerts the recipient; it never edits or removes the original.

Mailbox state is intentionally observable:

- `*-PENDING-*` with no claim: ready but unclaimed;
- `CLAIMED--*--BY-<actor>--SEED-<seed>--AT-*`: actively owned by that actor instance;
- outgoing response present while incoming claim remains: response publication is in progress;
- incoming claim absent after outgoing publication: handoff completed and popped.

## Handoff directions

### Reviewer to implementer

After publishing a changes-requested `review-<timestamp>.md`, publish `work/REVIEW-PENDING-<same-timestamp>` pointing to it. The implementer claims it, records/implements the response, pushes an implementation handoff, then pops its claim.

To start the next serial finding, publish an initial immutable review in that finding describing readiness and requested action, then publish its review token.

### Implementer to reviewer

When implementation/research and the relevant immutable response snapshot are ready, publish a uniquely timestamped `work/IMPL-PENDING-*`. The reviewer claims it, reviews it, pushes the next review handoff or terminal signoff detail, then pops the claim.

### Human approval gate

When only Slawomir can resolve a decision—existing-test edits, language/spec rulings, or another explicit gate—the implementer publishes the complete immutable proposal and an `APPROVAL-PENDING-*` token.

No gated work proceeds while the approval is pending or claimed but undecided. Slawomir supplies the decision in his own words. The human or agent explicitly carrying out that decision records/publishes it before popping the approval claim. The requester cannot withdraw or self-approve its own request.

## Consuming handoffs safely

At the start of a pass:

1. Enumerate eligible pending tokens and existing claims.
2. Atomically claim exactly one intended token; do not batch-rename or glob.
3. Validate the claimed token payload and immutable target.
4. Process only the claimed snapshot. New pending tokens belong to later claims.
5. Atomically push the response detail and outgoing token.
6. Revalidate the exact claimed filename/payload, then pop only that claim.

Multiple token types and findings may coexist. Handle each by exact filename and target. Coexistence is not an error; duplicate processing of an already claimed token is.

## Stale and malformed claims

Claims have no automatic timeout. Long builds and reviews are normal; elapsed time alone never authorizes stealing, deleting, or requeueing a claim.

If a claimant crashes or a claimed token is malformed, leave it visible and raise an alarm. Recovery requires Slawomir's explicit ruling naming the exact claim and action. A recovery may atomically requeue the unchanged token under its original pending basename or retire it after immutable evidence is published; never perform silent recovery.

## Terminal reviews and lifecycle

- A review requesting changes pushes a new review detail/token before popping the implementation claim.
- A signing-off review atomically publishes its immutable signoff `review-*.md`, pushes no pending review token, then pops the implementation claim.
- On terminal signoff, the reviewer ends the user-facing handoff with exactly `finished`.
- Intermediate notes that are not ready for the other role create no pending token.
- Never delete, archive, or relocate a finding while any pending or claimed token points into it.

## Trial, versioning, and distribution

Version 3 adds the atomic single-winner claim transition and visible claimant identity (`actor-slug` plus per-agent seed). It replaces v2's direct pending-token consumption with `PENDING -> CLAIMED -> POP`.

By Slawomir's explicit instruction, v3 is currently a local trial in `drift-lang`. Peer repositories remain on protocol v2 until he approves distribution after practical use. This is an authorized temporary exception to the normal rule that semantic protocol changes are distributed byte-for-byte to every participating repository. Do not silently apply v3 behavior in a peer repository whose local protocol still says v2.
