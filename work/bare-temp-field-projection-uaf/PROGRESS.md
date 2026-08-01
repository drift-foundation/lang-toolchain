# Progress — bare-temp-field-projection-uaf

- [x] 2026-07-31 Slice opened (SEPARATE from the toolchain-meta-stamps
      release-gate run). Classification: LANGUAGE_BUG, release-blocking
      memory unsafety.
- [x] 2026-07-31 REPRODUCED (not yet a regression test — no RED pin
      exists until §6's fixtures are written and observed RED) on the
      current candidate: the
      26-line heap-backed `peek(mk().root)` aborts base exit 134
      (`String flags: reserved bit set`); `peek(&mk().root)` rejected
      E_REDUNDANT_ARG_BORROW; hoisted control rc 0. Variant probes:
      nested field `mk().mid.leaf` = SAME UAF; index `mk()[0]` = SOUND;
      `&mut mk().root` = REJECTED (addressable-place message, NOT
      E_MUT_RVALUE_ARG_BINDING_REQUIRED).
- [x] 2026-07-31 ROOT CAUSE traced through the requested paths
      (checker HBorrow synth → _visit_expr_HBorrow →
      _lift_rvalue_ref_base_for_borrow/_validate_lifted_chain →
      _visit_expr_HField source_is_owned_rvalue →
      _materialize_owned_temp / _materialize_owned_temp_for_borrow):
      DOUBLE drop-registration of one leaf backing — slot A (PR source,
      __field_src_) + slot B (Node borrow-temp, __borrow_tmp) both
      release root.text/root.children. The owned pure-field chain is
      REFUSED by the lift helper (owned base admitted only for
      index chains) and falls to the whole-expression materialization
      that shallow-aliases the leaf into a second droppable temp.
      Reviewer's hypothesis confirmed verbatim.
- [x] 2026-07-31 TRIGGER DECISIONS: String ownership-authoring
      conformance matrix FIRES (bounded matrix deliverable). Unify
      String/Arc DOES NOT FIRE — the ENTIRE refactor (Scope A + Scope B)
      already SHIPPED (0.33.75 classification/centralization → 0.33.79
      B-arch → 0.33.87 string_arc deletion → 0.33.88/ABI 22 DriftRcBytes);
      this is a POST-COMPLETION coverage defect in the unified
      architecture (the borrow-temp fallback accepted an alias-marked
      projection as owned instead of address-projecting / _copy_if_ref_
      alias'ing), and the address-projection fix RESTORES the existing
      contract rather than re-triggering any rewrite. Rationale in
      PLAN §7.
- [x] 2026-07-31 PLAN.md written: confirmed failure, exact subsystem,
      the three HIR/MIR comparisons, primary fix (admit owned pure-field
      chains in _lift_rvalue_ref_base_for_borrow, mirroring the sound
      index path — materialize base once + AddrOfField, no slot B) with
      the autoborrow_owned_rvalue_field_method_unchanged pin distinction
      to resolve, secondary fallback, &mut reconciliation, bounded
      matrix, and the 0.33.91 migration-guidance correction.
- [ ] REPORT BACK to maintainer (minimal failing path, subsystem,
      HIR/MIR diff, trigger decision, proposed matrix) — AWAITING
      approval before implementing. No fix code written yet.
- [ ] (post-approval) Regression-first RED pins; primary fix; matrix
      green on base+ASan+memcheck+alloc-track; extend
      test_rvalue_arg_temp_drop_ab.py with field/index/nested heap
      cells; &mut rejection pin; migration-guidance doc correction.
      Expected next compiler version, ABI 22 unchanged.

- [x] 2026-07-31 REVIEW-ROUND-1 CORRECTIONS folded into PLAN:
      * #1 (root cause): dest IS marked as a ref-alias
        (_visit_expr_HField line 4089, a separate `if`) — corrected §2.
        The real gap is _materialize_owned_temp_for_borrow storing the
        marked alias WITHOUT _copy_if_ref_alias (the one
        ownership-transfer boundary that omits it). "Add the missing
        mark" fallback WITHDRAWN.
      * #2 (base generality): _validate_lifted_chain accepts only
        HCall/HMethodCall/HInvoke bases; constructor/block/ternary
        owned temps stay exposed. Fix GENERALIZES the lift to arbitrary
        SAFE rvalue bases (matching stage1's _split_lift_place_chain);
        checker rejection reserved only as emergency fail-closed. Row
        CTF pins constructor-field ACCEPT-and-drop-once.
      * #3 (method pin): autoborrow_owned_rvalue_field_method_unchanged
        is an owned-field METHOD RECEIVER through this same helper (not
        a distinct shape); Handle{raw:Int} carries no owned state. To
        be REWRITTEN as a semantic run/drop pin (Handle+String+
        destructor), preserving behavior, dropping the "must not lift"
        contract.
      * #4: matrix now ENUMERATED as explicit rows (CF/NF/CTF/MIX/IDX/
        HOI/PROV/MRC/THR/MUF/MUI) with per-row lanes.
      * #6: migration guidance VERSIONED — field projections unsafe on
        0.33.91–0.33.93, pure-index already sound, bare field
        projection valid again after the fix (not a permanent
        exception).

- [x] 2026-07-31 REVIEW-ROUND-2 CORRECTIONS: CTF resolved toward
      ACCEPTANCE (shared rvalue borrows are established behavior; lift
      generalizes to safe constructor/block/ternary bases) not the
      accept-or-reject fork; matrix gained SFU (string_from_utf8_bytes
      representative producer — fired-trigger requirement) and LIT (an
      explicitly-labelled static-literal MASK CONTROL, not an ownership
      proof); per-row alloc-track lanes marked (CF/CTF/MIX/SFU/THR);
      constructor-constraint "row CF" typo → CTF.

- [x] 2026-07-31 REVIEW-ROUND-3 (gate details): (1) PROV upgraded from
      runtime-only to IR BYTE-IDENTITY per DriftQuery ask #2 — literal
      LLVM-IR equality for fixed CF between the programmatic explicit
      baseline and the bare spelling with build metadata held constant
      (stamp/provenance sections fixed-or-stripped), an accepted IDX
      byte-identity counterpart, and diagnostic-equivalence (not bytes)
      for the rejected &mut pair. (2) SFU arity fixed to
      core.string_from_utf8_bytes(ptr, len) with a real buffer pointer
      + length. (3) LIT description made precise: a static String masks
      ownership defects because releasing its static backing is a
      no-op (not "bitcopy-ish"). (4) Matrix STATE column added: only
      CF/NF are CONFIRMED RED (observed via probes); CTF/MIX/SFU/THR
      are "expected RED; must be observed before the fix" — flip to
      confirmed only after regression-first execution.

- [x] 2026-07-31 REGISTRY/DOC CORRECTION (maintainer, revised after full
      history check): doc/refactor_triggers.md "Unify String/Arc
      ownership" entry MARKED COMPLETED (~~struck~~ title + Status
      blockquote) citing the adopted sequence 0.33.75 Scope A → 0.33.79
      B-arch → 0.33.87 string_arc endgame/deletion → 0.33.88/ABI 22
      DriftRcBytes; all "future work"/"deferred"/"Scope when triggered"
      framing superseded (Scope B paragraph annotated SHIPPED). Entry
      closed, not kept artificially open; a new representation project
      would need a new registry entry. Bug-slice PLAN §7 + PROGRESS
      reframed to post-completion coverage defect. (Docs only — not
      gate-tested; safe alongside the running suite.)

- [x] 2026-07-31 REVIEW-ROUND-4 (gate detail): &mut diagnostics
      corrected — the two SOURCE spellings genuinely DIFFER, so no
      equivalence between them: `bump(&mut mk().root)` (real
      source-written) → E_REDUNDANT_ARG_BORROW (new MRB redundancy-rule
      sanity pin); bare `bump(mk().root)` → bind-first mutable-rvalue
      diagnostic (MUF/MUI). Diagnostic-EQUIVALENCE now compares the BARE
      form ONLY against its programmatic source_written=False baseline
      (never bare-vs-explicit). §5 + IR-identity section + matrix
      updated. MRC (rewritten String+destructor pin) and LIT (new
      mask control) relabeled "expected sound; observe before fix" —
      only IDX/HOI carry "observed" (they were probed).

- [x] 2026-07-31 FIX IMPLEMENTED + VERIFIED (maintainer go-ahead).
      Root cause CORRECTED per review: _materialize_owned_temp_for_borrow
      stored the alias-marked field projection as owned WITHOUT
      _copy_if_ref_alias (the mark was present; the deep-copy was the
      gap). FIX: _validate_lifted_chain now admits owned-rvalue bases
      with PURE-FIELD chains for SHARED borrows (one line — dropped the
      index-hop requirement, kept &mut refusal); struct constructors are
      HCall here so CTF/base-generality is covered by the same path.
      Materialize base once + AddrOfField, no slot B, no double-register.
      * Matrix (13 e2e rows): CF/NF/CTF/SFU RED→GREEN (base+ASan+
        memcheck); THR throwing-edge GREEN (base+memcheck); IDX/HOI/MIX
        (index-bearing sound)/LIT controls green; MUF/MUI bind-first
        reject; explicit &mut → E_MUT_RVALUE_ARG_BINDING_REQUIRED
        (NOT E_REDUNDANT_ARG_BORROW as the plan speculated — pinned to
        the real, cleaner diagnostic). MRC fixture rewritten
        (autoborrow_owned_rvalue_field_method_drops_once: real String +
        method receiver, single-drop) replacing the invalid "must not
        lift" pin.
      * Regression: 67 existing borrow/autoborrow e2e unaffected; 765
        checker/stage2/borrow-driver green.
      * DRIFTC_VERSION 0.33.93 → 0.33.94; ABI 22 unchanged. history.md
        0.33.94 entry + versioned migration correction (field
        projection unsafe 0.33.91-0.33.93, sound again 0.33.94).
      REMAINING: version-sensitive stamp/version test check; corpus
      enumeration for the new fixtures (universe +13 -1 rename); full
      gate + reviewed promotion (maintainer runs).

- [x] 2026-07-31 REVIEW-ROUND-6 CORRECTIONS:
      * VALUE-CONTROL-FLOW rejection: probed `peek((cond ? mk_a() :
        mk_b()).root)` — it reached the unsafe path and (like the
        whole-value `peek(cond ? a : b)`) double-freed exit 134 on
        CERTIFIED 0.33.90 too, i.e. an independent pre-existing hole.
        Sound lowering needs a deep _visit_expr_HField rework (out of
        scope); REJECTED upstream instead. New checker helper
        `_reject_cfv_rvalue_borrow` (type_checker `_apply_autoborrow_args`,
        both allow_rvalue branches) rejects a shared borrow whose
        materialized rvalue root is HTernary/HMatchExpr,
        `E_PROJECTED_RVALUE_ARG_BINDING_REQUIRED`, scoped to droppable
        pointees (`is_bitcopy` guard) so string-concat / Copy-scalar
        rvalue borrows are untouched. Two new e2e reject rows
        (ternary_field_rejected, ternary_whole_rejected).
      * PROV row REFUTED + REPLACED. Empirically, bare CF and the
        programmatic explicit baseline do NOT emit byte-identical IR:
        the checker-synthesized borrow is injected after stage1 and
        lowered by the MIR rvalue-base lift (`__borrow_tmp`), while the
        explicit `&…` is normalized by stage1 BorrowMaterializeRewriter
        (`__tmp_borrow`) — two routes, both sound. Renamed to
        **A/B lowering-route ownership parity** (maintainer-ratified):
        compile+RUN bare and explicit-baseline of CF and IDX, assert
        identical observable result + scope-end drop timing +
        exactly-one-drop under base/ASan/memcheck (3-pass alloc-track
        sensitivity), plus a structural pin (bare CF = one owning base +
        address-project, no second owned leaf temp; not whole-IR). The
        two `&mut` spellings pinned SEPARATELY (bare → addressable-place
        bind-first; explicit `&mut` → E_MUT_RVALUE_ARG_BINDING_REQUIRED),
        no programmatic-bypass equivalence.
      * STALE lowering comments in hir_to_mir.py corrected (owned CALL
        bases now admit pure-field AND index chains; non-call rvalue
        bases rejected upstream, NOT deep-copied in the reverted
        fallback).
      * history.md: 0.33.91 "surviving spelling's IR is byte-identical"
        marked erratum (semantic parity, not backend-text identity);
        0.33.91 "one-token deletion" migration note gained an inline
        field-projection erratum; 0.33.94 entry extended with the
        value-control-flow rejection + the A/B-parity gate description;
        row count 13 → 15.

- [x] 2026-07-31 REVIEW-ROUND-7 CORRECTIONS:
      * WRONG PREDICATE FIXED (P1, real hole). The CFV guard scoped on
        `is_bitcopy(inner)` — the PROJECTED pointee — but the double-
        registered owner is the ROOT. `peek_int((cond ? a : b).count)`
        (bitcopy `Int` field, `String`-owning root) slipped through and
        would double-free the root's String. Re-keyed on the ROOT type
        via `type_table.has_drop(root_ty)` (the destruction authority),
        in a shared `_cfv_rvalue_borrow_hazard` helper. Bitcopy/no-drop
        roots (`read(cond ? 1 : 2)` at `&Int`) now correctly NOT
        rejected. Added the exact RED regression
        (rvalue_field_proj_ternary_bitcopy_field_rejected) + the
        accept-run control (rvalue_field_proj_ternary_bitcopy_scalar_ok).
      * EXPLICIT-BORROW REDUNDANCY CONTRADICTION FIXED (P1).
        `peek(&(cond ? a : b).root)` reached the W0 redundancy classifier
        and reported E_REDUNDANT_ARG_BORROW with a "pass directly" fix-it
        naming the also-rejected bare form. Stage1 had already
        materialized the ternary into a temp, erasing the CFV kind at
        classify time, so stage1 now stamps a new HBorrow provenance flag
        `materialized_rvalue_cfv` (borrow_materialize peels to the lifted
        deepest base); the classifier (`_cfv_source_borrow_hazard`, gated
        on that flag + `has_drop` of the materialized owner via the
        HPlaceExpr base) emits the bind-first diagnostic instead. New
        fixture rvalue_field_proj_ternary_explicit_rejected; asserted NOT
        to contain E_REDUNDANT_ARG_BORROW.
      * DIAGNOSTIC-CODE PINS: new driver test
        `test_cfv_rvalue_borrow_codes.py` asserts
        E_PROJECTED_RVALUE_ARG_BINDING_REQUIRED (the e2e runner checks
        only message + phase) for bare-field / bare-whole / bitcopy-
        field-droppable-root / explicit shapes, plus the bitcopy-scalar
        accept+run and the not-redundant assertion.
      * CLEANUP: A/B driver's "alloc-track" wording → "memcheck" (it
        does not run DRIFT_ALLOC_TRACK); `_compile_ir` now writes under
        the caller's pytest `tmp_path` (auto-cleaned) instead of leaking
        a `mkdtemp` per call.
      * dmp encoder strips the new `materialized_rvalue_cfv` flag
        alongside its provenance siblings (provisional_dmir_v0).

