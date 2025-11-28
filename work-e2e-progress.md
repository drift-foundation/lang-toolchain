bring these old MIR codegen tests back, **test-by-test**, on top of the new SSA-first pipeline and the `expected.json`-style e2e harness we talked about.

Reference files:

```text
tests/mir_codegen/
  attr_array
  attr_array_large
  domain_default
  domain_override
  error_path
  error_path_ok
  exception_domain
  frames_captures
  frames_chain
  frames_one
  frames_three
  frames_two
  runtime_basics
  runtime_effects
  runtime_functions
  runtime_index_bounds
  runtime_logic
  runtime_module_invalid_name
  runtime_module_reserved_prefix
  runtime_mutable_bindings
  runtime_reserved_keyword_name
  runtime_ternary
  runtime_try_catch
  runtime_while_basic
  runtime_while_nested
  runtime_while_try_catch
  sum_int64s
  try_catch
  try_else
  try_else_error
```

All of these have `input.drift`, `main.c`, and `expect.json`.

---

## Global e2e structure (shared for all tests)

Before going per-test, this is the *common* target state for every one of them:

For each test `<name>`:

```text
tests/e2e/<name>/
  main.drift        # from input.drift, adjusted to current syntax/imports
  harness.c         # from main.c (same filename or main.c, up to you)
  expected.json     # from expect.json, maybe normalized slightly
```

And a **single** `tests/e2e_runner.py` that, for each test dir:

1. Invokes:

   ```bash
   .venv/bin/python3 -m lang.driftc \
     --ssa-check --ssa-check-mode=fail --ssa-simplify \
     tests/e2e/<name>/main.drift \
     # plus flags telling driftc to:
     #   - emit an object or C file suitable for linking
     #   - *not* set SSA_ONLY so codegen actually runs
   ```

2. Compiles `harness.c` + Drift-generated object(s) + runtime libs into a binary.

3. Runs the binary, compares `{exit, stdout, stderr}` to `expected.json` (same keys you already used: `"exit"`, `"stdout"`, `"stderr"`).

Keep the old `"exit"` key; no reason to rename to `exit_code` unless you want to.

---

## Phase 0 – Bring back infra first

**Goal:** e2e exists, but with only 1–2 tests wired.

1. Create `tests/e2e/` and `tests/e2e_runner.py` as described.

2. Add **one** trivial test (I’d pick `runtime_basics`) to prove the harness:

   * `runtime_basics/main.drift` ← `tests/mir_codegen/runtime_basics/input.drift`, but:

     * Update `import sys.console.out` → whatever your current console import is (likely `import std.console.out`).
     * Adjust any renamed functions if you changed the stdlib surface.
   * `runtime_basics/harness.c` ← `tests/mir_codegen/runtime_basics/main.c` (it just calls `main_drift()` and returns 0).
   * `runtime_basics/expected.json` ← copy `tests/mir_codegen/runtime_basics/expect.json` (`exit: 0`, `"hello, tests\n"`).

3. Add `just test-e2e`:

   ```just
   test-e2e:
       ./.venv/bin/python3 tests/e2e_runner.py
   ```

Once that compiles, links, runs, and matches `expected.json`, infra is good enough. Then:

---

## Phase 1 – “Happy path” runtime tests

These don’t stress error runtime or module system; they’re pure “does code execute correctly?”

### 1. `runtime_basics`  ✅ (pilot)

**Behavior:** prints `"hello, tests\n"`; exit 0.
**Plan:** Already used as pilot above.

### 2. `runtime_functions`

**input.drift:** pure function calls + returns (no IO/try).
**expect:** exit 0, deterministic stdout.

**Plan:**

* `tests/e2e/runtime_functions/main.drift` ← copy + adjust imports if needed.
* `tests/e2e/runtime_functions/harness.c` ← copy main.c (likely just calls `main_drift()`).
* `tests/e2e/runtime_functions/expected.json` ← copy.

**Risk:** Low. You already support function calls and returns under SSA; legacy codegen handled this before.

---

### 3. `runtime_logic`

**input.drift:** boolean expressions, `and`/`or`, comparisons, prints some booleans/text.
**expect:** exit 0, a few lines showing short-circuit behavior.

**Plan:**

* Same mapping as above; only adjust the `import sys.console.out` → current console module.
* If your bool printing changed (`true`/`false` vs `1`/`0`), update `expected.json` accordingly.

