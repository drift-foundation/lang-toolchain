# D5 — exact test-change list for approval

Status: REVISED after review round 1 — AWAITING FINAL APPROVAL (the single
remaining gate, PLAN.md §12). Round-1 outcome: existing fixture dispositions and
the two Python-test retirements APPROVED; decisions recorded — **A3: repurpose to
the true-rvalue test; A7: repurpose (preserves the unique call-boundary reborrow
coverage); B15/B16: messages may be finalized during W3 but must remain
user-facing type-conflict diagnostics and be presented for review before any
expectation change.** Round-1 corrections applied below: D1b(b) recorded as the
rule's sole non-redundancy rejection with its own classification/diagnostic
(PLAN.md §1); the rvalue A/B gate's baseline mechanism made post-rule-executable;
additions enumerated exactly (20 named fixtures + partitions); blanket diagnostic
unification withdrawn; summary count corrected.

Every disposition below was produced by reading the fixture/test source,
cross-checked against `recount/results3.json` and the frozen corpus manifest.
Mechanical `&`-deletions are pre-approved wholesale and NOT itemized; this file
lists only tests whose purpose or expectations change, plus the exact
corpus-promotion scope. Decision inputs resolved: D1 (rvalues in scope), D1b(b)
(sole exception — `MUT_RVALUE_BINDING` class, `E_MUT_RVALUE_ARG_BINDING_REQUIRED`,
never `E_REDUNDANT_ARG_BORROW`), D2 (builtins INCLUDED — include-numbers primary),
D7(a), D8(b).

Two harness facts drive most dispositions (both verified in source):
1. `e2e/runner.py:752-774` matches expected diagnostics as a **subset** — extra
   diagnostics never fail a fixture; only *suppression* of the pinned one does.
2. `driftc.py:5898` — **borrowcheck is skipped entirely when typecheck has any
   error.** So every negative fixture pinning a borrowcheck-phase diagnostic that
   would gain a typecheck `E_REDUNDANT_ARG_BORROW` fails, and must be rewritten
   bare to keep its pin.

## Summary for approvers

- e2e fixtures: **0 retirements** — **13 positive repurposes** (A1-A7, A10-A15;
  A8/A9 are unaffected-in-purpose), plus the B-group bare rewrites, everything
  else mechanical.
- Python tests: **2 retirements** (the two `&`-as-selector tests, replaced by the
  R2 fixture below), ~14 repurposed/rewritten, the rest mechanical or unaffected.
- Corpus promotion (D2=include): **426 content-hash deltas** (392 `compiled_ok` +
  34 `failed`, incl. 13 regenerated `om_*` dirs) **+ exactly 20 additions
  (enumerated in §D) + 0 removals**; 16 more edited dirs are corpus-`excluded`
  (no manifest impact). **Universe 1,269 → 1,289 exactly.** Expected partition
  flips: **0**. One full reviewed promotion.
- Two **soundness-shaped verification gates** (B19, C4): if their bare forms stop
  producing their diagnostics, that is a compiler defect to fix in-slice, never an
  expectation edit.
- Three files/sites are **excluded from the textual sweep** (D8(b)/D7(a)/generic
  formals) — the sweep must be policy-aware, not textual (flags 3 below).

---

## A. e2e positive fixtures whose purpose is the explicit-borrow spelling

