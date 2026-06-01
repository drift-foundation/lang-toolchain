## Method resolution v1

### Goals

* Support **overloaded functions and methods**.
* Resolve a call expression to a **single concrete signature** and a **fully qualified method ID**.
* Provide the type checker and borrow checker with:

  * The resolved parameter types (including receiver).
  * The resolved return type.
  * The “self mode” (`self`, `&self`, `&mut self`, etc.).
* Keep v1 **deterministic** and **simple**:

  * No overloading by return type.
  * No “best match” magic: ambiguous matches are compile errors.
  * Minimal interaction with generics (explicit type arguments only).

---

## Terminology

* **Receiver type**: The static type of the expression before the `.` in a method call, e.g. `a` in `a.push(x)`.

* **Explicit arguments**: The arguments written in the call parentheses (`x, y` in `a.m(x, y)`).

* **Full argument list**: Receiver + explicit arguments.

* **Candidate**: A method or function declaration with:

  * A **fully qualified ID** (e.g. `modA::impl Vec<T>::push`).
  * A **parameter list**, including the implicit `self`/receiver param (for methods).
  * A **return type**.
  * A **visibility/module** context.

* **Self mode**: How the receiver is passed:

  * `self`       — by value
  * `&self`      — shared reference
  * `&mut self`  — mutable reference

---

## Non-goals for v1

* No overloading by return type.
* No auto-resolution of **generic** methods via type inference (other than explicit `<T>` arguments).
* No trait-based method resolution rules beyond “if it’s in the method registry and visible, it is a candidate”.

You can extend these later without breaking the basic structure.

---

## Call forms

We consider three call forms for v1:

1. **Function call** (no receiver):

   ```drift
   foo(x, y)
   ```

2. **Method call** (dot syntax):

   ```drift
   receiver.method(a, b)
   ```

3. **UFCS-style call** (optional in v1, but easy to support):

   ```drift
   TypeName::method(receiver, a, b)
   ```

Resolution is defined separately for functions and methods, but uses the same ideas.

---

## Data model for method registry

The compiler maintains a **method/fn registry** during/after symbol collection.

### Method entries

Each method entry has (conceptually):

* `method_id`: opaque unique identifier, including module path and impl id.
* `name`: method name, e.g. `"push"`.
* `impl_target_type_id`: type id that this method is implemented for (e.g. `Vec<T>`, `MyStruct`).
* `param_types`: ordered list of parameter types, including receiver:

  * Example for `fn push(&mut self, value: T) -> Void`:

    * `param_types = [ &mut Vec<T>, T ]`
* `result_type`: return type.
* `self_mode`: one of `SelfByValue`, `SelfByRef`, `SelfByRefMut`.
* `visibility` / `module`: necessary to enforce module scoping and privacy.
* `is_generic`: whether the method has type parameters (for v1: generics only participate when fully instantiated).

The **lookup key** at registry level does **not** need to be unique by `(receiver_type, name)` alone; it can store multiple entries and let resolution pick.

Internally, you’ll index by something like:

```text
(method_name, impl_target_type_id) -> [method_entry_1, method_entry_2, ...]
(method_name, module_scope) -> [free_fn_entry_1, ...]  // for free fns
```

But the important spec requirement is: the **resolved result** is a single `method_id` plus its instantiated signature.

---

## Resolution overview

Resolution has three phases:

1. **Candidate collection**: Find all declarations that *could* apply based on name, receiver type, and visibility.
2. **Viability filtering**: Discard candidates that do not match arity or cannot be called with the given argument types (without illegal conversions).
3. **Best-candidate selection**: If exactly one viable candidate remains, that candidate is chosen. If zero → “no matching overload.” If >1 with equal specificity → “ambiguous call.”

The result is:

* `resolved_callee_id` (function or method id).
* `resolved_param_types` (after any auto-borrow/auto-ref adjustment).
* `resolved_result_type`.

This information is attached to the call expression in typed HIR and used by borrow checker and return-type typing.

---

## Function resolution (no receiver)

Call: `foo(arg1, arg2, ...)`.

### 1. Candidate collection

* Let `name = "foo"`.
* Collect all **visible functions** with that name from the current module’s scope:

  * Free functions in the current module.
  * Free functions imported via `use` or equivalent.
  * Do **not** include methods (they are only used via dot or UFCS syntax in v1).

This yields a set `C` of zero or more function candidates.

### 2. Viability filtering

Let the argument expressions be `arg_1..arg_n` with static types `A_1..A_n`.

For each candidate function `f` with parameter types `P_1..P_m`:

* **Arity check**:

  * If `n != m`, `f` is not viable.
  * v1 has no variadics; arity must match exactly.

* **Parameter compatibility**:

  * For each i:

    * Accept the candidate only if `A_i` is **exactly equal** to `P_i`.
  * v1: **no implicit numeric conversions, no optional promotion, no subtyping** in overload selection.

    * You can still have your usual assignment compatibility checks later, but for **overload filtering**, use strict equality.

