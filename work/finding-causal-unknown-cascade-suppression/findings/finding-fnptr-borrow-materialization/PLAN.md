# Plan: materialize named function-pointer borrows safely

1. Preserve the round-2 repro and finalized-binding control.
2. Add a new red structural test that records the borrow subject before and
   after `_apply_fnptr_consts` and identifies the exact `.name` consumer.
3. Add full compile/run positives for `&named_function` and pending `&f` after
   shared finalization.
4. Trace stage1 borrow materialization, place canonicalization, fnptr mark
   installation/replacement, borrow checking, and MIR lowering in order.
5. Repair the earliest semantic boundary that can recognize the function
   constant and provide addressable temporary storage without widening
   canonical places to arbitrary rvalue bases.
6. Retain a clean negative for an actually illegal mutable/non-addressable
   borrow and the existing finalized-local control.
7. Run focused borrow materialization, fnptr, pending-lambda, and full
   compile/run tests; fold the result into pending 0.35.0 with ABI 22.

No existing test edit is planned for this child. Any later need must be added
to the parent's authorization ledger and approved before editing.
