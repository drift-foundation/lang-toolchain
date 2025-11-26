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