If any parameter type is unknown (type inference not yet solved), v1 behavior:

* Either:

  * **Conservatively reject** all candidates that require that param (leading to “cannot infer type” or “no matching overload”).
  * Or mark resolution as “underconstrained” and issue a dedicated error.
* Do **not** delay resolution or backtrack; v1 is eager and simple.

### 3. Best-candidate selection

* Let `V` be the set of viable candidates.
* If `|V| == 0`: compile error: *no matching overload for `foo(A_1, ..., A_n)`*.
* If `|V| == 1`: select that candidate.
* If `|V| > 1`:

  * Since v1 has no detailed specificity ranking beyond equality, **this is an ambiguity error**:

    * *ambiguous call to `foo`: candidates are ...*

No magical tie-breaking by “closer module,” etc. You can add that later.

---

## Method resolution with receiver (dot syntax)

Call form: `receiver.method(arg1, arg2, ...)`.

### 1. Derive receiver type

* Type checker first computes the type `R` of `receiver`.
* This should be done before method resolution (or in a mutually recursive way), but spec-wise, assume `R` is known.

### 2. Candidate collection

Let `name = "method"` and `R` be the receiver type.

We collect candidates in this order (conceptually):

1. **Inherent methods**:

   * Methods defined in `impl R { ... }` blocks for the exact type `R`.
   * Also methods for the underlying nominal type if `R` is a reference:

     * You may treat `&R0` and `&mut R0` as receivers for impls on `R0` in combination with auto-borrow; see below.

2. **Trait / extension methods** (if present in your language and v1):

   * Any **trait method** where:

     * The trait is imported/visible in the current module.
     * There is an impl `impl Trait for R0` or `impl Trait for &R0` etc. in the registry.
   * For v1, you can simply say: if the registry lists a method with `impl_target_type_id` equal to `R` (or `underlying type of R`), and it is visible, it becomes a candidate.

Result: a candidate set `C` of method declarations.

> v1 requirement: The registry lookup includes **both** `(name, R)` and possibly `(name, underlying(R))` to support auto-borrow/auto-deref if you want it. See next section.

### 3. Viability filtering and auto-borrow

Let the explicit arguments be `arg_1..arg_n` of types `A_1..A_n`.

For each candidate method `m` with:

* Parameter types `P_0..P_n` (where `P_0` is the receiver parameter).
* Self mode `self_mode` (`self`, `&self`, `&mut self`).

We check viability in two steps: **receiver compatibility** and **argument compatibility**.

#### 3.1 Receiver compatibility

Let static receiver type be `R`.

We define **receiver compatibility** as:

* If `self_mode == SelfByValue`:

  * Candidate is viable if `R == DeclReceiverType` (usually the impl target type).
  * v1: **do not** automatically copy/dup; this is purely type equality.
* If `self_mode == SelfByRef`:

  * Candidate is viable if **either**:

    * `R == &DeclReceiverType` (receiver already a shared ref), or
    * `R == DeclReceiverType` and the language allows auto-borrow to `&DeclReceiverType`.
* If `self_mode == SelfByRefMut`:

  * Candidate is viable if **either**:

    * `R == &mut DeclReceiverType` (receiver already a mutable ref), or
    * `R == DeclReceiverType` and the language allows auto-borrow to `&mut DeclReceiverType`.

Where `DeclReceiverType` is the type `T` for which the method is defined (`impl T { fn method(&self, ...) }`).

In v1, you can standardize that:

* The receiver param type in the method signature is always one of:

  * `T`, `&T`, `&mut T`.
* Auto-borrow is **only** allowed in the direction:

  * `T` → `&T` or `T` → `&mut T`.
* Auto-borrow **cannot** convert `&T` to `&mut T` or vice versa.
* Auto-deref (like Rust’s `Deref`) is **not** performed in v1.

If a candidate requires auto-borrow on the receiver, record that at the call site for the borrow checker (so it knows you created a temporary borrow of `receiver` for the duration of the call).

If `R` cannot be made compatible with `P_0` under these rules, the candidate is not viable.

#### 3.2 Explicit argument compatibility

After receiver compatibility passes:

* Let `P_1..P_k` be the remaining parameters.

* Check **arity**:

  * If the number of explicit arguments `n` is not equal to `k`, candidate is not viable.

* For each explicit argument position `i` (1..n):

  * Argument type `A_i` must be **exactly equal** to parameter type `P_i`.
  * v1: no numeric promotions, no subtyping. Strict equality for overload discrimination.

If all parameters match, the candidate is **viable**.

### 4. Best-candidate selection

Same rules as for free functions:

* Let `V` be the set of viable candidates for `(receiver_type R, name, A_1..A_n)`.

* If `|V| == 0`:

  * Error: *no matching method `name` for receiver of type `R` and argument types `(A_1, ..., A_n)`*.

