# Regex engine allocation removal — resume status

Date paused: 2026-07-26  
Branch: `regex-engine-allocation-removal`  
Base commit: `32d676bbd4f351b6f794b9873756cab81ac614e5`  
State: implementation complete and measured; static review HOLD remains; no
corpus-baseline promotion, full-suite handoff, certification, or deployment has
occurred.

This file is the restart authority for the branch. Read it together with:

- `REGEX-ENGINE-ALLOCATION-CHECKPOINT.md`
- `Progress.md`
- `/tmp/drift-announce/2026-07-26T145559Z-regex-engine-allocation-review.md`
  if that temporary copy still exists

## 1. What is implemented

`stdlib/std/regex/regex.drift` contains the packed-workspace Thompson NFA
executor:

- one function-local `Array<Int>` split into epoch marks, two state-list
  regions, and an iterative worklist;
- no per-candidate or per-byte allocation;
- deterministic generation-overflow reset;
- direct next-closure construction;
- iterative epsilon closure, replacing recursive `_add_state`;
- one workspace per top-level search and one workspace across an entire
  `replace_all`;
- no mutable scratch stored in `Regex`, preserving shared-use reentrancy and
  thread safety;
- String and `StringByteView` matching through the same range authority.

The exported underscore-internals `_make_bitmap`, `_clear_bitmap`, and
`_add_state` are removed after a zero-consumer audit. `_try_match_at` and
`_find_from` remain compatibility wrappers. `_find_from_gen_saturated` is the
overflow-reset test hook.

The candidate currently stamps compiler `0.33.89`, runtime ABI `22`.

## 2. Current diff inventory

Tracked modifications:

- `stdlib/std/regex/regex.drift`
- `doc/history.md`
- `lang/versions.py`

New files:

- `lang/tests/driver/test_regex_scratch_counts.py`
- `lang/tests/codegen/e2e/std_regex_view_offsets_alternation/`
- `work/regex-engine-allocation-removal/`

At pause time the branch changes were uncommitted. If a recovery/WIP commit is
made after this report, record its hash here or in `Progress.md`; do not assume
the base commit above then remains HEAD.

## 3. Evidence already obtained

Correctness and safety:

- 1000-case legacy/new dual-engine differential: zero mismatches;
- 16/16 regex e2e fixtures;
- String-view regex pins: 9/9;
- valgrind clean;
- exact allocation-contract test green;
- epoch-reset equivalence green;
- corpus hard gates zero.

Allocation contract:

- exactly one real allocation and one real free per top-level search;
- no allocation growth with input length or candidate-start count;
- `replace_all` uses one workspace across the whole operation;
- String matching adds no retains;
- view matching adds exactly its expected backing retain/release.

Performance, interleaved legacy/current:

- all representative 64 B–4 KiB matching rows: 2.0–2.45x faster;
- representative 256 B and 4 KiB late-hit/no-match cases: about 2.35x;
- 2 MiB carrier: 2.33x;
- 2 MiB no-match: 2.46x;
- 16-branch wide alternation: 1.61x;
- compile time and optimized binary size: parity.

Checked-in measurement evidence remains under `bench/results/`.

## 4. Corpus state — measured, not promoted

Candidate run:

`build/tmp/ownership-corpus-regex-20260726-082739-3276579`

Comparison run used for per-fixture attribution:

`build/tmp/ownership-corpus-20260725-102238-2166044`

Measured result:

- exactly one added fixture: `std_regex_view_offsets_alternation`;
- no removed fixtures;
- failed and excluded populations unchanged;
- zero content-hash changes among the 924 pre-existing fixtures;
- every pre-existing fixture has the same modal delta:
  `events -3`, `c3_moveout_owned -3`,
  `site_class:moveout_expansion -3`;
- zero outliers;
- the new fixture contribution is itemized;
- every aggregate counter reconciles with residual zero;
- all hard gates are zero.

