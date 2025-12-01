# Exception code redesign – work-progress

This file tracks the implementation of the exception event-code scheme for Drift, as specified in `drift-exception-code-spec.md`. :contentReference[oaicite:0]{index=0}

The goal is to move from ad-hoc/index-based exception codes to ABI-stable 64-bit event codes with:

- 4-bit kind (test/user/builtin)
- 60-bit payload (xxHash64 of FQN for user exceptions)
- Compile-time and link-time collision checks
- Simple integer comparison at runtime for `try/catch` dispatch

---

## 0. Status snapshot (living checklist)

### 0.1 Spec & ABI

- [x] Write and freeze the exception code spec (`drift-exception-code-spec.md`).
- [ ] Decide on an ABI version tag that “includes exception-code v1” (if you want a formal ABI version bump).
- [ ] Document “mixing modules compiled under different exception-code schemes is forbidden”.
- [x] ABI note on xxHash64 implementation/seed added in code.
- [x] ABI note on xxHash64 implementation/seed added in code.

### 0.2 Runtime representation (Error struct & helpers)

- [x] `Error` struct in the runtime has a 64-bit `code` field.
- [x] `drift_error_new_dummy(Int64 code, DriftString payload)` stores both `code` and first payload into `Error`.
- [x] `Error.code` / `Error.payload` are addressable in the language (checker + SSA codegen).
- [ ] Introduce helpers for constructing event codes by kind:
  - [ ] `uint64_t drift_event_code_from_user_payload(uint64_t payload60)`
  - [ ] `uint64_t drift_event_code_from_builtin_payload(uint64_t payload60)`
  - [ ] `uint64_t drift_event_code_from_manual(uint64_t manual_code)`
- [ ] Implement kind constants in the runtime (or shared header):
  - [ ] `DRIFT_EVENT_KIND_TEST   = 0b0000`
  - [ ] `DRIFT_EVENT_KIND_USER   = 0b0001`
  - [ ] `DRIFT_EVENT_KIND_BUILTIN= 0b0010`
- [ ] Add a tiny C unit test (or runtime-side test harness) that:
  - [ ] Asserts correct packing/unpacking of `(kind, payload)` → `event_code`.
  - [ ] Asserts the 60-bit mask is applied correctly.

### 0.3 Compiler: FQN construction & hashing

