# Progress — reject-redundant-call-borrows

- [x] 2026-07-28 Research sweep (4 parallel passes: auto-borrow mechanics; generics/
      overloads/interfaces/Fn-values; corpus + spec survey; diagnostics/versioning/ABI).
- [x] 2026-07-28 PLAN.md v1 — REJECTED in review (narrowed the rule, voted on the
      narrowed version, unscoped impact numbers).
- [x] 2026-07-28 Compiler probes round 1 (e1-e7) + parser-driven scoped recount
      (≈5,100 ± 60 firing sites; interface dispatch 4 sites; 431 e2e dirs / 415 in
      frozen corpus universe).
- [x] 2026-07-28 PLAN.md v2 — BLOCKED in review: row-19 silent narrowing (iface views),
      thin fn pointers missing, enforcement not centralized, R2 missing &T/&mut T,
      package provenance not implementable, counts/labels/artifacts issues.
- [x] 2026-07-28 Probes round 2 (e8-e10): thin fn-pointer bare call MISCOMPILES (e8d —
      latent defect, adjacent bug to file); bare concrete at &Interface fails (e9);
      &T/&mut T pair bare ambiguous (e10). Mode-erasure overload scan: 3 T-vs-& pairs
      (all known), 0 &-vs-&mut pairs repo-wide. Exact &mut-rvalue count: 2 (both in the
      pinning test).
- [x] 2026-07-28 Published reproducibility artifacts: probes/ (17 programs + results
      README), recount/ (site dataset results3.json, method README, clean R2 scan
      script, collection scripts).
- [x] 2026-07-28 PLAN.md v3 published: W0 centralized policy, D7 (interface views) and
      D8 (fn pointers) as explicit policy forks, D9 package policy (provenance stripped
      at emission; old packages valid by decode-default), R2 mode-erasure form, D1b
      mutable-rvalue choice, totals relabeled as estimates, full-suite responsibility
      assigned to implementer. Recommendation: conditional up-vote on D1-D9.
- [x] 2026-07-29 D3 RESOLVED by user: no compiler/path exemption for issues/; archival
      snapshots preserved verbatim (192 sites, never compiled → sweep skips them);
      active regression-test inputs migrate. Verified: 3 issues/ files are live inputs
      via test_drift_query_slice12_ices.py:48-50; only repro_single_file.drift fires
      (2 sites → migrate). Standing rule recorded: archived source promoted to active
      input must first be updated to current syntax. PLAN.md D3/§3/§7/§12 updated.
- [x] 2026-07-29 Reviewer bookkeeping fixes applied: §12 migration total ≈4,900 (after
      D3 archival exclusion); interface-dispatch impact restated as census 4 / 1 active
      migration site (repro_single_file.drift:211), W2 kept as language-correctness
      work. R-7 confirmed YES by probe e11 (Fn(T) typevar fn type compiles, exit 42);
      D8 rewritten accordingly — no total-provenance variant exists; e8d miscompile
      routed to the separate LANGUAGE_BUG regression-first process, W-FP treats it as a
      precondition only.
- [x] 2026-07-29 D8 RESOLVED → (b) (fn-pointer invokes exempt; provenance-honest
      after R-7=yes) and remaining review corrections applied (validator = policy-
      classification invariant; R2 = non-receiver mode erasure; e8d routed to its own
      LANGUAGE_BUG slice — since fixed as 0.33.91, committed 453a2f52).
- [x] 2026-07-29 FULL DECISION SLATE RATIFIED by user: D1 in-scope, D1b(b), D2
      include, D4 mode-erasure, D6 alias transparency, D7(a), D9 source-only. PLAN.md
      updated (decisions marked resolved; matrix rows 7/16; §9 row-11 removal +
      policy-classification wording; §12 rewritten — D5 is the single remaining
      gate).
- [x] 2026-07-29 Plan-consistency cleanup per review: header → "policy approved;
      awaiting D5"; release-ordering statement consolidated into §7 (bug fix landed
      first internally as 0.33.91/453a2f52, rule ships in the SAME 0.33.91 release —
      §12 now defers to §7); W7 retains DRIFTC_VERSION 0.33.91 / ABI 22 and specifies
      ONE combined history entry + ONE combined release-notes file (extend existing,
      no second announcement); W4 fixed to resolved D1b(b); §9 fn-pointer bullet →
      acceptance pins for BOTH spellings; O5/R-6 conditional wording removed.