The checked-in `reviewed-baseline` has NOT been promoted. The retained run is
eligible for promotion only if the production regex success path remains
unchanged through static closure. If a hot-path amendment is accepted, rerun
the corpus and attribute the new result before promotion.

## 5. Remaining static-review HOLD

### 5.1 Measure the remaining hot-loop overhead

Use scratch builds first; do not mutate production based only on intuition.
Compare the current executor against:

1. workspace/program-size validation moved out of
   `_try_match_at_scratch`, where it currently executes once per candidate
   start, and performed once per top-level search;
2. matching successors collected into one shared worklist and drained once per
   byte, rather than calling `_closure_into` separately for every matching
   state.

Measure the representative small-subject and wide-alternation suites. Adopt a
change only if it produces a material, repeatable improvement without semantic
movement. Record rejected ablations too. Any accepted production change
invalidates the retained corpus run and requires a fresh one.

### 5.2 Preserve a durable regex performance protocol

The permanent allocation tooth protects complexity, but `just perf-protocols`
currently has no regex timing surface. Add a compact shipped-engine protocol
under `tools/perf/` and wire it into `just perf-protocols`, covering at least:

- a 256 B representative subject;
- a 4 KiB representative subject;
- a wide-alternation subject.

It may remain a read-only/manual-review protocol like the existing String
protocols. The count-exact test remains the machine-independent hard gate.

### 5.3 Harden corpus attribution

`bench/attribute_corpus.py` currently prints the expected universe properties
but does not fail on all violations. Make it fail closed unless:

- the only addition is `std_regex_view_offsets_alternation`;
- there are no removals;
- the failed/excluded populations are unchanged;
- no pre-existing fixture hash changes.

Residual-zero and hard-gate checks remain mandatory.

### 5.4 Correct records and wording

- Replace “allocation-free matching” with “constant-allocation matching” or
  “allocation-free inner loop”; each top-level search still performs one real
  allocation.
- Say the failed/excluded populations are unchanged, not that the entire
  universe partition is unchanged.
- Correct the new fixture’s first-view comment: `byte_view(&s, 7, 11)` covers
  the complete `id42,buffer` suffix, not only `id42`.
- Ensure the history, checkpoint, progress record, and final announcement use
  the same terms.

### 5.5 Release sequencing

Do not certify this branch independently. The preceding 0.33.88 candidate is
blocked by the separately confirmed pure-String allocation/drop/equality
performance regression. Regex should merge with the String hot-path recovery
and receive one final certification using the compiler version and runtime ABI
selected by that combined candidate. `0.33.89/ABI 22` is therefore provisional.

## 6. Exact restart sequence

1. Confirm the intended branch and inspect `git diff`; do not overwrite or
   discard unrelated work.
2. Re-read this file, the checkpoint, and the final review announcement.
3. Run the two hot-loop ablations in isolated scratch trees.
4. Apply only evidence-backed production changes.
5. Add the durable regex performance protocol.
6. Harden the attribution script and correct all wording.
7. Run focused semantic, allocation, view, valgrind, and performance gates.
8. If production changed, rerun and re-attribute the full corpus. Otherwise
   revalidate the retained corpus artifacts.
9. Return for final static delta review.
10. After clearance, promote the reviewed corpus baseline.
11. Run `./run-all-tests.sh` on the final combined candidate.
12. Merge with the String hot-path recovery and perform one certification and
    deployment—not a separate regex certification.

## 7. Known process incident

A stray `git stash` command was executed during an earlier compile-cost
measurement. The maintainer restored the sole stashed file
(`regex.drift`), and every reported result was re-measured afterward on the
restored tree. No stash operation is part of the resume procedure.

## 8. Pause disposition

Safe to switch away now. Do not promote the corpus baseline or start
certification while paused. Preserve this branch or a user-created recovery
commit so the uncommitted implementation cannot be lost.
