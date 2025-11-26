# Iteration in Drift

This document defines the iterator model in Drift: the traits involved, the role of `variant` types such as `Option<T>`, and the exact lowering of `for … in …` loops. The design is intentionally minimal, zero-cost, and consistent with Drift’s ownership and trait systems.

---

# 1. Variant types are the foundation

Iteration in Drift uses the standard optional type:

```drift
variant Option<T> {
	Some(value: T)
	None
}
```

`Option<T>` is a **real type**, not a trait. It is a tagged union with two variants:

* `Some(value = x)` – represents presence of a value
* `None` – represents absence

The iterator model commits to `Option<T>` as the canonical “maybe” type. There is **no `Optional<T>` trait**, no abstraction layer, and no pluggable “maybe” type family.

---

# 2. Iterator trait

An iterator is simply any type that can produce a sequence of items, one at a time:

```drift
trait Iterator<Item> {
	fn next(ref mut self) returns Option<Item>
}
```

Rules:

* `Some(value = item)` indicates there **is** a next element.
* `None` indicates the iterator is **exhausted**.
* Once `next()` returns `None` for a given iterator instance, every following call must also return `None`.

The trait is statically dispatched (monomorphized), like every Drift trait.

---

# 3. IntoIterator: converting collections to iterators

To make something iterable, a type implements:

```drift
trait IntoIterator<Item, Iter>
	require Iter is Iterator<Item>
{
	fn into_iter(self) returns Iter
}
```

The iterator returned by `into_iter(self)` may:

* consume `self` (move by value)
* or borrow from `self` (if `self` is a `ref` binding and you implement a borrowed form)

This is where the container controls the ownership semantics of iteration.

---

# 4. Consuming vs borrowed iteration

### 4.1. Consuming iteration (by-value)

Example for `Array<T>`:

```drift
implement<T> IntoIterator<T, ArrayIntoIter<T>> for Array<T> {
	fn into_iter(self) returns ArrayIntoIter<T> {
		return ArrayIntoIter<T>(arr = self, index = 0)
	}
}
```

This:

```drift
for x in xs { ... }
```

means:

* `xs` is **moved** into the iterator
* `xs` is invalid after the loop
* each element is moved out of the array lazily

### 4.2. Borrowed iteration (non-consuming)

Separately, you can allow:

```drift
for ref x in ref xs { ... }
```

by implementing:

```drift
implement<T> IntoIterator<ref T, ArrayRefIter<T>> for ref Array<T>
```

Here:

* `xs` is **borrowed**, not consumed
* each iteration yields a `ref T`
* mutation rules follow standard borrowing semantics
* `xs` remains usable after the loop

Borrowed and consuming iterators are completely separate `IntoIterator` implementations.

---

# 5. `for pattern in expr` syntax

Drift has a dedicated foreach loop form:

```drift
for pattern in expr {
	body
}
```

Where:

* `pattern` is a binding pattern (identifier or tuple pattern today)
* `expr` is any expression implementing `IntoIterator<_, _>`

This is distinct from the C-style `for (init; cond; step)` form.

---

# 6. Exact desugaring of foreach loops

The compiler rewrites:

```drift
for pattern in expr {
	body
}
```

into:

```drift
{
	val __iterable = expr          // evaluate once
	var __iter = __iterable.into_iter()

	while true {
		val __next = __iter.next()

		match __next {
			Some(value = pattern) => {
				body
			}
			None => {
				break
			}
		}
	}
}
```

Notes:

* `value` is the field name defined in `Option<T>`.
* This desugaring uses **only** standard language constructs:

  * `val`
  * variant construction
  * `match` on a variant
  * `while`
  * `break`
* No loop protocol, no special hidden trait, no compiler magic beyond this rewrite.

---

# 7. Patterns in foreach loops

Because `Some(value = pattern)` is a pattern match, you can write:

```drift
for (k, v) in entries {
	...
}
```

as long as the iterator yields items of type `(K, V)`.

This uses the same pattern rules as `match` arms.

---

# 8. Ownership model for iteration

Everything follows the ordinary ownership rules already in the spec:

* If `expr` is an owned value, `into_iter(self)` consumes it.
* If `expr` is a reference (`ref xs`), and you implement the borrowed form, the iterator borrows.
* Items yielded by `next()` move or borrow according to their type:

  * `Option<Item>` → `Some(value = item)` moves the `item`
  * `Option<ref T>` yields a borrow

The iteration model requires no special-case ownership rules.

---

# 9. Termination model

“Iteration until” is encoded directly by the variant type:

* `Some(value = x)` → continue
* `None` → stop

No separate Boolean flag, sentinel, or Option-like trait is needed.

This design makes loop termination explicit and predictable.

---

# 10. Why no `Optional<T>` trait?

Because iteration does not need a behavioral abstraction over “maybe values.”

* Drift already has **one canonical optional type**: `Option<T>`.
* The `Iterator<Item>` trait is defined in terms of that concrete type.
* Adding a trait like:

  ```drift
  trait Optional<T> { ... }
  ```

  would only make sense if multiple optional types existed — they do not.

So Drift keeps iteration simple by standardizing on a single variant type for “presence/absence.”

---

# 11. Minimal API for containers

To support iteration, a container only needs to implement:

```drift
implement IntoIterator<Item, Iter> for MyContainer
```

where:

* `Iter` implements `Iterator<Item>`
* `into_iter(self)` constructs that iterator

Everything else is type-checked and lowered automatically.

---

# 12. Summary

Drift’s iteration model is:

* **Variant-driven** → `Option<T>` signals “more” vs “no more”
* **Trait-based** → `Iterator<Item>` provides `next`
* **Zero-cost** → all static, monomorphized
* **Ownership-aware** → consuming or borrowed iteration
* **Desugared** → `for pattern in expr` lowers to a `while + match`
* **Minimalist** → no optional trait, no loop protocol object, no magic
