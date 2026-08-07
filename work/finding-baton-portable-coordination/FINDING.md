# Portable Baton coordination across arbitrary workspaces

## Status

Design finding only. No implementation is authorized by this file. The next step is an independent implementer review that tries to falsify the proposed model, inventories current v5 assumptions, and records corrections or alternatives before the design is locked.

This document is reviewer research, not an authoritative implementation specification. In particular, the names, schema, CLI spellings, migration shape, and security checks below are hypotheses. The implementer should verify the current code and may reject any proposal that is incomplete, unsafe, or unnecessarily coupled.

## Goal

Make Baton a reusable local agent-coordination tool that can be distributed with Drift projects and used across more than one source tree—or with material that is not a repository at all.

The intended use cases include:

- a reviewer working in one source tree asking the Drift compiler reviewer a question;
- Drift compiler, web, TLS, and MariaDB agents sharing one coordination mailbox while retaining distinct addresses;
- review of a directory of design documents before it has Git history;
- evidence or reports retained in a scratch workspace outside any project tree;
- independent projects using their own isolated Baton configuration and mailbox;
- continued use of the present `work/finding-*` convention without making that convention part of Baton's transport model.

The tool remains a same-host/shared-filesystem coordination mechanism. Network transport, remote synchronization, authentication against hostile users, and distributed consensus are outside this finding.

## Problem in protocol v5

The current implementation and protocol conflate four separate concepts:

1. the source repository containing `tools/baton`;
2. the hard-coded `work/` durable-content authority;
3. the hard-coded `work/mailbox/` transport directory;
4. generic roles such as `reviewer` and `implementer`, which are not unique when several projects share a mailbox.

Concrete current-tree assumptions to revalidate:

- `Mailbox.__init__` derives both `work/` and `work/mailbox/` from the directory containing Baton;
- durable `target` strings are interpreted relative to `work/`;
- response routing contains a special case for top-level `finding-*` directories;
- roles come from the adjacent `tools/baton/roles.json`;
- receipts are keyed by the inferred repository root;
- `BATON_REPO_ROOT` and the older `MAILBOX_REPO_ROOT` change the inferred repository rather than selecting an independent transport/configuration;
- filenames route only by generic role.

Those assumptions work for one repository-local workflow, but they make a shared external mailbox ambiguous and make Baton impose Drift's finding layout on unrelated users.

## Candidate conceptual model

Baton should separate these concepts:

### 1. Configuration

Every operational invocation receives one explicit trusted configuration path. The configuration selects a mailbox, declares addressable participants, and maps portable root identifiers to canonical absolute directories.

Candidate invocation:

```text
tools/baton/baton --config /home/sl/.config/baton/drift.json drift.reviewer wait
```

Candidate rules:

- `--config` is mandatory for every action that reads or mutates protocol state;
- the supplied path must resolve to an absolute regular non-symlink file;
- no current-directory search, script-relative active config, environment fallback, or implicit `work/mailbox` selection;
- `--help`, `version`, and a possible `init` command are the only commands that may run without an active config;
- aliases/wrappers may shorten human use, but the Baton process still receives the explicit configuration;
- distribution includes an example/schema, never a machine-specific active configuration.

Whether `init` should create the mailbox/config, and whether an operational command may create a missing mailbox, remain open questions. Fail-closed operation against an explicitly initialized real directory is the safer initial hypothesis.

### 2. Mailbox

The mailbox is only the transport endpoint. Its configured path is canonical and absolute and may live anywhere the actors can share, including outside every configured root.

Examples:

```text
/home/sl/.local/state/baton/drift-mailbox
/tmp/drift-baton-session/mailbox
/srv/team-agent-mailbox
```

`/tmp` is acceptable for deliberately ephemeral coordination but is not a safe implicit default because it may be cleared on reboot or by cleanup policy. The configuration should make the durability choice visible.

Atomic publication must create the temporary envelope inside the mailbox filesystem and use the existing no-clobber atomic rename into the final name. It must not stage the envelope in `/tmp` and assume that rename across filesystems is atomic.

### 3. Participant address

A participant is a mailbox-unique logical endpoint, not a generic role. The short address agreed during initial discussion is `drift.reviewer`; related examples are:

```text
drift.reviewer
drift.implementer
web.reviewer
mariadb.reviewer
human.slawomir
```

The namespace is organizational rather than repository-based. A participant may review source, documentation, generated output, or any other configured material.

The participant address answers where a message is routed. A concrete actor/instance identity answers who claimed or published it. The current stable actor plus random live-instance seed distinction may remain useful and must not be accidentally collapsed into the participant address.

