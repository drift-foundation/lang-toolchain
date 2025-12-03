## 1. Step-by-step plan to make Optional usable

### Step 0 – Lock the representation

Pick a concrete layout and write it down once (ABI + internal notes):

* Logical model:

  ```drift
  enum Optional<T> {
    Some(value: T)
    None
  }
  ```
* Physical model *for now*:

  ```c
  struct DriftOptional_T {
      Bool is_some;
      T    value;   // undefined / ignored when is_some == false
  }
  ```
* ABI notes:

  * `Bool` is the tag.
  * No null-pointer / niche optimization *yet*; can be added later if needed.
  * This is consistent with how you’ll later lower general enums/variants.

**Action items:**

* [ ] Add a short ABI note (e.g. `docs/design/drift-abi-optional.md` or a section in your existing ABI doc) describing this layout.
* [ ] Add a tiny C helper struct typedef for tests (even if you handle everything in codegen).
* **Status:** Renamed runtime/helper to `DriftOptionalString` and compiler type to `Optional<T>`.
* **Recent:** Fixed the `__exc_args_get` ABI to use an explicit sret out-param and updated SSA codegen to allocate/load `Optional<String>` results; the args-view Optional e2e now passes without crashes.

---

### Step 1 – Decide the minimal surface API

You don’t need full Rust-grade `Option` in one shot. Pick a tiny MVP that unblocks args-view (and other basic use cases):

I’d do:

```drift
struct Optional<T> {
	fn is_some(self) returns Bool
	fn is_none(self) returns Bool
	fn unwrap_or(self, default: T) returns T
}
```

Semantics:

* `is_some` → `self.is_some` flag.
* `is_none` → `!self.is_some`.
* `unwrap_or`:

  * If `self.is_some` ⇒ returns the inner `T`.
  * Else ⇒ returns `default`.

That’s enough to write:

```drift
val opt = view[key]; // Optional<String>
if opt.is_some() {
	println("arg = " + opt.unwrap_or("<unreachable>")); // or better: branch then use
}
```

**Action items:**

* [ ] Add a “Optional” section in the spec (types chapter or a new one under “standard library primitives”).
* [ ] Define these three methods with precise type signatures and rules.

Don’t add `map` / `and_then` / `unwrap_or_else` yet; they’re sugar, not a prerequisite.

---

### Step 2 – Teach the checker about Optional<T> methods

Currently the checker treats `Optional<T>` as opaque and rejects attribute lookups. You need a small special-case for this *one* builtin generic.

**Implementation outline:**

1. **Builtin type/trait table:**

   * Add an entry for `Optional` (or `Option`) as a builtin generic with known methods.
2. **Method lookup:**

   * When resolving `opt.is_some()`:

     * Infer type of `opt` = `Optional<T>` for some `T`.
     * Allow methods:

       * `is_some() -> Bool`
       * `is_none() -> Bool`
       * `unwrap_or(default: T) -> T`
3. **Type checking:**

   * `unwrap_or`:

     * Check arg type equals the inner type `T` of `Optional<T>`.
     * Return type = `T`.

**Action items:**

* [ ] Extend your builtin type registry to include `Optional<T>` + method signatures.
* [ ] Extend `resolve_method_call` / equivalent to handle `Optional<T>` specially.
* [ ] Add checker unit tests:

  * `Optional<Int>.is_some()` returns `Bool`.
  * `unwrap_or` rejects mismatched default type.
  * Attribute `opt.foo()` still fails with a clean error.

---

### Step 3 – Lower Optional methods to SSA

You need to define lowering patterns for these method calls. No runtime C helpers required if you’re okay with IR-level branching.

Assuming logical SSA struct:

```ssa
; %opt : Optional<T> = { is_some: Bool, value: T }

; is_some()
tmp = FieldGet %opt, "is_some" ; Bool
```

```ssa
; is_none()
flag = FieldGet %opt, "is_some"
res  = Not flag
```

```ssa
; unwrap_or(default)
flag    = FieldGet %opt, "is_some"
some_v  = FieldGet %opt, "value"

if flag then
	block_some:
	    v_some = some_v
	    goto join
else
	block_none:
	    v_none = default
	    goto join

join:
result = phi(v_some, v_none)
```

**Action items:**

