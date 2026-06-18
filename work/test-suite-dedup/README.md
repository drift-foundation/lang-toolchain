# Test-suite dedup audit — `driver/` + `codegen/e2e/`

**Status:** audit / planning. **No test is deleted as part of this work** until a
candidate has a recorded replacement + risk + validation and is signed off.
**Baseline commit:** `fc77f5b2`.

At baseline: **399** driver test *files* (`lang/tests/driver/*.py`; one file may hold
several pytest functions — function-level counts TBD if needed) and **1312** e2e
fixtures (`lang/tests/codegen/e2e/*/main.drift`). This audit looks for coverage
that can be removed, consolidated, or moved to a more targeted layer **without
weakening regression protection**.

---

## Goal

Identify, with evidence:
- **Redundant coverage** — two+ tests that exercise the same code path with the
  same failure mode.
- **Stale compatibility cases** — tests pinning behavior of removed/renamed
  features, or guarding bugs that are now pinned by a clearly-equivalent regression.
- **Duplicated fixture shapes** — multiple e2e fixtures with effectively the same
  source shape and the same expected behavior.
- **Mis-layered tests** — a full codegen/e2e (or memcheck) test whose real
  assertion is parser/checker-only and would be cheaper and clearer one layer down
  (or vice-versa).

The deliverable of *this* phase is the candidate list in PROGRESS.md, not edits.

## Non-goals

- **Not** deleting tests in the initial pass. Audit first.
- **Not** removing tests for being slow. Slowness alone is never the reason; only
  demonstrable duplication or obsolescence is.
- **Not** weakening LANGUAGE_BUG regression protection (see criteria below).
- **Not** rewriting test infrastructure or the e2e runner.
- **Not** touching suites outside `lang/tests/driver/` and `lang/tests/codegen/e2e/`
  in this audit (parser/checker/stage1/stage2 unit suites are out of scope except
  as *destinations* for a "move layer" proposal).

---

## Governing standard (reviewer) — default is KEEP

> **It is better to be wrong and keep a test than to remove coverage accidentally.**

The default decision for every test is **keep**. Removal requires a **concrete
coverage argument** — a specific surviving test that exercises the *same code path
and same failure mode* — not similar names or similar source shape. When in doubt,
classify **keep** or **needs investigation**, never "safe remove".

