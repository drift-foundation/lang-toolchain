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

---

## Review (K, 2026-07-12) — accepted with additions

**Verdict: the list is sound, no item blocks the next certification, and
`string_arc.py` is the highest-value target.** Two items were missing (8 and 9 below);
two existing items get sharper framing.

### Item-by-item

1. **`string_arc.py` deletion — top prize, and B-arch changed HOW to do it.** With
   C2 = 0, string_arc's residual authority is enumerable by the audit itself:
   temp last-use releases (~363k corpus emissions), overwrite releases (~137k), the
   ledger-consulted scope-exit sweep (~40k), site-4, and retain fallbacks the stakes
   made mostly dead. The deletion should NOT be a rewrite — it should be
   *prove-a-class-dead-then-delete* slices: use the reporter to show an emission class
   is zero (or migrate it to the generic cleanup_authoring/drop_flags authorities),
   delete that branch, corpus as the acceptance signature each time. This framing
   belongs in SCOPE-B-PLAN.md.

2. **C3 allowlist — a POST-CERT DECISION SLICE, not an open-ended exception.**
   "Explicit exception, byte-identical for six corpus generations" is precisely what
   C4 was — counted-never-failed — and C4's retirement proved forcing these to a
   decision is cheap. After cert, one small slice must either model flag-guarded
   cleanup MoveOuts in the ledger event model or bless the allowlist PERMANENTLY with
   a loud pin. The indefinite "temporary" exception is the item's real risk, not the
   11,441 count.

3. **Array release-elision — MEASUREMENT-FIRST.** Extend the reporter to
   ArrayDrop/`_drop_all_arrays` sites for one corpus generation (mirror of B-arch-0)
   and size the win before committing to implementation. Safety direction matches
   strings (wrongly-kept drops of zeroed arrays are no-ops; only wrongly-elided live
   arrays leak — the MUST_NOT_DROP-only guardrail carries over). If UNINIT/MOVED_OUT
   dominance repeats, it is the same slice shape at low risk; if not, drop the item.

4. **`pre_post_verdict_drift` → B-repr input — agreed**; already characterized as
   emission-independent.

5. **B-repr/B5 stays a design slice — agreed.**

6. **Two ownership meanings, one C signature — agreed on fragility, with a
   reframe: this item IS a B-repr input, not a separate track.** B5's accessor-style
   interop is the structural fix; fold the long-term options here (typedef aliases
   are documentation-strength; extern ownership annotations consumed by the AUDIT are
   the cheap version with teeth) into the B-repr design doc rather than doing them
   piecemeal. Interim mitigations now in place: the call-site-decides convention
   wording in string_runtime.h, the owned-string audit, and heap-string valgrind pins
   in both directions for the two new receivers. Residual risk to carry as a RULE,
   not precedent: a stdlib call-site change (`move` → live pass) flips a receiver's
   convention without touching C — therefore **new or changed Convention-A
   DriftString receivers need a heap-string ownership pin, or coverage by an existing
   representative pin** (static literals mask both failure directions; heap strings
   are the only decisive test).

### Missing items (7 and 8 join the "Not Clean Yet" inventory)

7. **Owned-at-extraction contract pin.** Exactly two codegen extraction lowerings
   retain the extracted element (`ArrayIndexLoad[Unchecked]`, `VariantGetField` via
   `_emit_copy_value`), and the stake pass encodes that only as terminal-with-
   comments. Nothing structural stops a future extraction node shipping with a hidden
   retain and being misclassified as a stakeable view — the exact 1d leak shape,
   which cost a full-suite catch. Cleanup: a STATIC AUDIT (same genre as the
   fresh-hint ambiguity scan) tying the set of codegen extraction nodes that call
   `_emit_copy_value` to the stake pass's terminal-producer list, so the class cannot
   regress silently.

8. **Corpus tooling promotion.** The B-arch corpus runner/aggregator live as session
   scratchpad scripts, but the "identical universe / arithmetically exact deltas"
   methodology is what signs off every Scope-B deletion slice. If deletions proceed
   corpus-signed, the runner/aggregator belongs in repo tooling (`tools/`, alongside
   the shared test-runner conventions) so the methodology is reproducible and not
   session-local.

### Answers to the review request

- **Completeness:** complete after adding items 7-8 above.
- **Cert-blocking:** nothing here blocks cert. Item 6 was the only live-bug
  candidate and is pinned in both directions.
- **Priorities:** string_arc deletion (incremental, corpus-signed) > C3 decision
  slice > Array-elision measurement > B-repr design (absorbing items 4, 5, 6).