* [ ] In the method-call lowering, detect receiver type `Optional<T>` and the specific method name.
* [ ] Emit the appropriate SSA pattern instead of a generic method call.
* [ ] Add MIR/SSA unit tests:

  * Manually construct an `Optional<Int>` SSA value and check the `is_some`/`unwrap_or` lowering matches your spec.
  * Negative test: ensure no “regular” method call slips through here.

---

### Step 4 – Lower construction of Optional<T>

You’re already “able to produce them,” but sanity-check the pipeline:

* Creating a `Some(x)` equivalent:

  * In your surface syntax (for now), this is probably some internal construct or literal.
  * Make sure lowering emits `{ is_some = true, value = x }`.

* Creating `None`:

  * `{ is_some = false, value = <undef or zero-init> }`.

**Action items:**

* [ ] Verify that wherever you return `Optional<T>` today, you’re constructing it in the agreed representation.
* [ ] Add a runtime/C helper test that inspects a lowered `Optional<Int>` to confirm the layout.

---

### Step 5 – Use Optional in EArgsView and add e2e

Now you can finally do the real thing you want: optional arg lookups.

Assuming:

```drift
struct EArgsView {
	fn get(self, key: EArgKey) returns Optional<String>
	// or operator[]
}
```

You can now write an e2e:

```drift
fn main() returns Int {
	val view = get_args_view_from_somewhere()

	val key = EArgKey("user_id")
	val opt = view[key]      // Optional<String>

	if opt.is_some() {
		val v = opt.unwrap_or("<unreachable>")
		println("user_id = " + v)
	} else {
		println("user_id not present")
	}

	return 0
}
```

**Action items:**

* [ ] Update the EArgsView API to return `Optional<String>`.
* [ ] Add SSA/e2e test that:

  * Constructs an exception with known args.
  * Builds an `EArgKey` at runtime.
  * Looks it up.
  * Uses `is_some + unwrap_or` to branch and print.
* [ ] Add a negative e2e where the key doesn’t exist and you print the “not present” branch.

---

### Step 6 – (Later) Add real pattern matching / sugar

Once the above works and is stable, then you can consider:

* `if let Some(x) = opt { ... } else { ... }`
* `match opt { Some(x) => ..., None => ... }`
* `?.` / safe-navigation operators.

But they are *optional* for now (pun inflicted).

For now, just make sure the `Optional<T>` ABI and method set are “match-ready,” i.e., compatible with how you’ll later lower pattern matching (tag test + field extraction).

---

## 2. Rename Option<T> to Optional<T>

### Arguments for `Optional<T>`

* More self-descriptive for people coming from Java / general OOP:

  * Java: `java.util.Optional<T>`.
  * Many will instantly read it as “this thing is optional.”
* Reads nicely in English:

  * “This function returns an optional string” → `Optional<String>`.
* Avoids the slightly abstract “Option” term; closer to your docs’ natural language.

Given your target audience (schools, enterprise backend devs, lots of Java/C/C++ background, not primarily Rust functional-programming folks):

* **Switch to `Optional<T>` now**, before the ecosystem and docs grow too heavy.

### What renaming actually costs

**Action items:**

* [ ] Global rename in the spec: all occurrences of `Option<T>` → `Optional<T>`.
* [ ] Update any ABI doc mentioning `Option`.
* [ ] Update checker/builtin table: `Optional` is the known generic type, not `Option`.
* [ ] Update any existing tests that reference `Option`.
* [ ] Decide whether to introduce a `type Option<T> = Optional<T>` alias later (not needed now).

This is a one-time sweep, and you’re early enough that it’s cheap. I’d just do it.

---

### Quick checklist summary

You can literally use this as a TODO list:

* [ ] Decide on name (I suggest `Optional<T>`).
* [ ] Document `Optional<T>` layout in ABI.
* [ ] Add small `Optional<T>` section to spec with `is_some`, `is_none`, `unwrap_or`.
* [ ] Teach checker to recognize `Optional<T>` and these methods.
* [ ] Implement SSA lowering for `is_some`, `is_none`, `unwrap_or`.
* [ ] Verify construction of `Optional<T>` (Some/None) matches the ABI.
* [ ] Wire `EArgsView` to return `Optional<String>`.
* [ ] Add e2e that exercises arg lookup via `is_some` + `unwrap_or`.
* [ ] Only then, think about future pattern matching and `?.` sugar.