**Targeted vs incidental coverage.** The named replacement must be **targeted**
coverage: a test whose *purpose* is the path/failure mode in question, so it will
keep exercising it and would visibly fail if that path regressed. **Incidental
overlap does not qualify** — a broad integration/e2e test that merely *happens* to
touch the path as a side effect is not a replacement, because an unrelated edit to
that test (or its fixture) can silently stop covering the path without anyone
noticing. A `safe remove` candidate must state whether the replacement is targeted
and why it is stable (won't be refactored away from the path); if the only overlap
is incidental, the candidate is at most **needs investigation**, never safe remove.

**Hard rules — these are NOT duplicates of each other:**
- a **compile-only driver** test vs a **runtime e2e** test (different failure modes);
- a **memcheck** test vs a **functional e2e** test (leak/UAF vs result);
- a **package / cross-module** test vs a **single-module** test (ABI/boundary
  behavior vs in-module);
- a **diagnostic-shape** test vs a **runtime-semantic** test (rejection vs behavior);
- a **LANGUAGE_BUG regression** vs anything that doesn't pin the *same bug trigger*
  at the same-or-stronger layer.

A candidate that matches any hard rule is **keep** (or at most **needs
investigation**), regardless of surface similarity.

**Classification buckets** (every candidate gets exactly one):
- **safe remove** — coverage is *strictly* duplicated and the replacement is named
  and verified. This is the only bucket that authorizes deletion, and only after
  sign-off + validation.
- **merge candidate** — could consolidate (e.g. several same-shape e2e into one
  parameterized fixture, or a "move layer" to parser/checker unit), but needs
  review; no coverage lost in the merge.
- **keep** — similar-looking but guards a distinct layer or bug trigger.
- **needs investigation** — unclear; **do not remove**.

Bias: anything not *obviously* safe remove goes to **keep** or **needs
investigation**. "move layer" proposals are **merge candidate** at most (they change
where coverage lives, so they need review), never "safe remove".

## Criteria — "duplicate enough to remove / consolidate"

A candidate qualifies only if **all** of these hold:
1. **Same code path.** The tests drive the same compiler/runtime path (same lowering
   branch, same diagnostic, same runtime behavior), not merely a similar surface.
2. **Same failure mode.** If test A regressed, test B would fail too — and vice
   versa. (If A can fail while B stays green, they are not duplicates.)
3. **No unique fixture value.** The source shape isn't a distinct edge case
   (boundary value, ordering, nesting, cross-module form) that the "kept" test
   doesn't also exercise.
4. **Replacement is named.** There is a specific surviving test (or a cheap new one)
   that retains the coverage, recorded in the candidate entry.
5. **Not a LANGUAGE_BUG pin** unless an equivalent pin demonstrably survives
   (criterion below).

## Criteria — "keep despite apparent duplication"

Keep when **any** of these hold:
- **Different layer, different failure mode.** A driver *compile-only* test and an
  e2e *runtime* test over similar source are usually **not** duplicates: the driver
  test catches "stops compiling / wrong diagnostic", the e2e catches "compiles but
  miscompiles / leaks at runtime". A regression can hit one without the other.
  (This is the single most important keep rule — see the call-out below.)
- **LANGUAGE_BUG regression** with no clearly-equivalent surviving pin. Default to
  keep. A bug regression encodes a specific historical miscompile; only fold it away
  if another test pins the *same* root cause (not just the same surface).
- **Distinct edge case** in an apparently-uniform cluster: boundary/overflow,
  signed-vs-unsigned, high-bit, empty/nesting, error-arm vs ok-arm, cross-module
  vs single-module.
- **Memcheck vs functional.** A memcheck/ASAN fixture and a plain exit-code fixture
  over the same source guard different things (leak/UAF vs result) — keep both
  unless one is strictly subsumed.
- **Negative vs positive.** A rejection test and a runtime-success test over the
  same feature are complementary, never duplicates.

### Driver/e2e pairing — the key distinction

> A driver test that **compiles** a source and asserts a diagnostic (or clean
> compile) and an e2e fixture that **runs** a similar source and asserts an exit
> code/stdout are covering **different failure modes**. Treat them as
> complementary by default. Only flag as duplicate if both assert the *same* thing
> at the *same* layer (e.g. two e2e fixtures that both just check exit code 0 over
> the same shape, or two driver tests asserting the same diagnostic on the same
> source).

Conversely: **multiple e2e fixtures with the same source shape and same expected
behavior** (differing only cosmetically) are the prime consolidation candidates.

---

## Audit method

1. **Inventory** (PROGRESS.md): counts per suite; e2e fixtures clustered by
   name-prefix; driver tests clustered by prefix and tagged compile-only vs
   runtime-asserting.
2. **Cluster review.** For each large e2e name-cluster (e.g. `std_text` ×57,
   `std_json` ×43, `local_const` ×24), diff the `main.drift` shapes and
   `expected.json` to separate "distinct edge case" from "same shape, cosmetic
   variation".
3. **Driver↔e2e cross-map.** For shared themes, classify each driver test as
   compile-only or runtime, and pair with the e2e fixture; apply the pairing rule —
   keep complementary pairs, flag same-layer/same-assertion overlaps.
4. **LANGUAGE_BUG sweep.** Identify every test that pins a fixed LANGUAGE_BUG (docstring/comment
   references, `project_*` memory links). Mark **keep** unless an equivalent pin is
   located and cited.
5. **Per-candidate write-up** in the format below; nothing is removed in this phase.
6. **Validation plan** per candidate (what to run to prove the replacement holds)
   recorded before any future deletion.

## Proposed categories

Each test gets one primary category (for cross-mapping and "move layer" proposals):
- **parser/checker-only** — assertion is a parse/type diagnostic; no codegen needed.
- **driver compile-only** — compiles via `driftc`, asserts diagnostic or clean
  compile; does not run a binary.
- **codegen e2e** — compiles, runs the binary, asserts exit code / stdout.
- **memcheck** — runtime under valgrind/ASAN; asserts no leak/UAF.
- **package / cross-module** — publish→consume, multi-module, ABI boundary.
- **deployment / PEX** — packaging, PEX, deploy-shape tests.

## Expected output format — removal candidates

Each candidate recorded in PROGRESS.md exactly as:

```
Candidate: <test path>
Current purpose:
Overlaps with:
Classification: safe remove | merge candidate | keep | needs investigation
Coverage argument:        # REQUIRED for "safe remove": the same-path/same-failure-mode proof
Replacement coverage:     # the specific surviving/new test that retains coverage
Replacement is targeted coverage? yes/no — why stable?   # incidental overlap does NOT qualify
Risk:
Validation needed:        # what to run to prove the replacement holds before any deletion
```

Rules for the form:
- **`safe remove` requires a non-empty `Coverage argument`** naming the exact
  surviving test and why it fails iff this one would. No argument → not safe remove.
- **`safe remove` requires `Replacement is targeted coverage? = yes`** with a
  stability reason. If the replacement only *incidentally* overlaps, the candidate is
  **needs investigation** at most.
- A hard-rule match (compile-only vs runtime, memcheck vs functional, package vs
  single-module, diagnostic vs runtime, LANGUAGE_BUG) forces **keep** /
  **needs investigation**.
- **`keep` entries are recorded too** (in the "Keep" list) — documenting *why we
  kept* a duplicative-looking test is as valuable as proposing a removal.

## References
- Inventory + candidate/keep lists: `PROGRESS.md`.
- LANGUAGE_BUG pins: memory `MEMORY.md` "Recently-fixed LANGUAGE_BUGs" /
  "Implemented features" sections and the linked `project_*` files.
- e2e runner: `lang/tests/codegen/e2e/runner.py`. Driver-test scaling rule:
  memory `feedback_driver_test_compile_timeout_scaling.md`.
