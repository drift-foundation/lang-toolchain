# Drift Loops and Iterators — Integrated Spec (with Grammar & Iterable)

## 1. Goals

Drift’s looping model centers on a single, composable construct:

    for pattern in iterable { body }

This replaces all C-style loops. There is **no** `for (init; cond; step)`.

**Rationale**

- Favor clear ownership and borrowing over implicit mutation.
- Make iteration a first-class capability (`Iterable` + `Iterator`), not a hard-coded special form.
- Express “this thing can be iterated” as a trait capability, consistent with `Debuggable`, `Destructible`, etc.
- Keep the `for` surface syntax simple while letting the library define new iteration strategies via traits.

All `for` loops are implemented in terms of a unified trait pair:

- `std.iter.Iterator<Item>`
- `std.iter.Iterable<Item, Iter>` with a method `iter()` returning an `Iterator`.

---

## 2. Placement & prelude

Iteration traits live in the standard library:

```drift
module std.iter

trait Iterator<Item> {
    fn next(self: &mut Self) returns Optional<Item>
}

trait Iterable<Item, Iter>
    require Iter is Iterator<Item>
{
    fn iter(self) returns Iter
}
````

The prelude re-exports them:

```drift
module std.prelude

export std.iter.{Iterator, Iterable}
```

The compiler implicitly imports `std.prelude` at the top of every compilation unit (unless a future strict mode disables this), so user code does **not** need to write:

```drift
import std.iter.Iterable
import std.iter.Iterator
```

The main spec (Chapter 8) will state explicitly:

> A `for pattern in expr` loop is well-typed iff `expr`’s type implements `std.iter.Iterable<Item, Iter>` for some `Iter` that implements `std.iter.Iterator<Item>`.

---

## 3. Core traits

### 3.1 `Iterator<Item>`

```drift
trait Iterator<Item> {
    fn next(self: &mut Self) returns Optional<T>
}
```

* Represents a **stateful cursor** over a sequence.
* `next()` advances the cursor and returns:

  * `Some(item)` for the next item, or
  * `None` when the sequence is exhausted.

### 3.2 `Iterable<Item, Iter>`

```drift
trait Iterable<Item, Iter>
    require Iter is Iterator<Item>
{
    fn iter(self) returns Iter
}
```

* Represents the **capability to be iterated**.
* Implemented on:

  * Collections (`Array<T>`, `Map<K, V>`, `Range`, …)
  * Views (`&Array<T>`, `&MyCollection`, …)
  * Custom data sources (streams, generators, etc.)
* `self` can be:

  * `T` (consuming iteration)
  * `&T` / `&mut T` (borrowed iteration)
* Each implementation chooses:

  * The `Item` type it yields.
  * The concrete iterator type `Iter`.

---

## 4. Iterator vs Iterable (conceptual)

They answer different questions:

* **`Iterator<Item>`** – “I *am* an iterator; call `next()` on me.”
* **`Iterable<Item, Iter>`** – “You can **iterate me**; here is how to obtain an `Iterator`.”

You need both:

* `Iterator` is used when you already have a cursor and want to step through it.
* `Iterable` is used by `for` (and any other consumer) to *start* iterating from a value.

Without `Iterable`, you’d be forced to manually build iterators:

```drift
var it = ArrayIter.from(xs)
while let Some(x) = it.next() {
    ...
}
```

With `Iterable`, `for` can do:

```drift
for x in xs {
    ...
}
```

desugaring to:

```drift
var it = xs.iter()
while let Some(x) = it.next() {
    ...
}
```

and it can pick different strategies based on the type of `xs` (`Array<T>`, `&Array<T>`, custom wrapper, etc.).

---

## 5. Foreach loop syntax

```drift
for pattern in expr {
    body
}
```

* `pattern` is a full **pattern** (identifier, tuple, nested variant pattern, etc.).
* `expr` must implement `Iterable<Item, Iter>` for some `Item`/`Iter`.
* There are **no other** `for` forms (no `for (init; cond; step)`).

---

## 6. Desugaring (normative)

A `for` loop:

```drift
for pat in expr {
    body
}
```

is desugared (conceptually) as:

```drift
{
    // `Iterable` comes from std.iter via the prelude
    val __iterable = expr
    var __iter = __iterable.iter()

    loop {
        val __next = __iter.next()
        match __next {
            Some(pat) => {
                body
            }
            None => break
        }
    }
}
```

**Key points:**

* `expr` is evaluated **exactly once**.
* `pat` is a full pattern, so all pattern forms work (`x`, `(k, v)`, `Some(x)`, `Some((k, v))`, etc.).
* Ownership flows according to the `Iterable` implementation:

  * `iter(self: T)` → consuming iteration (`for x in xs` moves `xs`).
  * `iter(self: &T)` → borrowed iteration (`for x in &xs` leaves `xs` usable afterwards).

---

## 7. Borrowed vs owned iteration

### 7.1 Owned (consuming) iteration

```drift
var xs = Array<Int>[1, 2, 3]
for x in xs {
    out.writeln(x)
}
// xs has been moved (consumed)
```

This is enabled by:

```drift
implement<T> Iterable<T, ArrayIter<T>> for Array<T> {
    fn iter(self) returns ArrayIter<T> {
        return ArrayIter(data = self, index = 0)
    }
}
```

### 7.2 Borrowed iteration

```drift
var xs = Array<Int>[1, 2, 3]
for x in &xs {
    out.writeln(x)
}
// xs is still valid after the loop
```

This is enabled by:

```drift
implement<T> Iterable<&T, ArrayRefIter<T>> for &Array<T> {
    fn iter(self) returns ArrayRefIter<T> {
        return ArrayRefIter { data = self, index = 0 }
    }
}
```

Where:

```drift
struct ArrayRefIter<T> {
    data: &Array<T>,
    index: Int
}

