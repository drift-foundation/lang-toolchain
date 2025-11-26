## 8. Imports vs Standard I/O (Chapters 7 and 18)

### Tasks:

* Chapter 7 should mention console I/O **only lightly** and refer to Chapter 18.
* Chapter 18 remains the canonical IO design.

---

## 9. Closure / Callable section (Chapter 6)

* Clarify whether `Callable<Args, R>` is a real interface defined somewhere.
* Move closures to a dedicated chapter and fully specify `Callable`

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


