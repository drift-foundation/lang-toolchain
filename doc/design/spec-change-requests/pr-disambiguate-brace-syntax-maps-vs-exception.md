# PR: Disambiguate brace syntax for maps vs record/exception initialization

## Summary

This PR makes brace syntax unambiguous and consistent:

* Map literals remain `{ key: value, ... }` as specified in collection literals. 
* Exception throwing is updated from `{ field: expr }` to `TypeName { field = expr }` to avoid collision with map syntax. (Spec currently says `throw ExcName { field: expr, ... }`.) 
* Grammar gains a dedicated `RecordInitExpr` (`Ident "{" FieldAssignList? "}"`) and updates `ThrowStmt` so it no longer accepts a generic `Expr`. (Grammar currently has `ThrowStmt ::= "throw" Expr TERMINATOR`.) 

## Motivation

Right now, the spec’s exception throw syntax uses map-style `:` fields (`throw InvalidOrder { order_id: ..., code: ... }`).  That collides with the map literal syntax (`{ key: value, ... }`) that the spec also defines. 

We’re standardizing:

* `:` = *map association (expression keys)*
* `=` = *schema-checked record/exception field initialization (identifier fields)*

---

# Changes

## 1) Update the spec (`drift-lang-spec.md`)

### 1.1 Add/expand a normative rule: “Brace forms”

Add a short subsection (either in Chapter 13 near literals, or Chapter 14 near exceptions) defining:

* **Map literal:** `{ key: value, ... }`

  * key/value are arbitrary expressions
  * resolves via `FromMapLiteral` (already described) 

* **Record initializer (struct/exception):** `TypeName { field = expr, ... }`

  * `TypeName` is an identifier that resolves to a struct type or exception event
  * `field` must be an identifier
  * field set must match the declared schema (unknown/missing/type mismatch are compile-time errors)

Also explicitly state:

* record/exception init is **not** a map literal
* maps never use `=`
* record init never uses `:`

### 1.2 Fix exception throw examples

Change all exception throw examples that currently use `:` to use `=`.

Example in Chapter 14.3.2 currently:
`throw InvalidOrder { order_id: order.id, code: "order.invalid" }` 

Update to:

```drift
throw InvalidOrder { order_id = order.id, code = "order.invalid" }
```

Also fix the earlier prose in Chapter 14 intro: it currently says `throw ExcName { field: expr, ... }`. 
Update to:

* `throw ExcName { field = expr, ... }`

### 1.3 Align rethrow guidance

Your spec currently shows “rethrow” by writing `throw e->` in the catch-all example. 
If the language now has a `rethrow` statement, update that example to prefer:

```drift
catch e {
    log(&e)
    rethrow
}
```

---

## 2) Update the grammar (`drift-lang-grammar.md`)

### 2.1 Add record initializer expression

In the **Expressions** section, extend `PrimaryExpr` (currently includes `MapLiteral`)  with a new production:

```ebnf
PrimaryExpr  ::= Literal
              | Ident
              | "(" Expr ")"
              | TupleExpr
              | ArrayLiteral
              | MapLiteral
              | RecordInitExpr
              | MatchExpr
              | TryCatchExpr
              | LambdaExpr

RecordInitExpr ::= Ident "{" FieldAssignList? "}"
FieldAssignList ::= FieldAssign ("," FieldAssign)* (",")?
FieldAssign     ::= Ident "=" Expr
```

Notes:

* This stays unambiguous with `MapLiteral` because `MapLiteral` starts with `{` while `RecordInitExpr` starts with `Ident`.
* Field names are identifiers by syntax, which matches “schema-checked fields”.

### 2.2 Restrict ThrowStmt syntax

Replace:

`ThrowStmt ::= "throw" Expr TERMINATOR` 

With:

```ebnf
ThrowStmt ::= "throw" ExceptionInit TERMINATOR
ExceptionInit ::= Ident ("{" FieldAssignList? "}")?
```

### 2.3 Keep map literal grammar unchanged

Leave:

```ebnf
MapLiteral   ::= "{" (MapEntry ("," MapEntry)*)? "}"
MapEntry     ::= Expr ":" Expr
```

as-is. 

### 2.4 Update grammar notes to mention the split

In the “Notes” section, add a line like:

* `{k: v}` is a map literal; `Type { field = value }` is a record/exception initializer.

---

# Acceptance criteria / checklist

## Spec consistency

* No exception throwing examples use `:` field syntax anymore (map-only). (Fix Chapter 14 intro + 14.3.2 at least.)  
* Map literal chapter continues to define `{ key: value, ... }`. 

## Grammar consistency

* Grammar has a distinct `RecordInitExpr` with `Ident "=" Expr` fields.
* Grammar `ThrowStmt` no longer accepts arbitrary `Expr`.  (this line should be removed/updated)
* Map literal remains `:`.

## Non-goals in this PR

* No compiler changes yet (parser/typechecker/lowering updates happen next PR).
* No decision in this PR about call-style struct construction like `Point(x = 1)` (leave as-is unless you want to explicitly deprecate later).

---