implement<T> Iterator<&T> for ArrayRefIter<T> {
    fn next(self: &mut self) returns Optional<&T> {
        if self.index >= self.data.len() { return None }
        val r = &self.data[self.index]
        self.index += 1
        return Some(r)
    }
}
```

---

## 8. Pattern support

`pattern` in `for pattern in expr` is any valid **pattern** (the same grammar used in `match`):

* **Identifier** – `for x in xs { … }`
* **Tuple pattern** – `for (k, v) in entries { … }`
* **Nested tuples** – `for (x, (y, z)) in triples { … }`
* **Variant patterns** – `for Some(x) in maybe_values { … }`, `for Some((k, v)) in maybe_entries { … }`
* **Named-field patterns** – `for Some(value = x) in maybe_values { … }`

For a single-field variant like:

```drift
variant Optional<T> {
    Some(value: T)
    None
}
```

the following patterns are equivalent:

```drift
Some(x)
Some(value = x)
Some((k, v))
Some(value = (k, v))
```

Positional forms (`Some(x)`, `Some((k, v))`) are preferred; named-field forms exist for clarity when there are multiple fields.

---

## 9. Iterator implementations (examples)

These are examples only; the stdlib will define them in `std.array` / `std.iter`.

### 9.1 Consuming iterator

```drift
struct ArrayIter<T> {
    data: Array<T>,
    index: Int
}

implement<T> Iterator<T> for ArrayIter<T> {
    fn next(self: &mut self) returns T? {
        if self.index >= self.data.len() {
            return None
        }
        val item = self.data[self.index]->
        self.index += 1
        return Some(item)
    }
}
```

### 9.2 Borrowed iterator

```drift
struct ArrayRefIter<T> {
    data: &Array<T>,
    index: Int
}

implement<T> Iterator<&T> for ArrayRefIter<T> {
    fn next(self: &mut self) returns Optional<&T> {
        if self.index >= self.data.len() {
            return None
        }
        val r = &self.data[self.index]
        self.index += 1
        return Some(r)
    }
}
```

---

## 10. No C-style loops

Drift has exactly these looping constructs:

* `for pattern in expr { ... }`
* `while condition { ... }`
* `loop { ... }` (planned: unconditional loop with explicit `break`)

Drift does **not** have:

* `for (init; cond; step)`
* `do { ... } while (cond)`
* Special `foreach` syntax beyond `for pattern in expr`.

All iteration uses `Iterable` + `Iterator`.

---

## 11. Index-based iteration (explicit)

Manual indexing remains available and is sometimes the simplest thing:

```drift
var i: Int = 0
while i < xs.len() {
    out.writeln(xs[i])
    i = i + 1
}
```

This uses only `while` and normal expressions; no iterators or traits are involved.

---

## 12. Grammar extension (normative for parsing)

To support `for pattern in expr`, extend the grammar (in `drift-lang-grammar.md`) as follows.

### 12.1 Statement grammar

```ebnf
Stmt        ::= ForEachStmt
              | WhileStmt
              | ValDecl | VarDecl | ExprStmt
              | IfStmt
              | ReturnStmt | BreakStmt | ContinueStmt
              | TryStmt | ThrowStmt
```

```ebnf
ForEachStmt ::= "for" Pattern "in" Expr Block
```

* There is **no** C-style `for` production.

### 12.2 Reserved keyword

Add `"in"` to the reserved keyword list (both in the lexer section and in `drift-lang-spec.md`’s keyword table).

### 12.3 Pattern grammar (tuple & variant destructuring)

```ebnf
Pattern       ::= SimplePattern

SimplePattern ::= Ident
                | "_"
                | Literal
                | TuplePattern
                | VariantPattern

TuplePattern  ::= "(" Pattern ("," Pattern)+ ")"

VariantPattern ::= Ident "(" PatternList? ")"

PatternList   ::= Pattern ("," Pattern)*
                | FieldPattern ("," FieldPattern)*

