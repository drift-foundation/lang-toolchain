# String-cleanup execution plan (accepted direction: FOLLOW-UP-CLEANUP-REVIEW.md)

Status: PLAN — written before any code, per review discipline. Ordering is the
user's stated preference: guardrails/tooling → C3 decision → Array measurement →
string_arc deletion campaign.

**Branch (proposed, user creates):** `refactor/string-authority-cleanup` — one
branch for the whole cleanup phase; slices land as separate commits with their own
review rounds, mirroring the B-arch cadence.

**Global rules (standing):** targeted tests only until review, full suite is the
user's gate; memcheck lanes in-gate from the start for anything touching ownership
emission; ledger rebuilt after every MIR-mutating pass before verdict queries; no
name-filtered pytest as final verification; stop-and-report supersedes
implementation whenever a listed trigger fires.

---

## Slice 1 — guardrails & tooling (no compiler-semantics changes)

### 1a. Owned-at-extraction static contract pin

Goal: the AIL/VariantGetField class can never regress silently — any codegen
extraction lowering that RETAINS must be mirrored as a terminal producer in the
stake pass.

The known retaining extraction set is THREE MIR node names in TWO conceptual
families: {ArrayIndexLoad, ArrayIndexLoadUnchecked} (indexed element loads) and
{VariantGetField} (variant payload extraction).

Mechanism (STRUCTURED and fail-closed — Python `ast` over the sources plus
explicit markers; no brittle regex over code shapes):
- Every `_emit_copy_value` call site in `llvm_codegen.py` must carry an explicit
  classification marker comment: `# owned-at-extraction: <Node>` (extraction —
  must be terminal in the stake pass) or `# copy-construction: <reason>`
  (constructor/dup/clone copy sites). The `ast` walk finds ALL call sites
  authoritatively; an unmarked site is a FAILURE (fail-closed), never a skip.
- The extraction set A derives from the markers; the pin asserts the invariant
  three ways: (i) every `_emit_copy_value` site is marked; (ii) every member of A
  is terminal/non-view in `string_stakes` (its `_is_string_value_view` contains
  no `isinstance(prod, <A-member>)` test, checked via `ast`, AND the terminal
  site carries the matching `# owned-at-extraction:` marker); (iii) A equals the
  expected three-node set — a LARGER A fails with an explicit STOP/REPORT
  message: a newly discovered retaining extraction is a candidate live
  leak-class requiring a report + heap-string probe, never silent normalization
  into the expected set.

Expected tests: NEW `lang/tests/codegen/test_extraction_retain_contract.py`
(the pin + a self-check that the scan finds exactly the expected set A).

STOP/REPORT (not implementation): if the scan finds a retaining extraction node
beyond the known three-node set — that is a candidate live leak-class (the 1d
shape, undiscovered), and it gets a report + heap-string probe before any pinning
normalizes it.

### 1b. Corpus runner/aggregator → repo tooling

Goal: Scope-B deletion slices get reproducible "identical universe / exact delta"
acceptance without session-local scripts.

Deliverable: `tools/drift_corpus_audit.py` (conventions of
`tools/drift_test_run.py`): runs the e2e-fixture universe with
`DRIFT_STRING_ARC_AUDIT=1`, `-jN`. Per-run outputs, strictly separated:
- `aggregate.json` — the COMPARABLE acceptance artifact: counters only, sorted
  keys, stable formatting; NO volatile fields (no paths, timestamps, PIDs,
  durations, temp dirs).
- `manifest.json` — the universe identity: sorted fixture rel-paths + source
  content hashes + compile-success set, plus an environment section (toolchain
  version, tool version) that is informational for humans but only the UNIVERSE
  section participates in baseline equality.
- `metadata.json` — all volatile context (timestamps, durations, host paths,
  jobs), explicitly non-comparable.
`--baseline <run>` asserts universe equality (loud error otherwise), prints the
sorted per-counter exact-delta table, and fails on any hard-gate that is nonzero
in the new run.

Expected tests: driver test over a small fixture subset pinning aggregate keys,
delta arithmetic, determinism (two runs → byte-identical aggregates), and
manifest mismatch detection (mutated fixture → loud universe error).

STOP/REPORT: none anticipated (pure tooling). Baseline discipline: build outputs
are NEVER committed — the phase's reference baseline is recorded in PROGRESS as
{manifest hash, aggregate table, exact command line, tool version}; future runs
reproduce the baseline from that record.

Slice-1 exit: both pins green, baseline corpus run recorded. No emission changes,
corpus byte-identical by construction.

---

## Slice 2 — the C3 decision (no indefinite temporary exception)

Structure: DECISION-FIRST. Part 1 is a mandatory stop-and-report checkpoint; Part
2 implements whichever arm is chosen.

