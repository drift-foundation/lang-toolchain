
# Drift Tuple Destructuring & Variant Pattern Semantics

This document defines the language rules for **tuple destructuring**, **variant patterns**, and **pattern usage in `for` loops**, `match` expressions, and closures. It provides the background and spec structure needed to patch the main Drift spec and grammar consistently.

---

# 1. Overview

Drift supports destructuring of compound values in **patterns**, including:

- Tuple destructuring  
  `(x, y)`
- Nested tuple destructuring  
  `(a, (b, c))`
- Variant destructuring  
  `Some(x)`, `Err(msg)`
- Nested variant destructuring  
  `Some((k, v))`, `Some(Err(message))`
- Named-field variant patterns  
  `Some(value = x)`
- Full patterns for `for pattern in iterable { … }`

Patterns appear in:

- `match` expressions  
- `for pattern in expr` loops  
- Closure/lambda parameters (if allowed in future revisions)  
- Future `let`/`val` destructuring (currently not part of the language surface)

This doc consolidates the semantics so that the main spec can reference a single authoritative rule set.

---

# 2. Tuple destructuring

Tuples appear both in type positions:

```
(T1, T2, …, Tn)     // n >= 2
```

and in expression positions:

```
(val1, val2)
```

A tuple pattern matches the structure:

```
(x, y)
```

Pattern semantics:

1. Pattern must have the same arity as the tuple:  
   `(x, y)` matches `(a, b)` but not `(a, b, c)`.

2. Patterns inside tuples are recursive:  
   ```
   (x, (y, z)) matches (1, (2, 3))
   ```

3. Identifiers inside patterns become new bindings.

4. Ownership follows destructuring:  
   - Matching `Some((k, v)) => …` moves the tuple `(k, v)` and then binds `k`, `v`.
   - In borrowed matches (`match &expr`), you will bind references instead.

---

# 3. Variant pattern semantics

A `variant` defines alternatives:

```
variant Optional<T> {
    Some(value: T)
    None
}
```

Patterns can destructure them using:

### 3.1 **Positional patterns** (recommended default)

```
Some(x)
Some((k, v))
```

Positional patterns are always allowed for single-field variants *without requiring the field name*.

### 3.2 **Named-field patterns**

```
Some(value = x)
Some(value = (k, v))
```

Use these when the variant contains multiple fields or when readability benefits from naming.

### 3.3 **Nested variant patterns**

```
Some(Err(msg))
Some((user_id, Some(role)))
```

Patterns recursively destructure the payload.

---

# 4. Decision: allow destructuring *without* `value =`  

Drift permits:

```
Some((k, v))
```

even though the variant formally has a field name:

```
Some(value: (Key, Value))
```

Why?

- More ergonomic.  
- Aligns Drift with Rust-like, ML-like destructuring.  
- Keeps pattern syntax independent of field names.  
- Allows nested destructuring naturally.

### Therefore:

**For single-field variants, positional patterns are equivalent to named-field patterns.**

That is:

```
Some(x)             == Some(value = x)
Some((k, v))        == Some(value = (k, v))
```

Both are valid and interchangeable.

---

# 5. Patterns in `for pattern in expr` loops

The loop desugaring:

```
for pat in expr {
    body
}
```

expands to:

```
{
    val __iterable = expr
    var __iter = __iterable.into_iter()
    loop {
        val __next = __iter.next()
        match __next {
            Some(pat) => { body }
            None => break
        }
    }
}
```

**`pat` is a full pattern**, so all of the following are legal:

```
for x in xs
for (k, v) in map_items
for Some(x) in opt_stream
for Some((k, v)) in opt_kv_stream
```

This relies on variant + tuple pattern semantics.

---

# 6. Benefits of this model

### ✓ Simple mental model  
Pattern forms align across loops, match expressions, and variant destructuring.

### ✓ Zero extra syntax  
No “destructuring let”; no special “foreach” keyword.

### ✓ Fully future-proof  
When pattern grammar expands (guards, alternation, fields), loops work automatically.

### ✓ Symmetric with type system  
Tuples and variants use identical shape-matching logic.

---

# 7. Grammar recommendations (to apply to drift-lang-grammar.md)

The current grammar:

```
Pattern ::= Ident | Literal | "(" Pattern ("," Pattern)+ ")"
```

should be replaced with:

```
Pattern        ::= SimplePattern

SimplePattern  ::= Ident
                 | "_"
                 | Literal
                 | TuplePattern
                 | VariantPattern

TuplePattern   ::= "(" Pattern ("," Pattern)+ ")"

VariantPattern ::= Ident "(" PatternList? ")"

PatternList    ::= Pattern ("," Pattern)* 
                 | FieldPattern ("," FieldPattern)*

FieldPattern   ::= Ident "=" Pattern
```

This allows:

- `Some(x)`
- `Some((k, v))`
- `Some(error = e)`
- `Node(left = l, right = r)`
- `(a, b, c)`
- Nested destructuring

This matches Drift’s desired expressiveness.

---

# 8. Spec changes to apply

Modify main spec sections on:

### 10.4 (pattern matching / exhaustiveness)

Add examples showing:

```
Some(x)
Some((k, v))
Some(value = x)
Some((a, (b, c)))
Some(Err(msg)))
```

Modify description to:  
> “The payload of a variant may be matched positionally or via named fields. For single-field variants, the positional form is preferred.”

### 8.5 Foreach loop section (to be added)

Include:

> “`pattern` is a full Drift pattern. Tuple and variant destructuring are supported.”

---

# 9. Conclusion

Drift adopts a flexible, powerful, and ML-consistent destructuring model:

- Positional destructuring is preferred.
- Named-field destructuring is optional.
- Patterns integrate smoothly with loops and match expressions.
- Grammar and spec changes are straightforward and well-contained.

This document serves as the rationale + implementation target for updating the main spec and grammar.