* If `|V| == 1`:

  * That candidate is selected.
  * The call expression is annotated with:

    * `resolved_method_id`.
    * `resolved_receiver_type` (after auto-borrow if any).
    * `resolved_param_types`.
    * `resolved_result_type`.
    * `self_mode`.

* If `|V| > 1`:

  * Error: *ambiguous method call `R.name(A_1, ..., A_n)`; candidates are: ...*
  * v1 has **no** additional specificity ranking like “more derived type,” “less generic,” etc.

Any further specificity rules (like preferring methods in the same module) can be added in v2; v1 is intentionally conservative.

---

## UFCS-style resolution

If you support `TypeName::method(receiver, args...)`:

* Treat `TypeName` as constraining the **decl receiver type** to `TypeName`.
* Then proceed as method resolution, but:

  * The candidate set is limited to methods whose `impl_target_type_id` equals `TypeName`.
  * The static type of `receiver` is still `R`, and must be compatible with the method’s receiver param under the same auto-borrow rules.
* This form is useful to resolve ambiguities explicitly.

If multiple candidates exist in different modules for `TypeName::method`, UFCS still uses the same visibility and ambiguity rules. If there is more than one visible candidate with the same signature, it’s still ambiguous.

---

## Generics in v1

To avoid complexity:

* A **generic method** or function participates in resolution **only if**:

  * All its type parameters are either:

    * Explicitly specified by the caller (`foo<type T>(...)`), or
    * Fully determined by the **receiver type** and argument types, with no remaining unknowns.
* v1 may choose to **require explicit type arguments** for generic methods/functions. That’s simpler and safe.

For the spec:

* v1 guarantees:

  * If a candidate method requires type inference of type params and those params **cannot** be fully resolved from:

    * explicit `<...>` arguments, or
    * known receiver/argument types,
  * then the method is **not viable** for overload resolution.

This prevents half-baked inference inside the resolution algorithm.

You can tighten or relax this later.

---

## Module and visibility

For both free functions and methods:

* Only **visible** declarations participate in candidate collection.
* Visibility rules (v1 suggestion):

  * Free functions:

    * Visible if defined in current module or imported via `use` or equivalent.
  * Methods:

    * Inherent methods on a type `T` are visible wherever `T` is visible and the method’s visibility allows it (`pub` vs `private`).
    * Trait/extension methods are visible only if:

      * The trait/module is imported and
      * The impl is visible.

If two methods with the same signature exist but one is not visible, only the visible one participates. If visibility makes the remaining set ambiguous, you still emit an ambiguity error, but list only visible candidates.

---

## Interaction with borrow checker

Once a method/function is resolved:

* The call expression node in typed HIR must contain the **concrete signature**:

  * `callee_id`
  * `param_types` (with explicit receiver type as first element for methods)
  * `result_type`
  * `self_mode`
  * A flag indicating whether a temporary auto-borrow was created for the receiver, and of what kind (`&` or `&mut`).

The borrow checker:

* Does **not** re-resolve methods; it simply uses the resolved signature and the auto-borrow flag to:

  * Create the appropriate borrow of the receiver’s region when `self_mode` is `&self` or `&mut self`.
  * Treat a `self` by value call as a move of the receiver value.

This is the entire point of the spec: **resolution happens once in the type checker; borrow checker trusts the resolved signature.**

---

## Error reporting requirements

The type checker must emit clear diagnostics for:

1. **No matching function or method**:

   * Include:

     * Name.
     * Receiver type (for methods).
     * Argument types.
   * Optionally suggest close arity matches.

2. **Ambiguous call**:

   * Show:

     * All candidate signatures (fully qualified names).
   * Suggest:

     * Using UFCS or explicit type arguments to disambiguate.

3. **Visibility / privacy**:

   * If there are candidates that would otherwise match but are not visible:

     * You may either:

       * Ignore them for resolution (the user just sees “no matching overload”); or
       * Emit a more specific message: *method exists but is not visible here*.
   * v1 can get away with ignoring non-visible candidates, but it’s better to have at least a “not visible” path for debugging.

---

## Implementation checklist for you

When you implement v1, follow this order:

1. **Method registry**:

   * Build and store entries with `impl_target_type_id`, `name`, `param_types`, `self_mode`, `result_type`, `visibility`, `method_id`.

2. **Typed HIR node extension**:

   * For calls, add fields:

     * `resolved_callee_id`
     * `resolved_param_types`
     * `resolved_result_type`
     * `self_mode` (for methods)
     * `receiver_autoborrow_kind: None | Shared | Mutable`

3. **Resolution algorithm**:

   * Implement the above three-phase process for both free functions and methods.
   * Use strict type equality for overload selection.

4. **Borrow checker integration**:

   * Read the fields from the call node.
   * Apply the auto-borrow rules you already have, using the info from resolution instead of guessing from syntax.
