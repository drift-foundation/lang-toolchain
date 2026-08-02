# The ownership-corpus certification gate and baseline promotion

Two distinct things share the "ownership" name in this repo:

* **`just ownership-matrix-check`** — the 51 curated, GENERATED
  ownership-transfer matrix fixtures (generator-freshness guard);
  runs inside `just test`.
* **`just ownership-corpus-check`** — THIS gate: the full fixture
  corpus (925 compiled / 1269 discovered at the time of writing)
  compiled one-by-one under `DRIFT_STRING_ARC_AUDIT`, its 14
  ownership counters aggregated and compared EXACTLY — identical
  universe, every counter delta +0, hard gates zero — against the
  checked-in reviewed baseline
  (`lang/tests/ownership_corpus/reviewed-baseline/`).  It runs
  exactly once per certification: from `just certify` and as the
  first stage of the maintainer's pre-handoff runner
  (run-all-tests.sh); never from `just test`.

The comparison is `--require-zero-delta` and fails closed on
malformed inputs, universe drift (fixture additions/removals,
content-hash changes, compile-partition flips), any nonzero counter
delta, and nonzero hard gates.  Tool:
`tools/drift_corpus_audit.py`; teeth:
`lang/tests/tools/test_drift_corpus_audit.py`,
`lang/tests/tools/test_ownership_corpus_check.py`.

## Runbook: re-baseline after a fixture change (start here)

`just ownership-corpus-check` fails with **UNIVERSE MISMATCH** whenever
the fixture set changes — you added, removed, or renamed a corpus
fixture (or a fixture's compile outcome flipped). That is by design:
deltas across different universes are meaningless. To move the reviewed
baseline forward, four commands — each prints the next one, so you never
hunt. `<name>` follows the existing records, e.g.
`0.33.94-bare-temp-field-projection-uaf`.

```
# 1. Acquire a candidate run over the current tree.  SKIP if you already
#    have a COMPLETE run dir (manifest.json + aggregate.json) — e.g. the
#    run that just reported UNIVERSE MISMATCH; use its path as <run-dir>.
just ownership-corpus-run

# 2. Build the promotion record from that run.  (Predecessor auto-resolves
#    from the checked-in record chain — do NOT pass a predecessor run.)
just ownership-corpus-promotion-draft <run-dir> \
     lang/tests/ownership_corpus/promotions/<name>

# 3. PREVIEW the deltas (dry-run; writes nothing).  Single RECORD-dir arg —
#    candidate/ and the approval file are resolved for you.
just ownership-corpus-promote \
     lang/tests/ownership_corpus/promotions/<name>

# 4. If you agree with the deltas: APPROVE + APPLY in one step.  This
#    renames approval-DRAFT.json -> approval.json (the review stamp; the
#    Git commit records who/when) and writes the baseline.
just ownership-corpus-approve \
     lang/tests/ownership_corpus/promotions/<name>
```

Then `just ownership-corpus-check` passes over the new universe, and you
commit the four reviewed-baseline files together with the whole
`promotions/<name>/` record and the fixtures that changed.

Notes:
* After step 2 the `build/tmp` run is disposable — the record under
  `promotions/<name>/` is self-contained (clone-sufficient), so steps 3
  and 4 take only the record dir.
* `ownership-corpus-promote <record>` is dry-run; add `--apply` (or use
  `ownership-corpus-approve`) to write. `--apply` is refused while the
  record still holds `approval-DRAFT.json` — approval is the rename.
* The explicit two-arg form
  `ownership-corpus-promote <run-dir> <approval-file>` still works if you
  ever need to point at artifacts outside a record.

The sections below are the reference for what each step checks and why.

## The reviewed baseline

`lang/tests/ownership_corpus/reviewed-baseline/` holds exactly four
files: `aggregate.json` (counters), `manifest.json` (universe
identity: inclusion rule, per-fixture whole-directory content hashes,
compile partition), `metadata.json` (origin-run provenance record),
and `BASELINE.md` (human-readable provenance, predecessor chain,
approved deltas, attribution).  Only aggregate + manifest participate
in comparison.

A broken candidate must never be able to approve itself: the
baseline changes ONLY through the reviewed promotion process below —
certification never regenerates or re-blesses it, and baseline drift
is always visible in the diff.

## Promotion: a manual review stamp, materialized by a tool

Promotion is these six steps, in order:

1. Revalidate the approved retained corpus run against the current
   reviewed baseline.
2. Confirm the exact universe, counter deltas, hard gates, and
   per-fixture attribution.
3. Replace the three baseline artifacts (`aggregate.json`,
   `manifest.json`, `metadata.json`).
4. Regenerate `BASELINE.md` with predecessor, provenance, approved
   deltas, and attribution.
5. Compare the retained run against the promoted baseline and
   require exact zero delta.
6. Run the baseline/tooling teeth, then commit the baseline change
   with the candidate.

Steps 1–5 are automated by **`tools/drift_corpus_promote.py`** behind
thin wrappers.  The everyday form takes a single promotion RECORD dir
(candidate/ and the approval file are resolved from it):

```
just ownership-corpus-promote <record-dir>            # dry-run / preview
just ownership-corpus-promote <record-dir> --apply    # write (approved only)
just ownership-corpus-approve  <record-dir>           # rename DRAFT->approval + apply
```

The explicit two-arg form still works for artifacts outside a record:

```
just ownership-corpus-promote <run-dir> <approval-file>            # dry-run
just ownership-corpus-promote <run-dir> <approval-file> --apply    # write
```

The critical property: **the tool materializes an already-approved
result; it cannot approve or bless its own input.**  The review stamp
lives in the APPROVAL FILE — a reviewed JSON document that pins,
ahead of time:

* the **predecessor** baseline's three artifact sha256s (a baseline
  that moved since the approval was written is a STALE PREDECESSOR
  and fails);