| # | Fixture | Pins today | Disposition |
|---|---|---|---|
| A1 | `borrow_string_param` | explicit `&` of a `val String` at `&String` param, exit 5 | REPURPOSE → bare `byte_len_ref(s)`; becomes the canonical shared-lvalue auto-borrow positive. Exit unchanged. |
| A2 | `borrow_mut_int` | explicit `&mut` of a `var` at `&mut Int`, exit 42 | REPURPOSE → bare `inc(x)`; canonical mutable-lvalue positive. |
| A3 | `borrow_rvalue_string_param` | **misnamed** — byte-identical to A1, no rvalue borrow at all | REPURPOSE → true rvalue bare form `byte_len_ref("abc")` (finally matches its name; pins the D1 motivating case). Approved. |
| A4 | `borrow_struct_field_param` | explicit `&mut` struct local at `&mut Point`; body's `&mut (*p).x` non-argument | REPURPOSE → bare `inc_x(p)`; body untouched. |
| A5 | `borrow_rvalue_shared_call_arg_ok` | borrow of computed rvalue `id(&(1 + 2))` | REPURPOSE → bare `id(1 + 2)` (computed-rvalue flavour, distinct from A3). |
| A6 | `array_borrow_tmp_drop` | **drop timing** of the aggregate `__borrow_tmp` for an rvalue borrow at a call arg | REPURPOSE → bare `array_sum(make_array())` and **promote to the R-2 A/B memcheck gate** (rvalue-sweep precondition). Highest-value fixture in this group — the only in-tree pin on aggregate-temp drop for the rvalue path. |
| A7 | `reborrow_mut_to_shared_call_site` | six cases, all "pass `&mut X` where `&X` declared" — exactly matrix row 3, which becomes an error; naive sweep collapses them into plain auto-borrow | REPURPOSE → ref-value form per case: `val m = &mut f; takes_shared_ref(m);` — keeps the `&mut T → &T` reborrow-at-call-boundary mechanism under test via the reference-typed-value exemption. **Verification required**: bare `&mut T`-typed value still coerces at a `&T` formal. |
| A8 | `reborrow_mut_to_shared_callback` | `cb.call(&mut f, 5)` on `Callback2<&Foo,Int,Int>` | **UNAFFECTED** (generic `A` formal — matrix row 10) and **must be excluded from the sweep**. |
| A9 | `mem_replace_helper_param_ref` | `mem.replace(slot, …)` already bare; helper-param acceptance | UNAFFECTED in purpose; 2 mechanical call-site edits only. |
| A10 | `std_mem_swap_replace` | `mem.swap`/`mem.replace` happy path, exit 4 | REPURPOSE → bare; seed for the §9 W3 bare-form matrix over all 11 intrinsics. |
| A11 | `swap_basic` | `mem.swap(&mut a,&mut b)`, exit 21 | REPURPOSE → bare `mem.swap(a, b)`. Corpus-excluded. |
| A12 | `method_overload_param_type_two_way` | deliberately pins BOTH spellings as separate arms with explanatory comments | REPURPOSE: delete the two explicit-`&` arms AND their comments; keep bare arms (a mechanical sweep would leave duplicated arms + lying comments). |
| A13 | `method_overload_param_type_three_way` | same paired-arm structure | REPURPOSE: drop the `&` arms; keep three-way dispatch. |
| A14 | `method_overload_param_type_cross_module` | same, cross-module | REPURPOSE: drop the `&` arms. Corpus-excluded. |
| A15 | `method_overload_param_type_concrete_beats_generic` | the concrete-beats-generic tiebreak — R2's justification for exempting mixed sets | REPURPOSE (minimal): delete only the `b.pick(&"world")` line. **R2-UNAFFECTED** — must survive. |

Verified UNAFFECTED (search over all 66 borrow*/reborrow*/autoborrow* dirs + all
1,318 expected.json descriptions): the four `autoborrow_*receiver*` fixtures
(receiver position), all `borrow_*` fixtures with `val`-init/non-argument borrows
(also the relocation targets for C3), `borrow_coerce_combo_*` (Borrow-trait path,
already bare), `ref_field_string_arg_coercion`, `ref_variant_binder_return_owned`.

## B. e2e negative fixtures (19 in scope, exhaustively computed; 27 verified UNAFFECTED)

Scope: 243 e2e dirs declare compile-error expectations; 46 contain `&`; 19 fire.

**B-i — rewrite bare; pinned diagnostic is borrowcheck-phase and would be suppressed** (harness fact 2):
| # | Fixture | Pinned diagnostic | Rewrite |
|---|---|---|---|
| B1 | `intrinsic_replace_use_after_move_rejected` | `cannot borrow from moved or uninitialized 'b'` | `mem.replace<type Box>(a, b)` — also exercises W3 bare + explicit `<type …>` interplay (R-1) |
| B2 | `replace_after_move_rejected` | `use after move of 'x'` | `mem.replace(x, 2)` (corpus-excluded) |
| B3 | `replace_while_borrowed_rejected` | `cannot write to 'p' while it is borrowed` | `mem.replace(p.x, 3)` (corpus-excluded) |
| B4 | `swap_while_borrowed_rejected` | same | `mem.swap(p.x, p.y)` (corpus-excluded) |
| B5 | `token_hvar_use_after_consume_rejected` | `use after move of 'tok'` | `make_token(sess)` |

