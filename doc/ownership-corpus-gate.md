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
a thin wrapper:

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
* an explicit **status**: dry-run tolerates `"pending"` (with a
  warning); `--apply` requires `"approved"`, a non-placeholder
  reviewer identity, and a filename without "DRAFT";
* the `BASELINE.md` fragments (title, predecessor description,
  attribution text) and the approver's identity + date, which are
  recorded in the regenerated `BASELINE.md` together with the
  approval file's own hash.

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

## Authoring an approval file

Do not hand-compute the pinned facts — generate the record:

```
just ownership-corpus-promotion-draft <candidate-run-dir> \
     lang/tests/ownership_corpus/promotions/<name> \
     <predecessor-run-dir>
```

The generator reads the current reviewed baseline and the explicit
retained candidate, validates both schemas and the candidate's hard
gates, refuses unsupported changes (inclusion rule, excluded
population), and writes a NEW, non-overwriting JSON with all
machine-derivable facts filled in — six artifact hashes, exact
universe changes, exact counter deltas and key-set changes — plus
`status: "pending"`, empty reviewer/date, and `baseline_md` fields
marked `<<HUMAN REVIEW REQUIRED>>`.

The clean separation: the DRAFT GENERATOR records facts; the
REVIEWER supplies judgment (verifies the per-fixture attribution,
writes the explanatory text, sets `approved_by`/`date`, flips status
to `"approved"`, renames the file out of DRAFT); the PROMOTION TOOL
verifies and materializes that judgment.  `--apply` refuses pending
status, placeholder identities, DRAFT-named files, and unreviewed
`<<...>>` placeholders — the dry run verifies every pinned value
either way, and authoring mistakes fail closed.