Candidate envelope fields:

```json
{
  "from": "web.reviewer",
  "to": "drift.reviewer",
  "author_actor": "k",
  "author_seed": "<live-instance-seed>"
}
```

Candidate filename routing remains human-inspectable:

```text
PENDING-FROM-web.reviewer-TO-drift.reviewer-<timestamp>-<message-id>
```

The exact identifier grammar, singleton behavior, and filename-length limits need review. `ALL` may remain the mailbox-wide non-claimable broadcast audience. Project-, namespace-, or group-scoped broadcasts are not required unless the generalized model makes them cheap and unambiguous.

### 4. Named filesystem root

A configured root is any trusted directory. It is not necessarily a Git repository, source tree, or `work/` folder.

Candidate configuration fragment:

```json
{
  "roots": {
    "drift.source": "/home/sl/src/drift-lang",
    "web.source": "/home/sl/src/drift-web",
    "language.design": "/home/sl/design/drift",
    "release.evidence": "/home/sl/evidence/drift-releases"
  }
}
```

Each configured value must be canonical and absolute. The envelope carries only the portable identifier plus a normalized relative path:

```json
{
  "reference": {
    "root_id": "language.design",
    "path": "proposals/fiber-stack-diagnostics/review.md"
  }
}
```

Absolute content paths must not appear in envelopes. The recipient resolves `root_id` only through the explicitly supplied trusted configuration. Unknown identifiers, absolute relative-path fields, traversal, missing files, non-regular files, and resolution outside the configured root fail closed.

A transient message needs no filesystem reference. The current single retained detail remains enough for the initial design; multiple attachments are not a requirement unless implementation research proves that forcing a synthetic index/detail file would be materially worse.

### 5. Workflow policy

Finding names, reviewer journals, `PROGRESS.md` ownership, response placement, and whether a response must remain in the incoming finding are Drift workflow policies. They should be documented in `AGENTS.md` and the mailbox protocol used by Drift, not hard-coded into a general Baton filesystem authority.

Baton may default a durable response to the incoming reference's root and parent directory. It should not recognize names such as `work`, `mailbox`, or `finding-*` except when a particular configuration or higher-level policy supplies that constraint.

## Candidate configuration shape

This is illustrative and deliberately not final:

```json
{
  "config_version": 1,
  "protocol_version": 6,
  "mailbox": {
    "id": "drift-local",
    "path": "/home/sl/.local/state/baton/drift-mailbox"
  },
  "participants": {
    "drift.reviewer": {"identity": "agent", "detail_prefix": "review"},
    "drift.implementer": {"identity": "agent", "detail_prefix": "implementation-response"},
    "human.slawomir": {"identity": "singleton", "singleton_actor": "slawomir", "detail_prefix": "approval-decision"}
  },
  "roots": {
    "drift.source": "/home/sl/src/drift-lang",
    "web.source": "/home/sl/src/drift-web",
    "language.design": "/home/sl/design/drift"
  }
}
```

Questions the implementer should challenge:

- Is a stable `mailbox.id` useful for receipt isolation and diagnostics, or is the canonical mailbox path sufficient?
- Does the config need a generation/version identifier so changing root mappings while messages are live fails safely?
- Should all participants use literally the same config file, or may separate local configs share a mailbox and root IDs? If the latter, how is dangerous mapping drift detected?
- Are participant declarations necessary for routing validation, or should any syntactically valid address be accepted?
- Should `detail_prefix` remain participant configuration, move to `kind`, or disappear?
- Does an explicit absolute `--config` rule create unacceptable UX, and if so can a wrapper solve it without adding ambiguous discovery?

## Immutability and integrity questions

Cross-root operation makes existing implicit trust more visible. The v6 design should decide explicitly:

1. Whether the envelope records the retained detail's SHA-256 and size at publication. Without this, a file changed between send and claim can become the accepted snapshot even though the protocol calls the published detail immutable.
2. Whether Baton always creates durable details itself, or can point at an already-existing file. Existing-file references require a clear immutability/hash contract.
3. Where claim, notice-seen, and author receipts live when there is no repository root. A likely location is a user-runtime/state directory keyed by mailbox ID/path and participant instance—not the referenced content root.
4. How receipt paths avoid collisions when the same Baton process uses several mailbox configurations.
5. Whether mailbox and root directory ownership/mode checks are required, given that seeds identify cooperative instances but are not authentication credentials.
6. Whether a symlink is forbidden only at the root/config/mailbox boundary or throughout a referenced path. At minimum, the resolved target must remain beneath the canonical configured root.
7. How `doctor` reports incompatible config, unknown roots/participants, old v5 messages, missing references, and stranded receipts without mutating anything.

