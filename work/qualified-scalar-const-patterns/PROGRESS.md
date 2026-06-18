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
