# LANG_GAP: expression-form `unsafe { expr }`

## Classification

LANG_GAP / SPEC_GAP — not a bug, but blocks intended ergonomics for any
unsafe API that returns a value.

## Problem

Drift has `unsafe fn` declarations and `unsafe { stmts }` statement blocks,
but no expression-form `unsafe { expr }`.  The grammar defines:

```lark
unsafe_block: UNSAFE block          # statement only (grammar.lark:565)
```

This is referenced only from `compound_stmt` (line 224), never from `expr`.
So this pattern does not parse:

```drift
var buf = unsafe { mem.array_byte_alloc_uninit(n) };
```

### Impact

1. User code cannot call an unsafe function in value position with a narrow
   `unsafe` scope.
2. The only workarounds are:
   - `--allow-unsafe` globally (too coarse)
   - wider `unsafe` blocks than needed (defeats the "minimal unsafe surface" principle)
3. This blocks the intended API shape for `array_byte_alloc_uninit` and
   `array_byte_as_mut_ptr`, and will affect any future unsafe API that
   returns a value.

### Existing breakage: `rawbuffer_read_write` e2e test

The test at `lang/tests/codegen/e2e/rawbuffer_read_write/main.drift` uses
`var buf = unsafe { mem.alloc_uninit<type Int>(2) };` — this syntax does not
parse.  The test passes vacuously because `expected.json` has the key
`"expect"` (typo) instead of `"exit_code"`, so the runner defaults the
expected exit code to the actual parse-failure exit code.

Fix needed: either fix the test's expected.json after this gap is closed,
or rewrite the test to not use the expression form.

## Proposed fix

### Grammar

Add `unsafe` as an expression form, reusing the existing `value_block`
production (which already supports `{ stmts; expr }`):

```lark
// In the primary rule:
primary: literal
       | ident -> var
       | leading_dot
       | cast_expr
       | "(" expr ")"
       | lambda_expr
       | array_literal
       | map_literal
       | unsafe_expr

unsafe_expr: UNSAFE value_block
```

### Parser (`parser.py`)

Handle `unsafe_expr` in `_build_expr` — produce an `UnsafeExpr` AST node
wrapping the inner expression.

### AST → HIR (`ast_to_hir.py`)

Lower `UnsafeExpr` to an `HUnsafeExpr` HIR node that sets `unsafe_context = True`
for the enclosed expression, then yields the inner value.

### Type checker (`type_checker.py`)

When visiting `HUnsafeExpr`, set `unsafe_context = True` in the local scope
for the inner expression (same as `HUnsafeBlock` does for statements).

### HIR → MIR (`hir_to_mir.py`)

Transparent pass-through: lower the inner expression with the unsafe flag
set on the context.

## Files to modify

1. `lang/driftc/parser/grammar.lark` — add `unsafe_expr` production
2. `lang/driftc/parser/ast.py` — add `UnsafeExpr` AST node
3. `lang/driftc/parser/parser.py` — handle `unsafe_expr` in `_build_expr`
4. `lang/driftc/parser/__init__.py` — convert `UnsafeExpr` to HIR
5. `lang/driftc/stage1/hir_nodes.py` — add `HUnsafeExpr` HIR node
6. `lang/driftc/type_checker.py` — type-check `HUnsafeExpr`
7. `lang/driftc/stage2/hir_to_mir.py` — lower `HUnsafeExpr`
8. `lang/tests/codegen/e2e/rawbuffer_read_write/` — fix expected.json + validate

## Regression tests needed

- Positive: `var x = unsafe { unsafe_fn() };` parses and runs
- Negative: `var x = unsafe_fn();` without block → diagnostic
- Negative: `var x = unsafe { safe_fn() };` → warning or pass (TBD)

## Dependencies

- Blocks: idiomatic user-facing API for `array_byte_alloc_uninit`, `array_byte_as_mut_ptr`
- Blocks: any future unsafe API that returns a value