* the **candidate** run directory (explicit — the tool never selects
  "latest" and never generates a corpus) and its three artifact
  sha256s;
* the **exact expected universe change**: compiled additions/removals
  by name, failed additions/removals, pre-existing content-hash
  changes (normally empty), and the resulting
  compiled/failed/excluded counts.  The inclusion rule and the
  excluded population (name AND reason) must be UNCHANGED — the tool
  does not support altering them; fixture lists are integrity-checked
  (unique names, disjoint partitions, fixtures = compiled ∪ failed);
* the **exact nonzero counter deltas** — any unexplained or
  mismatched delta fails; zero deltas are implicit and may not be
  listed; the counter-KEY SETS must be identical unless a schema
  change is explicitly approved (`counter_keys_added` /
  `counter_keys_removed`) — a key appearing or disappearing even with
  value zero is a schema change;
* **approval state is the EXACT FILENAME** — `approval-DRAFT.json`
  is pending (dry-run allowed, `--apply` refused); `approval.json` is
  approved (`--apply` allowed); any other filename, or both files
  present in one directory, fails closed.  The reviewer approves by
  RENAMING the draft — no JSON edits; reviewer identity and date are
  recorded by Git history (the commit that renames the file).  Legacy
  records may carry inert `status`/`approved_by`/`date` fields;
  authority comes only from the filename;
* the `BASELINE.md` fragments (title, predecessor description,
  attribution text) — mechanically composed by the generator so the
  draft is COMPLETE before review — recorded in the regenerated
  `BASELINE.md` together with the approval file's FULL sha256 and a
  pointer to Git history for the reviewer identity/date.

Safety properties (all toothed in
`lang/tests/tools/test_ownership_corpus_promote.py`):

* dry-run by default; `--apply` required to write anything;
* never called by `just test`, `just certify`, or run-all-tests.sh;
* fails closed on: run-dir/approval mismatch, stale predecessor,
  candidate artifact mismatch, malformed approval or corpus data,
  unexpected universe change, unexplained counter deltas, nonzero
  hard gates;