- [x] 2026-08-01 REVIEW-ROUND-8 CORRECTIONS:
      * GENERIC/TYPEVAR ROOT FAIL-OPEN (P1). `has_drop(TypeVar)` caches
        False (types_core ~1829), so a generic CFV borrow of a
        type-parameter-typed rvalue was ACCEPTED at the pristine
        generic-body check and would double-free when instantiated with a
        droppable type. Empirically confirmed the generic body IS checked
        with `T` as a typevar (a `pick<T>` borrowing `c ? move x : move y`
        rejects for BOTH `String` and `Int` instantiations). Fix:
        fail-closed — `has_typevar(root_ty)` is treated as hazardous in
        both `_cfv_rvalue_borrow_hazard` and `_cfv_source_borrow_hazard`.
        Pinned generic-droppable + generic-bitcopy(fail-closed) in the
        code driver test.
      * W0 TOTALITY didn't know the rejection class. typed_validator now
        fails loud on a surviving `policy_class == "cfv_rvalue_binding"`
        (like redundant / mut_rvalue_binding); unit-table case added.
      * `type_expr(..., used_as_value=False)` in both hazard helpers —
        classification queries must not reapply value-use side effects.
      * peeled-wording bug: the source classifier hardcoded peeled=True,
        so a WHOLE `&(cond ? a : b)` (no projections) got "field/index
        projected" wording. Now derived from `HPlaceExpr.projections`.
      * `policy_class` doc in hir_nodes.py lists `cfv_rvalue_binding`;
        test_borrow_provenance_stripped now sets+asserts the new flag is
        stripped and defaults to False.
