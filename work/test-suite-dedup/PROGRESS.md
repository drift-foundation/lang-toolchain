# PROGRESS — test-suite dedup audit

Running log. Newest entries at top of the Log section. See `README.md` for scope,
criteria, and the candidate format. Baseline commit at creation: `fc77f5b2`.

**Phase:** inventory done; cluster review NOT started. **No removals proposed yet —
every entry below is provisional.**

---

## Inventory (baseline `fc77f5b2`)

| Suite | Count | Notes |
|-------|-------|-------|
| `lang/tests/driver/*.py` | 399 **files** | file count, not pytest-function count (a file may hold several); ~160 files invoke a binary / assert exit code, remainder compile-only diagnostic tests. Function-level count TBD if it changes any decision. |
| `lang/tests/codegen/e2e/*/main.drift` | 1312 fixtures | one fixture per dir (`main.drift` + `expected.json`) |

### e2e name-prefix clusters (2-token prefix, top 30)

Large clusters are *candidate areas to review*, NOT presumed duplicates.

| Cluster | # | First-pass read |
|---------|---|-----------------|
| `std_text` | 57 | stdlib API coverage — review for same-shape variants vs distinct API surface |
| `std_json` | 43 | likely per-API-method; review encode/decode/err split |
| `local_const` | 24 | const-eval shapes — high chance of same-shape variation |
| `std_net` | 23 | network API surface |
| `std_io` | 23 | IO; some are memcheck-paired (extern-leak family) — likely KEEP |
| `std_sync` | 16 | concurrency primitives |
| `std_time` | 15 | time API |
| `std_regex` | 15 | regex API |
| `scalar_match` | 15 | **KEEP — see Keep list (distinct per-type / per-diagnostic)** |
| `om_array` | 15 | ownership-matrix array; drop-correctness — likely KEEP |
| `concurrent_channel` | 15 | channel slice; runtime + close semantics |
| `lockfree_mpsc` | 14 | MPSC queue |
| `concurrent_cancel` | 13 | cancellation paths |
| `borrow_struct` | 14 | borrow-checker struct shapes |
| `struct_ref` | 12 | — |
| `std_runtime` | 12 | — |
| `std_crypto` | 12 | — |
| `concurrent_spawn` | 12 | — |
| `borrow_array` | 11 | — |
| `qualified_ctor` | 10 | — |
| `callable_fn` | 10 | — |

(Full distribution captured by the cluster command in the Log.)

### driver test-prefix clusters (top, abbreviated)

| Cluster | # | Note |
|---------|---|------|
| `const_share` | 13 | const-share synthesis; many are LANGUAGE_BUG pins → default KEEP |
| `pub_error` | 6 | — |
| `hidden_lambda` | 4 | capture-id collision family (LANGUAGE_BUG) → KEEP |
| `for_in` | 4 | pairs with `for_in` e2e (8) — apply pairing rule |
| `match_arm` / `match_by` / `match_stmt` | 3+3+2 | match LANGUAGE_BUG pins → review against e2e, default KEEP |
| `std_json` / `std_log` / `struct_ref` | 2–3 each | driver(compile) vs e2e(runtime) — apply pairing rule |

---

## Candidate duplicates (provisional — none approved for removal)

_None yet — cluster review not started._ **Default decision is keep** (see README
"Governing standard"); removal needs a concrete same-path/same-failure-mode coverage
argument, not name/shape similarity. Entries use the README format:

```
Candidate: <test path>
Current purpose:
Overlaps with:
Classification: safe remove | merge candidate | keep | needs investigation
Coverage argument:        # REQUIRED for "safe remove"
Replacement coverage:
Replacement is targeted coverage? yes/no — why stable?   # incidental overlap does NOT qualify
Risk:
Validation needed:
```