* writes ONLY the four reviewed-baseline files; unrelated files in
  the baseline directory are untouched;
* `--apply` is staged and rollback-protected: all four files are
  written to a staging directory, the STAGED baseline must pass the
  exact zero-delta comparison before anything is replaced, originals
  are backed up during the swap and restored on failure, and residue
  from an interrupted promotion fails the next attempt closed;
* after writing, performs the exact zero-delta comparison
  (`drift_corpus_audit` semantics) between the new baseline and the
  candidate run — a nonzero result is a hard failure.

Step 6 remains yours: run the tooling teeth
(`lang/tests/tools/`) and commit the four changed baseline files
together with the candidate they bless.

## The durable promotion record (clean-repo reproducibility)

A corpus run's retained directory lives in untracked `build/tmp/` on
one machine.  Each promotion therefore materializes a durable,
self-contained RECORD in the repo:

```
lang/tests/ownership_corpus/promotions/<name>/
  approval-DRAFT.json          (later: approval.json)
  predecessor/
    aggregate.json  manifest.json  metadata.json  fixture-counters.json
  candidate/
    aggregate.json  manifest.json  metadata.json  fixture-counters.json
```

`fixture-counters.json` is the COMPACT extraction of the one
aggregate record per compiled fixture (fail-closed: exactly one
well-formed record each) — a few hundred KB instead of the raw
multi-MB audit logs, which need not be preserved once the record is
generated and verified.  Every evidence file's sha256 is pinned in
the approval, and the promotion re-proves the per-fixture attribution
(modal delta, outliers, new-fixture contributions, RESIDUAL ZERO
against the aggregate deltas) from this checked-in evidence on every
dry-run and apply.  A clean clone can audit or execute the promotion
end-to-end; after promotion, the dry-run switches to AUDIT MODE
(live baseline == candidate) and verifies the recorded transition
against the record's own predecessor copies.  `--apply` requires the
live baseline to equal the approved predecessor.

Commit the whole record with the promoted baseline.

## Reviewing the ownership counters by hand

The tool proves the deltas match the approval, but a *human* review should
answer a different question: **is every counter change explained by a real
lowering change I can point at?** The counters are plain JSON at three
levels — you never need the compare CLI to read them.

**1. Corpus totals** — the 14 counters summed over every compiled fixture:

```
cat lang/tests/ownership_corpus/reviewed-baseline/aggregate.json
```

**2. Per-fixture breakdown** — each fixture's contribution, checked into the
record (`predecessor/` and `candidate/`):

```
python3 -m json.tool \
  lang/tests/ownership_corpus/promotions/<name>/candidate/fixture-counters.json | less
```

**3. What actually changed** — diff the record's predecessor vs candidate
per fixture; this is the real review view (which fixtures moved, and by how
much):

```
python3 - lang/tests/ownership_corpus/promotions/<name> <<'PY'
import json, sys
rec = sys.argv[1]
pre  = json.load(open(f"{rec}/predecessor/fixture-counters.json"))["fixtures"]
cand = json.load(open(f"{rec}/candidate/fixture-counters.json"))["fixtures"]
for n in sorted(set(pre) | set(cand)):
    p, c = pre.get(n, {}), cand.get(n, {})
    if p == c:
        continue
    delta = {k: c.get(k, 0) - p.get(k, 0)
             for k in sorted(set(p) | set(c)) if c.get(k, 0) != p.get(k, 0)}
    tag = "NEW" if n not in pre else ("REMOVED" if n not in cand else "changed")
    print(f"[{tag}] {n}: {delta}")
PY
```

Every line should fall into one of three explainable buckets:

* **NEW** — a fixture added this promotion; it simply adds its counts.
* **REMOVED** — a fixture (or rename) dropped; it subtracts its counts.
* **changed** — a *pre-existing* fixture whose counts moved. These are the
  ones to scrutinize: the move must correspond to an intended compiler
  change. (Example, the 0.33.94 field-projection fix:
  `borrow_chained_ref_projection_noncopy` shifted by
  `{c3_moveout_owned: -8, moveout_expansion: -8, events: -8}` — the pure-field
  lift replaced a move-out *expansion* (copying the leaf into a second owned
  temp, the double-free cause) with an address projection, so those move-outs
  simply disappear. Fewer move-outs, one owner: the fix's fingerprint.)

The aggregate deltas equal `(NEW) − (REMOVED) + (changed)` exactly — that
identity is what the tool reports as `residual ZERO on every counter`; the
diff above lets you see it by name.

**Regenerate one fixture from scratch** (no audit tool — just the compiler
with the flag the tool sets), to confirm a number or inspect a fixture the
diff flagged:

```
DRIFT_STRING_ARC_AUDIT=1 DRIFT_STRING_ARC_AUDIT_FILE=/tmp/x.jsonl \
  PYTHONPATH=. .venv/bin/python -m lang.driftc.driftc --dev \
  lang/tests/codegen/e2e/<fixture>/main.drift -o /dev/null
cat /tmp/x.jsonl        # the `"record":"aggregate"` line IS that fixture's counters
```

That is exactly what `tools/drift_corpus_audit.py` does per fixture
(`DRIFT_STRING_ARC_AUDIT` + `..._FILE` → `<run>/audit/<name>.jsonl`), then it
sums the single `aggregate` record from each.

### What the counters mean

| counter | meaning |
|---|---|
| `events` / `fns` | total ownership-lowering events walked / functions processed |
| `c1_agree` / `c1_path_dependent` | the two analysis passes agree / the verdict is path-dependent |
| `c3_moveout_owned` / `_zero_safe` / `_flag_guarded` / `_unreachable_block` | how each move-out was classified |
| `site_class:scope_exit_release` | a release emitted at scope exit |
| `site_class:materialized_lastuse_release` | a release at the last use of a materialized temp |
| `site_class:overwrite_release` | a release before overwriting a slot |
| `site_class:moveout_expansion` | a move-out expanded into an explicit ownership transfer |
| `site_class:drop_before_overwrite_site4` | the site-4 drop-before-overwrite class |
| `pre_post_verdict_drift` | sites where the pre- vs post-ledger verdict differed |

A promotion is sound when every changed line has such an explanation; an
*unexplained* `changed` delta is the signal to stop and investigate before
approving.

## Authoring an approval file

Do not hand-compute the pinned facts — generate the record:

```
just ownership-corpus-promotion-draft <candidate-run-dir> \
     lang/tests/ownership_corpus/promotions/<name>
```

The predecessor is resolved automatically from the checked-in record
chain (clone-sufficient). Pass a third `<predecessor-run-dir>` argument
ONLY as the bootstrap escape hatch for a baseline that predates
record-keeping — normal promotions omit it.

The generator reads the current reviewed baseline and the explicit
retained candidate, validates both schemas and the candidate's hard
gates, refuses unsupported changes (inclusion rule, excluded
population), and writes a NEW, non-overwriting record whose
`approval-DRAFT.json` is COMPLETE: all machine-derivable facts —
evidence hashes, exact universe changes, exact counter deltas and
key-set changes, attribution facts — plus the finished `baseline_md`
text composed from those facts.  The draft is pending purely by its
FILENAME; there are no status/reviewer/date fields to edit.

The binding workflow:

```
generator produces a COMPLETE approval-DRAFT.json
reviewer reads it and approves by RENAMING it to approval.json
promoter dry-run
promoter --apply
the commit records the reviewer identity and date
```

The clean separation: the DRAFT GENERATOR records facts AND composes
the complete `baseline_md` text from them; the REVIEWER's only
mutation is the rename; the PROMOTION TOOL verifies and materializes
that judgment.  `--apply` refuses DRAFT-named files, alternate
filenames, ambiguous both-present states, and `<<...>>` placeholders
in an edited draft — the dry run verifies every pinned value either
way, and authoring mistakes fail closed.
