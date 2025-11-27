# Drift borrowing & reference model revision

This document proposes a revision of Drift’s borrowing and reference model.
It is intended as a *semantic* and *surface syntax* update to the core language, not an ABI change.

The goal is to:

* Make **borrowing ergonomics** consistent and predictable.
* Avoid treating control-flow constructs like `for` as special cases.
* Align Drift’s syntax with `&T` / `&mut T` style references while **still forbidding raw pointers**.
* Preserve the existing ownership rules: by-value parameters still represent ownership; `x->` is still the only way to move a value implicitly.

The changes affect both `drift-lang-spec.md` and `drift-lang-grammar.md`.

---

## 1. Overview of the change

Today:

* Borrowed types are spelled `ref T` / `ref mut T`. 
* Calls to a borrowed parameter require explicit `ref v` / `ref mut v`. 
* Methods use `ref self` / `ref mut self` as receivers. 
* The grammar has `Ty ::= "ref" Ty | "ref" "mut" Ty`. 

This proposal changes three things:

1. **Reference type syntax:**

   * Replace `ref T` with `&T`.
   * Replace `ref mut T` with `&mut T`.
2. **Call-site borrowing rule (global):**

   * For parameters and receivers of type `&T` / `&mut T`, passing an **lvalue** `v: T` automatically borrows:

     * `f(v)` ≡ `f(&v)` if parameter is `&T`.
     * `f(v)` ≡ `f(&mut v)` if parameter is `&mut T`.
   * Borrowing from temporaries (rvalues) is a **compile-time error**.
   * Nothing about `T` parameters changes: calling `f(v)` is copy-or-error; calling `f(v->)` moves.
3. **Receiver overloading on ownership vs borrow:**

   * Methods may be overloaded on `self`, `&self`, `&mut self`.
   * Calls on lvalues prefer borrowed receivers; calls on moved values (`obj->`) can only bind to `self`.

This is enough to make iteration and other patterns non-magic: control-flow constructs just desugar to normal function/method calls that obey the global borrowing rules.

---

## 2. Updated reference model

### 2.1. New reference type syntax

Replace all occurrences of `ref T` / `ref mut T` with `&T` / `&mut T` in the spec.

New terminology:

* `&T` — shared, immutable reference to `T`.
* `&mut T` — exclusive, mutable reference to `T`.

In **chapter 3 (“Variable and reference qualifiers”)**, the reference row becomes: 

| Concept           | Syntax   | Meaning                          |
| ----------------- | -------- | -------------------------------- |
| Immutable binding | `val`    | Cannot be rebound or moved       |
| Mutable binding   | `var`    | Can mutate or transfer ownership |
| Shared reference  | `&T`     | Shared, read-only borrow of `T`  |
| Mutable reference | `&mut T` | Exclusive, mutable borrow of `T` |
| Move              | `x->`    | Moves value, invalidating source |

Similarly, update all examples and API definitions that currently show `ref` or `ref mut` types, e.g.:

* `fn inspect(f: ref File)` → `fn inspect(f: &File)`. 
* `fn fill(f: ref mut File)` → `fn fill(f: &mut File)`. 
* `fn len(self: ref ByteBuffer)` → `fn len(self: &ByteBuffer)`. 
* `trait Sync` definition commentary: “shared references (`ref T`)" → “shared references (`&T`)”. 

Semantics remain identical: references are non-owning borrows; they never expose raw addresses, and they are subject to the same aliasing constraints described in the memory and pointer-free chapters. 

### 2.2. Borrow expressions

Introduce expression forms:

* `&v` — produce a shared reference `&T` from an lvalue `v: T`.
* `&mut v` — produce a mutable reference `&mut T` from a mutable lvalue `v: T`.

These are the explicit versions of the auto-borrow rules in §3.

Borrowing from temporaries (e.g. `&(foo())`, or `&mut (bar())`) is a compile-time error unless the temp is first bound to a local.

---

## 3. Global call-site borrowing rules

This is the core behavior change that unlocks simpler iteration and less special-casing.

### 3.1. By-value parameters (unchanged)

For a function:

```drift
fn f(x: T) returns R { ... }
```

Call rules stay as defined in the ownership chapter: 

* `f(v)`:

  * **If** `T` implements `Copy` → copy `v`.
  * **Else** → compile-time error.
* `f(v->)`:

  * Always **move** `v` into `x`, leaving `v` invalid.

