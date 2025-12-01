# Exception code specification

This document defines how Drift derives the integer **event code** stored in `Error.code`.
Event codes are used exclusively for *exception dispatch* during `try/catch`.

The goals:

* **Stable across modules and builds**
* **Deterministic** per exception type
* **Order-independent**
* **ABI-defined**
* **Cheap** to compute and compare at runtime
* **Collision-free** in practice and guaranteed by static checks

This mechanism replaces earlier index-based exception codes.

---

## 1. Code layout (64-bit)

Each event code is a 64-bit unsigned integer consisting of two parts:

```
[ 4 bits kind ][ 60 bits payload ]
```

### 1.1 Kind field (upper 4 bits)

Currently defined kinds:

| Kind (binary) | Meaning                                                        |
| ------------- | -------------------------------------------------------------- |
| `0000`        | Test / manual codes (e.g., `drift_error_new_dummy()` in tests) |
| `0001`        | **User-defined exceptions**                                    |
| `0010`        | **Core language / builtin exceptions**                         |
| `0011`–`1111` | Reserved for future use                                        |

All user exceptions must have kind = `0001`.
All builtins must use `0010`.
Test/manual codes always use `0000`.

### 1.2 Payload field (lower 60 bits)

* For **user exceptions**, the payload is a 60-bit truncated xxHash64 of the exception’s fully-qualified name (FQN).
* For **test/manual codes**, the payload is the literal integer supplied to `drift_error_new_dummy()`, masked to 60 bits.
* For **builtin exceptions**, the payload is a small constant chosen by the runtime.

---

## 2. Fully-qualified name (FQN)

User exceptions derive their codes from:

```
<module-name>:<exception-name>
```

Notes:

* Exactly one `:` separator.
* No package path components.
  (Module names are already globally unique through the module system.)
* Exception generics **never** appear in the FQN.
  `MyErr<T>` always hashes as `"mymodule:MyErr"`.

FQN construction is part of the language ABI and must not change.

---

## 3. Hash algorithm

User exceptions use the following payload derivation:

```
payload = xxhash64(fqn_string) & ((1uLL << 60) - 1)
```

Requirements:

* Use **xxHash64** (official implementation) in its standard configuration.
* The algorithm and seed value are **ABI-locked**.
* Payload must be the lower 60 bits of the 64-bit hash.

This gives strong distribution and extremely low collision probability while staying cheap.

---

## 4. Constructing the final event code

For user exceptions:

```
kind    = 0b0001
payload = hash60(FQN)

event_code = (kind << 60) | payload
```

For test/manual:

```
kind    = 0b0000
payload = (manual_code & ((1uLL << 60) - 1))

event_code = (kind << 60) | payload
```

For builtin:

```
kind    = 0b0010
payload = builtin_constant

event_code = (kind << 60) | payload
```

---

## 5. Collision handling

### 5.1 Compile-time (within one module)

As each exception is registered during compilation:

* Compute its event code.
* Keep a local map: `payload → exception-name`.
* If a new exception collides with an existing payload:

  ```
  error: exception code collision in module '<module-name>'
         '<existing-exception>' and '<new-exception>' mapped to the same code
  ```

Compilation halts.

### 5.2 Link-time (across all modules)

Each compiled module exports its `(kind, payload, fqn)` table.

At link / DMP packaging time:

* Merge all exception tables.

* For all entries with kind = `0001` (user exceptions):

  * If two distinct FQNs share the same payload → **hard error**.

  ```
  error: global exception code collision
         '<module1>:<exception1>' and '<module2>:<exception2>'
         share event code 0xXXXXXXXXXXXXXXX
  ```

* It is acceptable for the **same** FQN from identical module builds to appear multiple times.

This guarantees that runtime dispatch is unambiguous.

---

## 6. Runtime behavior

* `drift_error_new_dummy()` writes the 64-bit event code directly into the runtime `Error` struct.
* `drift_error_get_code()` returns this stored value.
* SSA lowering for `throw MyExc(...)` always uses the derived user exception code.
* SSA catch dispatch compares the stored 64-bit event code against the precomputed event codes for each catch arm.

Dispatch remains a simple integer comparison.

---

## 7. ABI considerations

The following are part of the **Drift ABI v1** and cannot change without breaking precompiled modules:

* FQN construction rule
* xxHash64 algorithm and seed
* The 4-bit kind partition
* The 60-bit payload mask
* The meaning of kind values `0000`, `0001`, `0010`

Changing any of the above requires a new ABI version and forbids mixing modules compiled under different versions.

---

## 8. Debugging / logging expectations

Although `Error.code` is an opaque internal discriminator:

* It may be logged for debugging.
* Developers should not hand-match on specific integer values.
* All user-visible dispatch must occur through `catch` and exception types, never through numeric comparisons.

---

## 9. Summary

This scheme delivers:

* Deterministic, stable exception codes
* No dependence on declaration order
* ABI-stable representation
* Protection against test/builtin/user overlap via kind bits
* Collision safety via compile-time + link-time checks
* Consistent, cheap runtime dispatch