**B-ii — rewrite bare; typecheck-phase pin on the same call, expectation unchanged**:
B6 `borrow_reborrow_mut_requires_mut_ref_rejected`, B7 `borrow_struct_field_param_mut_reborrow_rejected`, B8 `reborrow_mut_through_shared_ref_rejected` (all pin `cannot take &mut through *p …` in the callee body — `bad(x)`/`bad(p)`/`bad(f)`); B9 `return_ref_param_ambiguous_rejected` (`*pick(x, y, true)`); B10 `return_ref_registry_logger_helper_rejected` (`lg.info("auth-failed", ctx)`); B11 `mem_ptr_as_mut_ref_requires_unsafe` (`mem.ptr_from_ref<type Int>(x)`); B12 `array_byte_alloc_uninit_requires_unsafe` (source-declared `&mut Array<Byte>` formals in `stdlib/lang/thread.drift:292-293` — in scope regardless of D2).

**B-iii — rewrite bare + W3 RE-VERIFY (the intrinsic checks at `call_resolver.py:5100-5180` are structural; bare rewrites alone do not certify them)**:
| # | Fixture | Pinned diagnostic | Risk |
|---|---|---|---|
| B13 | `swap_requires_var_rejected` | `cannot take &mut of an immutable binding; declare it with 'var'` | message expected to survive via W3's routed immutable-binding check; a DISTINCT failure from D1b's mutable-rvalue diagnostic and stays worded as-is (flag 4) |
| B14 | `swap_same_place_rejected` *(survey miss)* | `swap operands must be distinct non-overlapping places` | survives if W3 doesn't short-circuit on the type gate first |
| B15 | `swap_type_mismatch_rejected` | `cannot infer type arguments for 'swap': conflicting constraints` | **EXPECTED-CHANGE LIKELY** — depends on W3's new element-type inference; implementer must state the post-W3 message |
| B16 | `replace_type_mismatch_rejected` | same for `replace` | **EXPECTED-CHANGE LIKELY** (corpus-excluded) |
| B17 | `replace_requires_mut_ref_rejected` | `cannot write through *p unless p is a mutable reference` | the borrow IS deletion-equivalent (`*p` is a place), so it fires; mutability rejection must be re-derived from the synthesized borrow (corpus-excluded) |

Plus a **no-edit W3 watch item**: `mem_replace_rejects_shared_ref` (already bare) pins `replace expects &mut T as the first argument` inside the block W3 rewires — W3 must not lose it.

**B-iv — D2 (resolved: include)**: B18 `std_array_extend_non_copy_rejected` → rewrite bare `a.extend(src)`, expectation unchanged.

**B-v — soundness gate**:
| B19 | `borrow_same_stmt_shared_vs_mut_rejected` | `takes(&x, &mut x)` at `(a: &Int, b: &mut Int)` pins the shared-vs-mut same-statement conflict | REWRITE-BARE `takes(x, x)` + **MANDATORY RE-VERIFY**: after the rule, this conflict can only arise from compiler-synthesized borrows. If the borrow checker doesn't visit synthesized borrows for this check, the program silently compiles — a soundness regression this fixture is the only guard against. If the bare form loses the diagnostic → **compiler fix, not expectation change. Release gate.** |

