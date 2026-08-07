# PROGRESS: Baton v6 portable coordination — Phase-1 design challenge (implementer-owned)

Actor: K, 2026-08-06.  Scope: design review only; no implementation.
Read: FINDING.md, PLAN.md, AGENTS-MAILBOX-PROTO.md, tools/baton/README.md,
tools/baton/baton (launcher), baton_v5.py (933 lines, complete),
roles.json, test_baton_v5.py.

## Verdict

The config/mailbox/participant/root/reference separation is the RIGHT
abstraction and is sufficient — I found no smaller model that still
covers the three target cases.  The finding's coupling list is accurate
but incomplete; the omissions below include one LIVE v5 defect class the
v6 receipts design must fix, not merely generalize.

## Coupling inventory — items OMITTED from the finding

1. **Receipts live in wipeable /tmp today** (`/tmp/drift-baton-{uid}/
   {sha(repo_root)[:24]}`, baton_v5.py:227).  Claims persist in
   work/mailbox but their receipts do not survive reboot/cleanup; reply/
   close hard-fails without the receipt (:611-619 verifies receipt
   identity + claim/content hashes).  A reboot mid-claim strands the
   claim permanently (same class as the notice `expire` author-receipt:
   the protocol's "lost author identity → human recovery" case is
   trivially triggered by tmpwatch).  This contradicts the finding's own
   "/tmp is not a safe implicit default" principle — it already bites
   the RECEIPT plane in v5.  v6 must move receipts to a durable state
   dir (recommend `$XDG_STATE_HOME|~/.local/state/baton/<mailbox-key>/
   <participant>/`), never /tmp.
2. **Claim receipts bind the literal repo_root string** (:617): moving a
   checkout invalidates every live receipt.  v6 receipts should bind the
   MAILBOX identity, not any content root.
3. **Launcher discovery is script-relative**: `main()` falls back to
   `Path(__file__).resolve().parents[2]` (baton_v5.py:879) — the tool's
   own location inside tools/baton/ is a third implicit authority beyond
   the two env vars the finding lists.
4. **roles.json is config-by-adjacency** (`Path(__file__).with_name`,
   :231) — same class as (3); replaced by --config.
5. **The finding-`*` special case** is at exactly one choke point
   (respond destination guard, :746) — good news: policy extraction is a
   one-site deletion, not a scattered sweep.
6. **Filename grammar interaction**: v5 role slugs forbid hyphens
   BECAUSE hyphens are the filename field separators.  Dotted addresses
   (`drift.reviewer`) preserve unambiguous parsing only if segments stay
   hyphen-free AND no other filename field may contain a dot; timestamps
   and hex ids are dot-free, so recommend the grammar
   `[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+` (>=2 segments, no hyphens) and
   a documented max address length (filenames concatenate two addresses
   + timestamp + id + claim suffixes; NAME_MAX=255 leaves ~80 chars per
   address worst-case — enforce, don't hope).
7. **Atomic publication is ALREADY same-filesystem-safe**: staging is
   sibling-in-target-dir (`.name.tmp-baton-pid-rand`, :180) +
   renameat2(RENAME_NOREPLACE).  The finding's cross-fs worry is a
   KEEP-THIS constraint, not a gap.
8. **No locking exists anywhere** — single-winner is entirely
   rename-noreplace.  That property is load-bearing and must survive the
   generalization (external mailboxes on NFS would silently lose
   renameat2/inotify semantics: v6 should DOCUMENT same-host local-fs
   only and have doctor warn on network filesystems).
9. **Wait watches exactly one directory** via inotify with a 60s rescan
   — generalizes cleanly to one external mailbox; multiple mailboxes per
   process = multiple invocations (fine; do not add multi-watch).
10. **content_sha256 already exists** on the claim receipt (:619) — the
    integrity Q1 hash can be PROMOTED into the envelope rather than
    invented.

## Positions on the finding's open questions

- **mailbox.id**: keep, and make it VERIFIABLE: `init` stamps an
  immutable `mailbox.json` (id + protocol_version) inside the mailbox;
  every open validates config.id == stamp.id.  This detects config/
  mailbox mapping drift cheaply and gives receipts a stable key that
  survives path moves.  Receipt key = stamp id + canonical path hash.
- **Config generation counter for root remapping**: NOT needed if
  envelopes carry the detail's sha256+size at publication (Q1 = yes):
  a remapped root either resolves the same bytes (harmless) or fails
  the hash check (fail-closed).  Hashes subsume generations — simpler.
- **Shared vs per-participant configs**: separate local configs sharing
  a mailbox MUST work (that is the whole point of cross-project use).
  Drift detection = mailbox stamp (transport) + envelope hashes
  (content) + fail-closed unknown root_id (resolution).  No global
  config file requirement.
- **Participant declarations**: required on the SENDING side for both
  `from` (you must be a declared participant) and `to` (typo guard —
  sending to an undeclared address is exit-2).  Claiming requires being
  the declared `to` participant.  Arbitrary syntactically-valid
  addresses are NOT accepted; silent misdelivery to a never-claimed
  address is the worst failure mode of a shared mailbox.
- **detail_prefix**: keep as participant configuration.  It names the
  participant's published artifacts; moving it to `kind` would collide
  distinct kinds into one prefix and removing it pushes naming policy
  into every caller.
- **Mandatory absolute --config UX**: acceptable for agents and
  scripts; wrappers/aliases solve human ergonomics.  Explicitly REJECT
  an env-var fallback (BATON_CONFIG) — it recreates the exact implicit
  authority v6 removes.  `init` may create mailbox+stamp+example config;
  operational commands must never create anything missing (fail-closed
  hypothesis confirmed).

## Integrity questions (finding §Immutability)

1. Envelope records detail `sha256` + `size` at publication: YES
   (promote the existing receipt hash).  Claim/see verifies before
   returning the reference.
2. Baton always CREATES durable details itself in v6.0; existing-file
   references are NOT supported (mutation-ambiguity; if demanded later,
   add only with mandatory caller-supplied hash pinning).
3. Receipts: durable user-state dir keyed by mailbox stamp id +
   participant instance (see omission 1).  Never /tmp, never a content
   root.
4. Same-process multiple configs: receipt paths already include the
   mailbox key → no collision; add the participant address to the key.
5. Ownership/mode: keep v5's 0o770 mailbox + regular-nonsymlink checks;
   state the cooperative same-host trust boundary; no auth theater.
6. Symlinks: forbid at config/mailbox/root boundaries AND resolve+
   containment-check every referenced path (v5's work/-escape logic
   generalized per-root); final file must be a regular non-symlink.
7. doctor: config-aware, report-only — unknown roots/participants,
   v5-era tokens, missing/hash-mismatched references, orphaned details
   (unreferenced files under detail prefixes newer than the oldest
   pending), stranded receipts, network-fs warning, `--assert-empty`
   for migration.

## Three target cases walked (model sufficiency)

- drift.reviewer → drift.implementer (same tree): config maps
  drift.source; durable details land under it exactly as today;
  behavior parity with v5 minus the finding-* core rule (moves to
  AGENTS policy).  COVERED.
- web.reviewer → drift.reviewer (cross-tree): one shared mailbox
  outside both trees; envelope references root_id=web.source paths;
  drift.reviewer resolves via ITS config mapping the same id.  COVERED
  — with the explicit consequence that both configs must agree on
  root_id→path mapping, which the envelope hash makes fail-closed
  rather than silently divergent.
- reviewer → non-repository doc root (language.design): no Git, no
  work/, no finding-*: nothing in the generalized core references them.
  COVERED.

## Migration additions

- Before cutover, receipts need NO migration (drain-first means no live
  claims; v5 receipts die with v5).
- `doctor --assert-empty` provides the "verify actually empty" step the
  finding asks for, without manual ls.
- The v5→v6 announcement must include the config path convention;
  recommend `~/.config/baton/<mailbox-id>.json` as the documented (not
  discovered) convention.

## Proposed Phase-3 test matrix (seed)

external mailbox on a different filesystem (tmpfs) with same-dir staging
verified; two reviewers, one mailbox, independent claims; cross-root
durable reference resolve + hash verify; non-repo root end-to-end;
missing/relative/symlinked --config fails; undeclared to/from fails;
traversal + symlink escape per root; mailbox stamp mismatch fails;
receipt isolation across two configs in one process; concurrent claim
single-winner (existing race tests generalized); detail hash mismatch at
claim fails-closed; orphan-detail doctor report; v5 token present →
doctor flags, operations refuse; address grammar + length limits;
NAME_MAX boundary.

## Questions for Slawomir's ruling (Phase-2 gate)

1. Mailbox stamp file (mailbox.json identity) — accept as the mapping-
   drift guard?  (My recommendation: yes.)
2. Undeclared-address send = error (my rec) vs warn?
3. Receipts location: ~/.local/state/baton (my rec) vs elsewhere?
4. v6.0 excludes existing-file references (my rec) — acceptable?
5. Documented config-path convention for wrappers.

## Round 2 (review 05-29-11Z) — revised recommended contract

The four P1s each exposed a real gap; revised positions, now concrete:

### P1-1: location authority = root binding markers, not content hashes

Conceded: sha256+size proves CONTENT, not LOCATION — two same-byte
clones would pass while responses land in the wrong tree.  Revised
contract: every configured root carries a generated collision-resistant
`root_uid` (uuidv4) recorded BOTH in the config entry and in an
immutable marker file `.baton-root` at the root directory itself
(written once by an explicit `init --bind-root <root_id>` ceremony).
Envelope references carry {root_id, root_uid, path, sha256, size}.
Resolution requires: config maps root_id → path, marker exists, marker
uid == config uid == envelope uid, then containment + hash checks.  A
clone lacks the marker (git-ignored by convention) unless deliberately
copied — a documented forbidden act; a moved root keeps its marker and
just needs the config path updated (uid unchanged, no re-bind).  No
absolute paths in envelopes; location authority proven at both ends.

### P1-2: ONE authoritative config per mailbox — accepted

Re-evaluated and conceded: my "separate configs must work" conflated
ergonomics with necessity.  The contract is one config file per
mailbox; every participant passes the SAME absolute path (which is
exactly as portable — any tree can pass it), and "independent projects"
are simply different mailbox+config pairs.  This deletes the
equivalence-proof problem outright.  The config's canonical location is
the humans' shared convention (documented, not discovered).

### P1-3: mailbox identity = {name, uuid, generation}; explicit state_dir

Stamp file written at init: human `name` (label, copyable), generated
`uuid` (collision-resistant identity), `generation` (see P1-4).
Receipts key on uuid + participant + actor — so MOVING a mailbox
directory preserves receipt validity (uuid travels in the stamp; no
path in the key).  COPYING a mailbox forks the uuid illegitimately:
`init` refuses to stamp over an existing stamp, and the documented copy
ceremony is `reinit --new-uuid` on the copy (doctor cannot detect
cross-directory duplicates and does not pretend to).  Receipt/state
location: an explicit absolute `state_dir` config field — conceded, no
new implicit XDG authority; the config is the single authority for ALL
paths.  Defect split stated plainly: durable receipts fix the tmpwatch
loss class; a reboot still loses the live-instance SEED by design, so
an unclosed claim after instance death remains the (now rare)
human-authorized recovery case — instance-owned claims are a feature,
not a bug.

### P1-4: config generation is first-class

`generation` (monotonic int) lives in the config AND the mailbox stamp.
Every open validates config.generation == stamp.generation.  Changing
participants/roots/policy requires: doctor --assert-empty, edit config,
bump generation, restamp (an explicit `regen` ceremony).  Live messages
therefore can never straddle a mapping change; envelopes additionally
record the generation they were published under for post-hoc audit.

### P2-1: unsupported filesystems fail CLOSED

At every mailbox open: probe renameat2(RENAME_NOREPLACE) with a scratch
pair inside the mailbox dir; on failure, hard error (the no-clobber
rename IS the claim authority — no warning, no override).  Known-
network statfs types: hard error.  inotify availability is separate and
merely degrades wait to the 60s rescan (documented as latency, not
safety).

### P2-2: doctor orphan reporting

Conceded: no age heuristic, no removal authority ever.  Doctor lists
"unreferenced files under configured detail prefixes" as INFORMATION
with an explicit "not proven orphaned" label.

### Schema delta (over the finding's candidate)

config: + "generation", + "state_dir" (absolute), roots become
{"path": ..., "uid": ...}; mailbox: {"name", "path"} (uuid lives only
in the stamp).  Stamp file mailbox.json: {"name", "uuid", "generation",
"protocol_version"}.  Root marker .baton-root: {"root_id", "uid"}.
Envelope reference: {"root_id", "root_uid", "path", "sha256", "size",
"generation"}.

### Test-matrix additions

root-marker missing/mismatched/copied-clone → fail closed; mailbox
moved (receipts still valid via uuid); mailbox copied without reinit →
stamp uuid duplicate documented-undetectable, reinit ceremony test;
generation mismatch config-vs-stamp → fail closed; regen ceremony only
under assert-empty; renameat2 probe failure → hard error; statfs
network type → hard error; state_dir relative/symlink → rejected;
envelope generation recorded.

## Round 3 (final candidate, review 05-39-58Z) — challenge + deliverables

### Challenge results: DESIGN-CLEAR with two recommendations and one
### documented residual risk

- Dropping .baton-root markers (refinement 5): SOUND.  My round-2
  markers compensated for multi-config divergence; with ONE
  authoritative config per instance (+ semantic config digest and
  generation in instance.json), all participants resolve root_ids
  identically and the two-clone wrong-tree hazard cannot arise among
  config users.  RESIDUAL RISK (documented, not fixable in-model): if
  the human edits the config to point a root_id at the wrong clone,
  everyone coherently uses the wrong tree — a configuration error
  outside the cooperative trust boundary; doctor cannot detect it.
- Instance directory (refinement 2): SOUND and SIMPLER than my
  state_dir split — receipts/mailbox/stamp/config move as one unit, so
  mailbox moves preserve everything with no uuid-keyed external state.
  Copy semantics still require the reinit --new-uuid ceremony.
  Confirmed consistency: --config remains mandatory/explicit even
  though baton.json lives in the instance dir (no instance-dir
  discovery).
- Existing-file references with pin-at-publication (refinement 6,
  reversing my exclusion): CONCEDED.  Baton hashes the existing file at
  publication; post-publication mutation fails closed at every verify.
  Pre-publication state is the sender's authority by definition; the
  TOCTOU window is sender-side and irrelevant.
- RECEIPTS REASSESSMENT (refinement 11), the substantive item:
  * NOTICE-SEEN receipts: KEEP — they are the only per-participant
    broadcast-consumption record; scan/wait filtering depends on them.
    Durable inside instance/receipts/.
  * CLAIM receipts: DELETE.  Their two v5 invariants are now covered:
    ownership verification comes from actor+seed in the CLAIM FILENAME
    (reply/close verifies the invoker matches), and content integrity
    comes from the envelope's publication-time pin (which also covers
    the claim→reply mutation window the receipt snapshot guarded).
    What a receipt uniquely proved — "I renamed it, not someone
    renaming AS me with my visible seed" — is spoof-detection outside
    the cooperative trust boundary (seeds are printed in filenames and
    are not secrets, refinement 10).
  * NOTICE-AUTHOR receipts: DELETE.  `expire` authorization verifies
    invoker actor+seed against the notice envelope's author fields —
    same strength as today under cooperative trust.  Instance death
    already stranded expiry in v5 (the receipt died in /tmp); the
    deletion leaves recovery semantics identical: human-authorized.
  Net: one receipt kind (notice-seen), durable, instance-local.

### Final schema (candidate for lock)