- [x] Implement a single canonical FQN builder for exceptions:

  ```text
  <module-name>:<exception-name>
````

* [x] Ensure exactly one `:` separator.

* [x] Ensure no package/path elements sneak in.

* [x] Ensure generics never appear in the FQN (`MyErr<T>` → `"mymodule:MyErr"`).

* [x] Integrate xxHash64 in the compiler:

  * [x] Vendor / depend on a stable xxHash64 implementation.
  * [x] Lock the seed and configuration in one place (ABI-visible comment + code).
  * [x] Expose `hash60(string) -> uint64` utility: `xxhash64(fqn) & ((1uLL << 60) - 1)` (guarded by a unit test).

* [x] At exception declaration time:

  * [x] Build FQN.
  * [x] Compute 60-bit payload via `hash60`.
  * [x] Build full `event_code` with kind = `USER`.
  * [x] Attach event_code to the exception symbol in the compiler’s symbol table.

### 0.4 Compiler: per-module collision table

* [x] Maintain a per-module `Map<payload60, ExceptionSymbol>` while registering exceptions.

* [x] On collision of payload60 with a *different* FQN in the same module:

  * [x] Emit the specified compile-time error:

    ```text
    error: exception code collision in module '<module-name>'
           '<existing-fqn>' and '<new-fqn>' mapped to the same code
    ```

  * [x] Abort compilation.

* [ ] Add negative tests:

  * [ ] Two distinct exceptions in one module intentionally forced to collide (e.g., stubbed hash for testing).
  * [ ] Check error text is stable and reasonably user-friendly.

### 0.5 Linker / DMP packer: global collision checks

* [ ] Extend the module’s “exported metadata” to include an exception table:

  ```text
  (kind: u4, payload60: u60, fqn: string)
  ```

* [ ] At DMP packaging / link-time:

  * [ ] Merge tables from all input modules.
  * [ ] For entries with `kind = USER`:

    * [ ] If two *different* FQNs share `(kind, payload60)`:

      * [ ] Emit hard error:

        ```text
        error: global exception code collision
               '<module1>:<exception1>' and '<module2>:<exception2>'
               share event code 0xXXXXXXXXXXXXXXX
        ```

      * [ ] Abort packaging/link.
    * [ ] If the FQN and module identity are effectively identical (duplicates from identical builds):

      * [ ] Allow silently, or warn only if you care.

* [ ] Tests:

  * [ ] Multi-module test with intentional collision (using stubbed hash).
  * [ ] Ensure collision in user exceptions doesn’t interact with test/builtin kinds.

### 0.6 Builtin exception codes

* [ ] Define the canonical list of builtin exceptions (e.g., `OutOfMemory`, `NullRef`, etc.).
* [ ] Assign each builtin a small constant payload60.
* [ ] Generate their `event_code` with kind = `BUILTIN`.
* [ ] Ensure these constants are exposed:

  * [ ] To the runtime (table or header).
  * [ ] To the compiler (for lowering `throw <builtin>`).
* [ ] Test:

  * [ ] Throw a builtin from a tiny program.
  * [ ] Inspect `Error.code` and verify kind/payload partition.

### 0.7 Frontend → MIR/SSA lowering

* [x] Throw lowering already passes an event code into `drift_error_new_dummy` for tests.

* [ ] Replace any old index-based code derivation with the new FQN-hash-based event_code.

* [ ] Ensure *all* exception construction sites use the same path:

  * [ ] `throw MyExc(...)`
  * [ ] Any factory helpers that construct and throw in one step.
  * [ ] Any “rethrow/clone” helpers, if present.

* [ ] On the catch side:

  * [ ] Ensure catch dispatch is purely integer comparison of `Error.code` vs precomputed event_code.
  * [ ] Make sure per-catch arm event_codes are precomputed once (not recomputed at runtime per throw).
  * [ ] Confirm that runtime dispatch does not depend on exception ordinal/index anymore.

* [ ] SSA tests:

  * [ ] Add SSA program that throws multiple exception types and catches them by type; inspect the generated SSA to see event_code constants.
  * [ ] Regression tests for `try { ... } catch (e: Error)` and simple `catch {}` arms.

### 0.8 Tests & diagnostics

* [ ] Add targeted unit tests around:

  * [ ] FQN builder (module/exception naming edge cases, generics, nested modules if you have them).
  * [ ] Hash60 helper (determinism/stability; maybe snapshot one well-known FQN hash in a test).
  * [ ] Event code packing/unpacking helpers.
  * [ ] Per-module collision detection.
  * [ ] Cross-module collision detection.
  * [ ] Round-trip: `throw → Error.code → catch` for:

    * [ ] user exception,
    * [ ] builtin exception,
    * [ ] manual/test exception via `drift_error_new_dummy`.

* [ ] Add an e2e test that:

  * [ ] Throws a user exception from module A.
  * [ ] Catches it in module B.
  * [ ] Asserts the catch arm is correct and logs/prints the `Error.code` (for manual inspection).

### 0.9 Documentation & tooling

* [ ] Update the language spec error-handling chapter to reference the new event-code scheme at a *conceptual* level (no hash details).
* [ ] Add an ABI appendix noting:

  * [ ] FQN construction rules.
  * [ ] xxHash64 requirement and seed.
  * [ ] 4-bit kind partition.
  * [ ] 60-bit payload mask.
  * [ ] Meaning of kind values `0000`, `0001`, `0010`.
* [ ] Add a dev doc for compiler/runtime maintainers:

  * [ ] “How to add a new builtin exception safely.”
  * [ ] “What breaks ABI and what doesn’t.”
* [ ] Optional: add a debug tool/CLI cmd:

  * [ ] Given an FQN, print its event_code.
  * [ ] Given an event_code, print known FQNs (from available module tables).

---

## 1. Implementation plan (sequence)

This is the recommended order of work so you don’t thrash the compiler and runtime:

### Step 1 – Lock the ABI and helpers (runtime + shared header)

1. Add the bitfield constants and helpers in a shared header:

   * Bit masks, shifts for kind/payload.
   * Functions for packing/unpacking.
2. Wire these helpers into `drift_error_new_dummy` (for test/manual codes).
3. Add minimal tests to ensure packing is correct.

**Exit criteria:** You can create and inspect valid test/manual event codes from C with the new layout.

---

### Step 2 – FQN builder and hash60 in the compiler

1. Implement a single FQN builder for exceptions in the compiler frontend.
2. Integrate xxHash64 (locked seed) and expose `hash60(fqn)`.
3. Temporarily add a debug option to print `<exception, FQN, hash, event_code>` for sanity checks.

**Exit criteria:** Each exception declaration gets a deterministic 60-bit payload and a full 64-bit event_code in the symbol table.

---

### Step 3 – Per-module collision detection

1. Add the `payload60 → exception` map in the module context.
2. On each new exception, check for collisions and emit the defined error.
3. Write one or two negative tests that force collisions (using stub hash or test hook).

**Exit criteria:** The compiler refuses to build a single module that has conflicting exception codes.

---

### Step 4 – Export exception tables and add link-time checks

1. Extend the module metadata export with `(kind, payload60, fqn)` rows.
2. Extend the DMP/pack/link tool to:

   * Read and merge all exception tables.
   * Run global collision checks for user exceptions.
3. Add tests:

   * Two modules with forced collision → link failure.
   * Two modules with identical FQNs → allowed.

**Exit criteria:** You cannot produce a DMP with ambiguous exception codes.

---

### Step 5 – Builtin exception constants

1. Enumerate builtin exceptions, assign small payloads.
2. Generate their event_codes using the same helpers (kind = builtin).
3. Update compiler lowering of builtin throws to use these constants.
4. Add a test program that throws each builtin once and dumps `Error.code`.

**Exit criteria:** Builtin exceptions use the same event_code machinery but with fixed constants.

---

### Step 6 – Replace old index-based paths in lowering

1. Remove any legacy “exception index” paths still in the compiler.
2. Ensure:

   * `throw` lowering pulls event_code from the exception symbol.
   * `try/catch` lowering uses those event_codes for dispatch.
3. Re-run all existing try/catch e2e tests:

   * They should still pass, now using the new codes.

**Exit criteria:** All exception paths in the compiler use event_codes derived from the spec; no vestigial index logic remains.

---

### Step 7 – Harden with tests & docs

1. Add the remaining SSA/e2e tests listed above.
2. Update the spec and ABI docs.
3. Remove any temporary debug prints or hash overrides.

**Exit criteria:** The scheme is covered by tests, documented, and stable enough to be relied on by future features.

---

## 2. Immediate next actions for you

If you want a concrete “do this next” list based on where things currently stand:

1. **Add the event-code helper functions and constants to the runtime header and C implementation** (kind bits, payload mask, pack/unpack).
2. **Implement the FQN builder + hash60 in the compiler**, attach event_codes to exception symbols, and add a debug dump.
3. **Introduce the per-module collision map and one negative test** to prove the collision error path works.

Once those three are in place, you’ll be ready to drive into the MIR/SSA lowering and link-time checks with much less friction.