- [x] 2026-07-29 D5-test-changes.md PUBLISHED (exact per-test dispositions, source-
      verified): 0 e2e retirements (15 repurposed in place), 2 Python retirements
      (selector tests → replaced by R2 pins), 19 negative fixtures in scope (not 26 —
      survey corrected; 7 additions, 4 capture-list fixtures reclassified
      UNAFFECTED), corpus promotion = 426 deltas + 0 removals + exactly 20
      enumerated additions (13 failed + 7 compiled_ok; universe 1,269 → 1,289), 0
      expected partition flips. Two soundness gates flagged (B19 same-stmt conflict
      via synthesized borrows; C4 capture-overlap via synthesized borrows). Sweep-
      exclusion list + W3 six at-risk messages + diagnostic-wording unification
      recorded. Three open reviewer choices (A3, A7, B15/B16 handling).
- [x] 2026-07-29 D5 round-1 review: existing dispositions + 2 Python retirements
      APPROVED; A3 repurpose-to-rvalue, A7 repurpose (keep reborrow coverage),
      B15/B16 finalize-at-W3 under user-facing + present-before-change constraints.
      Five corrections applied: (1) D1b(b) recorded as the rule's SOLE
      non-redundancy rejection — new MUT_RVALUE_BINDING classification +
      E_MUT_RVALUE_ARG_BINDING_REQUIRED diagnostic (never E_REDUNDANT_ARG_BORROW /
      "pass directly"); PLAN §1/W0/W4/row-7/§9/§12 updated. (2) Rvalue A/B gate
      baseline = programmatic-HIR driver test (source_written=False), since the
      explicit-baseline source is itself rejected post-rule; e2e keeps only
      rvalue_arg_temp_drop_bare. (3) Additions enumerated exactly: 20 named
      fixtures (13 failed + 7 compiled_ok) → universe 1,269 → 1,289 exact.
      (4) Blanket diagnostic unification withdrawn — distinct failures keep
      distinct messages; only the two equivalent D1b spellings share a remedy
      phrase. (5) Summary count fixed: 13 positive repurposes.
- [x] 2026-07-29 D5 round-2 revision applied: A/B baseline constraint recorded (the
      programmatic-HIR half MUST run full HIR→MIR→LLVM and execute under
      memcheck/ASAN; borrow-checker-only style insufficient); R2 fixture #13
      expanded to all four ratified shapes as four uniquely-named overload sets
      (defeats subset-matching aliasing); stale approximate corpus block removed
      (exact 426+20+0 / 1,269→1,289 authoritative); A3 stale alternative removed;
      PLAN §7 sweep wording fixed (one-token EXCEPT the two D1b mutable-rvalue
      sites, which take the binding/repurpose treatment).
- [x] 2026-07-29 D5 APPROVED (final revision accepted: 4-shape R2 fixture, exact
      corpus block, full-pipeline A/B constraint). IMPLEMENTATION STARTED (user
      directive; current runs = baseline evidence, final cert on combined tree).
- [x] 2026-07-29 W1 CORE LANDED: HBorrow.source_written (ast_to_hir only) +
      materialized_rvalue (borrow_materialize __tmp_borrow lift) + policy_class;
      W0 policy in _apply_autoborrow_args (deletion-equivalence redundancy test;
      E_REDUNDANT_ARG_BORROW w/ span-sliced operand + &-style type rendering;
      E_MUT_RVALUE_ARG_BINDING_REQUIRED for D1b; coercion class falls through);
      declared_ref_mask from TEMPLATE signatures wired at free-fn + method sites
      (receiver slot forced exempt; rotation mirrored); fn-pointer binding path
      stamps EXEMPT (D8(b)).
- [x] 2026-07-29 W2 LANDED: interface dispatch wired to the same engine
      (InterfaceParamSchema shape mask: evaluated-REF minus bare param_index refs).
      Verified: bare iface arg auto-borrows; explicit rejected with schema param
      name; &Concrete at &Interface (coercion) unaffected per D7(a).
- [x] 2026-07-29 INFERENCE MADE AUTO-BORROW-AWARE (the W3-class root problem, hit
      immediately by the stdlib sweep): (1) _infer peels declared &X on top-level
      arg constraints when the arg is bare (+ &mut→& reborrow pair peel) — fixed
      wait/wait_until-class method generics; (2) _borrow_infer_arg_types prefers
      plain auto-borrow over Borrow-trait coercion for typevar-bearing inners —
      fixed _publish_or_drop/Arc-class free-fn generics.