Part 1 (investigation, report-only): determine whether the ledger event model can
represent flag-guarded cleanup MoveOuts (3C's `*_cleanup_drop_<local>` blocks) as
conditional-ownership events. Deliverables: the event-model sketch, expected
verdict movement (should be pure reclassification: c3_moveout_not_owned 11,441 →
a new agree-class, all else byte-identical), complexity estimate, and a
recommendation. STOP for the user's arm selection.

Part 2A (model arm): ledger event-model extension + reporter reclassification.
Acceptance: corpus via the new tool — C3 11,441 → 0 with the exact count
reappearing in the new class; every other counter byte-identical; hard gates 0.
Expected tests: ledger unit pins for the conditional event; reporter pin;
stage2 + memcheck + ownership matrices targeted battery.

Part 2B (bless arm): the allowlist becomes PERMANENT and loud — the reporter
recognizes exactly the flag-guarded 3C shape (structural match, not a count), and
any C3-shaped MoveOut OUTSIDE that shape classifies UNCLASSIFIED (hard gate), the
retired-C4 pattern. Expected tests: reporter pin with a synthetic
out-of-shape MoveOut failing loudly; corpus counts identical.

STOP/REPORT triggers in Part 2: any hard-gate counter moves; the reclassification
count does not balance exactly; memcheck regression anywhere.

---

## Slice 3 — Array release-elision MEASUREMENT (report-only by definition)

Scope: reporter-side instrumentation ONLY — no emission change of any kind.
- Extend the audit reporter to tag Array return-boundary drops
  (`_drop_all_arrays` sweep) with site classes analogous to strings
  (`site_class:scope_exit_arraydrop`) and compare against
  `verdict_at(boundary, needs_drop=DropPolicy(Array<T>).needs_drop)` to produce
  the UNINIT / MOVED_OUT / TOMBSTONED / PATH_DEPENDENT / LIVE mix.
- One corpus generation with the slice-1b tool against the slice-1 baseline.
  Strings counters must be BYTE-IDENTICAL (proves the instrumentation inert).

Deliverable: a report with the emission mix, the projected elision win, the
Array-specific safety notes (element-walking drops; same MUST_NOT_DROP-only
guardrail direction), and a go/no-go recommendation. EXPLICIT STOP — no
implementation in this slice regardless of how favorable the numbers look.

Expected tests: reporter unit pin for the new site class; the inertness corpus
check.

---

## Slice 4 — string_arc deletion campaign (main architectural target)

Method per class (repeatable): corpus names the class's emission count → prove it
dead or migrate it under a generic authority (cleanup_authoring / drop_flags /
ledger) → TRIPWIRE stage (one release, because corpus-zero ≠ wild-zero) → delete
the branch → corpus exact-delta signed. Tripwire error shape: a CLEAN user-facing
internal-compiler-error diagnostic carrying the site-class and context (fn,
block, local), NEVER a raw assert or Python traceback — operators and downstream
teams see these if they fire.

**Slice 4a (first, bounded): tripwire the dead retain fallbacks.** B-arch drove
call_arg / value_position / store_value retains to 0 corpus-wide; string_arc's
late-retain fallback code for those positions should now be unreachable. Convert
each fallback to a loud internal error carrying the audit site-class breadcrumb.
Expected tests: unit pins that synthetic un-staked inputs trip the error
(constructed at MIR level); full targeted battery (stage2 + memcheck + stake
pins + matrices) proving nothing real trips. Version note: tripwires ride the
next release; deletion of the tripwired branches is 4a′ AFTER a full cert cycle
with no firings.

Later classes (each its own checkpoint, not bounded here): temp_lastuse_release
(~363k emissions) and overwrite_release (~137k) migrations to generic authorities;
the site-3 sweep consolidation; site-4; the final file collapse. Each gets the 4a
treatment through the slice-1b tool.

STOP/REPORT triggers: any tripwire fires anywhere (full suite, DriftQuery builds,
cert battery) — that is a discovered live path, report before touching it; any
corpus delta not predicted by the class being migrated; any memcheck movement.

---

## Sequencing & interaction notes

- Slice 1 has no dependencies and unblocks everything (1a protects slice-4-era
  codegen changes; 1b is the acceptance instrument for 2/3/4).
- Slice 2 and 3 are independent of each other; both consume 1b. Order per user
  preference: 2 then 3.
- Slice 4a can start immediately after 1 (it does not need the C3 decision), but
  per the stated ordering it queues behind 2 and 3; its later classes are the
  long-running campaign.
- The selfhost plan (work/selfhost-driftc/) consumes this phase's completion as a
  gate and is untouched by it.