There is **no borrow path** into a `T` parameter. If you want borrowing, you must declare `&T` / `&mut T`.

### 3.2. Borrowed parameters

For a function:

```drift
fn g(x: &T) returns R
fn h(x: &mut T) returns R
```

New global call rules:

* If the argument is an **lvalue** `v: T`:

  * `g(v)` is **defined as sugar** for `g(&v)`.
  * `h(v)` is **defined as sugar** for `h(&mut v)`.
* If the argument is an **rvalue** (`foo()` temporary, `v->`, etc.):

  * `g(tmp)` and `h(tmp)` are **compile-time errors**.
  * Borrowing requires a named lvalue; temporaries are either bound to a local or consumed by `T` parameters.

The explicit forms `g(&v)` and `h(&mut v)` remain legal; they are just redundant for lvalues.

This rule applies **everywhere**:

* Free functions (`fn inspect(x: &File)`).
* Trait methods (`fn clone(&self)` for a `Clonable` trait).
* Interface methods (`fn write(self: &OutputStream, ...)`) where the receiver is “by ref” in the interface type.

---

## 4. Method receiver overloading

### 4.1. Receiver modes

The existing spec already distinguishes receivers as: `self`, `ref self`, `ref mut self`. 

Under the new syntax, this becomes:

* `self` — by-value receiver; method takes ownership of the receiver.
* `&self` — shared borrowed receiver.
* `&mut self` — exclusive mutable borrowed receiver.

Example:

```drift
struct Point { x: Int64, y: Int64 }

implement Point {
	fn move_by(&mut self, dx: Int64, dy: Int64) returns Void {
		self.x += dx
		self.y += dy
	}

	fn norm(&self) returns Float64 {
		return (self.x * self.x + self.y * self.y).to_float().sqrt()
	}

	fn into_polar(self) returns Polar {
		// consumes Point, returns Polar
		...
	}
}
```

### 4.2. Call rules for receivers

For an **lvalue** receiver `p: Point`:

* If there is an `fn m(&self, ...)`, then `p.m(...)` is sugar for `p.m(&p, ...)`.
* Else, if there is an `fn m(&mut self, ...)`, then `p.m(...)` is sugar for `p.m(&mut p, ...)`.
* Else, if there is an `fn m(self, ...)`, then `p.m(...)` is treated like a call to a `T` parameter:

  * `p.m(...)` copies `p` if `Point : Copy`, otherwise it is an error and callers must write `p->m(...)`.

For an **rvalue** receiver (e.g. `p->`, `make_point()`), only `self` receivers are allowed:

* `p->m(...)` can only bind to `fn m(self, ...)`.
* `fn m(&self, ...)` and `fn m(&mut self, ...)` are not valid targets for an rvalue receiver.

This is the method version of the global “no borrowing from rvalues” rule.

### 4.3. Overloading on `self` vs `&self`

This enables patterns like:

```drift
implement<T> Array<T> {
	fn iter(&self) returns ArrayRefIter<T> { ... }        // borrowed iterator
	fn iter(self) returns ArrayOwningIter<T> { ... }      // owning iterator
}
```

Then:

```drift
var xs: Array<Int64> = [1, 2, 3]

xs.iter()        // calls &self overload (borrowed)
xs->iter()       // calls self overload (consuming)
```

No special `for` rules are required; `for` simply evaluates the expression you give it and drives the resulting iterator.

---

## 5. No borrowing from temporaries

To avoid lifetime confusion, the spec must explicitly state:

> Borrowed parameters (`&T`, `&mut T`) and receivers (`&self`, `&mut self`) **cannot** be bound from temporaries or moved values. Only lvalues can be borrowed.

Examples:

```drift
fn log_order(o: &Order) returns Void { ... }

log_order(make_order())   // ❌ error: cannot borrow from temporary
log_order(order->)        // ❌ error: cannot borrow from moved value

var order = make_order()
log_order(order)          // ✅ sugar for log_order(&order)
```

Method calls follow the same rule:

```drift
make_order().inspect()    // ❌ if inspect expects &self
order.inspect()           // ✅ auto-borrow &order
order->into_archive()     // ✅ if into_archive(self) consumes
```

This rule needs to be inserted into the ownership and memory chapters, near the explanation of lifetimes/RAII, and referenced from any future borrow-checker semantics.

---

## 6. Spec touchpoints to update