- [x] 2026-07-29 SPECULATIVE-RESOLUTION SELF-CONFLICT SOLVED: probe/UFCS clones
      share arg children; synthesized borrows now carry _ab_origin_arg (original
      arg node id) and the same-statement borrow window keys them by
      ("ab-origin", arg, is_mut) — probe clones dedupe, genuine f(a, a) conflicts
      preserved. Per-call-slot synth_cache added at all four apply sites.
- [x] 2026-07-29 STDLIB SWEPT + GREEN: compiler-span-driven sweeper
      (work/reject-redundant-call-borrows/sweep/rewrite_redundant_borrows.py,
      --json line:col token deletion, iterate-to-fixpoint) applied 498 edits across
      24 stdlib files, 0 skips; W6 rename resolved as DELETION of the dead
      _encode_node by-value wrapper (callers auto-borrow into the &-overload).
      Full stdlib compiles; hello-world runs; fnptr regression 11/11 + checker
      suite 75/75 green.
- [x] 2026-07-29 W5 LANDED: IIFE lambda path wired (declared lambda param types now
      win over arg-derived types for the expected Fn shape; mask from resolved lambda
      params). Bare `(…)(x)` at `&mut Int` runs with caller-visible mutation;
      explicit rejected with the lambda param name.
- [x] 2026-07-29 W3 LANDED: std.mem strict path pre-normalizes via a per-intrinsic
      formal-mode table (15 intrinsics) + the same W0 engine BEFORE the ad-hoc
      element-type inference — bare mem.replace(x,5)→15, mem.swap(a,b)→43 (mutation
      visible), explicit rejected; swap's structural &mut check satisfied by the
      synthesized nodes (type-normalized, 0.31.81 precedent). Stdlib re-swept: +212
      then +3 edits (713 total, 0 skips).
- [x] 2026-07-29 ASSOC PATH LANDED + LATENT MISCOMPILE FOUND/FIXED: bare
      `Type::assoc_fn(s)` at a declared &String formal previously typechecked and
      emitted ill-typed IR (e8d-class, assoc flavor — %DriftString where ptr
      expected). W0 wiring at the qualified-static record point (template mask via
      signatures_by_id; UFCS `self`-slot forced exempt). Bare now runs; explicit
      rejected.
- [x] 2026-07-29 R2/W6 LANDED: find_param_mode_overload_conflicts over the
      CallableRegistry (concrete decls only; receiver slot excluded from erasure;
      raw-TypeExpr fallback unnecessary after registry rewire) →
      E_OVERLOAD_PARAM_MODE_ONLY_DIFF at definition site, wired into the CLI
      pipeline next to find_impl_method_conflicts. Verified: free T/&T banned,
      method &/&mut banned, arity overloads legal, receiver-mode-only overloads
      legal. stdlib _encode_node by-value wrapper deleted (W6 rename resolved as
      deletion — wrapper was dead post-sweep).
- [x] 2026-07-29 D9 LANDED: _to_jsonable never encodes HBorrow.source_written/
      policy_class (decoders default False/None → all pre-rule packages stay valid,
      no payload bump). D2 string-builtins + Array.extend wired (explicit rejected,
      bare runs). W0 TOTALITY VALIDATOR landed in typed_validator (every source-
      written borrow arg classified; REDUNDANT-accepted = internal error) + fallback
      EXEMPT stamping at _record_call_info for out-of-scope families (fn-value
      invokes, trait-qualified statics) — tripwire drove wiring completeness and
      caught the unclassified paths during bring-up.
