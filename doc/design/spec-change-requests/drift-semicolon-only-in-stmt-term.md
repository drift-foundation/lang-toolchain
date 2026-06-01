# Change request: Semicolon-only statement termination and match as expression + compound statement

## Summary

This change removes newline-as-terminator entirely and makes statement termination explicit and predictable:

- Newlines and whitespace have **no lexical meaning** (beyond separating tokens and ending `//` comments).
- **Simple statements** require a trailing `;`.
- **Compound statements** (those that contain a `{ ... }` block) **must not** be followed by `;`.
- `match` remains an **expression** (for assignment/return/etc.) and is also allowed as a **compound statement** (so standalone `match` does not require `;`).
- `match` arms are **comma-separated**, with an optional trailing comma.

This preserves the nice expression form:

```drift
val y: Int = match x {
	0 => { log("zero"); 10 },
	_ => { 20 },
};
```

while also allowing standalone `match` without an ugly trailing semicolon:

```drift
match x {
	0 => { log("zero"); },
	_ => { },
}
```

## Motivation

The previous hybrid model (“newline is sometimes a terminator”) creates confusing context-sensitive behavior and complicates both lexer and parser:

* “Works at top-level but not inside blocks” surprises.
* Increased parser/lexer coupling (depth-sensitive newline handling).
* Harder-to-stabilize diagnostics and formatting rules.

This change makes syntax uniform and predictable:

* `;` is the only statement terminator.
* Newlines are formatting only.
* Block-carrying statements are structurally complete and do not accept `;`.

Separately, requiring commas between `match` arms keeps whitespace semantically meaningless and avoids the temptation to use `;` as an arm separator.

## Design goals

* Clarity: no context-dependent newline meaning.
* Flexibility: formatting changes never affect semantics.
* Correctness: unambiguous statement boundaries and stable error recovery points.
* Consistency: `if/while/for/try/match` are treated uniformly as compound statements.

## Proposed language rules

### Lexing

* Remove newline-as-terminator. Newlines are whitespace.
* `TERMINATOR` is `;` only.

### Statements

* `SimpleStmt` **must** end with `;`.
* `CompoundStmt` **must not** end with `;`.
* If a `;` appears immediately after a compound statement, it is a parse error with a dedicated diagnostic.

### Match

* `match` is an expression (`MatchExpr`) usable wherever expressions are allowed.
* `match` is also allowed as a compound statement (`MatchStmt`) so standalone `match` does not require `;`.
* Match arms require commas as separators; optional trailing comma is allowed.
* Expression-form `match` uses `ValueBlock` arms; statement-form `match` requires `Block` arms only (no values).

## Grammar changes (EBNF)

### Terminators and whitespace

**Before (conceptually):**

* Lexer could emit `TERMINATOR` on newline in some cases.

**After:**

* `TERMINATOR ::= ";"` only.
* Newline never produces a token with syntactic meaning.

### Statement classification

```
Stmt ::= CompoundStmt
       | SimpleStmt ";"

CompoundStmt ::= Block
               | IfStmt
               | WhileStmt
               | ForStmt
               | TryStmt
               | MatchStmt
               // (plus other block-carrying constructs as applicable)

SimpleStmt ::= ValDeclStmt
             | AssignStmt
             | ReturnStmt
             | BreakStmt
             | ContinueStmt
             | ExprStmt
             // (others as applicable)
```

### Control flow (examples)

```
WhileStmt ::= "while" Expr Block
ForStmt   ::= "for" Ident "in" Expr Block
IfStmt    ::= "if" IfCond Block ElseClause?
ElseClause ::= "else" Block

TryStmt ::= "try" (Block | Expr) CatchClause+
CatchClause ::= "catch" CatchBody
CatchBody ::= Block | ValueBlock
```

### Match

```
MatchExpr ::= "match" Expr "{" MatchExprArms "}"
MatchStmt ::= "match" Expr "{" MatchStmtArms "}"

MatchExprArms ::= MatchExprArm ("," MatchExprArm)* ","?
MatchExprArm  ::= Pattern ("if" Expr)? "=>" MatchExprArmBody
MatchExprArmBody ::= ValueBlock

MatchStmtArms ::= MatchStmtArm ("," MatchStmtArm)* ","?
MatchStmtArm  ::= Pattern ("if" Expr)? "=>" MatchStmtArmBody
MatchStmtArmBody ::= Block
```

## ValueBlock rule (unchanged, but now more important)

Statements inside blocks require `;`, but `ValueBlock` returns its final expression without `;`.

* Valid:

```drift
val x: Int = try foo() catch { log("x"); 0 };
```

* Invalid (final `Expr` in `ValueBlock` should not be `ExprStmt`):

```drift
val x: Int = try foo() catch { 0; }; // reject
```

## Diagnostics requirements

Add/ensure stable diagnostic codes for syntax errors introduced/clarified by this change:

* `E_EXPECTED_SEMICOLON` (or `E_MISSING_TERMINATOR`) for missing `;` after `SimpleStmt`.
* `E_UNEXPECTED_SEMICOLON_AFTER_COMPOUND` when a `;` follows `CompoundStmt` (e.g. `while ... { ... };`).

These codes must be present in JSON diagnostics.

## Test plan

Add driver tests that lock the behavior:

1. **Missing semicolon in block is an error**

```drift
fn main() nothrow -> Int {
	val a: Int = 1
	return a;
}
```

Assert:

* error severity
* code == `E_EXPECTED_SEMICOLON`

2. **Compound statements do not accept trailing semicolons**

```drift
fn main() nothrow -> Int {
	while true { break; };
	return 0;
}
```

Assert:

* error severity
* code == `E_UNEXPECTED_SEMICOLON_AFTER_COMPOUND`

3. **Match arms require commas**

```drift
fn main() nothrow -> Int {
	val y: Int = match 1 {
		0 => { 10 }
		_ => { 20 },
	};
	return y;
}
```

Assert:

* error severity
* code == (new or existing parse error code indicating missing comma / unexpected token)

4. **Expression match and statement match both work**

```drift
fn main() nothrow -> Int {
	val y: Int = match 1 { 0 => { 10 }, _ => { 20 }, };
	match y { 10 => { }, _ => { }, }
	return y;
}
```

Assert:

* compile succeeds

5. **ValueBlock final expression stays expression-only**

```drift
fn main() nothrow -> Int {
	val x: Int = try foo() catch { log("x"); 0 };
	return x;
}
```

Assert:

* compile succeeds

## Documentation updates

Update all Drift doc/spec examples to reflect `;` requirements:

* Add `;` after simple statements in code fences.
* Ensure compound statements are not shown with trailing `;`.
* For `match`, show commas between arms and optional trailing comma.

## Migration notes

This is a mechanical change for most code:

* Add missing `;` at the end of simple statements.
* Remove any reliance on newline-as-terminator (especially at top-level).
* Replace any match-arm separation by newline/terminator with commas.
* Remove trailing `;` after compound statements (if any existed in code or tests).

## Out of scope

* Any “automatic semicolon insertion” scheme.
* Indentation-sensitive parsing.
* Changes to expression precedence unrelated to `match`/statement termination.
