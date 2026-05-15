# Reply to mariadb team — `if` as expression (or lack thereof)

**Re: `if cond { a } else { b }` as inline argument**

You're right that the diagnostic is bad. The root cause is different than the
report framed, though — worth flagging in case it affects how you migrate the
spike.

## What's actually happening

Drift v1's grammar has no `if`-as-expression at all. Both forms in your report
fail with the same parser error from the same gap. Tested on current trunk:

```drift
val n = if v { 1 } else { 0 };                   // REJECTED  (col 10: right after `=`)
return fmt.format_int(if v { 1 } else { 0 });    // REJECTED  (col 10: inside paren)
```

Both give `Unexpected token Token('IF', 'if')`. The val-RHS form you noted as
"works" — either you tested a different shape, or it was working in some
earlier toolchain branch I don't have visibility into. Today's grammar
(`lang/driftc/parser/grammar.lark`) has only `if_stmt: IF if_cond block
else_clause?`. Nothing in the expression grammar admits `if`. So:

- **It's not an inconsistency** between val-RHS and call-arg position.
- **It's a deliberate-or-accidental v1 design choice**: `if` is statement-only.

## The canonical Drift v1 pattern is `match` over a Bool

Stdlib uses this everywhere — e.g. `stdlib/std/log/log.drift:1463`:

```drift
val min = match self.min_level {
    Some(level) => { level },
    None => { self.parent_min_level }
};
```

For your specific case:

```drift
return fmt.format_int(match v {
    true  => { 1 },
    false => { 0 }
});
```

`match` is an expression in Drift v1 and works in every expression position —
call args, return, struct fields, array elements, nested in other matches, etc.

## What we're doing on the toolchain side

We chose to keep `if` statement-only for now (adding `if`-as-expression is a
real language extension — grammar rule, AST, HIR lowering via
`HMatchExpr`-over-Bool, type-check both branches same type, LLVM lowering —
non-trivial). Instead:

1. The cryptic `Unexpected token Token('IF', 'if')` error is being replaced
   with a Drift-specific diagnostic naming `match` as the answer, plus a span
   on the `if` token itself.
2. `docs/effective-drift.md` will gain a "conditional values" note explaining
   the pattern.

If `if`-as-expression is something the bookkeeper / mariadb work would lean on
heavily (not just `format_int(if ... else ...)`-style one-liners but deep
nesting in render-pipeline code, etc.), let us know and we'll re-prioritize
the real-feature slice. Otherwise the `match Bool { true => ..., false => ... }`
pattern is the v1 idiom.

Tracking the diagnostic + doc fix as a small slice; targeting it for the
current release line.

— K