**B-vi — 27 UNAFFECTED negatives, by reason** (exhaustive): 5 `captures(&x)` capture-list fixtures (incl. all four from the earlier survey's partial list — `borrow_escape_spawn_rejected`, `borrowed_capture_interface_coercion_rejected`, `callable_borrowed_capture_callback_boxing_rejected`, `implicit_callback_borrowed_capture_rejected`, `closures_explicit_captures_shared_write_rejected`); 7 `val`-init/non-argument borrow fixtures; 8 constructor-field fixtures (`struct_ref_field_*`); 4 signature-only; 3 already-bare (`mem_replace_named_ref_rejects_live_reborrow`, `mem_replace_rejects_shared_ref` — see W3 watch item, `borrow_coerce_combo_rejected`).

## C. Python driver / borrow-checker tests

**C1. `test_ref_to_value_arg_coercion.py`** — `test_ref_to_value_coercion_loses_to_exact_ref_overload` and `test_ref_to_value_method_overload_prefers_exact_ref`: **RETIRE** (both pin `&`-as-selector; their programs become R2 definition-site errors) — replaced by two new R2 negative pins (free + method shapes). Other 5 tests mechanical; `test_ref_to_value_negative_destructible_rejects` unaffected.

**C2. `test_autoborrow_diagnostics_span.py`** — both tests **UNAFFECTED**: the mutable-rvalue rejection pin becomes **load-bearing under D1b(b)** (it is the diagnostic users hit after migration); the receiver test is out of scope. Wording note → flag 4.

**C3. `test_borrow_rvalue_move_args.py`** — most-affected file:
- Module docstring: REWRITE (it records the now-reversed "`&mut mk(move s)` is SUPPORTED at argument position" decision).
- `test_borrow_of_call_with_move_arg_compiles_and_runs` / `_asan` / `no_move_still_green` / `test_method_call_receiver_shape`: REPURPOSE → bare (`check_widget(mk_widget(move s))` etc.); purpose survives iff the auto-borrow path routes through the same `BorrowMaterializeRewriter` predicate — add an explicit verification.
- `test_direct_move_borrow_targeted_diag_with_location`: REPURPOSE → non-argument position (`val r = &(move w);`) — at an arg slot `E_REDUNDANT_ARG_BORROW` replaces the targeted guardrail message and bare would compile; relocation keeps the guardrail + span pin.
- `test_mut_borrow_of_move_call_materializes` (**the exactly-2 mutable-rvalue sites in the repo**): REPURPOSE → non-argument position (`val p = &mut mk_widget(move s); touch(p)`) — **the D1b(b) migration exemplar**, cite in the MIGRATION section.
- `test_mut_direct_move_keeps_targeted_diag`, `test_match_result_move_borrow_rejected`: REPURPOSE → non-argument position (same reasoning).
- `test_spanless_diag_never_names_first_file`: UNAFFECTED.

**C4. `test_lambda_capture_borrow_overlap.py`** (all 4 IIFE sites in the repo): all four REPURPOSE → bare (W5). **Structural requirements**: the helper asserts zero leaked diagnostics, so W5 must land first; and the borrow checker must see synthesized borrows for the overlap analysis — same soundness class as B19: the disjoint-fields-OK test would stay green while the three conflict tests silently degrade. **Verify all four.** (`_method.py` sibling unaffected — programmatic HIR.)

**C5. R2 exposure — independent repo-wide mode-erasure re-scan reproduces the plan exactly**: 3 T-vs-`&T` pairs (`json._encode_node` + the two C1 retirements), **0** `&`-vs-`&mut` pairs, **0 e2e fixtures with a mode-erasure pair** (incl. cross-module; `method_overload_param_type_*` family R2-unaffected; near-misses ruled out by reading: separate-program duplicates, generic-vs-concrete stdlib log sets, FnN/CallbackN throw-mode sets).

**C6. Other findings**:
- `test_fnptr_ref_arg_autoborrow.py`: **UNAFFECTED (D8(b) + generic exemption) — MUST be excluded from the sweep**; its controls deliberately keep `f(&s)` legal.
- `test_ref_to_interface_coercion.py` (D7 suite): UNAFFECTED in purpose, **precision hazard** — KEEP (do not sweep) L74, L125, L171, L301, L320, L376, L388 (coercion borrows / negatives whose bare form also fails); SWEEP L61, L91, L143 (redundant concrete-at-concrete). The sole surviving `&`-in-argument context → reviewer callout.
- `test_tmp_borrow_callback_collision.py`: REPURPOSE → bare (`node.get("key")`) + docstring rewrite (its bisect table names the `&"literal"` spelling) + RE-VERIFY the `binding_id`-collision pin survives the bare-rvalue materialization path.
- UNAFFECTED (verified): `test_value_position_call_arg_bare_hvar_rejected.py`, `test_auto_borrow_signatures.py` (programmatic HIR — natural home for W0/W1 unit coverage), the receiver-position files, `test_ref_to_value_extended_slots.py` / `_ctor_and_field_slots.py` (mechanical only).
- Everything else in the 936 embedded-Python sites (~135 files): **mechanical only**, extraction-aware sweep required (~1/3 have escape-shifted line attribution).

## D. Corpus-promotion scope (single full reviewed promotion)

Comparison semantics verified: any content change to any hashed fixture invalidates
the baseline wholesale (`drift_corpus_audit.py:323`, `_fixture_hash` covers every
file incl. expected.json).

> **Promotion arithmetic (exact, authoritative — matches the enumeration below):
> 426 content-hash deltas** (392 `compiled_ok` + 34 `failed`; includes the 13
> regenerated `om_*` dirs) **+ 20 additions (13 `failed` + 7 `compiled_ok`) + 0
> removals. Universe 1,269 → 1,289.** 16 further edited dirs are corpus-`excluded`
> (no manifest impact). **Expected partition flips: 0.**
> `environment.driftc_version` moves off 0.33.89; ABI held at 22.

`om_*` regeneration detail: ~16 emitter strings in `__ownership_matrix__/_gen.py`
(token axis :871-1040,:1193,:1267,:1281 — 7 dirs; extend axis :279,:393-410 — 6
D2-gated dirs) + narrative comments :260-265,:377-393,:1146-1147;
`just ownership-matrix-check` stays green post-regen; all regenerated dirs already
counted in the 426.

Flip-risk set (11 in-universe `failed` fixtures): B1, B5-B12, B18, B19 — all
rewritten to preserve their diagnostics; B19 is the only likely flip and any flip
there is a soundness signal, not a promotable delta. All 8 `swap_*`/`replace_*`
W3-risk fixtures are corpus-excluded, so B-iii expectation churn cannot flip the
manifest.

**New e2e fixtures — exactly 20, enumerated** (reviewers see concrete files at
implementation; names and partitions are binding):

`failed` partition — 13:
1. `redundant_arg_borrow_shared_local_rejected` (row 2, `read(&name)`)
2. `redundant_arg_borrow_mut_local_rejected` (row 2, `edit(&mut buffer)`)
3. `redundant_arg_borrow_mut_at_shared_param_rejected` (row 3)
4. `redundant_arg_borrow_of_ref_value_rejected` (row 5, `read(&r)`, `r: &String`)
5. `redundant_arg_borrow_rvalue_literal_rejected` (row 6, `read(&"alice")`)
6. `redundant_arg_borrow_rvalue_call_rejected` (row 6, `read(&make())`)
7. `redundant_arg_borrow_projection_operands_rejected` (row 8 — field, index,
   parenthesized, and `& x` whitespace operands in one program; four pinned
   diagnostics with rendered operand text, subset-matched)
8. `redundant_arg_borrow_alias_param_rejected` (D6 — `type Handle = &Session`
   formal, explicit `&` rejected)
9. `redundant_arg_borrow_intrinsic_rejected` (row 13, `mem.replace(&mut p, v)`)
10. `redundant_arg_borrow_interface_rejected` (row 14, post-W2)
11. `redundant_arg_borrow_lambda_iife_rejected` (row 15, post-W5)
12. `mut_rvalue_arg_binding_required_rejected` (row 7 —
    `E_MUT_RVALUE_ARG_BINDING_REQUIRED`, the D1b(b) classification; NOT a
    redundancy diagnostic)
13. `overload_param_mode_only_diff_rejected` (R2 — one program containing all
    FOUR ratified shapes as four **uniquely named** overload sets: free-fn
    T-vs-`&T`, free-fn `&T`-vs-`&mut T`, method T-vs-`&T`, method
    `&T`-vs-`&mut T` — four distinct definition-site diagnostics, each naming its
    own set, so the subset-matching harness cannot satisfy multiple expectations
    from one diagnostic; this is the replacement for the two C1 retirements)

`compiled_ok` partition — 7:
14. `autoborrow_bare_assoc_fn` (assoc-fn family positive)
15. `autoborrow_bare_interface_arg` (W2 positive)
16. `autoborrow_bare_mem_intrinsics` (W3 bare-form matrix across all 11 intrinsics)
17. `autoborrow_bare_lambda_iife` (W5 positive)
18. `autoborrow_bare_alias_param` (D6 positive — bare at `Handle` formal)
19. `autoborrow_bare_builtin_extend` (D2 positive — `a.extend(src)`)
20. `rvalue_arg_temp_drop_bare` (the bare half of the R-2 A/B gate; memcheck lane;
    paired with repurposed A6)

**A/B rvalue-gate baseline (round-1 correction 2):** the "explicit baseline" half
cannot be an e2e fixture — its source would be rejected by the rule itself. The
baseline is instead **programmatic HIR driven through the FULL pipeline** in a
driver test: the borrow is constructed with `source_written=False` (the
compiler-synthesized shape the rule permits) and then lowered **HIR→MIR→LLVM,
linked, and EXECUTED under memcheck and ASAN**, asserting drop-count/order parity
against `rvalue_arg_temp_drop_bare`. A borrow-checker-only harness in the
`test_auto_borrow_signatures.py` style does NOT satisfy this gate — the gate is
about runtime temp lifetime, so the baseline must run. This is persistent and
post-rule-executable. (A decoded pre-rule package was considered and set
aside: package fixtures don't fit the e2e corpus shape — `module_paths` exclusion
— and a checked-in binary artifact ages poorly.) Non-corpus additions
(driver/unit): this A/B baseline test, D9 package encode→decode→recompile pin, W0
validator-assert unit test, post-rename `json._encode_node` pin, D8(b) pins
(already existing in `test_fnptr_ref_arg_autoborrow.py`).

## Cross-cutting flags for reviewers

1. **Soundness verifications, not expectation edits**: B19 and C4 — synthesized
   borrows must be visited by the same-statement conflict and capture-overlap
   analyses; failures there are in-slice compiler defects. Release gates.
2. **W3 owns six at-risk messages**: B13-B17 + `mem_replace_rejects_shared_ref`;
   implementer states each post-W3 message before the sweep runs (R-1).
3. **Sweep exclusions (policy-aware, not textual)**: `test_fnptr_ref_arg_autoborrow.py`
   (all), `reborrow_mut_to_shared_callback` (generic formal), the KEEP-subset of
   `test_ref_to_interface_coercion.py`.
4. **Diagnostic wording — distinct failures keep distinct messages** (round-1
   correction 4; blanket unification withdrawn). Mutable-rvalue-needs-binding,
   immutable-binding-cannot-be-mutably-borrowed, and move-containing-borrow are
   three different failures whose messages stay separate and accurate. Only
   genuinely equivalent D1b paths are normalized: the new
   `E_MUT_RVALUE_ARG_BINDING_REQUIRED` (explicit `&mut <rvalue>` argument) and the
   existing bare-form "borrow requires an addressable place; bind to a local
   first" describe the same D1b condition from two spellings and should agree on
   the remedy phrase; `swap_requires_var_rejected`'s immutable-binding message and
   the move-guardrail messages are untouched.
5. **Survey corrections**: group B is 19 (not 26); 7 negatives added
   (`swap_same_place_rejected`, `replace_after_move_rejected`,
   `replace_type_mismatch_rejected`, `intrinsic_replace_use_after_move_rejected`,
   `array_byte_alloc_uninit_requires_unsafe`, `std_array_extend_non_copy_rejected`,
   `token_hvar_use_after_consume_rejected`); 4 overload fixtures added to group A;
   2 files added to group C.

## Reviewer choices — RESOLVED (round 1)

- A3: **REPURPOSE to the true-rvalue test** (approved).
- A7: **REPURPOSE to the ref-value form** — preserves the unique call-boundary
  reborrow coverage (approved).
- B15/B16: **messages finalized during W3**, under two binding constraints: they
  must remain user-facing type-conflict diagnostics (never internal-flavored), and
  the post-W3 strings are presented for review BEFORE any `expected.json` change.