FieldPattern  ::= Ident "=" Pattern
```

This allows:

* `(x, y)`
* `Some(x)`
* `Some((k, v))`
* `Some(value = x)`
* Nested combinations, e.g. `Some((k, (a, b)))`, `Some(Err(msg))`, etc.

### 12.4 Semantic constraints

Type checking must enforce:

* If `expr` has type `T`, then there exists `Item`, `Iter` such that:

  * `T` implements `Iterable<Item, Iter>`
  * `Iter` implements `Iterator<Item>`

Otherwise, `for pattern in expr` is ill-typed.

---

## 13. Summary

* `Iterator<Item>` represents the **state machine** that yields elements.
* `Iterable<Item, Iter>` represents the **capability to be iterated**, via `iter()`.
* `for pattern in expr` is the only `for` form; it desugars to `expr.iter().next()`.
* Patterns in `for` are full patterns, enabling tuple and variant destructuring.
* The main spec and grammar are extended to formalize this behavior.

````

---

## Required changes to `drift-lang-spec.md` and `drift-lang-grammar.md`

### A. Changes to `drift-lang-spec.md`

1. **Add/rename the trait from `IntoIterator` to `Iterable`**

   - In the chapter where you list canonical traits (likely near `Debuggable`, `Destructible`), add:

     ```drift
     trait Iterable<Item, Iter>
         require Iter is Iterator<Item>
     {
         fn iter(self) returns Iter
     }
     ```

   - If `IntoIterator` was previously mentioned, rename those mentions to `Iterable` and adjust any examples accordingly.

2. **Update prelude export**

   - In the prelude description (where you talk about `std.prelude`), ensure it exports:

     ```drift
     export std.iter.{Iterator, Iterable}
     ```

   - Remove/rename any `IntoIterator` export lines.

3. **Update the control-flow / `for` loop chapter (Chapter 8)**

   - Introduce a subsection, e.g. `8.5. for-each loops`, and:

     - Define the syntax `for pattern in expr { ... }`.
     - Paste or adapt the desugaring:

       ```drift
       val it = expr.iter()
       loop {
           val next = it.next()
           match next {
               Some(pattern) => { ... }
               None          => break
           }
       }
       ```

     - State the type rule: `expr` must implement `Iterable<Item, Iter>` with `Iter: Iterator<Item>`.

   - Clarify that C-style `for` does not exist.

4. **Update any examples that mention `IntoIterator` / `into_iter`**

   - In any spec examples that currently say `IntoIterator` or `.into_iter()`, change to `Iterable` and `.iter()`.
   - Update prose that said “IntoIterator” to “Iterable” and “into_iter” to “iter”.

5. **Update keyword list**

   - In the “Reserved keywords and operators” section, ensure `in` is included as a keyword.

6. **Update pattern-matching / variant sections**

   - In the variant chapter, add a note that positional and named patterns are both allowed for single-field variants, with examples:

     ```drift
     Some(x)
     Some(value = x)
     Some((k, v))
     ```

   - Mention that patterns are shared between `match` and `for` (both use the same pattern grammar).

### B. Changes to `drift-lang-grammar.md`

1. **Rename `IntoIterator` → `Iterable` and `into_iter` → `iter`**

   - In any non-grammar text (comments, notes, examples), replace:
     - `IntoIterator` with `Iterable`
     - `into_iter` with `iter`

   - If the grammar mentions `IntoIterator` in notes (not as tokens), update to `Iterable`.

2. **Add `ForEachStmt` production**

   - In the statement grammar section, update to:

     ```ebnf
     Stmt        ::= ForEachStmt
                   | WhileStmt
                   | ValDecl | VarDecl | ExprStmt
                   | IfStmt
                   | ReturnStmt | BreakStmt | ContinueStmt
                   | TryStmt | ThrowStmt

     ForEachStmt ::= "for" Pattern "in" Expr Block
     ```

   - Remove any previous `for` production that used `init ; cond ; step` if it still exists.

3. **Ensure `in` is a reserved keyword in the lexer**

   - In the lexical/keyword list, add `in` to the list of reserved keywords.
   - Ensure `in` is not parsed as an identifier.

4. **Update pattern grammar**

   - Replace the simplistic pattern grammar with:

     ```ebnf
     Pattern       ::= SimplePattern

     SimplePattern ::= Ident
                     | "_"
                     | Literal
                     | TuplePattern
                     | VariantPattern

     TuplePattern  ::= "(" Pattern ("," Pattern)+ ")"

     VariantPattern ::= Ident "(" PatternList? ")"

     PatternList   ::= Pattern ("," Pattern)*
                     | FieldPattern ("," FieldPattern)*

     FieldPattern  ::= Ident "=" Pattern
     ```

   - This is used by both `match` and `for`.

5. **Semantic notes**

   - In the commentary section of the grammar, add a note:

     > A `ForEachStmt` is only well-typed if the scrutinee expression’s type implements `std::iter::Iterable<Item, Iter>` and `Iter` implements `std::iter::Iterator<Item>`.

Those two sets of changes will bring **`drift-lang-spec.md`**, **`drift-lang-grammar.md`**, and **`drift-loops-and-iterators.md`** into full alignment on the `Iterable`/`Iterator` story and the `for pattern in expr` semantics.
