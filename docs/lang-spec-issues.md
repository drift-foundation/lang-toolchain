## 1. Cross-reference fixes (now that chapters are renumbered)

### 1.1 Ownership chapter → Copy trait

* Text still refers to “see Section 13.3”.
* Should now reference **Chapter 5** (Traits).

### 1.2 ByteBuffer → Send/Sync

* References “Section 13.13”.
* Should now reference **Chapter 5, section: Thread-safety marker traits**.

### 1.3 Pointer-free surface (Chapter 17)

* Mentions “see Section 15.1.2” for Slot/Uninit.
* Those definitions are now in **Chapter 16 (Memory model)**.
* Update all such references.

### 1.4 Plugins (Chapter 21) references

* Says Result is in “Chapter 9” and Error in “Chapter 9”.
* Should reference:

  * **Result → Chapter 10 (Variant types)**
  * **Error → Chapter 14 (Exceptions)**

### 1.5 Exceptions chapter referencing `variant`

* Add “See Chapter 10 for variant definition”.

### 1.6 Traits → concurrency

* Remove “Section 16.6”.
* Replace with “See Chapter 19”.

### 1.7 Appendix B header

* Still says “Trait/implement/where grammar in Appendix C”
* Should say “Traits/implement grammar in Appendix C”.

---

## 2. Null-safety vs Option<T> conflict (Chapters 10 & 11)

### Required fix:

* Decide whether the canonical optional type is `Option<T>` **or** `Optional<T>`.
* Strong recommendation:
  ✔ Unify around `variant Option<T>` in Chapter 10.
  ✔ Rewrite Chapter 11 to describe helpers *for* Option<T> instead of a second type.

### Tasks:

* Remove `interface Optional<T>` from Chapter 11.
* Replace `Optional.of/none` with `Option.some/none` (or whichever naming you choose).
* Update all examples accordingly.
* Ensure exceptions / algorithms / examples use the unified optional type.

---

## 3. Broken code blocks + misplaced tuple section (Chapter 11)

Tasks:

1. Close the code fence before “### Tuple structs & tuple returns”.
2. Move that entire tuple section into **Chapter 3** (structs) or a short separate chapter.
3. Fix method names inside examples (`to_string` rather than `toString`).
4. Fix usage of `let` inside examples.

---

## 4. Keyword consistency: remove `let`

* Replace all `let` usages with `val` or `var`.
* Confirm `let` is **not** listed in Chapter 9 keywords.
* Do a global pass.

---

## 5. Method naming consistency

Replace camelCase `.toString()` with snake_case `.to_string()` everywhere:

* Null-safety examples
* Exceptions chapter
* Variant examples
* ByteBuffer examples

---

## 6. Trait grammar cleanup (Chapter 5 + Appendix C)

### Tasks:

* Rewrite Appendix C to match actual syntax:

  * `require T is Trait`
  * `require Self is Trait`
  * Trait guards: `if T is Trait`
* Remove:

  * `where`
  * `Self has`
  * Old grammar references
* Update FnDef grammar if needed.

---

## 7. Pipeline operator formal spec (Chapter 15)

### Tasks:

1. Add `>>` to operator table in **Chapter 9**.
2. Add precedence/associativity notes.
3. Add grammar entry in Appendix B:

   ```
   PipeExpr ::= PipeExpr ">>" PipeStage | PrimaryExpr
   PipeStage ::= Ident | CallExpr | ...
   ```
4. Add desugaring rules in Chapter 15:

   * Mutator: `|ref mut T|` → borrow
   * Transformer: `|T|` → move
   * Finalizer: `|T| returns Void` → consume
   * Associativity: left-associative

---

## 8. Imports vs Standard I/O (Chapters 7 and 18)

### Tasks:

* Chapter 7 should mention console I/O **only lightly** and refer to Chapter 18.
* Chapter 18 remains the canonical IO design.

---

## 9. Closure / Callable section (Chapter 6)

* Clarify whether `Callable<Args, R>` is a real interface defined somewhere.
* Either:
  ✔ Move closures to a dedicated chapter and fully specify `Callable`
  ✔ OR mark the closure section as **preview** and non-normative.

---

## 10. Memory model vs Pointer-free chapter (16 vs 17)

### Tasks:

* Slot<T> and Uninit<T> definitions should live exclusively in **Chapter 16**.
* Chapter 17 should reference them instead of re-defining.
* Remove stale references like “see Section 15.1.2”.

---

## 11. Appendix B (grammar excerpt) cleanup

Either:

✔ Add a note: “Appendix B is partial; full grammar coming later.”

or:

⬆ Update the grammar fully (large task):

* `returns`
* `module`
* `variant`
* `exception`
* `require`
* pipelines
* interface syntax
* slot/uninit not exposed


