# PROGRESS — qualified scalar const patterns

Running log, newest on top. Design + threading map in `README.md`.
Baseline: `699b6733` (named-const patterns committed). ABI 17, target unchanged.

## Status

| Step | Deliverable | State |
|------|-------------|-------|
| 0 | Refactor-triggers scan | ✅ none apply (grammar/checker/lowering surface) |
| 1 | Grammar: `match_qual_const` (`NAME.NAME`) | ✅ builds, no LALR conflict; pattern-node list updated |
| 2 | AST fields (3 defs) + 6 construction sites | ✅ threaded |
| 3 | Alias resolution in `_resolve_types_in_block` | ✅ alias→module id via `file_aliases` + direct-path fallback |
| 4 | Checker: typed qual-const branch + stub defer + non-scalar reject | ✅ all three |
| 5 | Stage2: tighten 3 default-detection guards | ✅ exclude qual-const arms |
| 6 | e2e: cross-module, **`as`-alias**, **const re-export**, shadowing, signedness, unknown, **non-scalar reject**, variant no-regress | ✅ 7 new fixtures green |
| 7 | Full suite green | ✅ 34 scalar e2e + 755 pytest (parser/checker/type_checker/stage1/stage2) |

## Log

### 2026-06-18 — version bump + history entry
- `DRIFTC_VERSION` 0.33.41 → **0.33.42** (`lang/versions.py`); ABI stays **17**.
- Added `doc/history.md` top entry (2026-06-18, 0.33.42) covering: named/local
  scalar-const fixes, qualified scalar-const patterns (strict `NAME.NAME`), alias
  + re-export support, scalar/integer-only validation, the stub-checker default-arm
  fix, and the added regressions.
- No test hardcodes the version string (the 3 driver version tests import
  `DRIFTC_VERSION` dynamically). `test_abi_version_stamp.py` re-verified.
  Nothing committed.

### 2026-06-18 — review verification green
- After F1 fix + 4 new regressions: **38/38** scalar-match e2e; **362 passed**
  pytest (checker/type_checker/parser). ABI 17 unchanged. `DRIFTC_VERSION` bump to
  0.33.42 still OUTSTANDING (pre-cert action). Nothing committed.

### 2026-06-18 — whole-branch static review (pre-cert)
- **Finding F1 (Low, latent — FIXED): stub checker miscounts a qual-const arm as
  the default arm in bool/variant matches.** `checker/__init__.py:3682` (bool) and
  `:3761` (variant) guarded `scalar_literal_kind` but not `scalar_const_qual_name`,
  so a `tok.X` arm (ctor=None) was treated as `default`. Masked (never a false
  accept) because the typed checker runs first and aborts on its
  `E-MATCH-SCALAR-CONST` rejection (type_checker.py:7893) before the stub runs —
  but it violates the documented "every `arm.ctor is None` default check must also
  exclude scalar/qual-const arms" rule and breaks stub self-consistency. Fixed by
  deferring qual-const arms (`continue`) before the default check in both stub
  paths, mirroring the scalar stub path. Corroborated by 2 audit agents +
  empirical probes.
- **Verified OK (no bug):** threading complete across all 3 AST defs + 6
  construction sites (no old/new asymmetry; synthetic arms at const_share_synth /
  for-desugar correctly omit both); `match_qual_const` in both parser pattern-kind
  lists (parser.py:3517 membership + :3669 dispatch); all stage2 + typed-checker
  default classifiers carry the full triple guard; alias/re-export resolution
  goes through the value-expr `file_aliases` path; qual resolution never consults
  lexical/current-module; stage2 fails loud (assert) on an unresolved qual-const
  arm; signedness via `scalar_const_pattern_value`; value-dedup shares one set.
- **Coverage gaps (MISSING regressions) — all ADDED:** qual const after default
  (`scalar_match_qual_const_after_default_rejected`), duplicate-by-value
  literal-vs-qual (`scalar_match_qual_const_duplicate_rejected`), qual const in a
  Bool match (`scalar_match_qual_const_in_bool_match_rejected`), qual const
  resolving to non-integer/String (`scalar_match_qual_const_non_integer_rejected`).
  All 4 green.
- **Item 8 (version):** ABI 17 unchanged is CORRECT — no compiler/runtime boundary
  symbol, layout, calling-convention, or intrinsic changed. BUT `DRIFTC_VERSION`
  is still 0.33.41 (last bumped for the base scalar-match feature, `8b75fe48`);
  this branch adds user-visible syntax (named/local + qualified const patterns)
  and per house style must bump `DRIFTC_VERSION` → 0.33.42 + add a `history.md`
  entry BEFORE cert. Not yet done on the branch — flagged, not applied (release
  prose is the user's to own).

### 2026-06-18 — review asks addressed + full suite green
- **Negative pin (Medium):** added e2e
  `scalar_match_qual_const_in_variant_match_rejected` — a qualified const ref
  (`tok.A`) against a VARIANT scrutinee is rejected with
  `E-MATCH-SCALAR-CONST: qualified const pattern 'codes.A' is only valid when
  matching an integer scalar …`. Directly pins requirement #3 (qual const refs
  only in integer scalar matches), not just the checker branch.
- **Wording clarified:** the "direct-module-path fallback" comment
  (parser/__init__.py) and README now state the source syntax is strictly
  `NAME.NAME` — a dotted module path (`acme.tokens.TOK`) needs an `as` alias;
  the fallback only covers a single `base` segment that is itself a module id.
- **Full suite green:** 34 scalar-match e2e (27 prior + 7 new) + **755 passed**
  pytest across parser/checker/type_checker/stage1/stage2 (0 failed, 13m47s).
  ABI 17 unchanged; nothing committed.

### 2026-06-18 — implementation landed
- Grammar `match_qual_const` (`NAME DOT NAME`); builds, no LALR conflict; pattern
  discovery list updated. 2 new arm fields threaded through 3 AST defs + 6
  construction sites. Alias resolution in `_resolve_types_in_block` (same
  `file_aliases` path as value exprs). Typed checker resolves via module path
  ONLY + signedness via `scalar_const_pattern_value` + non-scalar reject; stub
  checker defers. Stage2's 3 default-detection guards tightened (qual-const arms
  excluded → unresolved fails loud); lowering still consumes only `scalar_value`.
- 6 happy/negative e2e green at this point (alias value, re-export, shadowing,
  signedness reject, unknown reject, variant-ctor distinction).

### 2026-06-18 — slice opened
- Scanned `doc/refactor_triggers.md`: no registered trigger matches (this is a
  grammar/checker/lowering surface feature, not a runtime change). No escalation.
- Completed the audit: grammar is LALR-safe (`NAME DOT NAME` vs the DCOLON
  variant form — disjoint lookahead). Threading map = 3 AST defs + 6 construction
  sites + 1 resolution pass + checker (2 frontends) + 3 stage2 default-detection
  guards. Confirmed stage2 already consumes only `arm.scalar_value`.
- User committed prior named-const work as `699b6733`; declined a separate branch,
  so this slice stacks on `feature/scalar-match-jump-table`.
- Follow-up: qualified const refs must follow the SAME module/export resolution
  as value expressions (incl. `import … as alias` and const re-exports
  materialized into the exporting module's const table). Design already does this
  (resolve alias→module id via the same `file_aliases` map; `lookup_const` sees
  re-exports). Must PIN both the `as`-alias form and the re-export form in e2e.
  Keep the `Type::Ctor` (variant ctor) vs `mod.CONST` (qualified const) distinction.