instance dir (e.g. /home/sl/.local/state/baton/drift/ — convention):
  baton.json      — the ONE config (passed explicitly via --config)
  instance.json   — {"mailbox_uuid", "protocol_version": 6,
                     "generation", "config_sha256"}  (semantic digest =
                     sha256 of canonical-JSON config minus comments)
  mailbox/        — flat; PENDING-/CLAIMED-/NOTICE- names + sibling
                     .tmp staging
  receipts/       — notice-seen only

baton.json:
  {"config_version": 1, "protocol_version": 6, "generation": N,
   "mailbox": {"name": "drift-local"},
   "participants": {"drift.reviewer": {"identity": "agent",
       "detail_prefix": "review"}, ...,
     "human.slawomir": {"identity": "singleton",
       "singleton_actor": "slawomir", "detail_prefix":
       "approval-decision"}},
   "roots": {"drift.source": "/home/sl/src/drift-lang", ...}}

envelope reference:
  {"root_id", "path" (normalized relative), "sha256", "size",
   "generation"}

filenames (flat, hyphen-separated; dotted addresses):
  PENDING-FROM-web.reviewer-TO-drift.reviewer-<ts>-<id>
  CLAIMED-...-BY-<actor>-SEED-<seed>-AT-<ts>
Address grammar [a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+, enforced max
length so worst-case names stay under NAME_MAX.

### CLI examples

  baton --config ~/.local/state/baton/drift/baton.json \
        drift.implementer wait --actor k --seed $SEED
  baton --config ... web.reviewer send drift.reviewer \
        --reference drift.source:doc/history.md --kind question <<<'...'
  baton --config ... drift.reviewer reply "$CLAIM" --kind review <<'EOF2'
  ...
  EOF2
  baton --config ... human.slawomir doctor --assert-empty
  baton init --instance ~/.local/state/baton/drift   # creates dirs,
        stamp, example config; refuses over an existing stamp
  baton regen --config ...   # assert-empty + generation bump + restamp
  baton reinit --new-uuid --config ...  # copy ceremony (fork identity)

### init/regen/recovery semantics

- init: creates instance dir skeleton + instance.json + example
  baton.json; REFUSES if instance.json exists; never touches roots.
- open (every operational command): validate config file regular/
  absolute/non-symlink; instance.json protocol/generation/config-digest
  match; mailbox flat-dir checks; renameat2 probe + statfs fail-closed.
- regen: doctor --assert-empty must pass; bump generation in config;
  rewrite instance.json digest+generation atomically.  Root/participant
  edits ONLY via regen.
- recovery: dead-instance claims (seed lost) remain human-authorized
  manual recovery — now RARE (no /tmp receipt loss class).  Expired
  notices from dead authors likewise.  doctor reports both, mutates
  neither.

### Exact existing-test/document edit ledger (needs Slawomir approval)

- REPLACE tools/baton/baton_v5.py → baton_v6.py (one contract).
- REPLACE tools/baton/test_baton_v5.py → test_baton_v6.py (full
  rewrite; old file deleted).
- DELETE tools/baton/roles.json (participants move into baton.json);
  DELETE stale v4 artifacts if still present (baton_v4.py,
  test_baton_v4.py, roles_v5.json, README-v5.md) — cleanup rider.
- REWRITE AGENTS-MAILBOX-PROTO.md as protocol v6; MOVE the finding-*/
  response-placement rule into AGENTS.md as Drift workflow policy.
- REWRITE tools/baton/README.md; ADD config schema/example file +
  distribution manifest (new files, no approval needed).
- Drift repo: retire the work/mailbox ignore entry when the Drift
  instance moves external (small .gitignore edit).
- Launcher tools/baton/baton: update import (trivial).

### Revised red-first test matrix (Phase 3)

1 mandatory-config (missing/relative/symlink/env-ignored) fail-closed;
2 init refuses existing stamp; 3 open validates protocol/generation/
digest; 4 regen only under assert-empty; 5 external mailbox on separate
local fs, sibling .tmp staging verified; 6 renameat2 probe + network-fs
fail closed; 7 two reviewers one mailbox independent claims;
8 undeclared from/to send errors; 9 dotted-address grammar + NAME_MAX
boundary; 10 root_id resolution + containment + symlink-escape fail
closed; 11 existing-file reference pinned at publication, post-publish
mutation fails at claim/see/reply; 12 Baton-created detail parity;
13 transient no-reference messages; 14 broadcast non-claimable +
notice-seen receipt dedupe, durable across instance move; 15 claim
filename ownership check replaces claim receipts (reply/close by wrong
actor/seed fails); 16 expire verifies author from envelope; dead-seed
expiry stays stranded-and-reported; 17 concurrent claim single-winner
races; 18 doctor report-only inventory incl. v5 tokens + unreferenced
details ("not proven orphaned"); 19 wait inotify + degraded polling
parity; 20 instance-move preserves receipts/claims (uuid unchanged);
copy + reinit ceremony forks identity.

## Round 4 (review 05-43-01Z) — final contract deltas

1. EXISTING-REFERENCE AUTHORITY: sole-authority mode adopted.
   `send --reference root:path` makes the referenced file THE durable
   message body; supplying stdin alongside it is a hard error (never
   silently discarded/combined).  Baton-created details = stdin body;
   transient = stdin embedded.  No note+attachment mode in v6.
2. REGEN AS A SINGLE-COMMIT TRANSACTION: the human edits baton.json
   (including bumping generation to stamp+1) BEFORE running regen;
   regen's only mutation is ONE atomic rewrite of instance.json
   {generation, config_sha256} after validating assert-empty AND
   config.generation == stamp.generation + 1.  There is no
   two-file-write window.  Crash before the commit leaves config ahead
   by exactly one — `open` RECOGNIZES generation==stamp+1 as the
   distinguished in-progress-regen state and directs to regen (which is
   idempotently re-runnable); any other mismatch (digest differs at
   same generation, jump >1, config behind) is a hard human-escalation
   failure.  Canonical JSON: conceded — strict JSON only, digest =
   sha256 of canonical serialization (sorted keys, compact separators)
   of the ENTIRE parsed config object (all routing/resolution policy
   fields by construction).
3. REINIT/MOVE SAFETY: `reinit --new-uuid` requires an EMPTY mailbox
   AND empty receipts/ (no notice-seen), else error.  Instance MOVES
   are supported only QUIESCED: no running Baton processes; live claims
   (filename-borne) survive the move, but all waiters must be stopped
   before and restarted after (inotify fds do not follow renames) —
   documented as the move ceremony, not inferred from self-containment.
4. NOTICE-SEEN CLEANUP: `expire` removes the notice FIRST, then sweeps
   its seen receipts (ordered).  A seen receipt whose notice no longer
   exists is DEAD-recognized garbage: doctor reports the count;
   `--assert-empty`'s deterministic rule = no messages in mailbox/ AND
   no receipts referencing a LIVE notice (dead receipts ignored);
   expire idempotently re-sweeps its own notice's dead receipts.
5. CONFIG-PATH/DOC CONSISTENCY: documented — the value Baton RECEIVES
   must be absolute (shell ~ expansion is fine); the digest covers the
   whole config (item 2), so no per-field policy list can drift.

Test-matrix additions: 21 stdin+reference hard error; 22 regen
crash-window (config at stamp+1 → open directs to regen; regen rerun
commits; digest-mismatch-same-generation hard-fails; jump/behind
hard-fail); 23 reinit refusal on non-empty mailbox OR receipts;
24 expire ordered cleanup + dead-receipt recognition +
assert-empty determinism; 25 quiesced-move ceremony (claims survive,
waiter restart required).

Edit ledger: unchanged (these deltas are all new-contract content).

## Round 5 (packaging review 05-50-15Z) — PEX --scie eager artifact design

Contract locked additions (incl. the pending expiry dead-receipt
recovery correction, now formally part of the locked contract): expire
removes the notice FIRST, then idempotently sweeps its seen receipts;
receipts referencing a gone notice are dead-recognized garbage that
doctor counts, assert-empty ignores, and any later expire re-run
sweeps — crash recovery is simply re-running expire.

### Boundary challenge

The candidate contract is sound; two boundary sharpenings recommended:
1. REUSE the proven helper functions from tools/deploy/steps/pex.py
   (venv staging, --scie eager, --scie-python-version pin, entry-point
   staging) by FACTORING the generic builder into a small shared
   module — but Baton's build must not import Drift deploy STEP logic
   (no BUNDLED_TOOLS_PACKAGES, no drift entry dispatch): standalone
   builder script tools/baton/build_pex.py calling the shared generic
   helper only.  If Slawomir prefers zero coupling, duplicate the ~30
   builder lines instead; flagging the choice.
2. SOURCE/TESTS DISTRIBUTION: recommend executables + protocol/README +
   schema/example + manifest to peers; source and tests remain in the
   canonical Drift tree (peers audit upstream, not vendored copies) —
   vendoring source alongside a sealed binary invites divergence.  The
   manifest's sha256 + version rows give peers the audit anchor.

### Recommended layout

tools/baton/
  baton_v6.py          — implementation (stdlib-only)
  baton                — dev launcher (python3 shim, unchanged role)
  build_pex.py         — builds bin/baton (PEX --scie eager)
  baton_entry.py       — dedicated PEX entry point (main())
  test_baton_v6.py     — unit tests
  README.md, AGENTS-MAILBOX-PROTO.md (v6), baton.schema.json,
  baton.example.json, DISTRIBUTION.md (manifest template)
Artifact: bin/baton (repo-relative build output; distributed file).

### Build command / entry point

  .venv/bin/python tools/baton/build_pex.py
    → pex (pinned version from requirements.txt) with:
      --scie eager --scie-python-version <pinned>
      --python <staging venv python3>
      -m baton_entry  (staged with baton_v6.py in a temp src dir)
      -o bin/baton
Stdlib-only deps: the PEX carries no third-party wheels, only the
embedded interpreter.

### Distribution set (exact)

  bin/baton                      (packed executable)
  AGENTS-MAILBOX-PROTO.md        (protocol v6)
  tools/baton/README.md
  baton.schema.json + baton.example.json
  DISTRIBUTION.md manifest: {tool version, protocol_version 6,
    platform/arch (e.g. linux-x86_64), sha256(bin/baton),
    scie python version}
  NEVER: any active baton.json/instance.json/mailbox/receipts content.

### Portability/cache limitations (documented)

- Platform/arch-specific artifact (scie bundles a native interpreter);
  the manifest names the platform; cross-platform peers rebuild from
  canonical source.
- First run extracts to SCIE_BASE (or the scie default user cache);
  read-only install trees must set SCIE_BASE to a writable location —
  same operational note as bin/driftc.
- The extraction cache is per-user; multi-user hosts each pay one
  extraction.
- renameat2/inotify remain Linux-specific (unchanged v6 constraint,
  now also baked into the artifact's platform row).

### New/changed-file ledger delta (adds to the approved ledger)

NEW: tools/baton/build_pex.py, baton_entry.py, baton.schema.json,
baton.example.json, DISTRIBUTION.md; bin/baton build output.
CHANGED (pending the coupling ruling): factor a generic
build_scie_pex() helper out of tools/deploy/steps/pex.py (behavior-
preserving for driftc/drift builds; their tests unaffected) — OR no
change there if duplication is ruled.
Peer repos: receive distribution set; their AGENTS.md gains the
invocation convention (out-of-band announcement per migration plan).

### Focused packaging tests (red-first additions, matrix 26-31)

26 bin/baton runs with a scrubbed environment (PYTHONPATH,
PYTHONSAFEPATH, DRIFT_PYTHON, VIRTUAL_ENV unset; PATH without any
python) — version/protocol report works; 27 invocation from OUTSIDE
every source tree with an external instance config end-to-end
(send/claim/reply round-trip); 28 read-only artifact + read-only
install dir with external SCIE_BASE; 29 missing/malformed --config
through the packed binary exits 2 with the same diagnostics as the dev
launcher; 30 artifact self-report (version, protocol 6, platform)
matches DISTRIBUTION.md manifest incl. sha256; 31 packed-vs-dev parity
smoke (same test vectors through both entry paths).

## Round 6 (packaging correction 05-51-47Z) — ZIPAPP recommended, PEX dropped

### Comparison (factor by factor)

- Artifact size: zipapp ≈ tens of KB (Baton code only) vs PEX-scie ≈
  tens of MB (embedded CPython).
- Architecture portability: zipapp is pure-Python — ONE artifact serves
  every Linux arch (Baton is Linux-only via renameat2/inotify
  regardless); PEX-scie needs per-arch builds.
- Build dependencies: zipapp builds with the stdlib alone (zipfile/
  zipapp), no pex, no staging venv, no network; deterministic builds
  are trivial (pinned ZipInfo timestamps → stable sha256).  PEX needs
  pinned pex + scie assets.
- Startup/cache: zipapp imports via zipimport directly — NO extraction,
  no SCIE_BASE operational note, works from read-only installs as-is.
- Auditability: a zipapp is a readable zip of the exact sources next to
  a sha256 manifest — materially easier to audit than a scie binary.
- Host contract: requires a documented compatible python3.  Baton v6
  targets the `X | None` union syntax etc. → floor python3.10+
  (recommend documenting 3.11+ to match the repo's own floor); startup
  performs an explicit version check and exits 2 with a clean message
  on older interpreters.  Shebang `#!/usr/bin/env python3` + exec bit
  (zipapp supports the interpreter line natively).
- zipimport vs ctypes/inotify/renameat2: NO issue — ctypes loads libc
  independent of import location, and v6 by design has zero
  __file__-relative resources (the only v5 one, roles.json-by-
  adjacency, is deleted; verified no other package-file reads exist).
  One rule follows: v6 code must never use __file__ paths (already
  mandated by the discovery deletion).

### Recommendation

ONE distribution format: executable Python ZIPAPP `bin/baton`.  PEX/
scie dropped for Baton entirely (no dual formats).  "Self-sufficient"
= all Baton modules in one artifact + documented host python3 floor.

### Ledger/test-matrix updates (replacing round-5 packaging entries)

NEW files: tools/baton/build_zipapp.py (stdlib-only, deterministic:
pinned entry timestamps, sorted names, fixed compression),
baton_entry/__main__.py inside the archive; DISTRIBUTION.md manifest
rows become {tool version, protocol 6, python_floor, sha256} — the
platform/arch row is replaced by "linux (kernel features), any arch".
DROPPED from round 5: build_pex.py, scie/SCIE_BASE notes, per-arch
manifest rows, the pex-helper factoring decision (moot — zero coupling
achieved by construction).

Packaging tests (matrix 26-31 revised): 26 scrubbed-env run from
outside every tree (PYTHONPATH/VIRTUAL_ENV unset) — version/protocol
report; 27 POISONED-PYTHONPATH leak test: a decoy baton module on
PYTHONPATH and in CWD must NOT shadow the archive's own modules
(zipapp path isolation proven, not assumed); 28 read-only artifact +
read-only install dir runs without any cache/extraction; 29 old-python
rejection: launching under a pre-floor interpreter exits 2 with the
documented message (subprocess matrix over available interpreters +
in-process version_info simulation); 30 completeness: end-to-end
send/claim/reply round-trip using ONLY bin/baton from a scratch CWD
with an external instance; 31 deterministic build: two builds from the
same tree yield byte-identical archives (sha256 equality) and match
DISTRIBUTION.md.

## Round 7 (design_ruling_review 15-34-17Z) — eight rulings audited + REPLYING contract

Independently checked before pinning. No implementation performed.

### (a) Verdict: DESIGN-CLEAR with 2 resolved deltas, 4 interaction pins, 0 hard contradictions

- **A1 delta (resolved in favor of ruling 4):** Round-3 text said config
  digest = canonical JSON "minus comments" (implying comments allowed in
  baton.json). Ruling 4 rejects comments on EVERY surface including
  config. Pinned: config is comment-free strict JSON; digest over the
  entire validated object, sorted keys, compact separators, finite only.
- **A2 consequence pin:** strict unknown-field rejection means ANY
  additive protocol-surface field (envelope/receipt/intent/stamp) is
  breaking by construction — under ruling 8, "minor tool feature" can
  never touch serialized surfaces within protocol 6. Consistent; pinned
  so a future minor doesn't try.
- **A3 ceremony disjointness pin:** ruling 3 (recover-claim: CLAIMED →
  original PENDING) and the reply contract (REPLYING never → PENDING)
  do not conflict, but ONLY if the ceremonies are disjoint: recover-claim
  REFUSES REPLYING-* names; recover-reply REFUSES CLAIMED-* names. Both
  refusals are explicit contract + tests.
- **A4 exit-code table pin:** ruling 1's exit 2 (python floor) joins
  existing small-int usage codes (today usage errors exit 4). v6 needs
  ONE documented exit-code table (0 ok; 2 version-floor; distinct codes
  for usage / validation-fail-closed / recovery-refusal) in README +
  self-report.
- **A5 implementation pin:** openat2 is NOT in the Python stdlib
  (≤3.12); ruling 6 is satisfiable stdlib-only via ctypes raw
  syscall(SYS_openat2, RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS) with a
  feature probe, falling back to the component-by-component
  O_NOFOLLOW/dir_fd walk. ctypes was already accepted in the round-6
  zipapp ruling. Non-Linux: fallback walk only.
- **A6 response_id semantics (challenge, per invitation):** "fixed
  response_id" must mean fixed ONCE a REPLYING token references it, not
  globally deterministic from the claim. If it were derived from the
  claim name, a crash BEFORE the REPLYING rename would pin a stale
  immutable intent that a legitimately recomposed reply could not
  replace without mutating an "immutable" record. Pinned: response_id is
  unique per reply attempt (ts+entropy); pre-REPLYING orphan intents are
  recognized garbage (see (d)). Ruling 31's scope line ("only for
  recovery of ONE interrupted transition") supports this reading.
- Ruling 2 (receipt GC) is race-safe as specified: notice names never
  reused + unlink-ENOENT-tolerant sweep = idempotent vs concurrent
  expire; requires receipts to embed the exact unique notice name
  (already the v6 receipt shape). Ruling 5/6/7/8: no gaps found beyond
  the pins above. Ruling 1 addendum: the zipapp __main__ bootstrap must
  restrict itself to ~3.6-parseable syntax so pre-3.11 interpreters
  reach the version test instead of dying on SyntaxError.

### (b) REPLYING state machine — exact delta

New instance subdir: `transactions/` (receipts/ stays notice-seen only).
New mailbox states: `REPLYING-…` and deterministic temp `.tmp-<response_id>`.

Filename: `REPLYING-FROM-<from>-TO-<to>-<ts>-<id>-BY-<actor>-SEED-<seed>-AT-<ts2>-WITH-<response_id>`
(claim name + `-WITH-<response_id>`; +17 chars for a 12-hex id — include
in the NAME_MAX worst-case budget).

Transaction record `transactions/<response_id>.json` (immutable, strict
JSON): {response_id, protocol_version, in_reply_to: <full claim name>,
from, to, kind, thread_id, outcome?, retention, body (transient) XOR
reference {root_id, path, sha256, size, generation} (durable), created}.

Reply sequence (each step per ruling-5 discipline):
1. durable retention only: commit the detail file first (temp, fsync,
   no-clobber rename, dir fsync) — ruling 5 ordering.
2. commit `transactions/<response_id>.json` (temp+fsync+rename+dirfsync).
3. no-clobber rename CLAIMED-x → REPLYING-x-WITH-<id>; mailbox dirfsync.
   (Intermediate state entered; never returns to PENDING from here.)
4. write mailbox/.tmp-<response_id> = complete outgoing envelope; file
   fsync; mailbox dirfsync. (Temp made DURABLY visible on purpose — it
   is the disambiguator below.)
5. rename <id>.json → <id>.published; transactions dirfsync.
   **COMMIT POINT: after this, recovery never rebuilds content; it only
   completes the rename or cleans up.**
6. no-clobber rename .tmp-<id> → PENDING-FROM-<me>-TO-<them>-<ts>-<id>;
   mailbox dirfsync. (The outgoing token's trailing id IS response_id —
   presence in ANY state, PENDING or CLAIMED-by-recipient, is greppable.)
7. unlink REPLYING-…; mailbox dirfsync.
8. unlink <id>.published; transactions dirfsync. (In-line delete;
   provability is only required while REPLYING exists — see (d).)

Recovery decision table (given REPLYING-…-WITH-<id>; entered by owner
`resume` or human-authorized `recover-reply`):
- `<id>.json` exists (pre-commit-point): rebuild .tmp-<id> from the
  immutable intent (idempotent overwrite, identical bytes; durable
  reference re-verified against pinned sha256/size first — mismatch =
  fail closed, damaged), then continue 4→8.
- `<id>.published` exists:
  - temp present → continue 6→8 (completing an interrupted rename of
    the same bytes is redelivery, not re-creation);
  - temp absent, outgoing token with -<id> present (any state) → verify
    identity against intent, then 7→8;
  - temp absent, outgoing absent → published-and-consumed → 7→8 only.
  (Post-crash fs state after an unsynced rename is pre XOR post; both
  rows handle it. Consumption can only happen after step 6, which is
  after the commit point, so "consumed before evidence" cannot occur.)
- neither record exists → REPLYING references a missing transaction:
  damaged; doctor-reported; recovery fails closed for human decision.

Ceremony/CLI delta: `resume` (same actor/seed; auto-offered when wait
finds own REPLYING); `recover-reply` (dead seed; requires --reason +
quiesced/dead warning; validates claim/intent/reference identity;
accepts NO new body/direction/kind/recipient/outcome/reference);
`recover-claim` (ruling 3; CLAIMED→original PENDING via no-clobber
rename + dirfsync; refuses REPLYING). wait/claim skip REPLYING;
scan/doctor report owner actor/seed, AT time, response_id, and the
recovery sub-state (intent-committed / published / temp-present).
Plain `close` keeps no intermediate state (single validated unlink +
dirfsync, idempotent). RECOMMENDATION: with reply now effectively-once,
the close+send disposition pairing should be deprecated in favor of
reply (close+send remains two independent messages and can still lose
the disposition between them — acceptable only where no disposition is
owed).

### (c) Test/edit ledger additions (continues approved matrix at 32)

32 reply crash matrix: deterministic kill at every boundary (post-intent
write / post-intent fsync / post-REPLYING rename pre-dirfsync /
post-temp durable / post-.published / post-outgoing rename /
post-REPLYING unlink) × fresh-process reopen → exactly-once logical
publication, no partial JSON, idempotent resume to completion.
33 owner resume completes; second resume no-ops cleanly.
34 recover-reply validates identity, refuses new content/routing,
requires --reason; refuses when a live owner is plausible (human
assertion path per ruling).
35 race: owner resume vs concurrent recover-reply → single winner via
no-clobber renames; loser exits with clean diagnostic; exactly one
outgoing.
36 consumed-tail crash: publish → recipient claims + transient-closes →
crash before REPLYING unlink → resume proves completion (.published +
no temp + no token) and cleans up without republishing.
37 REPLYING non-claimable: wait/scan skip; recover-claim refuses it;
recover-reply refuses CLAIMED.
38 orphan intent records (id unreferenced by any REPLYING and no
outgoing token): doctor reports; gc removes strictly-valid orphans only.
39 tombstone lifetime: .published with REPLYING present = in-recovery,
never garbage; .published without REPLYING = crash residue, gc-able.
40 strict-JSON matrix across ALL surfaces incl. transaction records:
dup keys / trailing content / NaN-Inf / unknown field / Bool-where-Int
(type(x) is int, not isinstance) / malformed address / bad version.
41 python-floor bootstrap: pre-3.11 interpreter → exit 2 with clean
diagnostic; bootstrap file parses on 3.6.
42 receipt GC: valid seen-receipt w/ absent notice removed; malformed
untouched + doctor-reported; receipt for live notice untouched;
concurrent expire race idempotent.
43 recover-claim: atomic return to the ORIGINAL PENDING name + dirfsync;
exact envelope validation; reason required.
44 storage-layer fault-hook seam: injection is test-only; no ambient
production CLI switch.

### (d) GC/tombstone lifetime requirements (new recognized-garbage classes)

1. Orphan transaction intents `transactions/<id>.json` with no
   `REPLYING-*-WITH-<id>` and no outgoing `*-<id>`: pre-commit
   abandonment residue → gc-able (ruling-2 style: strictly valid,
   provably unreferenced; malformed stays doctor-only).
2. `.published` tombstones: MUST outlive their REPLYING token — the
   sequence orders publish → .published (5/6) → REPLYING unlink (7) →
   .published unlink (8), so .published strictly outlives REPLYING;
   survivors without REPLYING are crash residue → gc-able.
3. Deterministic temps `.tmp-<id>`: garbage only when their id resolves
   to no record at all or to .published-without-REPLYING; otherwise they
   are live recovery state and doctor reports them as such (never "not
   proven orphaned" — the id makes them proven).
Invariant: while any REPLYING-*-WITH-<id> exists, every artifact keyed
by <id> is live and untouchable by gc.

State: rulings pinned; REPLYING delta + ledger returned for review.
Implementation remains UNAUTHORIZED.

## Round 8 (design_ruling_review 15-39-59Z) — four corrections incorporated

All four accepted; the round-7 deprecation suggestion for close+send is
WITHDRAWN (terminal-signoff close is a first-class contract; any change
there is Slawomir's ruling, not mine).

### P1-1 resolved: content-bearing close gets the same invariant

One generalized transaction machine, two intermediate states for
scan/doctor clarity: `REPLYING-…-WITH-<id>` (publishes an outgoing
token) and `CLOSING-…-WITH-<id>` (publishes none). Intent records carry
`mode: "reply" | "close"`.

Content-bearing close sequence:
1. commit the final disposition detail (durable; ruling-5 ordering).
2. commit `transactions/<id>.json` = {mode:"close", in_reply_to: <full
   claim name>, outcome, reference {root_id, path, sha256, size,
   generation} pinning the ALREADY-CHOSEN detail, created}.
3. no-clobber rename CLAIMED → CLOSING-…-WITH-<id>; mailbox dirfsync.
4. rename <id>.json → <id>.published; txn dirfsync. (COMMIT POINT: the
   disposition is final; recovery never re-chooses outcome or detail.)
5. unlink CLOSING; mailbox dirfsync.
6. unlink <id>.published; txn dirfsync.

Recovery from CLOSING: intent .json present → verify the pinned detail
exists and hash-matches (missing/mismatch = damaged, fail closed) →
continue 4→6; .published present → same verification → 5→6 only.
Recovery accepts no new outcome/detail/reference — identical to
recover-reply. recover-close is the dead-seed ceremony; CLOSING is
non-claimable; recover-claim refuses it.

Residue pin: a crash after step 1 but before step 2 leaves the claim
CLAIMED and an unreferenced detail file; the retry composes fresh
(timestamped detail names never collide) and the AUTHORITATIVE
disposition is defined as the intent-referenced one. Unreferenced
details stay in doctor's "not proven orphaned" inventory — unchanged
class, now with a definition of authority.

Truly bodyless, no-disposition close (nothing owed to anyone): remains
a validated single unlink + dirfsync, idempotent, no intermediate state.

### P1-2 resolved: pre-transition intents are NOT generally GC-able

Corrected lifecycle: `transactions/<id>.json` (pre-.published) is LIVE
while EITHER (a) any REPLYING/CLOSING-*-WITH-<id> exists, OR (b) any
CLAIMED whose full name equals intent.in_reply_to exists — because the
owner sits between step 2 (intent committed) and step 3 (state rename)
with no atomic two-entry commit available. General gc NEVER deletes
pre-transition intents. They are removed only by:
- the OWNING ceremony: a fresh reply/close attempt on the same claim
  deletes its own prior orphan intents (in_reply_to == its claim)
  before committing the new intent — owner-scoped supersede;
- recover-claim: before returning CLAIMED → original PENDING it MUST
  enumerate and delete every transactions/*.json with in_reply_to ==
  that claim (dead owner's pre-commit intents; returning to PENDING
  while they linger would leave ambiguous liveness);
- a human-asserted quiescent sweep (doctor-verified no live actors),
  the only path that may collect intents whose claim no longer exists.
Invariant (revised): every artifact keyed by <id> is untouchable while
the WITH-<id> intermediate state OR the in_reply_to-matching CLAIMED
exists.

### P1-3 resolved: .tmp is committed durable state, never a scratch target

`.tmp-<id>` (the staged outgoing envelope) is itself committed from a
distinct sibling scratch name (`.scratch-<id>`): O_EXCL|O_NOFOLLOW
create, complete write, file fsync, no-clobber rename to `.tmp-<id>`,
mailbox dirfsync. Recovery finding an existing `.tmp-<id>` verifies its
EXACT bytes against the intent-derived envelope: match → proceed;
mismatch → damaged state, fail closed, doctor-reported. Never
"idempotently overwrite". Absent (pre-.published) → rebuild via
scratch→rename. Leftover `.scratch-<id>` is always reclaimable by its
owner ceremony (it is the only genuinely scratch artifact; unreferenced
scratches with no matching intent are recognized garbage). The same
distinct-scratch → fsync → no-clobber-rename → dirfsync discipline
applies to every transaction artifact (.json, .published transitions
are renames already; creation goes through scratch).

### P2-4 resolved: 128-bit ids + runtime NAME_MAX accounting

- response_id / message id: 128 random bits, 32 lowercase hex —
  uniform with instance seeds; ALL v6 message ids move to 32-hex (the
  outgoing token's trailing id IS the response_id). No-clobber rename
  collision remains a clean error, now cryptographically negligible.
- Name budget: computed at open() from pathconf(mailbox, NAME_MAX)
  (fail closed if unavailable), against the worst-case name:
  `REPLYING-FROM-<from>-TO-<to>-<ts>-<32hex>-BY-<actor>-SEED-<32hex>-AT-<ts>-WITH-<32hex>`.
  Fixed literal + hex + timestamp overhead is a compile-time constant;
  the residual is the address/actor budget. Participant addresses and
  actor names are validated against this budget at init/regen AND at
  open (a config that fits at regen time on fs A must revalidate on
  fs B after an instance move) — never discovered late at send time.
- Outgoing-response location during recovery: structured parse of
  candidate names (state prefix, FROM/TO, ts, id fields) + envelope
  message_id/content identity validation against the intent — never a
  suffix-substring grep.

### Test-ledger additions (continues at 45)

45 close crash matrix: kill at every content-bearing-close boundary
(post-detail / post-intent / post-CLOSING rename / post-.published /
post-CLOSING unlink) × reopen → exactly one authoritative disposition,
no re-choice, idempotent completion; bodyless close stays a single
validated unlink.
46 pre-intent close residue: crash between detail commit and intent
commit → retry produces a fresh detail; doctor lists the unreferenced
one; authority = intent-referenced detail only.
47 live-producer-vs-GC race: gc runs concurrently with steps 2-3 of a
live owner → intent survives (CLAIMED in_reply_to match); after the
WITH rename, gc still refuses; only owner-supersede, recover-claim, or
human-quiesced sweep remove pre-transition intents.
48 recover-claim intent sweep: dead owner's pre-commit intents deleted
before CLAIMED returns to PENDING; return refused if deletion cannot be
proven complete.
49 .tmp mismatch: corrupted/foreign .tmp-<id> bytes → recovery fails
closed + doctor damage report; correct bytes → completes; leftover
.scratch-<id> reclaimed by owner, reported otherwise.
50 id/address boundaries: 32-hex id grammar enforced; NAME_MAX budget
computed from pathconf; address exactly at budget passes, +1 fails at
init/regen AND at open after simulated instance move; recovery locates
outgoing via structured parse + message_id validation (a decoy file
with the id as a substring is ignored).

State: round-8 revision returned for review. Implementation remains
UNAUTHORIZED.

## Round 9 (design_ruling_review 15-42-35Z) — recovery exclusivity, scratch GC, actor budget

### P1-1 resolved: RECOVERING-CLAIM — exclusive ownership before any sweep

New flat state: `RECOVERING-CLAIM-<orig-pending-suffix>-BY-<recovery-actor>-SEED-<recovery-seed>-AT-<ts>`
(orig-pending-suffix = FROM-…-TO-…-<ts>-<id>, preserved verbatim so the
final rename target is derivable from the name alone).

Sequence:
1. Human-authorized invocation (--reason, exact claim name, envelope
   validation, quiesced/dead warning).
2. Atomic no-clobber rename CLAIMED → RECOVERING-CLAIM; mailbox
   dirfsync. EXCLUSIVITY POINT: both contenders act on the same source
   name, so either the owner's CLAIMED→REPLYING/CLOSING or recovery's
   CLAIMED→RECOVERING-CLAIM wins; the loser gets ENOENT and a clean
   diagnostic. Owner-wins ⇒ recovery refuses and directs to
   recover-reply/recover-close.
3. Only now sweep transactions/*.json with in_reply_to == the original
   CLAIMED name (unlink + txn dirfsync each). The owner can no longer
   transition (its claim name is gone; its step-3 rename ENOENTs and it
   aborts without damage).
4. No-clobber rename RECOVERING-CLAIM → original PENDING name; mailbox
   dirfsync.

Race residue pin: an owner that commits a fresh intent AFTER recovery's
sweep and then loses its rename leaves a stale pre-intent whose claim
name can never recur (in_reply_to embeds owner/seed/AT). It is
doctor-reported garbage removable only by the human-quiesced sweep —
NOT damage, and no REPLYING-without-intent can ever be produced by the
recovery tool (the round-8 defect is closed by ordering).

RECOVERING-CLAIM crash/recovery table: the state is resumable only as
the ALREADY-AUTHORIZED return-to-PENDING — resume re-runs the
(idempotent) sweep and completes step 4; it accepts no new work,
content, or alternative disposition; it never converts to any other
state. wait/claim skip it; scan/doctor report recovery actor/seed, AT,
and sub-state (pre-sweep / swept). recover-reply and recover-close
refuse RECOVERING-CLAIM names; recover-claim refuses
REPLYING/CLOSING/RECOVERING-CLAIM sources.

NAME_MAX note: "RECOVERING-CLAIM-" joins the worst-case set; the budget
formula takes max(REPLYING-…-WITH form, RECOVERING-CLAIM form).

### P1-2 resolved: scratch is report-only for online GC

`.scratch-<id>` files are REPORT-ONLY during ordinary operation: every
legitimate creation has a scratch-before-rename window, and no online
predicate can distinguish a live writer from residue. Removable only by
the exact owning ceremony (same id, during its own run) or the
human-asserted quiescent sweep. Online-GC-able classes stay: dead
notice-seen receipts (ruling 2); `.published` without its WITH-state;
`.tmp-<id>` with NO WITH-state and NO transaction record — race-free
by construction because a legitimate `.tmp` writer only exists while
REPLYING-WITH-<id> exists (step 4 follows step 3) and ids are unique
per attempt. Pinned with the impossible-by-construction argument.

### P2-3 resolved: actor grammar with reserved budget

Actor grammar: `[a-z][a-z0-9_-]*`, fixed ACTOR_MAX = 32 chars; seeds
are exactly 32 hex. init/regen validates participant addresses and
singleton_actor values against the worst-case filename budget with
ACTOR_MAX RESERVED for the actor component — and documents exactly
that: agent actor names are NOT validated at init (unknown then).
open validates the supplied --actor (grammar + ACTOR_MAX) BEFORE any
mutation, plus the accepted pathconf(NAME_MAX) revalidation per
instance location. A config valid at regen is therefore valid for
every conforming actor on that filesystem; a nonconforming actor fails
at open, never mid-operation.

Also pinned per acceptance note: "bodyless close" is NARROW — a close
carrying ANY outcome or durable final record routes through CLOSING
even if its text body is empty; only a literally-nothing close (no
outcome, no detail, nothing owed) is the bare validated unlink.

### Test-ledger additions (continues at 51)

51 owner-vs-recover-claim single-winner at every boundary: no-clobber
rename decides; loser exits clean; owner-wins forces recover-reply
path; recovery-wins leaves owner aborted with no damage.
52 sweep-race residue: owner intent committed post-sweep + lost rename
→ stale intent doctor-reported, quiesced-sweep-only; asserts no
WITH-state-without-intent is constructible via the recovery tool.
53 RECOVERING-CLAIM crash/resume matrix: kill post-rename / mid-sweep /
post-sweep-pre-final / post-final-pre-fsync → resume completes the
authorized return; resulting PENDING byte-identical to original;
no new content accepted; doctor reports sub-state throughout.
54 scratch writer-vs-GC: online gc never touches .scratch-* (live
writer mid-window survives a concurrent sweep); owning ceremony
reclaims its own; quiescent sweep collects orphans; .tmp online
predicate verified against a live writer (construction impossibility
asserted by test harness ordering).
55 actor budget: bad grammar / over-ACTOR_MAX actor rejected at open
pre-mutation; init/regen participant validation uses the ACTOR_MAX
reserve; worst-case includes RECOVERING-CLAIM form; at-budget passes,
+1 fails; pathconf revalidation after instance move.
56 bodyless-close narrowing: empty-text close WITH outcome/record
routes through CLOSING; literally-nothing close is a bare validated
unlink and leaves no transaction artifacts.

State: round-9 revision returned for review. Implementation remains
UNAUTHORIZED.

## Round 10 (design_ruling_review 15-44-55Z) — recovery record identity + recursive takeover

### P1 resolved: bounded recovery transaction record (identity never lost)

`transactions/recover-<recovery_id>.json` (recovery_id = 128-bit /
32-hex), strict JSON, committed via scratch→fsync→no-clobber→dirfsync,
dirfd/no-follow, BEFORE the exclusive rename:
{record: "recover_claim", recovery_id, original_claim_name (FULL
CLAIMED-… name incl. BY/SEED/AT), pending_target (original PENDING
name), recovery_actor, recovery_seed, reason (inline) XOR
reason_reference {root_id, path, sha256, size, generation}, created,
protocol_version, generation}.

Final recover-claim sequence:
1. Human authorization; validate the CLAIMED envelope.
2. Commit the recovery record (after owner-scoped supersede: the
   ceremony deletes its OWN prior unreferenced recover records for the
   same original_claim_name — mirror of the reply-intent rule).
3. Atomic no-clobber rename CLAIMED →
   `RECOVERING-CLAIM-<orig-pending-suffix>-WITH-<recovery_id>`;
   mailbox dirfsync. Exclusivity point unchanged; owner-win ⇒ ENOENT ⇒
   refuse; the record is then unreferenced → report-only online,
   quiescent-GC-able (pre-transition rule).
4. Sweep transactions/*.json with in_reply_to ==
   record.original_claim_name — the EXACT full key survives the
   identity-erasing rename because the record is authoritative; a
   decoy intent keyed to a different claim instance of the same
   pending (different BY/SEED/AT) is untouched.
5. No-clobber rename the state → record.pending_target; mailbox
   dirfsync. (Filename-derived target and record.pending_target are
   cross-checked first; mismatch = damage, fail closed.)
6. Unlink the recovery record; txn dirfsync. RECORD STRICTLY OUTLIVES
   THE STATE (same lifetime rule as .published) — so
   state-present ⇒ record-present in every crash schedule, and
   state-without-record is damage, fail closed, doctor-reported.

Record-present / state-absent triage: original CLAIMED still present ⇒
pre-rename abandonment (resume from step 3 or supersede per step 2);
original CLAIMED absent and state absent ⇒ completion evidence ⇒
cleanup (step 6) only. Longest-name accounting: the budget formula
takes max over all state forms; REPLYING-…-BY-…-SEED-…-AT-…-WITH-…
remains the maximum — the bounded record avoids the NAME_MAX explosion
exactly as intended, and RECOVERING-CLAIM-…-WITH-<32hex> is included
in the computed set regardless.

### P2 resolved: recovery-actor death — recursive redelivery

Normal wait/claim skip RECOVERING-CLAIM always. The original recovery
actor/seed may resume (idempotent re-validate + re-sweep + steps 5-6).
After a human confirms the recovery actor dead, `recover-recover-claim`
(name per reviewer, open to improvement) completes ONLY the exact
persisted return-to-PENDING transaction: no new target, content, or
reason accepted; it durably records the takeover identity as a sibling
record `transactions/recover-<recovery_id>.takeover-<new 32hex>.json`
{takeover_actor, takeover_seed, reason, created} committed via the
same scratch discipline BEFORE proceeding (the original record stays
immutable; audit is durable). The takeover record is live while the
recover record or state exists and is unlinked after step 6. The rule
is redelivery-not-recreation applied recursively; no automatic
timeout at any level.

### Test-ledger additions (continues at 57)

57 original-identity sweep proof: after the identity-erasing rename,
sweep uses record.original_claim_name; decoy intent for a different
claim instance (same pending, different BY/SEED/AT) survives; exact
full-name matching asserted.
58 owner-win orphan record: unreferenced recover record is report-only
online; quiescent sweep collects it; a fresh ceremony on the same
claim supersedes its own prior record; no cross-ceremony deletion.
59 recovery-actor death/takeover: recover-recover-claim completes the
exact persisted transaction; new target/content/reason rejected;
takeover identity durably recorded (scratch discipline); crash
schedules prove record strictly outlives state and
state-without-record fails closed as damage.
60 recovery-record lifecycle + crash injection at every boundary of
steps 2-6: live-while-state-exists; removal order state→record→
takeover-record; record-present/state-absent triage (CLAIMED present ⇒
resume-or-supersede; completion evidence ⇒ cleanup only).

State: round-10 revision returned for review. Implementation remains
UNAUTHORIZED.

## Round 11 (architecture_alternative_review 15-46-03Z) — pivot analysis: filename machine vs flocked JSON vs SQLite

NOTE: this review crossed with my round-10 reply (15-46-13Z). Rounds
7-10 are hereby marked CONTINGENT: they are the completed record of the
filename-machine design and are SUPERSEDED if the pivot below is
adopted. Their INVARIANTS survive regardless (effectively-once
publication, redelivery-never-recreation, original-identity
preservation, human-authorized recovery, fail-closed everywhere).

### (1) Recommendation: SQLite. Independent reasoning, not deference.

The decisive evidence is the trajectory of rounds 7-10 themselves:
every review round of the filename machine added a state (REPLYING,
CLOSING, RECOVERING-CLAIM), a record type (intent, .published,
recover-*, takeover-*), a temp discipline (.tmp, .scratch), a GC class,
or a NAME_MAX term. Each crash window fixed spawned a sibling. When a
design needs a per-state recovery decision table and states beget
states (RECOVERING-CLAIM begat takeover records), the abstraction is
wrong: we were hand-writing a transactional database and its WAL in
filename grammar. The correct move is a transaction authority; then
reply/close/recover collapse to single atomic transactions and entire
artifact classes cease to exist.

Why SQLite over flocked whole-file JSON (the honest middle option):
- Flocked JSON is STILL a custom database: we own the lock-inode
  lifecycle (the unlink/reopen flock hole is a classic bug class), the
  replace/fsync protocol, versioning, integrity checking, and a
  compaction ceremony once the single file grows. Smaller custom
  database, but custom.
- Per-row conditional UPDATE gives single-winner claims without
  serializing every operation behind one global lock; probed live:
  rowcounts 1/0 on stdlib sqlite3 (SQLite 3.46.1 on this host's 3.13;
  WAL + synchronous=FULL confirmed; sqlite3 has shipped in every
  CPython ≥ our 3.11 floor).
- integrity_check, user_version, busy_timeout, and crash recovery are
  battle-tested; the flocked-JSON equivalents would be ours to prove
  under crash injection forever.
- Bounded SQL GC + an append-only transitions ledger replace filename
  history with a BETTER audit trail.
- Whole-file JSON keeps one real advantage — cat/jq inspection — which
  `baton scan`/`baton dump`(new) and doctor cover.
Falsifiable grounds to revisit: if concurrent BEGIN IMMEDIATE claims
ever double-win, if synchronous=FULL commits are lost under kill -9
storms on a supported local fs, if the WAL-watch wait provably misses
commits, or if target Pythons lack the sqlite3 module, the
recommendation fails and flocked JSON is the fallback (filename machine
stays third).

### (2) Atomicity/durability model

DB opened with journal_mode=WAL, synchronous=FULL, foreign_keys=ON,
busy_timeout (~10s); every write in explicit BEGIN IMMEDIATE.
- claim: UPDATE messages SET state='claimed', claim_actor=?,
  claim_seed=?, claim_at=? WHERE id=? AND state='pending' AND
  to_participant=?; rowcount=1 is THE winner (no filename rename).
- reply: ONE transaction — verify claim ownership row; INSERT the
  fixed outgoing message (transient body inline, durable content as
  hash-pinned external reference); UPDATE incoming → 'completed' with
  responds_to linkage. Crash before COMMIT = nothing visible, retry
  re-executes; after = both visible. The REPLYING apparatus, intent
  records, .published tombstones, and consumed-tail proofs all
  disappear — SQLite's commit IS the commit point.
- content-bearing close: ONE transaction recording outcome/detail
  reference and completing the claim. Terminal-signoff contract kept
  first-class; bodyless close is the same transaction minus content.
- External details: unchanged discipline — scratch → fsync →
  no-clobber rename → dirfsync in work/<dest>/ COMMITTED FIRST; the DB
  transaction then publishes the reference. Crash between leaves an
  unreferenced detail (doctor class unchanged); a visible reference to
  missing/mismatched content remains impossible.
- recover-claim: ONE transaction — INSERT recovery-audit row (actor,
  seed, reason or pinned reason reference, original claim identity —
  which the messages row preserves FOREVER, solving round-10's
  identity problem structurally) + UPDATE claimed → pending. A dead
  recovery actor mid-ceremony leaves NOTHING (uncommitted transaction
  evaporates), so recover-recover-claim and takeover records are
  unnecessary — the recursion terminates at depth zero.
- expire: ONE transaction deleting the notice and its seen rows
  (ruling-2 GC becomes a DELETE, race-free by transaction).
- instance move: quiesce ceremony + PRAGMA wal_checkpoint(TRUNCATE) +
  copy {baton.json, instance.json, mailbox.sqlite3}; uuid rules
  unchanged; -wal/-shm must be empty/absent after checkpoint or the
  move refuses.

### (3) wait/wakeup — query-arm-requery, no daemon

1. Run the eligibility query; on hit, claim in the same connection and
   return. 2. Arm inotify (IN_MODIFY|IN_CREATE|IN_MOVED_TO) on the
   instance dir filtered to mailbox.sqlite3 / -wal / -shm (every commit
   touches the WAL). 3. REQUERY to close the query→arm race. 4. Block
   on events; each event → requery; keep the existing safety-rescan
   interval (60s default) as defensive poll. Degraded (no inotify):
   pure interval polling — exact v5 behavior. No wakeup artifact
   needed: WAL modification is the signal. Test must prove a commit
   landing between step 1 and step 2 is claimed without waiting for
   the defensive poll.

### (4) Schema outline + versioning

- meta(k,v): mirrors instance uuid/generation/config_sha256 for
  cross-check against instance.json at open (both must agree;
  mismatch fails closed). PRAGMA user_version = protocol = 6.
- messages(id TEXT PK /32hex/, from_participant, to_participant, kind,
  thread_id, outcome, retention, body NULL, detail_root_id NULL,
  detail_path, detail_sha256, detail_size, detail_generation,
  created_ts, state CHECK IN
  ('pending','claimed','completed','closed','expired'), claim_actor,
  claim_seed, claim_at, responds_to REFERENCES messages(id),
  completed_at). Index (to_participant, state), (thread_id).
- notices(id PK, from_participant, kind, body/ref, created_ts, ttl,
  expired_at NULL); notice_seen(notice_id REFERENCES notices ON DELETE
  CASCADE, participant, actor, seed, seen_at, PK(notice_id,
  participant, actor)).
- recoveries(id PK, message_id, prev_state, new_state, recovery_actor,
  recovery_seed, reason, reason_ref-cols, created_ts).
- transitions(seq INTEGER PK AUTOINCREMENT, message_id, from_state,
  to_state, actor, seed, at) — append-only ledger; RECOMMENDED (cheap,
  strictly better audit than filenames ever gave).
- Participants/roots stay config-owned in baton.json (validated at
  operation time against config) so regen does not migrate the DB.
- Versioning: user_version gate at open; unknown → fail closed;
  migrations are an explicit quiesced `baton migrate` ceremony;
  protocol integer (ruling 8) = schema contract version.

### (5) Test matrix + distribution implications

Tests 32-60 (the filename-transaction crash/race corpus) are
SUPERSEDED. Replacement core: (a) N-process concurrent claim → exactly
one winner; (b) reply/close exactly-once under kill-at-arbitrary-point
— post-crash state is all-or-nothing, retry safe; (c) detail-first
ordering — unreferenced detail reportable, dangling reference
unconstructible; (d) wait arm-race commit not missed + degraded
polling parity; (e) user_version gate + migrate-under-quiesce only;
(f) doctor integrity_check + corrupted-db fail-closed; (g) quiesced
move with checkpoint, -wal/-shm empty, uuid preserved; (h)
recover-claim single transaction + audit row + identity preserved +
dead-ceremony-leaves-nothing; (i) expire+seen single transaction;
(j) bootstrap probes: python floor AND sqlite3-module-missing → clean
exit-code diagnostics; (k) unsupported/network fs fail-closed at open
(statfs; SQLite-on-NFS explicitly refused); (l) strict JSON unchanged
for config/stamp/references; (m) NAME_MAX shrinks to external detail
filenames (existing convention) — the mailbox name-budget apparatus
disappears; actor/address grammar validation retained at config/open.
Retained from the approved matrix: 1-4 (config/init/regen/open), 10-12
(roots/containment/reference pinning), 13-14 (transient/notice
semantics, now SQL), 18-20 (doctor inventory, wait parity, instance
move), 26-31 (zipapp packaging + new sqlite3 probe).
Distribution: zipapp unchanged (sqlite3 is stdlib); DISTRIBUTION
manifest adds schema/protocol version; python_min stays 3.11;
document the sqlite3 CLI or ship `baton dump` for human inspection
(3.11 lacks `python -m sqlite3`, which arrived in 3.12 — ship dump).

### (6) What the central store cannot meet

- `ls`-glance mailbox state and per-envelope files in git are lost;
  scan/dump/doctor and the external detail files (which remain plain
  reviewable .md) cover the substance. No locked v6 goal fails.
- Rulings previously expressed in filename mechanics (ruling 5's
  "mini filesystem database", rounds 7-10 deltas) need re-locking as
  transaction-level invariants; dirfd/no-follow (ruling 6) still
  governs instance/config/roots/details; crash injection (ruling 7)
  targets OUR seams (detail-vs-DB ordering, wait, move, migrate), not
  SQLite internals.

State: pivot analysis returned — RECOMMEND SQLite; round-10 filename
delta stands only as the contingent alternative record. Implementation
remains UNAUTHORIZED.

## Round 12 (architecture review 15-50-20Z) — central-resource revision

Pivot directionally accepted by review; all eight points incorporated —
none successfully challenged. Point 5 is conceded as MY error: SQLite
WAL permits one writer at a time and BEGIN IMMEDIATE serializes writes;
the conditional predicate provides single-winner CORRECTNESS, not
writer concurrency. Readers proceed on WAL snapshots; writers serialize
with bounded busy_timeout — acceptable at mailbox volume, now
documented accurately.

### Revised schema (STRICT tables where supported; capability-probed at open)

- instance_meta(one_row INTEGER PRIMARY KEY CHECK(one_row=1),
  uuid TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=1),
  config_sha256 TEXT NOT NULL, protocol INTEGER NOT NULL,
  maintenance INTEGER NOT NULL DEFAULT 0 CHECK(maintenance IN (0,1)),
  maintainer_actor TEXT, maintainer_reason TEXT, created_ts TEXT)
  — typed singleton, replaces meta(k,v); cross-checked against
  instance.json at open.
- messages(id TEXT PK /32hex/, from_participant, to_participant, kind,
  thread_id, retention CHECK IN ('durable','transient'),
  body BLOB NULL, body_sha256 TEXT NULL,
  attach_root_id/attach_path/attach_sha256/attach_size/
  attach_generation NULL-group, created_ts,
  state CHECK IN ('pending','claimed','completed','closed','expired'),
  responds_to REFERENCES messages(id), completed_at)
  — message row owns DELIVERY STATE ONLY; no claimant columns.
- claims(claim_id TEXT PK /32hex/, message_id REFERENCES messages,
  actor, seed, claimed_at,
  state CHECK IN ('active','completed','recovered'), terminal_at)
  + PARTIAL UNIQUE INDEX ON claims(message_id) WHERE state='active'
  — immutable per-attempt identity; recover→reclaim yields a NEW row;
  history is never rewritten.
- dispositions(claim_id REFERENCES claims UNIQUE, kind
  CHECK IN ('reply','close'), outcome, body BLOB NULL, body_sha256,
  response_message_id REFERENCES messages NULL, created_ts)
  — the ≤1-terminal-disposition-per-claim invariant is a CONSTRAINT,
  not app logic.
- notices / notice_seen (as round 11, FK CASCADE).
- recoveries(recovery_id PK, claim_id REFERENCES claims, actor, seed,
  reason, reason_ref-cols, created_ts).
- transitions(seq INTEGER PK AUTOINCREMENT, entity CHECK IN
  ('message','claim'), entity_id, from_state, to_state, actor, seed,
  at) — populated by AFTER-UPDATE/INSERT TRIGGERS on
  messages.state/claims.state, so NO code path can skip the ledger;
  BEFORE UPDATE/DELETE triggers on transitions RAISE(ABORT) —
  immutable under ordinary connections; only the migration ceremony
  (which rebuilds schema under the maintenance gate) may restructure.

### Content authority (point 1)

- Baton-GENERATED response/disposition bodies are canonical IN the DB:
  stored (with sha256) in the SAME transaction that publishes the
  outgoing message and completes the claim. The finding-side .md is an
  idempotent PROJECTION: materialized post-commit from exact stored
  bytes (best-effort at command exit; `baton materialize <id>` re-emits
  any time); never required to claim/read/reply; its absence after a
  crash is cosmetic, not protocol state.
- Pre-existing separately-authored evidence stays an external
  hash-pinned ATTACHMENT: committed/fsynced BEFORE the transaction,
  referenced with {root_id, path, sha256, size, generation}; it is an
  attachment, never the message authority.
- CLI distinguishes them: body via stdin/--body (canonical, projected)
  vs --attach root:path (external attachment). The old --destination
  detail flow becomes the projection target.

### Transactions (each ONE BEGIN IMMEDIATE)

- claim: INSERT claims(active) + UPDATE messages pending→claimed
  (predicate rowcount=1); partial unique index backstops the race.
- reply: verify active claim by claim_id+actor+seed; INSERT outgoing
  message; INSERT disposition(kind='reply', body,
  response_message_id); UPDATE claim active→completed; UPDATE incoming
  claimed→completed. Ledger rows via triggers.
- close: same shape, kind='close', no outgoing message (or with
  outcome only); bodyless close = disposition row with NULL body.
- RETRY idempotence (point 3): reply/close on a claim first SELECTs
  dispositions by claim_id. Found ⇒ validate supplied
  routing/kind/outcome/content-hash against the committed row: match ⇒
  report already-committed + re-materialize projection (redelivery);
  mismatch ⇒ fail closed. Not found ⇒ crash was pre-COMMIT ⇒ nothing
  visible ⇒ execute normally. Effectively-once falls out of the
  UNIQUE(claim_id) constraint.
- recover-claim (point 4): UPDATE claims active→recovered (exact
  claim_id) + INSERT recoveries + UPDATE messages claimed→pending.
  Later claimants insert NEW claim rows; no history mutation; a dead
  ceremony pre-COMMIT leaves nothing.
- expire: DELETE notice + seen rows, one txn.

### wait/eventing (Slawomir pin incorporated)

Notification is never authority. Sequence: eligibility query → arm
inotify on the INSTANCE DIRECTORY (IN_CREATE|IN_DELETE|IN_MODIFY|
IN_MOVED_TO filtered to mailbox.sqlite3, -wal, -shm — the -wal inode
is created/deleted/reset by checkpoints, so the watch is on the
directory, never a single inode) → eligibility REQUERY (closes the
query/arm race) → block; every relevant event is only a prompt to
requery the store; 60s safety rescan retained; degraded mode = pure
polling. Waiters also observe instance_meta.maintenance on each
requery and stand down cleanly.

### Maintenance/quiescence gate (point 7 — enforceable, not social)

Enter: one txn sets maintenance=1 + maintainer identity/reason. Every
ordinary operation checks the flag FIRST inside its transaction and
stands down with a clean diagnostic; waiters exit on next
requery/rescan. Maintainer then drains: BEGIN EXCLUSIVE (succeeds only
with no competing locks) + wal_checkpoint(TRUNCATE) which MUST return
non-busy — refusal with active readers is part of the proof — then
copy/migrate with -wal/-shm verified empty/absent, then clear the
flag. A dead maintainer leaves a stale flag: cleared only by a
human-authorized ceremony (same philosophy as claim recovery, audited
in recoveries). Instance move is RETAINED under this gate (it was a
locked v6 goal), now enforceable rather than asserted.

### Open validation (point 8)

At every open, before any mutation: SQLite library capability probe
(version + STRICT support; fail closed if absent), PRAGMA user_version
== 6, schema-object validation against the expected DDL set,
foreign_key_check, quick_check (integrity_check in doctor),
instance_meta ↔ instance.json cross-check, local-filesystem check.
Strict JSON continues to govern config/stamp/attachment references.

### Test ledger (supersedes the round-11 sketch)

T1 concurrent claim: N processes → exactly one active claims row;
partial-unique backstop verified under direct-SQL race.
T2 claim history: recover→reclaim = two immutable rows; original
identity intact; new claim_id issued.
T3 reply retry: commit-then-crash → retry discovers disposition,
validates identity, re-materializes, publishes NOTHING new;
content-mismatch retry fails closed; pre-commit crash → clean re-run.
T4 disposition uniqueness enforced at constraint level (second
reply/close rejected by UNIQUE, not app code).
T5 body authority: claim/read/reply function with projection file
absent; materialize is byte-exact; attachment mutation detected at
read via sha256; attachment committed before txn.
T6 ledger inseparability: direct-SQL state change still produces a
transitions row (trigger); UPDATE/DELETE on transitions aborts.
T7 maintenance gate: ops stand down; waiter exits; checkpoint refuses
with an open reader snapshot and succeeds after drain; stale-flag
human clear ceremony audited.
T8 eventing: commit in the query/arm window is caught by requery;
-wal delete/recreate across checkpoint still wakes the
directory-watcher; degraded polling parity; 60s rescan fires.
T9 writer serialization: two writers, busy_timeout honored, loser
gets a bounded clean retryable diagnostic; documented.
T10 open validation matrix: capability probe, user_version gate,
schema validation, foreign_key_check, quick_check, corrupted-db
fail-closed, meta/stamp mismatch fail-closed.
T11 recovery atomicity: audit row + claim recovered + message pending
in one txn; mid-ceremony death leaves nothing.
T12 expire+seen one txn; GC classes now SQL-bounded.
T13 bootstrap: python floor exit-2; missing-sqlite3 clean diagnostic.
T14 unsupported/network fs refused at open.
Retained: config/init/regen/open strict-JSON tests, roots/containment
(dirfd/no-follow), zipapp packaging + probes, scan/dump/doctor
observability.

State: round-12 revision returned. Implementation remains UNAUTHORIZED.

## Round 13 (central-store review 15-53-52Z) — checkpoint, retention, content, attribution, edges, cutover

All five items incorporated. Item 1 independently REPRODUCED on this
host before revising: `PRAGMA wal_checkpoint(TRUNCATE)` inside `BEGIN
EXCLUSIVE` raises OperationalError "database table is locked"; with an
active reader snapshot it returns (busy=1, log=1, ckpt=0); after the
reader drains, (0,0,0). The round-12 ceremony was invalid as written —
conceded.

### 1 (P1) Corrected maintenance/move ceremony

- Txn A (BEGIN IMMEDIATE): set instance_meta maintenance=1 +
  maintainer actor/seed/reason + fresh maintenance_token; COMMIT. This
  serializes behind all prior writers. The cross-filesystem copy lock
  is the FLAG plus cooperative refusal — never an open SQLite
  transaction.
- Cooperative refusal: every operation's first in-txn check; waiters
  observe on requery/rescan and stand down.
- Drain loop (NO open transaction): run `wal_checkpoint(TRUNCATE)`
  with bounded backoff until it returns busy==0 AND log==checkpointed;
  timeout → abort ceremony, flag stays set, human decision (audited
  clear ceremony as before).
- Verify: -wal truncated (size 0/absent) and stable on a re-check;
  maintainer closes ITS OWN connection too; verify again.
- Copy (move): dirfd/no-follow copy of {baton.json, instance.json,
  mailbox.sqlite3} to destination; fsync files + destination dir.
  Destination carries maintenance=1 in the copied DB: final step at
  the DESTINATION runs full open validation, then a txn clearing the
  flag. SOURCE is decommissioned by atomically replacing its
  instance.json with a tombstone {"moved_to", "moved_at",
  maintenance_token} so stale actors fail closed at open rather than
  resuming the old store. uuid/generation unchanged by move.
- Challenge answered: maintenance is NOT a general-purpose lock — it
  exists solely for move/migrate (the locked instance-move goal);
  no other command may take it.

### 2 (P1) Retention lifecycle in the DB model

- Transient scrub is part of the CONSUMING transaction: when a claim
  on a transient message reaches its terminal disposition, the same
  txn deletes the contents row and NULLs messages.content_id;
  messages.content_sha256 (always populated at insert) survives as
  the idempotence/audit anchor, along with routing, kind, outcome,
  and exact claim/disposition identity.
- Post-commit retry of a transient response after consumption reports
  the committed identity/hash ONLY — it cannot and does not promise
  bytes the retention contract erased. (Durable bodies remain
  materializable indefinitely.) v5 transient semantics are preserved,
  and the DB cannot grow ephemeral bodies.
- Bounded GC: `baton gc` deletes terminal-state message/claim/
  disposition METADATA rows older than config retention_days
  (config-owned; default 90) — EXCEPT: transitions and recoveries are
  retained PERMANENTLY. Why: they are the audit trail the ledger
  exists to provide, they are tiny fixed-width rows, and every
  deletion elsewhere remains explainable by them. FK from transitions
  to messages is relaxed to (entity, entity_id) soft reference for
  this reason — ledger rows must be deletable-never, even after their
  subject rows are GC'd.

### 3 (P2) One canonical content owner

Normalized immutable `contents(content_id PK /32hex/, body BLOB NOT
NULL, sha256 TEXT UNIQUE NOT NULL, size, created_ts)` with abort
triggers (immutable). messages.content_id → contents (NULL after
scrub or for bodyless); dispositions carry NO body: for kind='reply'
the disposition references response_message_id and a trigger enforces
dispositions.content_sha256 == the outgoing message's
content_sha256; for kind='close' the disposition owns content via its
own content_id. Exactly one canonical copy in all cases; retry
comparison is against contents.sha256 (or the surviving
content_sha256 after scrub); drift is unconstructible by constraint.

### 4 (P2) Mechanical ledger attribution + in-schema state graph

- `op_context` strict singleton (one_row CHECK(one_row=1), op_id TEXT
  NULL /32hex/, actor, seed, verb). Every write transaction's FIRST
  statement sets a fresh op_id + attribution; its LAST statement
  clears op_id — both inside the same serialized txn, so the row is
  never observably shared.
- BEFORE-UPDATE state triggers on messages/claims RAISE(ABORT) when
  op_context.op_id IS NULL → uncontextual direct-SQL mutation fails
  closed (it cannot silently emit unattributed rows). AFTER-UPDATE
  triggers insert transitions rows copying op_id/actor/seed/verb.
- The same BEFORE triggers validate the (old.state → new.state) edge
  against the legal transition set and abort otherwise — the state
  graph is ENFORCED in schema, not just logged.
- Direct SQL that deliberately sets op_context is attributed and
  legal; the doctor cross-checks transitions.op_id uniqueness per
  logical operation.

### 5 (P2) Event edges, library floor, cutover

- Waiter event handling: IN_Q_OVERFLOW → requery + rearm;
  IN_IGNORED (watch invalidation) → rearm, else degrade to polling;
  IN_DELETE_SELF / IN_MOVE_SELF / IN_UNMOUNT on the instance dir →
  full re-open validation (which sees a move tombstone or missing
  instance and fails closed) before any rearm; WAL/SHM
  create/delete/reset already covered by the directory watch. Events
  are hints ONLY in every case; the 60s rescan is the floor.
- SQLite library floor: EXPLICIT — SQLite >= 3.37.0 REQUIRED (STRICT
  tables); open fails closed on older libraries with a clean
  diagnostic. No "where supported" ambiguity anywhere in the design.
- Cutover proof (one-way): v5 doctor --assert-empty (every claim/
  pending/notice drained or closed) + all v5 waiters stopped; THEN
  protocol-6 init; v5 artifacts deleted per the approved ledger. NO
  dual reader/importer exists unless Slawomir separately approves
  one.

### Test-ledger delta (extends T1-T14)

T15 maintenance ceremony: checkpoint-inside-txn defect pinned as a
regression (must NOT be attempted); drain loop converges only after
reader/writer drain (busy==0, log==checkpointed, verified live on
this host as (1,1,0) → (0,0,0)); timeout leaves flag + audited human
clear; source tombstone fails stale actors closed; destination clears
flag only after full open validation; uuid/generation preserved.
T16 transient retention: scrub occurs in the consuming txn; sha256/
identity metadata survives; retry-after-consumption reports identity
without bytes; durable bodies still materialize; gc honors
retention_days and NEVER touches transitions/recoveries.
T17 content normalization: single contents row per body; reply
disposition/message hash-drift unconstructible (trigger); close owns
content; scrub deletes contents without breaking idempotence
comparison.
T18 attribution: direct SQL without op_context aborts; with context
succeeds attributed; illegal state edge aborts in-schema; op_id
cleared at txn end; ledger rows carry correct attribution under
concurrent writers.
T19 event edges: queue overflow, watch invalidation, dir
replace/unmount, WAL reset — each yields requery/rearm or clean
degradation; no missed-commit under each injected condition.
T20 floors: SQLite < 3.37.0 refused at open; python floor + missing
sqlite3 module diagnostics (merges T13).
T21 cutover: protocol-6 init refused while v5 state undrained; no
dual-reader path exists.

State: round-13 revision returned. Implementation remains UNAUTHORIZED.

## Round 14 (central-store review 15-57-39Z) — single authority completed

### 1 (P1) instance.json DELETED — the DB owns all mutable instance state

- instance_meta owns: uuid, protocol/schema version (with PRAGMA
  user_version), ACCEPTED config generation + canonical digest,
  maintenance, move_status/move_token/moved_to. baton.json remains the
  explicit external policy INPUT only.
- No locator file is needed: the instance dir is the directory of the
  explicitly passed --config; the DB is <dir>/mailbox.sqlite3 by
  convention. init creates the DB and refuses over an existing one;
  open with config-but-no-DB fails closed with a distinct diagnostic.
  instance.json is deleted from the design — no bootstrap file, so
  nothing to prove immutable.
- regen becomes ONE transaction: validate the new strict-JSON config,
  require generation == accepted_generation + 1, update
  instance_meta.accepted_generation/digest. The impossible cross-file
  atomicity requirement disappears.
- Move decommissioning is expressed IN the source DB:
  move_status ∈ ('none','moving','moved'). Order: (1) source txn sets
  maintenance=1 + move_status='moving' + fresh move_token; (2) drain +
  checkpoint (round-13 ceremony); (3) copy DB + baton.json; the copy
  starts gated with move_status='moving'; (4) DESTINATION activation
  txn after full open validation: move_status='none', maintenance=0,
  activation audited with the token; (5) SOURCE decommission txn:
  move_status='moved', moved_to recorded, maintenance stays 1 forever;
  ordinary ops on a 'moved' store fail closed pointing at moved_to.
- At-most-one-eligible proof across crash points: before copy → only
  source (gated, recoverable); after copy before activation → ZERO
  active (both gated) — recovery is human-authorized either way;
  after activation before decommission → exactly destination active,
  source still gated. The stale-flag-clear hazard is closed by making
  the clear ceremony on a 'moving' store REFUSE by default: it
  requires --abort-move naming the exact move_token plus an operator
  attestation that the destination copy is destroyed — audited in
  recoveries. Cross-filesystem activation/decommission cannot be
  atomic; the design guarantees at-most-one-ACTIVE at every crash
  point and makes both recovery paths explicit, audited, and
  default-refuse. init/regen/open/doctor/move/cutover tests refreshed
  (T22 below).

### 2 (P1) Per-owner content rows — dedup removed

contents loses UNIQUE(sha256) (kept as a non-unique index). Every
logical message/close body gets its OWN immutable content row; no
cross-message sharing exists, so transient scrub in the consuming
transaction clears the sole owning reference and deletes exactly that
row — no FK violation, no erasure of live content, no refcount model.
"Immutable" is clarified in-schema: BEFORE UPDATE ON contents always
aborts (bodies never mutate); BEFORE DELETE aborts unless
op_context.verb is an authorized deleting operation
('consume_transient', 'gc') — deletion by the retention contract is
not mutation. Retention scope pinned: gc applies to transient/expired
terminal metadata; durable messages and their contents are permanent
record (v5 durable semantics preserved).

### CLI contract pin

Generated body and separately-authored attachment remain strictly XOR
at the CLI (stdin/--body vs --attach; both = error), exactly as locked
in the earlier v6 design. The schema's ability to hold both is NOT a
surface change; expanding to body-plus-attachment requires Slawomir's
explicit approval.

### Consolidation

The single current implementation plan now lives at
work/finding-baton-portable-coordination/PLAN.md (rewritten this
round). It supersedes reconstruction from rounds 1-14; rounds remain
as history. PLAN.md carries: architecture + invariants, full schema,
transaction inventory, ceremonies, CLI surface, open validation,
eventing, packaging/distribution, cutover, and the consolidated test
matrix (T1-T23 below).

### Test-ledger delta

T22 single-authority instance lifecycle: init/open/regen/doctor with
no instance.json; regen one-txn generation acceptance; config-but-no-
DB and DB-but-no-config diagnostics; move crash matrix at every
boundary proving at-most-one-active; abort-move requires token +
attestation and is audited; 'moved' store refuses ordinary ops with
moved_to diagnostic.
T23 per-owner content: identical bytes in two messages produce two
content rows; transient scrub deletes only its own row with a live
durable twin intact; UPDATE on contents always aborts; DELETE outside
authorized verbs aborts; body-XOR-attachment enforced at CLI (both
supplied = error).

State: round-14 revision + consolidated PLAN.md returned.
Implementation remains UNAUTHORIZED.

## Round 15 (consolidated-plan review 16-03-17Z) — final plan deltas, all in PLAN.md

All five incorporated directly into PLAN.md (the sole implementation
source; rounds historical). Item 2's proposed open shape independently
PROBED on this host before adoption: dirfd O_DIRECTORY|O_NOFOLLOW +
O_NOFOLLOW existing-only DB open + file:/proc/self/fd/<fd>?mode=rw
canonicalizes to the real DB with -wal/-shm beside it; triggers work
under trusted_schema=OFF; missing DB refuses at the OS open; symlink
refuses ELOOP. Added my own hardening: post-connect identity
verification — PRAGMA database_list's canonical path must fstat to the
SAME (st_dev, st_ino) as the held fd with the held dirfd as parent —
closing the /proc round-trip; if any platform breaks the sidecar
invariant, STOP for Slawomir (no pathname-validation fallback).

Deltas landed: (1) extraction purity as an architecture requirement —
tools/baton/ semantically Drift-free, neutral fixtures, Drift-specific
config/policy outside the reusable package, extraction = path/build
changes only; (2) the fd-anchored SQLite open boundary section; (3) the
full connection/durability contract restored (WAL established at init,
per-connection set/verify of WAL + synchronous=FULL + foreign_keys=ON +
busy_timeout + trusted_schema=OFF, existing-only operational opens,
read-only connection class, validation-precedes-mutation, reopen
refusal on mode/schema drift, and the explicit clarification that
transient scrub is protocol retention, not forensic page/WAL erasure);
(4) ledger birth events via AFTER-INSERT triggers (NULL→pending /
NULL→active), uncontextual INSERT fails closed, context-bearing direct
INSERT logged, gc emits final ledger events before deletion; (5) the
complete move proof in T15 (restart/resume at every boundary,
idempotent-by-token activation/decommission, abort-move refusal rules,
same-UUID copy never generically clearable). New tests T24-T26
(fd-anchored open matrix, ledger birth/GC events, extraction purity in
an isolated checkout).

State: PLAN.md is implementation-ready pending Slawomir's
authorization. Implementation remains UNAUTHORIZED.

## IMPLEMENTATION AUTHORIZED (review-2026-08-06T18-23-03Z)

Slawomir authorized implementing the consolidated PLAN.md (red-first
T1-T26, approved edit ledger, one protocol, extraction-ready, no v5
compat). Cutover of the ACTIVE mailbox is explicitly deferred and
coordinated — therefore v5 files (baton_v5.py, test_baton_v5.py,
launcher, README, AGENTS-MAILBOX-PROTO.md) remain UNTOUCHED until the
drain/cutover ceremony; v6 lands as NEW files beside them
(tools/baton/baton_v6.py + test_baton_v6.py + example config +
DISTRIBUTION + build_zipapp.py), and the ledger's REPLACE/DELETE/
REWRITE items execute at cutover.

Handoff plan (bounded, per authorization):
1. Storage core: strict JSON + config validation, init, fd-anchored
   open + connection contract + schema/integrity validation, op_context
   + triggers + ledger, send/claim/reply/close + retry idempotence +
   transient scrub (tests T1-T6, T9, T10, T17, T18, T23, T24, T25).
2. Notices/see/expire, recover-claim, regen, gc, attachments
   (T2, T11, T12, T16, T22-nonmove).
3. Maintenance/move/migrate ceremonies (T7, T15, T22-move).
4. wait/eventing + doctor/scan/dump/materialize + CLI (T8, T19).
5. Packaging: zipapp + bootstrap floors + DISTRIBUTION + extraction
   purity harness (T13/T20, T26); cutover prep docs (executed at the
   coordinated ceremony).

Status: Handoff 1 in progress.

## Handoff 1 delivered — storage core green

NEW files (v5 untouched, live channel preserved):
- tools/baton/baton_v6.py (~1000 lines): strict JSON (dup-key /
  non-finite / trailing / exact-type / unknown-field rejection,
  canonical digest); config validation (addresses, singleton actors,
  roots, retention_days); statfs local-fs allowlist; fd-anchored
  SQLite open (dirfd O_DIRECTORY|O_NOFOLLOW, existing-only O_NOFOLLOW
  child, file:/proc/self/fd URI mode=rw, post-connect dev/inode +
  parent identity verification); connection contract (WAL-established-
  at-init + per-connection verify/set of WAL, synchronous=FULL,
  foreign_keys, busy_timeout, trusted_schema=OFF); full schema (10
  STRICT tables, 5 indexes incl. the one-active-claim partial unique,
  22 triggers: op_context guards, birth+transition ledger events,
  in-schema state graphs, frozen columns, contents/dispositions/
  ledger/recoveries immutability, authorized-verb content deletion);
  exact sqlite_master schema validation + user_version gate +
  foreign_key_check + quick_check + instance_meta cross-check +
  maintenance/move gates at open; operations: send (body XOR attach,
  transient cap), claim (conditional-update single winner), reply /
  close_claim (one transaction each: contents + outgoing + disposition
  + claim/message completion + transient scrub) with round-12 retry
  idempotence (committed-disposition discovery, identity validation,
  redelivery; mismatch fails closed); reads: get_message/get_claim/
  scan. Exit-code table 0/2/3/4/5/6/7 documented.
- tools/baton/test_baton_v6.py: 49 tests, ALL GREEN — neutral acme/hq
  fixtures (extraction purity test greps the module for host-project
  strings and passes); init/open matrix (no-clobber, no-autocreate,
  symlink DB/config refusal, journal-mode/user_version/schema-drift/
  corruption/config-drift refusals, readonly class, sidecar location);
  strict-JSON matrix incl. Bool-is-not-Int; 8-process claim race with
  exactly one winner + partial-unique direct-SQL backstop; reply/close
  retry redelivery + three mismatch classes failing closed +
  failed-txn-leaves-nothing + wrong-owner + constraint-level second
  disposition; transient scrub in the consuming txn with sha anchor
  surviving + durable retention + per-owner no-dedup rows + contents
  immutability/authorized-verb deletion; ledger birth+transition
  events with attribution, uncontextual INSERT/UPDATE aborting,
  context-bearing direct SQL logged, illegal edges aborting,
  append-only ledger, frozen claim columns; bounded busy diagnostics.

Covers: T1, T3, T4, T5(core), T6, T9, T10, T16(core), T17, T18,
T23, T24, T25, T26(grep part). Remaining per plan: handoffs 2-5.
Status: awaiting review of Handoff 1; Handoff 2 (notices/recovery/
regen/gc/attachments) next.

## Review pause honored (18-37-15Z)

Reviewer paused edits: the handed-off module grew 978→1276+ lines
mid-review (my Handoff-2 code landed in the same file). FROZEN at
baton_v6.py 1303 lines / sha fdca6cae…, test_baton_v6.py 533 lines /
sha 73616263…; 49/49 green at the boundary. Process rule adopted:
reviewed files stay byte-stable from handoff until reply/close;
follow-on work isolates in non-overlapping files or queues. Handoff-2
code is in-tree (triggers, attachments, notices, recover_claim, gc,
regen, _txn_begin validation); Handoff-2 tests NOT yet written —
queued behind the Handoff-1 review.

## Combined handoff (review-round-1 fixes + Handoff-2) delivered

All eight Handoff-1 findings fixed + Handoff-2 completed with tests;
81/81 green. Fixes: (1) transaction-time gates — _txn_begin re-reads
instance_meta after BEGIN IMMEDIATE and enforces protocol/generation/
digest/maintenance/move against THIS handle's config, with ceremony
modes (regen validated as accepted+1 at txn time; stale handles get
EXIT_GATED "reopen"); (2) transient close commits hash-only identity,
zero retained bytes, cap enforced; (3) config opened existing-only
O_NOFOLLOW RELATIVE to the held instance dirfd and read through the fd
— no re-resolution window, same dir identity binds config and DB;
(4) _txn_begin rolls back on every post-BEGIN failure (never strands;
op_context remains NULL; store reusable — pinned with the reviewer's
own rejecting-trigger probe); (5) content_id scrub requires context +
consuming verb + old-non-NULL; completed_ts/terminal_ts rewrites
require terminal verbs; messages DDL gains exactly-one
CHECK((content_sha256 NOT NULL)+(attach_root_id NOT NULL)=1) and
attach NULL-group consistency; send/reply enforce exactly-one at API;
(6) crash-atomic init — unique .init-* scratch 0600, transactional
schema+user_version, checkpoint+validate+fsync, hardlink no-clobber
publish, dirfsync; stale scratch never blocks; partial-final refused;
(7) retry validation adds thread routing and treats a missing
committed response row as EXIT_DAMAGE; (8) v5 response-retention
override surface preserved (explicit override, inherit default —
v5 respond() line 760 audited).
Handoff-2 ops + tests: notices (see dedupe per participant/actor,
author early-expire, TTL sweep, one-txn CASCADE cleanup), recover_claim
(immutable history: recover→reclaim two rows, reason required,
recovered claim cannot reply), gc (aged transient metadata with 'gc'
ledger events; durable/recovery-referenced/responds_to-referenced
spared; ledger+recoveries permanent), attachments (containment,
symlink/escape refusal, hash pin, claim-time mutation DAMAGE), regen
(one-txn accepted-generation +1, stale-handle refusal after regen).
Boundary: baton_v6.py + test_baton_v6.py FROZEN from this handoff
until the reviewer replies (hashes in the baton message).

## Revision 2 delivered (round-2 findings 1-7) — 111/111 green

(1) dispositions gain an immutable retention column: reply AND close
take the v5 optional override (inherit default); effective retention
decides retained-body vs hash-only; retry compares it (pins: override
both directions, body lifetime, mismatch, invalid). (2) notices store
author_actor/author_seed; early expiry requires the exact
participant+actor+seed triple; TTL defaults to 86400 and MUST be
positive-finite at API and CHECK level (immortal unconstructible);
notices/notice_seen immutability + insert-context triggers. (3)
recover_claim requires a config-declared SINGLETON participant; audit
+ op_context/transitions carry participant+actor+seed; regen requires
singleton; gc any configured participant (split documented as
challengeable). (4) regen is live-state-aware: refuses removing
participants named by pending/claimed messages or live notices;
accepted_roots table (seeded at init, rewritten only by regen)
preserves referenced root mappings immutably — remap refused while
retained attachments reference the root; verify_attachment enforces
attach_generation + accepted-mapping identity. (5) triggers are now
row-shape/edge-based: completed_ts only with claimed→terminal (or
NULLed on recovery re-pend), terminal_ts only with active→terminal,
scrub only on terminal transient — context-bearing wrong-row probes
pinned. (6) init: fault-injection seams (_FAULT_HOOK) + 5-point
subprocess kill matrix proving absent-or-valid + retry safety;
checkpoint tuple checked (busy==0, log==checkpointed). (7) roots
validated as canonical absolute no-follow directories at
init/regen/open; attachment hashing double-fstats (size/mtime_ns)
with a mid-hash mutation pin via the seam.
Frozen boundary hashes in the handoff message; files stable until
reviewer reply.

## Guidance corrections applied (18-53-28Z, crossed with revision 2)

The two corrections landed on top of revision 2 — 116/116 green:
1. Authority is an EXPLICIT config capability, never endpoint
   cardinality: per-participant `capabilities` list (strict-validated,
   values {"recovery","config"}); recover_claim requires "recovery",
   regen requires "config"; an agent participant WITH a declared
   capability is legal (pinned), a singleton WITHOUT one is not
   authority; no "human" or workflow role is hard-coded anywhere in
   the reusable tool — the host config decides.
2. attach_generation now identifies the ROOT BINDING: accepted_roots
   = {root_id, path, binding_generation}; unchanged roots RETAIN their
   binding generation across regen (pinned: an unrelated
   participant-adding regen does not invalidate old attachments);
   new/remapped roots stamp the new generation; publication records
   the binding generation from accepted_roots; verification requires
   current binding to match root id + path + binding generation
   (tamper pin = damage). Remap/removal of referenced bindings stays
   refused.

## Revision 3 delivered (round-3 findings 1-6 + wording) — 133/133 green

(1) reply normalizes effective recipient/thread from the incoming row
BEFORE disposition lookup; retry validates them unconditionally (None
= inherit on both paths, never wildcard); participant validation on
the fresh path only. (2) Bidirectional coupling as table CHECKs:
(state IN ('pending','claimed')) = (completed_ts IS NULL) on messages;
(state='active') = (terminal_ts IS NULL) on claims; missing-timestamp
transitions and prefilled births both rejected at constraint level.
(3) gc is a retention-graph fixpoint: aged transient terminal
candidates minus recovery-anchored, iteratively minus anything
anchored by a retained responds_to child or a retained claim's
disposition response reference; deletion dispositions→claims→messages
children-first via reference-derived topological order (timestamp
ties broke the first attempt — order now derives from responds_to
itself)→contents. Pinned contract: a transient response anchored by a
RETAINED durable disposition stays retained as metadata; all-transient
chains collect fully; gc never aborts on a valid graph; retry after
gc fails clean ("unknown claim"); ledger permanent. (4) The no-follow
boundary is a component walk from an opened "/" dirfd shared by
instance dir and roots; intermediate-symlink negatives for both.
(5) notice_seen: unconditional update-abort + expire/gc-only delete
triggers (cascade compatibility asserted); recoveries gains an
insert-context guard — uncontextual forged recovery rows abort.
(6) Attachment snapshot compares (dev,ino,mode,size,mtime_ns,ctime_ns)
across the held fd — the same-size/restored-mtime probe is pinned
(ctime detects it) — and the leaf opens O_NONBLOCK so FIFOs reject
before blocking (plus a test-side FIFO cleanup so host tmp scanners
never meet it). Docstrings now say capability-authorized.

## GC correction (plan-review 19-09-02Z, crossed) folded in — 135/135

Anchor rule (c) added: a candidate whose OWN disposition is durable is
retained — a durable close on a transient envelope keeps its
disposition body (pinned both ways: durable close retained through gc
with body intact; transient close still collected). The topological
half of the correction was already in revision 3 (reference-derived
children-first order + fail-closed on cycles).

## Final storage-core boundary (post 19-21-49Z review)

The review's P1 (durable-close anchor) was already fixed at the newer
boundary it observed (eb74f2…); this snapshot adds the remaining
items: the durable-close pin now also asserts message row, claim
state, retained content bytes, AND retry identity
(already_committed=True) survive gc; the stale created-DESC comment is
replaced with the graph-order description + fail-closed note; the last
"its human" wording removed from _require_capability. 135/135 green.
Boundary: baton_v6.py 1690 / c3dbc1ec…, test_baton_v6.py 1580 /
f0a08ad1… — frozen; later phases will NOT touch these files (new
files only) per the standing process rule.

## PHASE SIGNED OFF (review 19-24-44Z)

Storage/notices/recovery/GC phase ACCEPTED at baton_v6.py 1690 /
c3dbc1ec…, test_baton_v6.py 1580 / f0a08ad1… — reviewer-independent
py_compile + 135/135. All contract points confirmed: durable-
disposition anchors, graph-order deletion with fail-closed cycles,
reply-retry effective defaults, terminal row-shape CHECKs,
component-walk no-follow, immutable receipts/recovery birth context,
ctime snapshot detection, nonblocking non-regular rejection, explicit
capabilities, root binding generations.

NEXT PHASE (per PLAN): maintenance/move/migrate ceremonies (T7, T15,
T22-move). Handoff rule: since these extend the same module, the
handoff waits until the edit/test cycle is COMPLETE — no intermediate
boundary claims.

## Maintenance/move/migrate phase delivered — 149/149 green

New in this cycle (single complete edit/test cycle per the stability
rule): ceremonies audit table (immutable, insert-context-guarded, six
kinds); instance_meta CHECKs (moving/moved ⇒ maintenance;
move_status='none' ⇔ token NULL); ceremony open mode (_for_ceremony)
that still runs full validation but admits gated instances;
maintenance_enter (config capability + reason; optional move=True
minting the token; double-enter refused), maintenance_exit
(DEFAULT-REFUSES on a moving store — completing or aborting the move
are the only exits, so a copied same-UUID instance can never be
generically un-gated), checkpoint_drain (no-txn TRUNCATE loop, busy==0
AND log==checkpointed, bounded backoff, fail-with-flag-set on
timeout), move_copy (requires the move gate; drain + WAL-truncated
verification; scratch→fsync→no-clobber-hardlink→dirfsync copy of
config+DB into a component-walk-validated destination; copy starts
gated), move_activate / move_decommission (exact-token, audited;
destination clears its gate only after full open validation; source
becomes 'moved' forever pointing at moved_to), abort_move (exact
token + destination-destroyed attestation + reason, audited),
migrate_instance (capability + maintenance-gated entry point that
honestly reports no migration path from protocol 6). Tests: T7
maintenance matrix incl. ceremony-row immutability; drain
reader-blocks-then-converges; T15/T22-move full happy path with
uuid-identity preservation, zero-active window pins, generic-clear
refusal on both copies, token/attestation refusals, populated-
destination refusal, plain-vs-move gate distinction, crash-window
recovery via activate; migrate gate pins.
Boundary: hashes below; frozen from the handoff message until reply.

## Move-ceremony rework delivered (round-4 findings + resume refinements) — 156/156

(1) Role/peer state machine: instance_meta gains move_role
('source'/'destination') + move_peer (the canonical absolute
DESTINATION CONFIG PATH, one unambiguous meaning everywhere), bound
atomically with the token at maintenance_enter(move=True,
destination=…) BEFORE any copy; the copy inherits role='source' and
can never activate; move_bind_destination (new audited ceremony)
verifies the copy physically resides at the bound peer (parent-inode +
basename) and flips only it to 'destination'; activation requires
moving+destination+token; decommission requires the TRUE source
(role + NOT-resides-at-peer, since a copy inherits role bytes) with
moved_to == bound peer; abort is source-only with the same physical
check — a destination copy can never ungate itself. The reviewer's
three-authority repro is the headline pin: exactly one of
source/bound-copy/rogue-copy can ever activate through the API.
(2) Exact-token idempotence: bind/activate/decommission retries
discover the immutable committed ceremony row and return
already_committed; move_copy is stage-aware — pre-bind retries demand
byte/digest equality with the held drained source, post-bind and
post-activation retries discover the committed stage from the
destination's ceremony/token/UUID history; unexplained artifacts and
foreign UUIDs fail closed; a 4-point subprocess kill matrix proves
fresh-process resume of the SAME move at every copy boundary.
(3) instance_meta is fail-closed like the protocol tables: frozen
identity columns, config acceptance under regen/migrate context only,
gate/move fields under maintenance/move ceremony context only, legal
move_status edges in-trigger, plus shape CHECKs (role/peer/token
non-NULL iff moving-or-moved, moved_to iff moved, moved retains
token/role/peer as evidence); the old raw-mutation tests are now
public-ceremony-based plus explicit corruption negatives.
(4) Copy integrity: DB bytes pread from the HELD drained fd (pathname
replacement cannot be copied); config re-read via the held dirfd and
canonical-digest-verified before its exact bytes publish; short writes
loop; destination fs preflighted against the local allowlist at enter
AND copy; the gated destination pair full-opens before success.
(5) migrate audits its authorized attempt in a committed ceremonies
row before raising no-path. (6) move/destination_destroyed require
exact bools; destination/moved_to canonical absolute; existing
non-regular destination refused. Boundary frozen from the handoff.

## Move round-5 revision delivered — 167/167 green

(1) Post-bind clone closed: move_activate revalidates PHYSICAL
residence at the bound route before committing (a destination-role
clone/rename refuses with EXIT_DAMAGE), and both bind and activate
committed fast paths revalidate residence against the ceremony's
recorded route — red-first pins attempt the rogue activation BEFORE
the real one and prove exactly one path becomes writable, plus
activated-clone and bound-clone retry-refusal pins. (2) ceremonies
gains an immutable peer column: bind/activate/decommission audit the
canonical route; decommission's committed fast path validates the
retried moved_to against it (the reviewer's wrong-route repro is the
pin); move_copy post-activation discovery validates the ceremony peer.
(3) Committed-boundary crash matrix: post-COMMIT/pre-return fault
hooks in enter/bind/activate/decommission with fresh-process recovery
pins — enter-crash recovery discovers the committed token/route via
the new readonly move_status_inspect and resumes the same move; the
other three re-invoke to already_committed; final assert: exactly one
active authority. (4) Streaming copy: bounded-chunk pread→scratch with
inline hashing (COPY_CHUNK), fsync, no-clobber publish; resume
stream-hashes an existing REGULAR (nonblocking/no-follow) artifact;
premature EOF, zero-byte writes, source-changed-mid-stream, FIFO
artifacts, and mismatches all fail closed; pins exercise the helper
with tiny chunks and hostile artifacts, no giant fixtures.
(5) Stage-discovery recovery classification narrowed to the two
absence shapes; other errors keep their own reason.

## Move round-6 revision delivered — 173/173 green

(1) Symmetric SOURCE route binding: instance_meta gains move_source
(the canonical absolute source config path, bound at enter alongside
the destination; CHECK-coupled to move state; retained through
'moved' as evidence; cleared only by activation/abort). Source-only
ceremonies (move_copy, move_decommission, abort_move) now require
PHYSICAL residence at the bound source route — the reviewer's
two-active abort repro (rogue source-role copy + truthful attestation
after real destination destruction) is the red-first pin, plus rogue
decommission/copy refusals; enter itself must run at the source's own
config path. (2) Decommission is activation-gated per the PLAN
sequence: it full-opens the bound destination and requires same
immutable UUID, committed move_activate for this token AND route, and
active (ungated) state — negatives pinned before copy, before bind,
and before activation; happy path + committed retry stay green.
(3) Config artifacts open nonblocking/no-follow with ISREG everywhere
(_read_config_at, _publish_bytes_at existing-artifact path, move_copy
source read); _write_all fails closed on zero-byte writes; FIFO pins
for destination config and instance config.

## Move round-7 revision delivered — 178/178 green

Immutable move-binding authority adopted per the reviewer's
recommendation: new `moves` table keyed by token — {token,
instance_uuid, source_config, source_dev, source_ino,
destination_config, destination_dev, destination_ino, created_ts} —
created inside the enter transaction from ALREADY-OPEN no-follow
descriptors, copied with the DB, insert-context-guarded and
update/delete-abort, surviving activation by construction. Both
following-stat predicates (_resides_at, _resides_at_route) are
DELETED; the single _validate_route_identity helper requires canonical
committed path + component-walk parent open whose fstat equals the
bound {dev,ino} + the Store's own held dirfd at that same identity +
basename match — applied to source copy/decommission/abort and
destination bind/activate including every committed retry fast path,
and move_copy validates the reopened destination dir against the
bound identity before publishing. The reviewer's five-step symlink
repro is the red-first pin (symlinked source path at a rogue copy →
refuse; true source aborts; exactly one active), plus
directory-replacement negatives for BOTH roles (rename-aside +
copytree = same path, new inode → refuse; restored original works),
moves-row immutability/context pins, and binding-survives-activation
+ move_status_inspect completeness (now returns move_source and the
full binding). Token-mismatch diagnostics precede binding lookup.

## Move round-8 revision delivered — 184/184 green

(1) Same-directory moves refused at enter (identity equality check
regardless of basename); pinned with exact-same-path and
different-basename-same-dir cases, source active after each refusal.
(2) The moves binding is now the ONLY consumed authority: _move_binding
validates {token, instance_uuid} (mismatch = corruption; forged-row pin
via context-bearing insert); _validate_route_identity additionally
requires the handle's EXACT canonical config path to equal the
committed route (alternate spellings refuse — pinned); move_copy
derives the destination from binding.destination_config with live
move_peer/move_source demoted to cross-checked mirrors, and stage
discovery validates the opened peer against the bound destination
identity before returning ANY stage (destination-replacement pin);
move_decommission validates the opened destination against the binding
identity (replacement pin) and compares moved_to against the binding.
(3) Move entry uses the distinct context verb move_enter; the moves
insert guard requires exactly that verb, so a valid 'move' context
cannot forge bindings (pin). Token-mismatch diagnostics precede
binding lookups everywhere.
Boundary (post-final-run): baton_v6.py 2488 / e741c80b…,
test_baton_v6.py 2476 / 7ceb852c….

## Move round-9 revision delivered — 186/186 green

(1) The first-publication final validation now runs the binding
destination-identity validator on the checked pair AND cross-checks its
live mirrors/role — a destination substitution between publication and
final validation fails (pinned via the move:db-copied fault hook with
restore-then-resume). (2) move_copy's source-config re-read requires
ISREG on the reopened fd before any read (FIFO-replacement pin via the
move:post-drain seam: prompt regular-file rejection, move stays gated
and resumable); _publish_bytes_at translates existing-artifact open
errors into the BatonError damage surface (ELOOP and other OSError).
Boundary (post-final-run): baton_v6.py 2508 / 2469e149…,
test_baton_v6.py 2522 / f7055403….

## MAINTENANCE/MOVE/MIGRATE SLICE SIGNED OFF (review 20-26-11Z)

Signed off at the round-9 pair (2469e149…/f7055403…, 186/186,
independently verified). Final model per reviewer: one immutable
move-binding authority, exact no-follow source/destination identity,
role-specific transitions, activation-gated decommission, exact-token/
routing retry validation, bounded-memory resumable copying,
lost-response crash recovery; no reviewed repro produces multiple
active authorities through the API. PLAN.md status line updated to
reflect Slawomir's authorization. NEXT: CLI over the transaction APIs,
wait/eventing, doctor/scan/dump/materialize, then packaging/cutover —
red-first, one frozen boundary per phase.

## wait/eventing + CLI + observability phase delivered — 198/198 green

wait_for_message: query → arm inotify on the instance DIRECTORY
(ctypes inotify_init1/add_watch, filtered to mailbox.sqlite3* names;
never a single WAL inode) → REQUERY closing the arm race → block with
the rescan interval; overflow/invalidation/dir-replacement/unmount
events force full re-open validation; events are hints only; degraded
mode is pure polling (pinned by breaking the watch class); a gated
instance stands the waiter down with the gate diagnostic; timeout is
clean EXIT_NONE. Pins: existing-message immediate return, late-send
wake well under the rescan interval (watch-driven), timeout, degraded
parity, gate standdown. Observability: doctor (read-only integrity/
foreign-key/gate/count/claims report + stale-scratch and
unrecognized-file inventory, non-zero exit on problems), dump
(byte-redacted table snapshot incl. contents + transitions tail),
materialize (byte-exact idempotent projection re-emit via the
no-clobber publisher; refuses scrubbed bodies). CLI: full argparse
surface over every transaction API (init/regen/send/send-notice/
claim/wait/see/expire/reply/close/recover-claim/gc/scan/doctor/dump/
inspect/materialize/maintenance-*/move-*/abort-move/migrate), stdin
bodies, ROOT:PATH attach spelling, JSON output, exit codes from the
module table (roundtrip, none=3, gated=7, missing-config=4 pinned).
Boundary (post-final-run): baton_v6.py 2996 / f6db52bc…,
test_baton_v6.py 2685 / 0b373438….

## wait/CLI round-2 revision delivered — 219/219 green

(1) One lossless delivery shape shared by claim and wait: claim
metadata + immutable envelope with body {base64, size, sha256,
utf8-when-clean} or None and the complete pinned attachment tuple;
pins: text, all-256-bytes non-UTF-8, empty, attachment,
transient-readable-until-consumed. (2) CLI totality: argparse
SystemExit maps to validation code 4 (help/version stay 0; 2 reserved
for floors); body-file/config-UTF-8/attach-syntax/numeric errors
convert to clean BatonError diagnostics (no tracebacks — pinned);
send uses a real mutually-exclusive body/attach group (attach alone =
attachment-only, both = parser rejection, neither = stdin) and
--no-body is deleted; CLI attachment path pinned end-to-end.
(3) Event matrix: wait:armed seam proves the arm-race requery;
WAL-checkpoint-reset wake; synthesized overflow/ignored/move-self/
unmount flags force a counted validated reopen before rearm;
gate-WHILE-blocked standdown; degraded polling honors the configured
interval (1s cap removed); timeout/rescan validated as finite reals
with bool excluded (NaN/Inf/negative/zero pins at API and CLI);
IN_UNMOUNT added to the armed mask. (4) doctor: explicit
problems-vs-warnings (ok/exit derive from problems only; residue
warns), dirfd-anchored enumeration, and the planned logical checks —
ledger birth consistency, orphan contents, attachment pins vs
accepted_roots incl. binding generation, accepted/config coherence —
with corruption pins; materialize requires retention='durable'
(pending AND claimed transient refusals pinned). (5) dump includes
op_context and labels the transitions-tail truncation with the total;
protocol JSON output is explicitly encoded and fail-closed
(default=str removed); notice bodies use the lossless representation.
Boundary (post-final-run): baton_v6.py 3128 / b4431c27…,
test_baton_v6.py 2978 / cf47a6ce….

## wait/CLI round-3 revision delivered — 237/237 green

(1) Atomic wait delivery: wait_for_message builds the lossless delivery
from the SAME validated Store that committed the claim; the CLI
consumes that single result; the wait:claimed seam pin gates the
instance post-claim and proves byte-exact delivery of already-owned
content; _body_repr/_delivery RECOMPUTE the body's sha256 and size
against stored content metadata and fail as damage on mismatch
(hash-tamper pin). (2) accepted_roots has INSERT/UPDATE/DELETE guards
permitting only the regen verb; corruption tests are rebuilt on an
explicit raw-sqlite _raw_corrupt construction (trigger drop → mutate →
trigger restore) so production mutation is never normalized;
uncontextual/wrong-verb refusals + public regen success pinned.
(3) doctor validates the full audit chain per entity — exactly one
birth, contiguous from→to order, legal edges (message and claim
graphs incl. 'gc' closure), tail equal to the live row, GC'd subjects
closing in 'gc', verb sanity — with forged duplicate-birth/broken-
chain/wrong-tail pins; every retained attachment goes through
verify_attachment (real post-publication mutation pin); every contents
row is re-hashed/re-sized with an EXACTLY-ONE-owner invariant; plus
the planned projection inventory over configured projection_dirs
(orphans reconciled against durable messages as warnings, pinned).
(4) The real event decoder (_decode_inotify) is extracted and driven
by raw synthetic records per exact mask (overflow/ignored/move_self/
delete_self/unmount + named-file relevance); the armed-mask bits are
asserted; the reopen test feeds the DECODER's verdict; the WAL
checkpoint/reset happens while the waiter is blocked; degraded
sleep instrumentation proves the configured interval reaches sleep.
(5) _to_jsonable rejects non-string keys and non-finite floats
(pinned). Boundary (post-final-run): baton_v6.py 3277 / 0603ec6b…,
test_baton_v6.py 3198 / 382e2cd1….

## wait/CLI round-4 revision delivered — 240/240 green

(1) The wait:claimed seam now fires BETWEEN claim commit and content
fetch; the gate-at-seam pin proves _delivery reads byte-exactly
through the already-open store after the instance gates. (2) doctor
validates transition ATTRIBUTION: op_id/actor/seed/participant/at_ts
against the protocol grammars — forged-attribution pin with an
otherwise-valid chain proves the failure is credited to attribution
alone. (3) WAL-reset test synchronizes from the wait:armed seam (the
checkpoint provably happens while armed+blocked); the degraded-interval
test publishes during the FIRST sleep with timeout=None so
sleeps[0] == the configured 3.0 exactly; poll() returns the decoder
verdict untouched with the requery-on-every-wake contract documented.
(4) projection_prefix is now real: materialize takes a prefix (CLI
--prefix), doctor inventories per-directory configured-prefix UNIONS
(shared dirs supported), non-default prefix pinned end-to-end,
default-prefix files under a review-prefixed config are correctly NOT
counted, invalid prefixes rejected.
Boundary (post-final-run): baton_v6.py 3298 / fe241ae1…,
test_baton_v6.py 3258 / ea593b8c….

## wait/CLI round-5 revision delivered — 243/243 green

Attribution MEANING validated: a finite edge→producing-verb table
(births and gc edges included) flags any edge/verb pairing the
transaction contract cannot produce, and all rows sharing an op_id
must carry ONE coherent (participant, actor, seed, verb, at_ts) tuple;
participant checks apply the 64-char budget. Pins: impossible
edge/verb pairing (verb-only corruption, chain/tail intact,
cross-credited checks asserted silent), same-op attribution split
(actor changed on exactly one row of a two-row transaction), and
oversized participant. Packaging-doc note recorded: configured
prefixes define what doctor owns/inventories; materialize --prefix is
an explicit caller choice, not a participant lookup.
Boundary (post-final-run): baton_v6.py 3331 / 77ee06a1…,
test_baton_v6.py 3298 / 94e49c3f….

## PHASE 5 EXECUTED — packaging + one-way v5→v6 cutover (247/247)

wait/CLI/observability slice signed off via the phase-5 work order.
Delivered: deterministic zipapp (byte-identical rebuilds, artifact
cf2de45e… for source 77ee06a1…) + DISTRIBUTION.json (tool 1.0.0,
protocol 6, python_min 3.11, sqlite_min 3.37.0); ~3.6-parseable
bootstrap with floor-before-import (pinned); example-baton.json;
neutral README with the prefix ruling verbatim; isolated-checkout
smoke (live + permanent test) proving the reusable set with no Drift
tree; grep-clean purity gate over all reusable assets. CUTOVER: Drift
v6 instance initialized at ~/.local/state/baton/drift (uuid
8184940f…, doctor ok; 106 projection-orphan warnings = v5-era files,
honest); final v5 message published completing the drain (only item in
work/mailbox; readable as plain md); baton_v5.py/test_baton_v5.py/
roles.json DELETED (no compatibility reader); launcher execs v6;
AGENTS-MAILBOX-PROTO.md rewritten as the v6 deployment pin
(finding-folder policy stays in AGENTS.md); /work/mailbox/ .gitignore
entry retired. v6 waiter armed as drift.implementer. Remaining after
final review: standalone-repo extraction (post field/cutover signoff,
per the work order).

## Packaging round-1 revision delivered via v6 — 250/250 green

(1) Distribution-root contract: build(root) writes <root>/bin/baton +
<root>/DISTRIBUTION.json with the artifact path resolving from that
root; the CHECKED-IN root is tools/baton/ itself (bin/baton cf2de45e…,
DISTRIBUTION.json committed, staleness pinned against source hash);
config-schema.json added (neutral descriptive strict-JSON schema) with
a pin that its field inventory matches the runtime validator and the
example validates. (2) Genuine T26: the isolated harness copies the
COMPLETE reusable set (module, tests, builder, schema, example,
README, launcher, bin/baton) into a bare tree and runs the FULL suite
there via subprocess pytest with cwd/PYTHONPATH/HOME confined and a
BATON_ISOLATED recursion guard (outer suite now ~79s); the purity
gate covers every asset incl. the packed archive bytes with
project-specific split-constructed needles (drift-lang/drift./ /work//
finding-/AGENTS — self-match impossible); poisoned-CWD+PYTHONPATH
zipapp probe proves the archive imports its own module. (3) AGENTS.md
handoff rule rewritten for v6 (explicit --config, transactional
claim/reply/close, no raw SQLite, no mailbox scan); new Drift-side
lang/tests/tools/test_baton_deployment.py (5 tests green) audits
AGENTS.md/PROTO/launcher/v5-deletion/gitignore so stale v5
instructions cannot reappear. First v6-channel review round-trip
completed — the review arrived and this response returns through the
new store. Boundary: baton_v6.py unchanged 77ee06a1…; test file 3422 /
220f943b…; build_zipapp e56aaa59…; config-schema d1f5affd…;
DISTRIBUTION 1757fcc0…; bin/baton cf2de45e…; README 15251055…;
launcher 91bb0ff3….

## Resumed on the SHARED suite instance — packaging round-2 delivered (251/251)

New deployment (human-directed): shared instance
/home/sl/src/mailbox/baton.json (drift-suite-local, nine project
domains + human.slawomir); my identity is lang.implementer, reviewer
lang.reviewer; drift.* addresses retired; old instance decommissioned
as a recovery copy; the generic protocol moved INTO the distribution
(tools/baton/AGENTS-MAILBOX-PROTO.md), repo-root copies removed.
Mailbox intentionally initialized empty — no queue replay; resumed
from filesystem + code per instruction.

The three outstanding review items closed on the current tree
(preserving the reviewer's concurrent README/proto/AGENTS.md edits):
(1) distribution layout/manifest internally consistent — the
committed root (tools/baton) carries bin/baton + DISTRIBUTION.json
with root-resolving artifact path, staleness pins for source AND
protocol doc; (2) isolated-distribution proof — the full reusable
suite runs in isolation (BATON_ISOLATED guard), PLUS a new packed-
distribution roundtrip driving init/send/claim/reply/doctor through
bin/baton under poisoned CWD+PYTHONPATH; (3) the distribution ships
the generic AGENTS-MAILBOX-PROTO.md beside the executable — builder
copies it into any build root, manifest pins protocol_doc +
protocol_doc_sha256 (a88ec646…), REUSABLE_ASSETS/isolated
harness/purity gate include it; purity needle refined to the
host-policy-file form so the distribution's own protocol document is
a legitimate self-reference. Drift-side deployment audit rewritten
for the new layout (6 tests: bindings declared, proto
distribution-only, launcher v6, v5 gone, gitignore clean).
Boundary: test_baton_v6.py 5549e5a5…, build_zipapp.py 711abdf5…,
DISTRIBUTION.json 89f9252b…, proto a88ec646…, bin/baton cf2de45e…
(unchanged), baton_v6.py 77ee06a1… (unchanged), README 2b4151a0…
(reviewer's edit, preserved).

## Manifest refresh after reviewer's protocol-doc boundary edit

Slawomir's deployment-boundary clarification (policy binds identities,
never executable/config locations) changed the distribution-owned
protocol doc post-handoff (3705b3e1…). Canonical distribution rebuilt:
DISTRIBUTION.json now 54d014bc… recording the new protocol_doc_sha256;
artifact/bin unchanged cf2de45e…. Gates re-run: TestPackaging 8/8
(incl. isolated inner suite) + deployment audit 6/6. No source changes.

## TERMINAL SIGNOFF (final_review 2026-08-07T02:56:28Z) — FINDING COMPLETE

lang.reviewer signed off with outcome=approved: manifest/protocol/
artifact/source hashes verified independently (54d014bc… / 3705b3e1… /
cf2de45e… / 77ee06a1…), git diff --check clean, distribution-root +
deployment audits 7/7, packaging 14/14 with the isolated full suite.
"No open packaging or code finding remains. Baton v6 is ready for
standalone-repository extraction. This is a terminal signoff."
Claim closed with outcome=acknowledged. The finding is COMPLETE:
design (15 rounds) → implementation (storage, ceremonies, wait/CLI/
observability, packaging — ~20 review rounds, 251 tests) → live
cutover → shared-suite deployment → terminal signoff. Remaining
(outside this finding): standalone-repo extraction on Slawomir's go.
