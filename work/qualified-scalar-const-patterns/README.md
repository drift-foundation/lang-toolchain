# Qualified scalar const patterns

Allow scalar integer `match` arms to use **module-qualified** const references:

```drift
import tokens;
match tok {
    tokens.TOK_EOF   => { ... }
    tokens.TOK_IDENT => { ... }
    default          => { ... }
}
```

This extends the unqualified named-const pattern support (committed in
`699b6733`) to the cross-module case. Baseline commit: `699b6733`. Branch:
`feature/scalar-match-jump-table` (stacked on the named-const work; the user
declined a separate branch).

## Requirements (from the slice brief)

1. Preserve existing variant-constructor pattern behavior.
2. Parse qualified **const** refs (`mod.NAME`, DOT) distinctly from qualified
   **variant ctors** (`Base::Ctor`, DCOLON).
3. Only allow qualified const refs in **scalar integer** matches.
4. Resolve qualified const refs **only** through the named module/export path —
   never lexical scope, never current-module consts.
5. Preserve signedness/range validation via `scalar_const_pattern_value`.
6. Reject qualified names that don't resolve to integer scalar consts with
   `E-MATCH-SCALAR-CONST`.
7. Stage2 consumes only the checker-set `arm.scalar_value`; no lowering
   reinterpretation.
8. e2e: cross-module token consts, shadowing (current-module const must NOT win),
   signedness mismatch, unknown qualified const, variant-ctor no-regression.
9. ABI unchanged (grammar/checker/lowering surface only; no compiler/runtime
   boundary change).

## Refactor-triggers scan

`doc/refactor_triggers.md` reviewed before starting. Registered triggers cover
borrow-checker walker consolidation, DMIR discriminators, drop-aware RawBuffer,
and implicit-move materialization — **none apply**. This is a grammar/checker/
lowering surface feature, confirmed not a runtime change. No escalation.

## Design

### Grammar (LALR-safe)

New `match_pat` alternative: `NAME DOT NAME -> match_qual_const`. Distinct from
`qualified_member` (which always requires `DCOLON`). LALR(1)-decidable: after
`NAME DOT NAME`, lookahead `FATARROW` reduces to the const pattern, while
`DCOLON`/`QUAL_TYPE_LT` continue the variant-ctor production — disjoint follow
sets, no shift/reduce conflict.

### AST fields (parser-owned raw data, like `scalar_literal_kind`)

Two new fields on `MatchArm`/`HMatchArm` (all 3 AST layers):
- `scalar_const_qual_base: Optional[str]` — module alias as written; the parser
  resolution pass rewrites it **in place** to the resolved module id (mirrors
  `TypeExpr.module_id`). Used for `lookup_const` and diagnostics.
- `scalar_const_qual_name: Optional[str]` — the const name.

### Resolution

`_resolve_types_in_block` (parser/__init__.py) resolves the base alias →
module id via `file_aliases` (same map + direct-module-path fallback used for
`ctor_base`). This is the ONLY resolution path; the checker never consults
lexical scope or the current module for these arms.

**Source-syntax limitation (by design, v1):** the pattern is strictly
`NAME.NAME` (one dot, two names). The `base` is therefore always a single
segment — an import alias (`import my.tokens as tok` → `tok.X`) or a
single-segment module spelled outright. A *dotted* module path
(`acme.tokens.TOK`, three+ names) does **not** parse as this pattern and
requires an `as` alias; there is no longer qualified value-path pattern form.
The "direct-module-path fallback" only covers the case where the single `base`
segment is itself a known module id (alias == module, no `as` needed).

### Checker

New branch in the scalar-arm loop (typed checker), gated on
`scalar_const_qual_name is not None`: resolve via `lookup_const(f"{base}::{name}")`
ONLY; validate with `scalar_const_pattern_value`; value-dedup; set
`arm.scalar_value`. Reject non-resolving / non-integer-scalar with
`E-MATCH-SCALAR-CONST`. Reject a qualified const pattern in a non-scalar match.
Stub checker (`checker/__init__.py`) defers these arms (no lexical scope).

### Stage2

No reinterpretation — `_lower_scalar_match` already partitions on
checker-set `scalar_value`. The three "default arm" detections
(`ctor is None and scalar_literal_kind is None`) are tightened to also exclude
qualified-const arms, so an unresolved qual-const arm fails loud (assertion)
rather than being silently treated as `default`.

## Threading map (3 defs + 6 construction sites)

- defs: `parser/ast.py`, `stage0/ast.py`, `stage1/hir_nodes.py`
- ctor: `parser/parser.py` (parse), `parser/__init__.py` (→stage0),
  `stage1/ast_to_hir.py` (×2: renamer + main), `stage1/borrow_materialize.py`,
  `stage1/place_canonicalize.py`
- resolve: `parser/__init__.py` `_resolve_types_in_block` MatchExpr arm
- consume: `type_checker.py` (typed), `checker/__init__.py` (defer),
  `stage2/hir_to_mir.py` (×3 default-detection guards)
