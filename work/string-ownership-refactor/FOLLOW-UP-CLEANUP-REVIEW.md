# String Cleanup Follow-Up Review

Status: follow-up note for K after the immediate blocking-FFI audit bug is patched.

## Context

Recent String work made the ownership model cleaner: direct String copy classification is mode-independent, invisible retain stakes are now ledger-visible, large no-op release classes have been elided, and retired allowlists now fail closed.

The remaining concern is not that the direction is wrong. The concern is that the system is still mid-migration: the architecture is cleaner than before, but there are still explicit places where old and new ownership authorities coexist.

## Not Clean Yet

1. `string_arc.py` still exists.
   - The long-term goal is still to delete it or shrink it until it is no longer an independent ownership authority.

2. C3 moveout modeling is still allowlisted.
   - It has stayed byte-identical through multiple slices, so it is not an urgent signal, but it remains an explicit exception in the audit model.

3. Array release-elision is still a separate follow-up.
   - String scope-exit release elision landed, but the Array side of the analogous cleanup is not yet evaluated or implemented.

4. `pre_post_verdict_drift` remains as a modeling artifact.
   - This is now cleanly isolated and should feed the B-repr/B5 representation planning rather than being treated as a correctness blocker by itself.

5. Runtime representation is still the old `{len, ptr}` layout.
   - B5/RcBytes remains the planned representation direction if we proceed with B-repr: keep inline len, move the header to offset 0 behind `ptr`, use accessor-style C interop, and retire the `{0, NULL}` special case.

6. By-value `DriftString` at the C boundary still has two ownership meanings with the same C signature.
   - Owned-transfer receivers must release exactly once, usually via `DRIFT_OWNED_STRING`.
   - Borrowed pass-through receivers must not release and need an explicit audit allow marker.
   - The distinction currently lives in the Drift call shape (`move` versus live pass), comments, and the owned-string audit. That is acceptable for the current hot fix, especially with heap-string Valgrind pins, but it is still fragile because a source-level call-site ownership change does not change the C function signature.
   - Long-term cleanup options: ABI-facing aliases/macros such as `DriftOwnedString` versus `DriftBorrowedString`, extern ownership annotations consumed by codegen/audits, or borrowed string-view APIs for runtime functions that only inspect/copy bytes.

## Review Request

After the immediate bug is patched, please review whether this list is complete and whether any item should block the next certification. My current read is:

- No item here blocks cert by itself.
- The highest architectural value is still deleting or collapsing `string_arc.py`.
- Array release-elision is worth evaluating, but only if it removes real runtime work or prevents the same class of defects we just saw.
- B-repr/B5 should remain a design slice until the remaining authority cleanup is stable.