Below is a non-exhaustive but important list of places in `drift-lang-spec.md` that need updating.

### 6.1. Chapter 3 — variable and reference qualifiers

* Replace the `ref T` / `ref mut T` row with `&T` / `&mut T`. 
* Add a subsection explicitly stating the call-site sugar rules for `&T` / `&mut T` params and receivers, and the restriction on rvalues.

### 6.2. Ownership examples

All functions that use `ref` parameters should be updated:

* `fn inspect(f: ref File)` → `fn inspect(f: &File)`. 
* `fn fill(f: ref mut File)` → `fn fill(f: &mut File)`.
* Any occurrences of `inspect(ref f)` / `fill(ref mut f)` in examples should be changed to `inspect(f)` / `fill(f)` with a note explaining the auto-borrow, or kept explicit if you want to demonstrate `&` syntax.

### 6.3. Array / ByteBuffer API

Update signatures using `ref` / `ref mut`:

* `fn len(self: ref ByteBuffer)` → `fn len(self: &ByteBuffer)`. 
* `fn clear(self: ref mut ByteBuffer)` → `fn clear(self: &mut ByteBuffer)`.
* `fn ref_at(self: ref Array<T>, ...) returns ref T` → `fn ref_at(self: &Array<T>, ...) returns &T`. 

Update `ByteSlice` / `MutByteSlice` descriptions to talk in terms of `&ByteSlice` / `&MutByteSlice` when describing API receivers. 

### 6.4. Traits and interfaces

In the traits chapter, replace any use of `ref self` with `&self` and adjust examples accordingly. 

In the interfaces chapter, update all receiver types:

* `fn write(self: ref OutputStream, ...)` → `fn write(self: &OutputStream, ...)`. 
* `fn area(self: ref Shape)` → `fn area(self: &Shape)`.

Also, in the `Send` / `Sync` explanation, change “shared references (`ref T`)" to “shared references (`&T`)”. 

### 6.5. Concurrency

In the concurrency chapter, any text that refers to sharing `ref T` across threads (for `Sync`) should be updated to `&T`. 

### 6.6. Closures

In the closures chapter, capture modes currently mention `ref x`, `ref mut x` as “not yet supported”. 

* Those should be rewritten as `&x`, `&mut x` and still marked “not yet supported” until the borrow checker is fully defined.
* When you later enable borrow captures, they will follow the same `&T` semantics as parameters.

### 6.7. Everywhere else

A grep-style pass over the spec for `ref ` and `ref mut` will turn up:

* Examples with `translate(ref point, ...)`. 
* Logging and error examples using `log(ref e)`. 

These should be converted to `&` syntax and, where appropriate, simplified to rely on auto-borrowing.

---

## 7. Grammar changes (`drift-lang-grammar.md`)

The grammar currently encodes `ref` in type positions and does not mention `&`. 

### 7.1. Lexical tokens

Add `&` as an operator/punctuation in §1:

```text
Operators/punctuation: + - * / % == != < <= > >= and or not ! ? : >> << |> <| . , : ; = -> => [ ] { } ( ) &
```

You may also treat `&mut` as two tokens: `&` and `mut` (where `mut` is an identifier, *not* a keyword), since `mut` is only meaningful immediately after `&` in types and exprs.

### 7.2. Types

Update the `Ty` production:

Current:

```ebnf
Ty           ::= Ident TraitParams?
              | Ty "[" "]"
              | "ref" Ty | "ref" "mut" Ty
              | "(" Ty ("," Ty)+ ")"
              | VariantType | InterfaceType | TraitType
```

Proposed:

```ebnf
Ty           ::= "&" "mut"? Ty
              | Ident TraitParams?
              | Ty "[" "]"
              | "(" Ty ("," Ty)+ ")"
              | VariantType | InterfaceType | TraitType
```

Notes:

* `&` is now a **prefix** unary type operator.
* `&mut T` is parsed as `'&' 'mut' Ty`.
* You may want to clarify that `mut` in this position is a bare identifier token, not a reserved keyword.

### 7.3. Unary expressions

Add `&` and `&mut` to `UnaryOp` and expression grammar:

Current:

```ebnf
UnaryExpr    ::= UnaryOp* PostfixExpr
UnaryOp      ::= "-" | "!" | "not"
```

Proposed:

```ebnf
UnaryExpr    ::= UnaryOp* PostfixExpr
UnaryOp      ::= "-" | "!" | "not" | "&" | "&mut"
```

Semantics:

* `& expr` requires that `expr` be an lvalue of type `T` and yields `&T`.
* `&mut expr` requires that `expr` be a mutable lvalue and yields `&mut T`.
* Applying these to non-lvalues is a semantic error.

### 7.4. Receiver syntax

The grammar currently models receivers indirectly via `FnSig` and `Params`. Receivers are just parameters with the name `self` inside an `implement` block. 

You **do not** need to change the grammar to distinguish receivers; you only need to change the spec text to describe:

* The allowed forms: `self`, `&self`, `&mut self`.
* The call-site resolution rules in §4 above.

No grammar change is required for that distinction—it’s semantic.

---

## 8. Rationale

### 8.1. Why change now?

You’re already considering a global shift to make borrowing more ergonomic (auto-borrow on `&T` params) so that higher-level constructs like `for` do not need special rules. Making the reference syntax match that shift (`&T`, `&mut T`) is a low-cost, high-clarity change while you’re touching the same area.

### 8.2. Why `&T` instead of `ref T`?

* **Composability**: `&mut Result<T, E>` reads better than `ref mut Result<T, E>`, and it behaves like a unary type operator.
* **Alignment with expressions**: `&v` / `&mut v` line up naturally with `x: &T` / `x: &mut T`.
* **Avoid C/C++ pointer confusion**: there is no `T*` or `T&` in Drift’s user-visible surface, so `&` is *only* “borrow”, never “address-of”.
* **Grammar simplicity**: prefix `&` is easy to slot into `Ty` and `UnaryExpr`; postfix `T&` would demand more precedence rules.

### 8.3. Why auto-borrow for `&T` parameters?

Three reasons:

1. It removes the need to special-case loops, iterators, and other constructs. They just call normal functions/methods that take `&T`.
2. It pushes the programmer’s attention onto **ownership boundaries** (plain `T`) rather than noisy `ref` everywhere.
3. It keeps the simple triad:

   * `T` parameter: copy or move, caller chooses.
   * `&T` / `&mut T` parameter: always borrow; caller just passes an lvalue.
   * No hidden third mode.

### 8.4. Why forbid borrowing from rvalues?

Allowing `&` of temporaries forces you into lifetime algebra and borrow checking that resembles C++ and Rust. The current spec emphasizes deterministic RAII and simple lifetimes (“values are destroyed at scope end or when moved”), and you don’t have a formal lifetime system in place.

By banning “borrow from rvalue” you get:

* Easy reasoning: if you see `&x` / `&mut x`, you know `x` is a named binding that lives at least as long as the borrow’s scope.
* A clear separation: rvalues are for ownership transfer; lvalues are for borrowing.

If later you want lifetime-extended temporaries for specific syntactic forms, you can introduce them deliberately rather than by accident.

---

## 9. Impact on iteration work

With this model in place, iteration becomes straightforward:

* `Iterator<Item>` remains:

  ```drift
  trait Iterator<Item> {
      fn next(&mut self) returns Option<Item>
  }
  ```

* Container APIs use `&self` / `self` overloading for borrowed vs owning iteration:

  ```drift
  implement<T> Array<T> {
      fn iter(&self) returns ArrayRefIter<T> { ... }
      fn iter(self) returns ArrayOwningIter<T> { ... }
  }
  ```

* `for` only needs to:

  * Evaluate the header expression once: `let it = xs.iter()` or `let it = xs->iter()`.
  * Repeatedly call `it.next()` until it returns `Option::None`.

No special `for`-specific borrowing rules are required; the global rules in this document handle all of it.

---

## 10. Migration notes

* The change is **purely syntactic + call-site behavior**; there is no ABI impact if `ref T` and `&T` are represented the same way at runtime.
* A mechanical rename (`ref` → `&`, `ref mut` → `&mut`) plus a compiler warning for old syntax would get you most of the way there.
* Auto-borrow behavior should be introduced with diagnostics explaining the new sugar and the prohibition on rvalues.

---

If you decide to adopt this model, the next step is to:

1. Copy this document into your repo as `docs/drift_borrowing_and_reference_model_revision.md`.
2. Update `drift-lang-spec.md` and `drift-lang-grammar.md` according to §6 and §7.
3. Then we can finalize the iteration chapter with the assumption that this borrow model is in effect.