**Risk:** Low; SSA supports logic ops, and legacy MIR codegen used to as well.

---

### 4. `runtime_mutable_bindings`

**input.drift:** nested `while` loops, `var` + reassignment, integer arithmetic, `out.writeln(acc)`; exit 0, prints a number.

**Plan:**

* Same directory mapping.
* This will exercise:

  * nested loops,
  * mutable vars,
  * simple arithmetic.
* SSA already handles while + assignments; codegen did before.

**Risk:** Low-medium; good sanity for env threading + while lowering.

---

### 5. `runtime_while_basic` / `runtime_while_nested`

**input.drift:** `while` loops updating counters; maybe prints final value.
**Plan:**

* Mirror mapping.
* These tests stress:

  * multiple backedges,
  * loop-carried variables.

You already have SSA unit tests and SSA regression programs for while; these e2e tests will confirm codegen matches.

---

### 6. `runtime_ternary`

**input.drift:** likely uses the conditional/ternary expression you implemented (`cond ? a : b` equivalent in Drift surface).
**Plan:**

* Same mapping.
* Confirms your lowering + codegen handle ternary correctly under SSA.

---

### 7. `sum_int64s`

**input.drift:** defines `fn add(a: Int64, b: Int64) returns Int64`.
**main.c:** calls `add(40, 2)` and prints `add(40, 2) = 42`.
**expect:** exit 0, that output.

**Plan:**

* `tests/e2e/sum_int64s/main.drift` ← copy unchanged.
* `tests/e2e/sum_int64s/harness.c` ← copy main.c.
* `tests/e2e/sum_int64s/expected.json` ← copy.

This one tests “Drift as a library function”:

* driftc must emit an object file exporting `add`,
* your C harness links against it.

If your current `driftc` CLI differs (e.g., `--emit-obj` vs `--emit-exe`), encode that in `e2e_runner.py` for this category: for tests that **don’t** define `main`, treat them as library cases and compile harness + drift object only.

---

## Phase 2 – Runtime + exceptions (happy-path exit 0)

These tests exercise thrown exceptions + try/catch, but expected exit is 0 (success cases).

### 8. `runtime_effects`

**input.drift:** defines an exception, throws/catches, may print “ok N\n”; exit 0.
**Plan:**

* Same mapping.
* Confirms full stack: SSA exceptions, mir Throw, try/catch, plus runtime/FFI glue.

### 9. `runtime_try_catch`

**input.drift:** try/catch with an exception type; prints a sequence like:

```text
2025-11-20
caught InvalidDate
1970-01-01
```

**Plan:**

* Same mapping.
* This will validate:

  * exception type carrying payload,
  * control flow into catch and afterwards,
  * runtime error representation.

### 10. `try_catch`, `try_else`, `try_else_error`

These are narrower exception tests:

* `try_catch`: basic try/catch semantics.
* `try_else`: a `try foo() else fallback` expression test.
* `try_else_error`: specific condition where else/try interplay is important, expected `"ok 7\n"`; exit 0.

**Plan:**

* One directory per test, same mapping.
* These assert you didn’t regress your exception semantics when you changed the error model.

---

## Phase 3 – Error paths (exit 1)

These tests **expect** a non-zero exit and particular stderr content.

### 11. `error_path` (exit 1)

* Drift defines a function that can return an error; harness calls it, prints any error message to stderr and returns 1.
* `expected.json` has `"exit": 1`, empty stdout, stderr with JSON error payload.

**Plan:**

* Same mapping.
* The `e2e_runner` must compare stderr *exactly* or with a substring match (your old `expect.json` has the full JSON; keep using exact match if possible).

### 12. `error_path_ok` (exit 0)

* Same code path as above, but error path not taken; `stdout: "ok 2\n"`, `stderr: ""`.

**Plan:**

* Same mapping.
* This pair (`error_path`, `error_path_ok`) ensures:

  * your error/Ok model,
  * runtime wrappers (`Pair { val, err }`),
  * and error formatting are unchanged.

---

### 13. `attr_array` / `attr_array_large` (exit 1)

* Drift defines exceptions with many attributes; throws them; harness prints the serialized error (JSON) to stderr and exits 1.
* These are about the **shape and ordering** of attributes in error JSON.

**Plan:**