- [x] 2026-07-29 D5 MANUAL DISPOSITIONS: A7 ref-value repurpose (verifies bare
      &mut-typed values reborrow at &T formals); A12-A15 explicit arms deleted
      (concrete-beats-generic pin annotated as R2's justification); om_* generator
      emitters bared (22 strings) + regenerated, freshness check green; C4 lambda-
      overlap 4/4 bare — SOUNDNESS GATE PASSES (synthesized borrows reach the
      capture-overlap analysis); B19 GATE PASSES (bare takes(x,x) still conflicts);
      C3 test_borrow_rvalue_move_args fully reworked 9/9 (bare materialization
      predicate verified; D1b binding exemplars pinned; docstring records the
      reversal); C1 selector tests RETIRED w/ note (replacement = R2 fixture),
      5 mechanical sites bared, 6/6; D7-aware interface-coercion edits (3 swept,
      coercion borrows kept); test_tmp_borrow_callback_collision bared + docstring.
- [x] 2026-07-29 NEW FIXTURES (7 of 20, the positives): autoborrow_bare_{assoc_fn,
      interface_arg,mem_intrinsics,lambda_iife,alias_param,builtin_extend} +
      rvalue_arg_temp_drop_bare — ALL GREEN first run (D6 alias pin needed zero
      extra compiler work; R-2 bare-half drop-timing gate holds: mid=0/after=1).
- [ ] IN FLIGHT: corpus_sweep.py (parallel, e2e-driven, iterating) + embedded_sweep
      (concat-chain aware) running in background.
- [x] 2026-07-29 A/B GATE COMPLETE (test_rvalue_arg_temp_drop_ab.py 3/3):
      programmatic-HIR baseline (parse explicit shape → clear source_written →
      FULL pipeline HIR→MIR→LLVM→link→EXECUTE) runs plain + under valgrind + under
      ASan/UBSan with pinned drop parity (mid=0/after=1); the A-half proves the
      same source shape is rejected. D9 package pin
      (test_borrow_provenance_stripped.py 3/3): encode omits the fields entirely;
      decode defaults False/None; legacy payloads tolerated. W0 validator unit pins
      (test_w0_policy_totality_validator.py 5/5): unclassified → internal error,
      REDUNDANT-accepted → internal error, exempt/coercion/mut_rvalue_binding pass,
      synthesized borrows and ctor targets outside the rule.
- [x] 2026-07-29 NEW FIXTURES 9/20: + overload_param_mode_only_diff_rejected (all
      FOUR ratified shapes as uniquely named sets, four distinct definition-site
      diagnostics) and mut_rvalue_arg_binding_required_rejected (the D1b sole
      non-redundancy rejection). Both verified.
- [x] 2026-07-29 DOCS (core): spec §3.5 (rvalue-borrow divergence reconciled:
      binding-position materialization documented), §3.6 REWRITTEN (bare is the
      only spelling; redundancy criterion; coercion-borrow + D1b exceptions;
      fn-pointer/generic/ctor scope; R2), §1.3 predict-the-verdict drill both
      occurrences now bare with the declaration-spells-the-borrow note, §3.2
      borrow-trait table updated (auto-borrow precedence for generic inners);
      effective-drift auto-borrow section rewritten ("never & as decoration",
      D1b asymmetry, surviving-& contexts, R2 guidance).
- [ ] IN FLIGHT (background): corpus_sweep (e2e-driven, iterating), embedded batch
      (299 py files, 8-way), examples/tools sweep.
- [x] 2026-07-29 IMPLEMENTATION REVIEW ROUND 1 (5 findings) RESOLVED — compiler
      status had been over-claimed and is now re-verified strictly:
      (1) blanket EXEMPT fallback REMOVED from _record_call_info; D7 coercions now
      stamped at the borrowed-iface-view branch (which had returned before
      classification); require-bound trait dispatch (`body.call` on require-F
      receivers) and trait-qualified statics (`Trait::method(recv,…)`) wired as
      OWNING paths with masks from the trait decl's param type_exprs — gaining
      full auto-borrow parity, not just stamps; apply-loop default-exempt narrowed
      to mask-known-False slots (mask-None survivors now FAIL validation);
      validator additionally rejects surviving MUT_RVALUE_BINDING (unit pin
      added). Stdlib compiles with ZERO errors under the strict validator.
      (2) D2 formals now DECLARATION-derived (extend: &Array<recv-elem>;
      string builtins: fixed &String) — wrong-typed explicit borrows fall through
      to type mismatch, never misread as redundant. Found+fixed pre-existing
      LANGUAGE_BUG: extend accepted a mismatched source element type and
      EXECUTED (String payloads into Array<Int>, exit 3) — regression-first
      (array_extend_elem_mismatch_rejected confirmed failing pre-fix).
      (3) D9: materialized_rvalue also stripped; the approved
      encode→decode→RECOMPILE gate added (production codec on a parsed pre-rule
      body; recompile through the full pipeline; provenance-leak assert) — 4/4.
      (4) A/B baseline made equivalent-modulo-ONE-borrow (inner call bare;
      cleared == 1 asserted; clear-walk restricted to main module) — 3/3.
      (5) Assoc miscompile documented as LANGUAGE_BUG with regression-first
      evidence + subsystem + mandatory refactor_triggers scan (no trigger
      matches; W0 validator is the structural close-out for the family) —
      LANGUAGE_BUGS-found-during-implementation.md. Assoc explicit-rejection
      fixture queued with the negative batch (post-corpus-sweep).
      Review-response suites: 115/115. Examples/tools swept (197 edits/16 files).
- [x] 2026-07-29 CORPUS SWEEP COMPLETE: 2,798 edits / 6 iterations / 0 skips
      (420 e2e files + 28 stdlib files). All 12 remaining negatives created and
      verified firing (projection fixture renders 4 operand texts); 22 new
      fixtures total on disk.
- [x] 2026-07-29 IMPLEMENTATION REVIEW ROUND 2 (3 findings) RESOLVED — see
      REVIEW-ROUND-2-REPORT.md: (1) D6-on-trait-paths fixed via centralized
      declared_ref_formal (three root causes: literal &-token checks; trait-impl
      DIRECT templates hiding the trait's generic contract; string type_params
      emptying the generic-name set) + 5 pins incl. both alias directions and the
      Fn1 generic exemption; (2) D5 arithmetic reconciled [round-2 figures,
      superseded — see the post-round-3 correction below: 23 additions
      (15 failed + 8 compiled_ok), universe 1,269 → 1,292], counting slip
      explained, all 13 approved negatives verified on disk; (3) extend explicit
      wrong-type driver assertions added incl. E_REDUNDANT-absence.
      Targeted verification green; BIG SUITES DELIBERATELY HELD for review per
      user direction (report-before-suites).
- [x] 2026-07-29 ROUND 3 (2 blockers + clerical) RESOLVED: (1) W0 formal
      detection ACTUALLY centralized — build_declared_ref_mask/
      declared_ref_formal now the single constructor for free, method, assoc-
      DIRECT, trait-qualified, require-bound, interface, and lambda sites (the
      D2 fixed-signature builtins document their hardcoded-declaration masks);
      table-driven classifier unit tests added (concrete refs, D6 aliases,
      &-rooted generics, bare generics at refs, param_index, receiver
      exclusion). (2) The "impl-lookup gap" was NOT a contract — classified as
      LANGUAGE_BUG #3 via isolating probes (Taker<Int> worked; Taker<&String>
      failed): traits/world.py normalize_type_key stamped the caller's module
      onto module-less Ref/RefMut/fn TypeKeys, diverging call-side obligations
      from impl registrations. Regression-first
      (trait_qualified_ref_type_arg_impl_lookup confirmed failing), fixed at
      normalize, triggers re-scanned (no match), stronger user-trait pin (d)
      restored. (3) D5 consolidated to one authoritative enumeration —
      post-round-3 review correction: exactly 23 additions (15 failed +
      8 compiled_ok, trait_qualified_ref_type_arg_impl_lookup enumerated as
      compiled_ok #23, no "non-enumerated" tier) → universe 1,269 → 1,292;
      superseded-appendix flow retired. Round-3 targeted verification:
      190/190 (16-way).
- [x] 2026-07-29 MIGRATION TAIL COMPLETE while gates run: A3 converted to the
      true-rvalue form (exit 3, matches its name at last); effective-drift
      scattered samples swept (55 fenced-block call-arg edits + 5 inline prose
      notations + 4 residuals; Share/captures/signature notation preserved);
      grammar-doc semantic-rule note added at the UnaryExpr production; SCR
      addendum recording the reversal of "remain legal; just redundant";
      COMBINED 0.33.91 history entry written (rule + 3 LANGUAGE_BUGs + MIGRATION
      section + versioning, fn-pointer half nested); release-notes file extended
      into the combined entry.
- [x] 2026-07-29 CORPUS AUDIT REPORT-MODE RESULT: 16 UNEXPECTED compiled_ok→
      failed flips (D5 predicted zero) in four classes; all four root-caused
      and fixed:
      * CLASS 4 — projection-chain auto-borrow parity (3 fixtures:
        borrow_chained_ref_projection_noncopy, ref_array_jsonnode_usage_matrix,
        effective_drift_emitter_example). Bare `f(chain()[i])` / `f(*f())` at a
        declared-ref formal hit two pre-existing explicit-borrow-only layers:
        (a) the shallow checker's HBorrow arm didn't suppress the indexed-
        element Copy check (borrowing an element is not copying it; source
        borrows were always rewritten by borrow_materialize before this arm
        saw them, synthesized borrows are not) — suppression added mirroring
        the HField-through-index arm; (b) MIR `_validate_lifted_chain` /
        `_lift_rvalue_ref_base_for_borrow` only supported HField hops — added
        "index" steps (LoadRef array + AddrOfArrayElem, mirroring
        _lower_addr_of_place) and explicit-deref-at-base (`&*f()` = address
        no-op).
      * CLASS 2 — for_in_byvalue family (7 fixtures): STALE-ARG-TYPES
        idempotency defects in `_apply_autoborrow_args` exposed by the new
        assoc/trait-qualified wiring: re-resolution passes hand stale
        arg_types against the already-mutated args list; the `&T→T` symmetric
        coercion then wrapped a SECOND deref ("deref requires a reference
        value"), and the `&&T→&T` nested-deref coercion dereffed INTO the
        pointee (non-Copy → "cannot copy Array<JsonNode>"). Both branches now
        skip when the slot's node already types to the formal (structural
        check; node markers don't survive normalize rebuilds).
      * CLASS 3 — E-INFER-CONFLICT on `conc.lock` (5 fixtures incl.
        effective_drift_emitter_example): the round-earlier typevar-inner
        auto-borrow preference in `_borrow_infer_arg_types` was too broad —
        `lock<T>(m: &Mutex<T>)` with an Arc<Mutex<Counter>> arg needs the
        Borrow-TRAIT view. Preference narrowed to head-match: bare-typevar
        inner always; structured inner only when the arg's head constructor
        matches (MutexGuard<T>←MutexGuard<C> yes; Mutex<T>←Arc<...> no).
      * CLASS 1 — om_local_assign_token / om_return_value_token: two emitted-
        template sites in `__ownership_matrix__/_gen.py` missed by the om
        regen (`make_token(&mut dst_sess)`, `produce_<shape>(&mut sess)`);
        generator fixed, all 51 om_* fixtures regenerated; full om sweep
        compile+run+exit-code verified (51/51).
      Verification after all four fixes: all 16 flipped fixtures compile and
      run with expected exits; feature test batch 101/101 (16-way).
- [x] 2026-07-29 PERF GATE TRIAGED — NO CODE CHANGE: the three
      test_std_json_parse_perf_gate rows all pass when the protocol is
      honoured. The morning failures (allocation counts + scaling) cleared
      after the oracle-fragment sweep; the residual bands failure
      (tiny_arr absΔ, malformed ratio+absΔ) was CPU contention — the gate ran
      in the background while a 16-way pytest batch ran in the foreground,
      exactly the contention mode the module docstring forbids. Clean serial
      rerun on the idle box: 1 passed in 142s, all shapes inside bands.
- [x] 2026-07-30 GATE CHAIN (snapshot run) — phase 1 perf GREEN (3 passed,
      serial idle). Phases 2-4 ran CONCURRENTLY with the round-4 correction
      edits (torn-tree reads) and are void as evidence, EXCEPT one real
      finding: lang/tests/test_tmp_root_compliance.py flagged the work/ sweep
      tools' bare tempfile calls + a /tmp docstring example — annotated with
      drift-tmp-root-audit allows, 3/3 green.
- [x] 2026-07-30 ROUND-4 REVIEW CORRECTIONS RESOLVED (see
      REVIEW-ROUND-4-REPORT.md for the flip-fix narrative):
      (1) borrow_chained_ref_projection_noncopy restored to DUAL coverage —
      SECTION A explicit borrows in non-argument bindings (stage1
      _split_lift_place_chain, exits 11-15), SECTION B same chains bare in
      argument position (checker-synthesized HBorrow → MIR lifted chain,
      exits 1-5), SECTION C owned rvalue base + index bare (exit 21);
      narrative rewritten; compiles+runs ok/0.
      (2) HIndex receiver contract RECONCILED by COMPLETING parity (the
      cited comments' own "when MIR coverage lands, widen" mandate):
      _ultimate_base_is_rvalue_call widened to HIndex + deref-at-base;
      MIR _validate_lifted_chain admits OWNED bases for index-bearing
      shared chains (materialized into a drop-registered temp; pure-field
      owned chains keep the fallback pinned by
      autoborrow_owned_rvalue_field_method_unchanged — verified green);
      receiver e2e autoborrow_method_receiver_through_ref_rvalue_chain
      extended with n4 (ref-base index), n5 (deref-at-base), n6 (OWNED-base
      index) compile+run pins + expected.json description updated;
      test_method_receiver_autoborrow_through_ref_rvalue_hindex_rejects
      FLIPPED to _accepts (as its own docstring mandated); the
      _visit_expr_HBorrow narrative + docstrings updated to the real
      contract. One more unswept embedded site found and bared
      (test_autoborrow_receiver_place get_inner_ref(&inner)).
      (3) D5 single authoritative enumeration: 23 additions (15 failed +
      8 compiled_ok, trait_qualified pin = compiled_ok #23), all 22/1,291
      statements corrected or supersession-annotated; LANGUAGE_BUGS ledger
      intro fixed ("The first two" are the e8d family; #3 is separate).
      (4) NEW driver pins lang/tests/driver/test_autoborrow_reresolution_pins
      .py (4 rows): both stale-arg-types idempotency branches (minimized
      for-in Copy-iterable + &&Array shapes) and BOTH Borrow-inference
      head-selection directions (same-head Carrier<T> plain auto-borrow;
      mismatched-head conc.lock(Arc<Mutex<Int>>) Borrow-trait view), each
      compile+RUN, with designated e2e carriers named in docstrings.
      Final-tree feature batch: 120/120 (16-way).
- [ ] FINAL GATE CHAIN queued on the frozen post-correction tree (tree must
      stay untouched while it runs): perf serial → corpus audit report-mode
      (the FINAL promotion numbers; expect universe 1,269→1,292, the 23
      D5-enumerated additions, zero unexpected flips) → full memcheck lane →
      full ASAN lane. Then: announcement updated to final-tree certification
      (replacing the zero-delta fn-pointer checkpoint), final report +
      promotion package; user runs run-all-tests.sh.
- [x] 2026-07-30 ROUND-5 REVIEW CORRECTIONS RESOLVED (see
      REVIEW-ROUND-5-REPORT.md): (1) nested-HIndex borrow LANGUAGE_BUG fixed
      regression-first — failing repro peek(make_matrix()[0][0]) confirmed
      ("cannot copy Array" on the INNER index), then the shallow checker's
      suppression widened to EVERY HIndex on the borrow-subject spine
      (HIndex/HField hops only; never inside [...] expressions); MIR needed
      nothing; pinned as borrow_chained SECTION D (exits 22-23);
      trigger scan: no match. (2) same-head inference pin made LOAD-BEARING:
      inspect<T>(a: &Arc<T>) with bare Arc<Int> (competing Borrow<T> view);
      proven by disabling the preference → row fails → restored → passes.
      (3) owned-base &mut negative companion landed
      (test_method_receiver_mut_through_rvalue_index_rejects_cleanly, both
      owned and shared-ref flavors, ICE-absence asserted); _peek(mk()[1])
      ICE determined TRANSIENT (certified-0.33.91 probe rejects cleanly) —
      recorded in the ledger, no LANGUAGE_BUG #4. (4) doc close-out:
      expected.json 4-section description, _ultimate_base_is_rvalue_call
      stage1-claim corrected to the MIR twin, all four hir_to_mir regions,
      R4-report statuses, D5 driver list. Round-5 compiler delta = ONE hunk
      (spine-walk suppression).
- [x] 2026-07-30 Round-5 final-tree feature batch: 121/121 (17 files, 16-way;
      includes the reworked Arc same-head pin and the new negative companion).
      Awaiting snapshot-chain completion, then the FINAL chain launches on the
      frozen tree (no edits until it reports).
- [x] 2026-07-30 CLOSE-OUT items resolved: (1) nested-HIndex promoted to
      numbered LANGUAGE_BUG #4 in the ledger (repro, failing regression,
      subsystem, fix, trigger scan); doc/history.md updated three→FOUR
      rule-tripwire bugs (+ the head-match inference sentence corrected);
      announcement tripwire sentence extended with the spurious-rejection
      fourth bug. (2) mutable-receiver negative tightened: asserts a
      diagnostic on EACH distinct source line (owned-base and shared-ref-base
      call sites separately; duplicate diags from one call can no longer
      mask the other path) — 1/1 green.
- [x] 2026-07-30 LANGUAGE_BUG #5 (regression-first, per instruction): the
      FIRST-EVER driver-shard lane run failed 36 tests / 74
      E_REDUNDANT_ARG_BORROW diags, all reducing to one classifier defect —
      declared_ref_formal's param_index exemption was gated on name is None,
      but builtin Callback* schemas carry name="" on param refs, so
      Callback1<&mut Scope,R>.call(&mut s) (LEGAL row-10 spelling; live at
      ffi.drift:428 with_cstring_scope) read as a declared &mut formal.
      Speculative W2-site hunk REVERTED per review (over-exempting + didn't
      fix); real fix: param_index is AUTHORITATIVE unconditionally (review-
      corrected from a falsey-name check). Regression pins written FIRST and
      confirmed failing 4/4 (test_callback_iface_generic_ref_param_exemption
      .py: direct + require-bound boxed callbacks × shared/mut), then 4/4
      green; classifier unit rows for all three producer shapes (None, "",
      residual name); trigger scan: no match. Oracle fragment hash re-pinned
      to the review-approved value (18-borrow sweep; hash verified). Ledger/
      history (four→FIVE)/announcement/D5 inventory updated. 14-file rerun
      of the failed shard in flight.
- [x] 2026-07-30 DRIVER-SHARD FALLOUT FULLY RESOLVED (36 tests / 74 diags →
      157/157 on the affected 18 files):
      * LANGUAGE_BUG #5 classifier fix (param_index authoritative) — see the
        dedicated entry above; resolved the callback/require-bound class.
      * TRAIT-NAME-FALLBACK completion (implementation fix to this branch's
        own round-3 wiring, NOT ledgered — flagged for reviewer concurrence):
        in the compile_stubbed_funcs harness pipeline, DIRECT trait-impl
        resolutions carry a module-less registry TraitKey; the record
        point's name-based fallback required exactly one match and the
        harness registers one trait def under several per-world keys →
        ambiguous → NO mask → the totality validator fired (BY DESIGN —
        "internal: unclassified source-written borrow argument survived")
        on stdlib array.drift:366 Comparable::cmp. Fix: dedup matches by
        trait-def identity, then prefer the DIRECT impl fn's own module for
        still-ambiguous module-less keys. Validator success story: the
        tripwire caught the uncovered pipeline exactly as specified.
      * EMBEDDED MIGRATION, 33 genuinely-redundant sites hand-bared across
        16 driver-test files + the TWO D3-designated active sites in
        issues/mir-missing-binding-id-conditional-move-ice/
        repro_single_file.drift (:183 &fpfx, :211 &t — the D3 ruling's
        exact pair, missed by the original sweep because the embedded
        sweeper cannot compile multi-file/package test setups standalone).
        All edits diagnostic-guided, iterated to fixpoint (4 rounds); one
        docstring narrative hit was reverted to preserve the historical
        pre-fix spelling (typed_catch test).
      * ffi.drift:428 was NOT swept — it is the LEGAL row-10 spelling and
        stayed intact; the b5 failure was bug #5.
      Full normal-mode driver suite launched (per instruction: complete
      suite before restarting certification).
- [x] 2026-07-30 FINAL CERTIFICATION (K's gates) COMPLETE on the frozen tree:
      (1) perf-protocols serial/idle: 3 passed, all shapes comfortably inside
      bands (tiny_arr absD 48.6ns vs 75 band; malformed ratio 1.34 vs 1.5).
      (2) corpus promotion audit (build/tmp/corpus-promotion-final2): ZERO
      unexpected flips (only-baseline EMPTY on both partitions), 0 removals,
      additions = EXACTLY the 23 D5-enumerated fixtures (15 failed +
      8 compiled_ok incl. trait_qualified #23), universe 1,269 -> 1,292.
      (3) complete normal driver suite: 2220 passed / 10 skipped.
      Announcement updated to final-tree certification (fn-pointer zero-delta
      checkpoint replaced). Sanitizer lanes deferred to run-all-tests.sh per
      user instruction. GREEN LIGHT given for run-all-tests.sh; commits and
      corpus-baseline promotion remain with the user.