Buckets: **safe remove** (strictly duplicated + named replacement — only bucket that
authorizes deletion) · **merge candidate** (consolidate w/ review, incl. "move
layer") · **keep** · **needs investigation** (do not remove). Bias to keep /
needs investigation for anything not *obviously* safe remove.

First clusters to work, in priority order (largest same-shape potential first):
1. `local_const` (24) — const-eval variants; check for cosmetic-only differences.
2. `std_json` (43) — split encode/decode/err; look for repeated round-trip shapes.
3. `std_text` (57) — largest; likely mostly distinct API surface, but verify.

---

## Keep list (looks duplicative, guards a different layer/failure mode)

> Worked examples seeding the rule; expand during the audit.

**`scalar_match_*` (15 e2e).** Looks like one cluster but each fixture guards a
distinct thing: one **positive runtime** per scalar type (`int_value`, `uint`,
`byte`, `int32_negative`, `uint32_highbit`, `uint64_highbit`) — different widths /
signedness / high-bit bit-patterns, not interchangeable — plus `statement_form`,
plus **negative diagnostics** that each pin a *different* checker error
(`missing_default`, `duplicate`, `byte_overflow`, `unsigned_to_signed`,
`negative_to_unsigned`, `ctor_in_scalar`, `int_literal_on_bool`,
`int_literal_on_variant`). Positives and negatives are complementary; the high-bit
cases guard sign-extension/width bugs the others can't. **Keep all.**

**driver compile-only ↔ e2e runtime pairs (general).** e.g. a
`lang/tests/driver/test_<feature>.py` asserting a diagnostic vs a
`lang/tests/codegen/e2e/<feature>/` asserting exit code: complementary failure
modes (stops-compiling/wrong-diagnostic vs compiles-but-miscompiles/leaks). **Keep
both** unless they assert the same thing at the same layer.

**`std_io_*` memcheck family.** Several `std_io` fixtures pin extern-leak fixes
(e.g. `drift_io_open` path-refcount, `__borrow_tmp` outer-scope drop). Memcheck +
functional guard different failure modes. **Keep** pending confirmation each leak
pin has a distinct trigger.

---

## Open questions

1. Is there an existing e2e "skip/xfail/policy-skip" registry, and are any clusters
   already partially disabled (so apparent duplicates are inert)?
2. For `std_*` clusters: are these generated from a table/spec (making consolidation
   a generator change) or hand-written per case?
3. Do any driver tests duplicate a parser/checker *unit* test exactly (pure
   diagnostic, no driver-specific behavior)? Those are "move layer" candidates that
   would speed the suite without losing coverage.
4. Cost signal: which clusters dominate wall-clock (to prioritize *review effort*,
   not to justify removal)?
5. LANGUAGE_BUG inventory: produce the definitive list of bug-pinning tests in both
   suites (grep `LANGUAGE_BUG` / `project_` / `RESOLVED` references) before any
   consolidation touches a match/borrow/ownership fixture.

## Review outcomes

- _(none yet)_

---

## Log

### 2026-06-18 — review corrections
- Added the **targeted-vs-incidental coverage** rule to the governing standard and
  the candidate format: a `safe remove` replacement must be *targeted* coverage
  (purpose is that path, stable under refactor); incidental overlap from a broad
  test does not qualify → at most **needs investigation**. New required field:
  `Replacement is targeted coverage? yes/no — why stable?`.
- Corrected counts to **driver test files** (399 = `*.py` file count, not
  pytest-function count) in README + inventory table + log.

### 2026-06-18 — reviewer standard adopted
- Baked the conservative standard into README: **default = keep**; "better to wrongly
  keep than wrongly remove"; removal requires a concrete same-path/same-failure-mode
  coverage argument (not name/shape similarity).
- Adopted 4-bucket classification (**safe remove / merge candidate / keep / needs
  investigation**) with bias to keep / needs investigation; "safe remove" is the
  only bucket that authorizes deletion and requires a named, verified replacement.
- Recorded the five hard "NOT duplicates" rules (compile-only↔runtime,
  memcheck↔functional, package↔single-module, diagnostic↔runtime-semantic,
  LANGUAGE_BUG↔non-same-trigger). Updated the candidate format accordingly.

### 2026-06-18 — work area created; inventory captured
- Created `work/test-suite-dedup/` with README + PROGRESS.
- Counts: 399 driver test *files*, 1312 e2e fixtures (baseline `fc77f5b2`).
- Captured e2e 2-token prefix clusters and driver prefix clusters (tables above).
  Cluster command:
  `find lang/tests/codegen/e2e -name main.drift | sed -E 's#.*/e2e/([^/]+)/main.drift#\1#' | sed -E 's/^([a-z0-9]+_[a-z0-9]+).*/\1/' | sort | uniq -c | sort -rn`
- Seeded the Keep list with worked examples (`scalar_match_*`, driver↔e2e pairs,
  `std_io` memcheck) to anchor the "keep despite apparent duplication" rule.
- No tests inspected at the per-fixture level yet; no removals proposed. Audit-only.