* Same mapping.
* Ensure your new error struct / JSON serialization code still matches old layout. If you’ve changed JSON shape, either:

  * update `expected.json`, or
  * intentionally adjust the serializer and make a note that these tests lock in the *new* contract.

---

### 14. `exception_domain`, `domain_default`, `domain_override` (most exit 1)

* `exception_domain`: tests that an exception declared with `domain = "net"` carries that domain into the JSON. Exit probably 1.
* `domain_default`: tests default domain (“main”), exit 1.
* `domain_override`: tests overriding domain per call, exit 1.

**Plan:**

* Same mapping; focus on stderr JSON comparing `domain` fields.

---

### 15. `frames_*` (stack frames)

* `frames_one`, `frames_two`, `frames_three` – expected exit 0; probably ensure trace depth matches.
* `frames_captures`, `frames_chain` – exit 1; check that captured stack frames include module/file/line and captured locals.

**Plan:**

* Same mapping, but be strict:

  * If your “frame capture” format changed, adjust expectations consciously.
  * Consider using substring matching for some fields (e.g., module names) but keep `event`, `domain`, and top frame stable.

These are your **canary tests** that error propagation and stack capture still work after all the SSA refactor work.

---

## Phase 4 – Module/name validation (compile-time-ish behavior)

These tests probably don’t rely heavily on runtime; they validate module/identifier rules.

### 16. `runtime_module_invalid_name`

* Likely tests that invalid module names are rejected, or that runtime names are sanitized.
* `expect.json` currently has `"exit": 0` and maybe empty output – this one might be more subtle.

**Plan:**

* Once you remember the exact intent, either:

  * treat this as a **compile-time** error test (where `expected.json` encodes the compiler error substring), or
  * keep it as a runtime test but assert on some non-output condition.

You may want to re-read `input.drift` & `main.c` and decide.

### 17. `runtime_module_reserved_prefix`

* Similar, but about reserved module prefixes (e.g., `lang.` / `sys.` not allowed for user code).

**Plan:**

* Same approach as above.

### 18. `runtime_reserved_keyword_name`

* Probably tests that you can’t name a variable/module using a reserved keyword.

**Plan:**

* This might be **compile-error expected**:

  * `expected.json` would then carry `"compile_error": "reserved keyword"` or similar (you can extend the schema for these).

---

## Phase 5 – Index bounds & misc

### 19. `runtime_index_bounds` (exit 1)

* Drift program intentionally goes out of bounds, runtime aborts or throws, stderr shows something, exit 1.

**Plan:**

* Same mapping; this validates your runtime bounds checks + error formatting.

---

## How to actually sequence this work (practical order)

You don’t have to bring all 30 online at once. A sane order:

1. **Infra + Pilot**

   * Implement `tests/e2e_runner.py`.
   * Wire `runtime_basics` only.
   * Get `just test-e2e` green for that one.

2. **Batch A (no exceptions, no modules)**

   * `runtime_functions`
   * `runtime_logic`
   * `runtime_mutable_bindings`
   * `runtime_while_basic`
   * `runtime_while_nested`
   * `runtime_ternary`
   * `sum_int64s`

3. **Batch B (exceptions, but exit 0)**

   * `runtime_effects`
   * `runtime_try_catch`
   * `try_catch`
   * `try_else`
   * `try_else_error`

4. **Batch C (error paths, exit 1)**

   * `error_path`
   * `error_path_ok`
   * `exception_domain`
   * `domain_default`
   * `domain_override`
   * `attr_array`
   * `attr_array_large`

5. **Batch D (frames)**

   * `frames_one`
   * `frames_two`
   * `frames_three`
   * `frames_captures`
   * `frames_chain`

6. **Batch E (modules / names / bounds)**

   * `runtime_module_invalid_name`
   * `runtime_module_reserved_prefix`
   * `runtime_reserved_keyword_name`
   * `runtime_index_bounds`

For each test in each batch, the repeated micro-steps are:

1. Create `tests/e2e/<name>/`.
2. Copy/adjust `input.drift → main.drift` (update imports, syntax if needed).
3. Copy `main.c → harness.c` (or keep `main.c` if you prefer).
4. Copy `expect.json → expected.json` (and tweak only if your observable behavior changed and you *want* the new behavior).
5. Add `<name>` to `e2e_runner.py` (usually just picked up automatically by walking directories).
6. Run `just test-e2e`; fix whatever falls out (usually either codegen flag mismatch or minor runtime/JSON drift).