The same-host cooperative-user trust boundary should be stated plainly. Baton must not imply cryptographic sender authentication merely because an envelope contains an actor and seed.

## Cross-filesystem ordering

The retained detail and external mailbox may be on different filesystems, so they cannot be committed in one atomic rename transaction. The current safe ordering remains the starting hypothesis:

1. publish and fsync the durable detail inside its configured root;
2. compute and retain its immutable metadata/hash;
3. publish and fsync the envelope inside the mailbox;
4. allow claiming only after the complete envelope exists.

A crash after step 1 but before step 3 may leave an unreferenced detail, which is preferable to a visible baton pointing to missing content. The design should document how such orphans are detected or deliberately left for human cleanup.

## Distribution goal

Once the generalized design has passed review and its tests, the Baton distribution set should contain the complete reusable tool rather than only the protocol prose:

- executable launcher and implementation modules under `tools/baton/`;
- tool README and protocol document;
- configuration schema/example;
- role/participant migration guidance;
- Baton unit tests or another self-checking verification surface;
- a manifest or documented copy list so peer projects receive one coherent version;
- the appropriate ignore guidance for whichever mailbox path that project/config selects.

An active machine-specific configuration must not be copied into peer projects. Each use creates or selects its own explicit configuration. A future standalone Baton project/package is compatible with this model but is not required for the first generalized release.

## Migration hypothesis

This is a breaking protocol change. Under the one-current-contract policy, the implementation should not retain v5 publication, dual readers, legacy role addressing, `BATON_REPO_ROOT`, `MAILBOX_REPO_ROOT`, or implicit `work/mailbox` discovery unless Slawomir approves a narrow exception.

Before conversion:

1. drain or explicitly recover every v5 pending message, claim, and notice;
2. inventory and preserve any durable detail still needed;
3. stop every v5 waiter;
4. replace the protocol/tool/config as one change;
5. distribute the complete new set only after local trial;
6. announce the exact new invocation to K and other users out of band for the first transition.

The implementer must verify whether an active local mailbox is actually empty at migration time. This finding does not authorize deleting or rewriting protocol state.

## Non-goals

- Git integration or a requirement that a configured root contain a repository.
- Networked mailboxes, cross-host locking, or message replication.
- Authentication against malicious local users with mailbox write access.
- A daemon, dashboard, or always-running server.
- Workflow-specific verbs for review, implementation, approval, security, or planning.
- Compatibility with v5 after the cutover.
- Making `/tmp` the invisible transport default.
- Encoding absolute content paths in mailbox envelopes.

## Acceptance criteria for the eventual implementation

- Every operational command requires an explicit config and reports the selected mailbox and participant in diagnostics.
- The configured mailbox may be outside all roots and on a filesystem different from retained details.
- Two distinct reviewers such as `web.reviewer` and `drift.reviewer` can address and claim messages independently in one mailbox.
- A retained detail can live beneath any configured directory, including a non-repository document tree.
- Transient messages work without any configured content reference.
- Envelope references contain `root_id` plus a relative path, never an absolute content path.
- Unknown roots/participants, traversal, symlink escape, config mismatch, and malformed routing fail closed.
- Atomic publication and single-winner claim behavior remain intact under concurrency.
- Receipts are isolated by mailbox and participant instance without relying on a repository root.
- Broadcasts remain non-claimable and directed messages remain single-claim work.
- Baton core contains no `work`, `finding-*`, Git, or repository-layout special case.
- Local Drift finding workflow remains possible through configured roots and AGENTS policy.
- The distribution set installs/copies a coherent Baton implementation, documentation, config example, and verification surface.
- The cutover leaves exactly one supported protocol and no active v5 waiter or transport token.

## Requested independent review

Before implementation, the implementer should:

1. inspect the whole current Baton implementation, tests, protocol, roles file, and receipt behavior;
2. identify every repository/work/finding/role assumption omitted from this finding;
3. test the conceptual model against at least the three cases `drift.reviewer -> drift.implementer`, `web.reviewer -> drift.reviewer`, and a reviewer targeting a non-repository document root;
4. look for unsafe or ambiguous configuration and migration behavior;
5. decide whether the proposed address/root/config separation is sufficient or recommend a smaller/better abstraction;
6. record disagreements, unanswered questions, and a proposed test matrix in implementer-owned `PROGRESS.md`;
7. do not begin implementation until the design response has been reviewed and Slawomir has approved any required edits to existing tests.

