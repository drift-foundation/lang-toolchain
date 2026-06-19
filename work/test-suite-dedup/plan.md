# Test-suite dedup — static audit plan

**Status: STATIC AUDIT ONLY. No tests edited or deleted.** Baseline commit: `f46c9eb7`.

**Rev 2 (post-review):** incorporated 4 review findings — (1) JSON crash-min cluster
re-traced to an ownership/drop lowering defect (`a32fa743`), reclassified from
"remove" to **HELD** pending a per-fixture root-cause pass; (2) Batch 1 split into
1a byte-identical / 1b strict-subset-needing-survivor-confirmation; (3) scalar
`local_value` justified via the signed-local lexical path (`_local_shadows_module`),
not module-const; (4) core-language scope held to byte-identical + survivor-confirmed.

Goal: reduce duplicate/overlapping coverage WITHOUT weakening regression protection.
This document is the audit output; implementation (if approved) is a separate slice.

## Suite size (why this is fan-out work)

| Area | Count |
|------|-------|
| e2e fixtures (`lang/tests/codegen/e2e/*/`) | 1336 |
| driver pytest | 396 |
| stage2 pytest | 70 |
| packages pytest | 38 |
| parser pytest | 37 |
| memcheck pytest | 35 |
| type_checker pytest | 29 |
| stage1 / stage4 / borrow_checker / core / checker | 20 / 13 / 14 / 12 / 11 |

## Method

Six audit passes organized BY CONTRACT DOMAIN (not by file), each spanning all
phases (parser → checker → stage1/2/4 → codegen) and all lanes (functional e2e,
package emit→consume, valgrind/memcheck). Each candidate is judged against the
rubric below. Findings are grouped by the contract they pin.

## Decision rubric (applied to every candidate)

- **keep** — unique contract, OR the only proof at its boundary/lane, OR unsure.
- **remove** — a stronger test pins the identical contract at the same/stronger
  boundary; nothing unique is lost.
- **merge** — several fixtures pin the same contract with trivial input variation;
  fold into one parameterized/table test.
- **rename** — keep, but the name misdescribes the contract (discoverability only).
- **convert to narrower unit** — a slow full compile/run that only needs to assert
  a checker/stage decision → replace with a fast structural test (but keep one
  full compile/run if a codegen/runtime boundary is involved).

Each row: `test path | covered contract | overlap reason | recommendation | risk | required validation if changed`.

## Hard constraints (non-negotiable for any later implementation)

1. Do NOT dedup a LANGUAGE_BUG regression unless the replacement pins the **same
   root cause at the same or stronger boundary** (named explicitly).
2. Prefer keeping **one fast structural test + one full compile/run test** per
   checker/lowering/codegen boundary.
3. Preserve **sanitizer/memcheck** coverage where it is the only proof of
   ownership/drop/runtime safety. A memcheck test and a functional test of the
   same feature are NOT duplicates (one proves value, the other proves no
   leak/UAF) — both stay.
4. Package emit→consume→link→run is a distinct boundary from same-source compile,
   even when the source is identical.
5. If unsure → **keep**, with the reason recorded.

---

## Findings by domain

_Populated from the six domain audits as they complete._

### (1) Match-family

Scope: integer scalar match (named + qualified const patterns), bool match,
variant/ctor match — e2e + stage2 units + parser/type_checker + memcheck. The
redundancy concentrates in the freshly-added scalar-const e2e set; the older
per-width and LANGUAGE_BUG fixtures are all distinct.

| test path | covered contract | overlap reason | rec | risk | validation if changed |
|---|---|---|---|---|---|
| `e2e/scalar_match_const_local_value/` | local (block-scope) const as scalar pattern, signed, resolved lexically | removable **only because `scalar_match_const_local_shadows_module/` already covers a SIGNED local const resolving lexically** (the same local-const code path). Do NOT justify via `scalar_match_const_value/` — that is the MODULE-const path, which is distinct from local-const resolution. The unsigned sibling `_local_unsigned_value` additionally pins `UintConst→int` coercion. So the signed-local path survives in `_local_shadows_module`; the unsigned-local path survives in `_local_unsigned_value` | **remove** (keep both `_local_shadows_module` and `_local_unsigned_value`) | low | run `_local_shadows_module` (signed local lexical) + `_local_unsigned_value` (unsigned local coercion) |
| `e2e/scalar_match_const_unknown_name_rejected/` | bare unknown name → E-MATCH-SCALAR-CONST | weakest of three rejection-message variants; `_const_val_local_rejected` pins the specific local-binding submsg, `_ctor_in_scalar_rejected` pins the ctor/unknown path | **remove** (merge into those two) | low | run `_const_val_local_rejected`, `_ctor_in_scalar_rejected` |
| `e2e/scalar_match_const_signed_in_unsigned_rejected/` | const declared-signedness mismatch (one direction) | 5 fixtures hit the one `scalar_pattern_value` signedness gate (this + `_unsigned_in_signed` + literal `_unsigned_to_signed` + literal `_negative_to_unsigned` + qualified `_qual_const_signedness`); this is the redundant mirror | **remove** (keep other direction + 2 literal + qualified) | med | run the 4 kept signedness fixtures + `test_scalar_match_checker_lowering_contract.py` |
| `e2e/scalar_match_const_duplicate_rejected/` | value-dedup literal-vs-module-const | middle of three; `scalar_match_duplicate_rejected` (two literals) is simplest, `_qual_const_duplicate_rejected` (literal-vs-qualified) is strongest cross-source proof | **remove** (keep those two) | low | run `scalar_match_duplicate_rejected`, `_qual_const_duplicate_rejected` |
| `e2e/scalar_match_const_default_before_arm_rejected/` + `_qual_const_after_default_rejected/` | const arm after `default` unreachable | same source-agnostic ordering rule (fires pre-resolution); only differ unqualified vs qualified | **merge** to one (prefer keeping qualified — qual resolution is otherwise less covered) | low | run kept ordering fixture + `scalar_match_missing_default_rejected` |

**Net: ~5 fixtures (4 removals + 1 merge), all in the recently-added scalar-const set.** No LANGUAGE_BUG fixture touched. Every checker/lowering/codegen boundary retains ≥1 fast structural + ≥1 full e2e after trims.

**Looks redundant but KEEP:**
- Per-width positives `scalar_match_{int_value,uint,byte,int32_negative,uint32_highbit,uint64_highbit}` — distinct width/signedness; high-bit + negative pin sign-extension/representability codegen.
- `scalar_match_value_owns_string` + `bool_match_value_owns_string` — sole drop proofs for Switch (scalar) vs If (bool) value-position cleanup edges.
- `test_scalar_match_checker_lowering_contract.py` — only structural pin that stage2 consumes signed `scalar_value` (`i64 -5`, not raw `+5`) + single LLVM `switch`. `test_cfg_successor_contract.py` — only pin for `SwitchTerminator` successors/remap contract.
- `scalar_match_qual_const_{value,shadowing,reexport,vs_variant_ctor}` — distinct resolution rules (alias / no-current-module-shadow / re-export table / DOT-vs-DCOLON LALR guard).
- `scalar_match_int_literal_on_{bool,variant}_rejected` + `_qual_const_in_{bool,variant}_match_rejected` — bool vs variant scrutinee dispatch AND literal-arm vs qual-const arm are different `ctor=None` code paths; the 4 are not dupes.
- `scalar_match_const_local_shadows_module` (local wins) vs `_qual_const_shadowing` (qualified does NOT consult current module) — opposite rules.
- LANGUAGE_BUG regressions (driver): `test_match_arm_move_local_result_scope.py`, `test_match_arm_lambda_capture.py`, `test_match_arm_binding_nested_block_scope.py`, `test_variant_borrowed_match_construct_int_payload.py`, `test_unmatched_typed_catch_propagate_no_uaf.py`, `test_inline_match_rvalue_string_payload_leak.py`, the `*_match_return_no_leak`/`om_match_bind_*` drop-matrix fixtures — each pins a distinct resolved root cause; no same-root replacement exists.
- `match_value_producing_non_copy_drop_once` vs `_generic_variant_token_drop_once` — different root causes (no-tombstone fallback vs stale generic-instance-cache tombstone).

### (2) Exceptions / typed-catch

Scope: throw/rethrow, typed/untyped catch, throws-signature (phase1–4),
throw-unwind drop, auto-try/or_throw, on_error/on_none, pub-error — ~140 tests
read. Real duplication is small; the "Err twice" crash-min family looks bloated
but is ~21/24 genuinely distinct scope-cleanup paths.

| test path | covered contract | overlap reason | rec | risk | validation if changed |
|---|---|---|---|---|---|
| `e2e/json_err_twice_same_input/` | repeated `json.parse` Err, no access (double-free repro) | same lowering as `json_err_twice_no_access/`; same-vs-different input is not a distinct branch | **merge** (keep `_no_access`) | low | e2e green on `_no_access` |
| `e2e/result_err_large_struct_twice/` | custom error struct twice, no access | `result_err_struct_with_strings_twice_min/` is the harder superset (droppable String fields exercise drop); large-no-strings is just a size variant | **merge** (keep `_with_strings_` variant) | low | e2e green on strings variant |
| `e2e/json_parse_array_err_tag_only_no_crash/` | array parse error, `e.tag` only | duplicate of `json_err_second_case_tag_only_min/` | **merge** (keep one, rename survivor clearer) | low | e2e green |
| `e2e/result_on_error_try_block_no_catch_rejected/` + `optional_on_none_try_block_no_catch_rejected/` | on_error/on_none lambda-throw under nothrow rejected | "declared nothrow but may throw" already pinned by `checker/test_can_throw_inference.py` + `driver/test_lambda_nothrow_diagnostics.py` | **convert to narrower unit** (only if e2e count matters) — but e2e also proves on_error/on_none lowering *reaches* the checker | med | if removed, confirm those 2 tests assert the same code on an on_error/on_none shape |
| `e2e/rethrow_outside_catch_error/` + `rethrow_inside_try_body_error/` | "rethrow only valid inside a catch block" | same diagnostic, same boundary; differ only by rethrow position (fn-body vs try-body) | **merge** to one (borderline; try-body is slightly stronger) | low | e2e green |

**Net (CORRECTED per review): 0 immediate.** The three "twice" rows above
(`json_err_twice_same_input`, `result_err_large_struct_twice`,
`json_parse_array_err_tag_only_no_crash`) are part of the **same ownership/drop
lowering cluster as JSON §5a** — they pin the `a32fa743` keepalive double-free
class, NOT a JSON/exception *behavior* contract, and have no leak coverage.
**HELD for the dedicated JSON-crash-min root-cause pass** (do not delete; consider
`alloc_track_leak` upgrade). The 2 borderline non-cluster consolidations (the
`_rejected`→unit convert, the rethrow-position merge) remain as Batch-2/3
candidates with survivor confirmation.

**Looks redundant but KEEP:**
- `unmatched_typed_catch_propagate_no_uaf` **triad** — e2e (functional exit-0) + `memcheck/test_unmatched_typed_catch_propagate_no_uaf.py` (valgrind, 4 variants) + `stage2/test_caught_error_propagation_pins.py` (MIR cleanup-hook shape) — the textbook fast-structural + full + sanitizer triad for the 2026-05-18 bug. Required by the one-each rule.
- `memcheck/test_throw_unwind_destructible_{drop,call_err,cross_pkg_}` (BUG #102) — distinct unwind edges (explicit-throw / can-throw-call auto-unwind / cross-package stale-snapshot); only proof of unwind-drop correctness.
- The 5 `callable_fn_ptr_throwing_field_nothrow_*` (fn/local/var/branch/refmut) — distinct value-flow coercion sites into a throwing fn-typed field (consolidate only if the coercion logic is later unified).
- `test_throws_signature_phase1/2/3_5` + `typed_catch_phase4d` — sequential phases, each a different checker boundary (parse → terminal-flow → trait → typed-catch codegen).
- `result_err_convert_*` / `cleanup_err_with_*` / `loop_err_return_*` / `_after_array_local_*` "twice" crash-mins — each pins a *different* scope-cleanup-on-early-return path (guards the `__borrow_tmp`/scope-drop regression class); ~21/24 distinct.
- `rethrow_semicolon` + `rethrow_newline_terminator` — distinct parser grammar paths (terminator), keep the pair; only the *propagation* assertion (shared with `nested_rethrow_outer_specific` etc.) is over-covered, not the terminator distinction.
- All bug-pinned: typed-catch-binder-native-ctor (0.33.35), lambda-catch-binder-collision, throw-raise-stmt-alias, `test_match_by_ref_result_err_binder`, `test_typed_catch_through_pub_type_alias`, `test_lambda_void_callback_throw_check`, `test_dv_string_borrowed_exception` vs `test_pub_error_params_view_drop` (different leak mechanisms).
- Fast-structural lane verified present for try-lowering & throw-unwind (`stage2/test_hir_to_mir_try*.py`, `checker/test_can_throw_inference.py`, `test_try_expr_semantics.py`) — no boundary left slow-only or structural-only.

### (3) Ownership / drop / sanitizer

**Verdict: KEEP essentially everything — zero true duplicates (no remove/merge).**
This domain is the safety core; the suite is already deduplicated *by proof tier*,
and MEMORY flags it protected (`feedback_ownership_lattice_change_bar.md`,
`feedback_memcheck_in_gate.md`). The real risk here is the **opposite** of
redundancy: accidentally downgrading a sanitizer proof to functional-only.

**Proof-tier taxonomy (non-substitutable — same contract, different proof):**
structural unit (`stage2/*`, asserts MIR shape, no leak detection) · typing/compile
driver · e2e functional (exit/stdout only) · **e2e forced `alloc_track_*`** (36
fixtures; leak-checked on *every* run via `runner.py:824`) · **valgrind memcheck**
(`memcheck/*`, 38 files; strongest — leak+UAF+double-free). A `*_memcheck.py` and
its same-source e2e are NOT duplicates.

**Actionable: none (no deletions).** Three structural notes only:
- `e2e/variant_match_loop_owned_payload_leak/` vs `memcheck/test_inline_match_rvalue_string_payload_leak.py` — names suggest overlap; they pin *different* bugs (loop variant-Array payload drop vs `Some(v)=>move v` env.get leak). KEEP both; flagged so a future sweep doesn't conflate.
- `stage2/test_drop_policy_contract.py` + `test_drop_policy_standalone_matches_hir_to_mir.py` — the standalone file is an explicit **equivalence pin** between two deliberate duplicate drop-policy impls; do NOT merge (merging defeats its purpose).
- `driver/test_drop_policy_copy_short_circuit_bug.py` + `test_if_join_drop_destructor_uniform_move.py` — distinct LANGUAGE_BUG root causes; KEEP.

**Looks redundant but KEEP — sanitizer/alloc-track is the ONLY safety proof** (do not delete a `*_memcheck.py` "because there's an e2e", do not strip `alloc_track_*` keys):
- ConstShare/ConstArc refcount: `memcheck/test_const_arc_memcheck.py`, `test_const_share_memcheck.py`, `_phase1_synthesis_`, `_phase4_variants_`, `_phase5_implicit_duplication_` (drivers prove typing only).
- `memcheck/test_typebox_take.py` (drained-box `take<T>` ordering), `test_mem_replace_string_uaf.py`, `test_throw_unwind_destructible_drop.py`+`_call_err`+`_cross_pkg_` (BUG #102, 3 unwind edges), `test_match_by_ref_variant_drop.py`, `test_partial_move_copy_binder_string_slot_leak.py`, `test_patch3_nested_scope_uaf_regression.py`, `test_ref_to_value_arg_coercion_memcheck.py`, `test_mut_struct_string_field_self_concat.py`, `test_drift_error_phase1_helpers.py`.
- **Package-consumer drop divergence** `memcheck/test_pkg_*.py` (5: array_string_scope_drop, cross_package_scope_drop, map_literal_string_leak, nested_struct_drop_leak, vs_raw_stdlib_drop_policy_divergence) — proven discriminators for the web.rest/bookkeeper cert leaks; the bug only appears on the `.dmp` consumer path, so a source-mode test cannot replace them.
- The 36 `e2e/*` `alloc_track_leak`/`alloc_track_no_leak_if_enabled` fixtures — sole always-on leak proof for their shapes (arc/destructible/hashmap/jsonnode/std_json/result-match-return/registry).
- Driver LANGUAGE_BUG 1:1 regressions: `test_box.py`, `test_arc_not_copy.py`, `test_arc_relocation.py`, `test_arc_intrinsic_bridge.py`, `test_field_projection_noncopy_arc_uaf.py`, `test_nested_callback_move_invariant_regression.py` (+ the VT set under domain 4).

### (4) Concurrency / runtime

Scope: VT/spawn, FutureGroup/join, Condvar (#1–#16 matrix), Channel, Mutex,
conc.sleep, liveness, reactor/executor. **The suite is overwhelmingly
well-differentiated** — only 3 true overlaps; bias-to-keep held throughout given
concurrency flakiness.

| test path | covered contract | overlap reason | rec | risk | validation if changed |
|---|---|---|---|---|---|
| `e2e/concurrent_cancel_before_start_race_stress_diagnostic/` | cancel-before-start race under load | `main.drift` **byte-identical** (diff RC=0) to `concurrent_cancel_before_start_race_stress/`; only `expected.json` differs (adds description + empty stdout/stderr) | **merge** stdout/stderr keys into the non-`_diagnostic` dir, then remove this one | low | run kept dir with merged assertions |
| `e2e/concurrent_spawn_infers/` | spawn lambda→callback coercion + result inference | functionally identical body to `concurrent_spawn_coerce_callback/` (whitespace + var-name + `pub` only) | **remove** (keep `_coerce_callback`, clearer name) | low | re-run kept dir |
| `e2e/concurrent_many_short_tasks_min_repro/` | deque-of-`VirtualThread<Int>` join drain | subset of `concurrent_many_short_tasks/` (1 task vs 500 — debugging artifact); full version subsumes the contract | **remove** (unless it pins a distinct lowering bug — nothing in source suggests so) | low–med | run `concurrent_many_short_tasks` (sum==125250) to confirm join-drain covered |

**Net: 3 small actions** (1 merge + 2 removes). No regression/memcheck test is a candidate.

**Looks redundant but KEEP (distinguishing axis noted):**
- `concurrent_cancel_*` (`conc.spawn`/`VirtualThread<T>`) vs `concurrent_future_cancel_*` (`conc.spawn_future`/`Future<T>`) — **distinct API surfaces** (future join-after-complete returns CLOSED; VT returns value). Same for the `join_timeout_nonzero_ok` spawn-vs-future pair.
- `callback_arc_mutex_full_mutation` vs `std_concurrent_arc_mutex_full_mutation` — different lock API path (`conc.lock(...)` helper vs explicit `.get().lock()` guard).
- `conc_sleep_*` family — each pins a distinct runtime code site (read-side vs write-side direct-resume stale park_token; double-timer-registration; spawned-VT vs sequential).
- Condvar #1–#16 — numbered exhaustive matrix; `wait_until` (absolute) vs `wait_timeout` (relative) are different APIs; `wait_until_past_deadline` is a named regression.
- `concurrent_channel_*` — differentiated by payload type (Int/String/Arc/Optional/variant) and protocol (parked-recv/close-drain/send-after-receiver-drop/recv_timeout) — distinct drop paths.
- **Do NOT touch (high-value regressions):** `driver/test_vt_result_ownership_matrix.py` (R2–R8, 979ln), `test_vt_drop_started_running_uaf.py` (R1), `test_registry_arc_parked_vt_leak.py`, `test_vt_capture_implicit_move_atexit_uaf.py`, `test_channel_close_race.py` (valgrind conservation), `test_channel_destructor_deadlock.py`, `test_liveness_interrogator.py`, `test_log_vtid_tid.py` — each a distinct root cause / sole no-UAF-no-deadlock-under-contention proof; e2e channel tests are single-threaded and do NOT subsume the valgrind ones.

### (5) JSON / reload / fs-io

**Largest opportunity: ~30 of 82 JSON fixtures retire/merge with zero loss of
distinct contract or leak/UAF proof.** The redundancy is concentrated in three
ladders that landed together as debugging bisection sets.

**(a) JSON `*_crash_min` / `result_*_err_*` ladder — ~18 fixtures.** ⚠️ **CORRECTED
after review (Finding #1): these pin an OWNERSHIP/DROP LOWERING defect, NOT JSON
parsing semantics. NO deletion in any zero-risk batch — held for a dedicated
root-cause review pass.**

Root cause (traced): the cluster landed with commit **`a32fa743`**, whose
compiler changes were `llvm_codegen.py` + `stage2/hir_to_mir.py` (NOT the JSON
parser). The defects fixed:
1. **Keepalive by-value store double-owned refcounted values** (`llvm_codegen.py`
   `_FuncBuilder` keepalive storage: stored String/struct/variant/interface
   without retain/release balancing → **double-free**; the fix restricts keepalive
   to scalar POD). Building/dropping a `Result<JsonNode|String|struct,
   JsonErrorData|E>::Err` with refcounted payload is exactly the trigger.
2. **Array-grow join `LoadLocal` → `MoveOut`** (`hir_to_mir.py:3457`) — drop/move-out
   correctness (the `result_err_after_array_local_*` shape).
3. **`&&`/`||` short-circuit lowering** (`hir_to_mir.py:1339`) — control-flow.

Required root-cause column (per Finding #1), answered at cluster level — per-fixture
confirmation is the deliverable of the dedicated review pass:

| field | finding |
|---|---|
| exact defect originally pinned | a32fa743 keepalive double-ownership of refcounted `Result::Err` payloads (double-free) + array-grow move-out + short-circuit lowering — an **ownership/drop/control-flow** class, not parse semantics |
| does the proposed survivor exercise the same drop/early-return path? | **NO.** `std_json_parse_error_position` / `_invalid_syntax_tag` / `_policy` assert *parse offsets/tags*; none construct **and drop** a `Result::Err` across the twice / loop-early-return / after-array-local / cleanup shapes |
| same or stronger boundary? | **NO — different boundary.** Survivors hit the parser; the crash-mins hit stage2/codegen drop lowering |
| alloc_track_leak / valgrind coverage if leak/UAF/double-free class? | **NONE.** All are bare `exit_code:0` crash-repros — they catch the *crash* (abort/SIGSEGV → exit≠0) but a **leak/double-free that doesn't crash would slip through** |

Byte-identity check (ran `diff`): the pairs the earlier pass called "duplicates"
(`json_parse_array_err_tag_only_no_crash` vs `_second_case_tag_only_min`;
`json_err_twice_same_input` vs `_no_access`) are **NOT byte-identical** — they differ
in input (`{` vs `[`), var names, and access pattern, i.e. distinct construct/drop
shapes. So there are **zero safe byte-identical deletions** here.

**Revised recommendation: KEEP the entire cluster as ownership/drop lowering
regressions.** Two non-deletion actions for the dedicated pass:
- **Upgrade representatives to `alloc_track_leak:true`** (e.g. `result_json_err_drop_crash_min`, `try_wrapper_json_result_crash_min`, the five `*_local_*`) so the cluster guards the *leak/double-free* variant, not just the crash — this *strengthens* coverage and is the right response to "the defect was a double-free class with no leak proof."
- Only after per-fixture root-cause mapping (defect → does a kept sibling pin the *same drop path at the same/stronger boundary, with leak coverage*) may any be merged. Treat as LANGUAGE_BUG-adjacent regressions under constraint #1.

_(This reconciles with the Exceptions domain's "Err twice" finding as a single
drop-path-ladder decision — same cluster, same hold.)_

**(b) duplicate-key cluster:** remove `std_json_parse_duplicate_as_object_get`, `_duplicate_get_only`, `_duplicate_only_no_access` (subset of `std_json_parse_policy`); keep `std_json_parse_basic_duplicate_keys` (only `expect_path` on dup keys) + `std_json_parse_policy` + `std_json_rfc_strings_limits` (canonical policy/RFC pins).

**(c) encode-with-config cluster:** remove `std_json_encode_with_config_{object,scalar,twice_scalar}` (subsumed by `std_json_encode_ordered_lex_utf8`); keep that + `std_json_encode_with_config_twice` (idempotency). Keep all 5 `..._determinism_*_snapshot` (distinct axes).

**(d) json_handle cluster:** remove `json_handle_share_access`, `json_handle_clone_read` (subset of `json_handle`), `json_handle_clone_deep_root`+`_subnode` (covered by `json_clone_deep` + `json_node_clone_deep_subtree`); keep `json_handle`, `_array_object_accessor`, `_missing_access`, `_encode_parity`.

**(e) fn_get path cluster:** merge `std_json_fn_get_{int,bool,float,string}_at_path` → one `std_json_fn_get_typed_at_path`; keep `std_json_fn_get_path` (dotted-string traversal is a different API).

**(f) misc:** remove `hashmap_jsonnode_empty_twice_min` (trivial len==0); `scope_drop_json_leak` has NO alloc tracking despite its name → **upgrade to `alloc_track_leak:true`**.

**io / fs / env:**
| test path | rec | reason |
|---|---|---|
| `e2e/std_io_file_read_write/` | **remove** | strict subset of `std_io_file_builder_read_write_api/` |
| `e2e/std_io_configured_read_line_api_shape/` | **Batch 2 / needs survivor confirmation** (not remove) | compile/**API-shape** coverage; `std_io_stdin_line_edge_matrix/` proves runtime behavior but may NOT prove the same public API shape (Finding #2) — keep unless a survivor explicitly asserts the configured-read-line API shape |
| `e2e/env_get_set/` + `env_get_unset/` | **merge** to one (two arms of `std.env.get`) | |
| `e2e/std_io_block_on_write_timeout/` | **flag, fix** (not dedup) | dead assertion — both Err arms `return 0`, never distinguishes WouldBlock |

**Keep (sole proofs / distinct, do NOT touch):**
- JSON leak proofs (valgrind/alloc-track): `std_json_leak_{parse_null,number,string}_loop`, `std_json_leak_stress_parse_loop`(+`_drop_only`), `jsonnode_{object,string,string_dynamic}_drop_no_leak`, `hashmap_drop_dynamic_string_jsonnode_no_leak`, `hashmap_resize_jsonnode_no_leak`, `hashmap_jsonnode_duplicate_{get,insert}_no_double_free`.
- JSON canonical pins: `std_json_parse_{policy,number_forms,reject_nonfinite,large_number_raw,error_position,invalid_syntax_tag,into_wrappers}`, `std_json_canonical`, `std_json_located`(+`_invariant`), `std_json_rfc_strings_limits`, cursor/entries/wrapper/cfg-builder set.
- **fs/reload (all sole-proof):** `driver/test_std_fs_read_dir.py` (read_dir + multiple named UAF/lost-wake/stale-token regressions), `test_reload_coordinator.py` (drop-outside-lock regression), `test_runtime_selection_sentinel.py`, `test_driftc_wrapper_env_modes.py` (DT_NEEDED dep-leak + `--sanitize`).
- **Diagnostic/exception JSON lane** (only *consumes* std.json as an oracle — no overlap with the data lane): `test_exception_{params,context,envelope}_json.py`, `test_bounds_check_params_json.py`(+`_escape.py`), `test_diagnostic_json_helpers.py`, `test_std_json_parser_policy_api.py`, `test_std_json_regressions.py`.
- `env_drift_tmp_path_sanitization` — **security** regression (path-traversal sanitizer), not the `drift_io_open` leak.

**Cleanup flags (not dedup):** `test_diagnostic_json_helpers.py` has a dead `_SLICE_5_PENDING = xfail(strict=True)` never applied; optional rename `test_driftc_typeenv.py` → `_ssa_type_env.py` (tests SSA TypeEnv, not OS env).

### (6) Core language surface

Scope: arrays/strings/closures/generics/traits/structs/imports/packages/casts.
**SAMPLED, not exhaustive** — concentrated on import/const, string len/eq/byte_at,
interface call, array basics, the `om_*` matrix, and the trait resolution cluster.
Generics, structs, casts/narrow-int, closures, and the full trait/packages dirs
were NOT exhaustively read and may hold further (B)-pattern clusters → a follow-up
sampling pass is warranted before any sweep here.

**Fixture clusters (e2e):**

| test path | covered contract | overlap reason | rec | risk | validation if changed |
|---|---|---|---|---|---|
| `e2e/import_alias/` | import symbol via alias, exit 42 | ⚠️ **NOT byte-identical** (earlier "diff rc=0" claim was WRONG — verified 2026-06-18): `main.drift` identical, but `lib.drift` differs by whitespace (one-line vs multi-line `add` body, same logic) and `expected.json` differs only in `description` (same exit 42). **Semantic duplicate** — `import_basic_one_symbol/` covers the same contract (`import lib as lib` where alias==module, single-symbol call, exit 42) | **semantic-duplicate / Batch 1b candidate** (NOT 1a, NOT byte-identical) — remove only after explicitly confirming `import_basic_one_symbol` covers the import-alias contract | low | confirm survivor asserts the alias-import contract, then run e2e import suite |
| `e2e/const_import_basic/` | pub const via module alias, exit 42 | differs from `const_module_alias_access/` only in alias name; latter's `x` alias is the non-degenerate case | **merge** (keep `const_module_alias_access`) | low | run e2e import suite |
| `e2e/string_len_fn/` + `string_len_literal/` | `.len` / `.byte_length()` on a literal | same underlying accessor | **merge** into one asserting both agree | low | run e2e string suite |
| `e2e/string_eq_id/` | `==` on literals wrapped in `id(...)` | same `==` contract as `string_eq_basic/`; `id()` adds an rvalue-operand shape | **keep** (lean) — distinct operand path, cheap | low | n/a |
| `e2e/interface_dynamic_call_nothrow/` | nothrow interface method | misnomer — calls impl on the concrete value (static dispatch), same as `interface_call_nothrow/`; not actually dynamic | **rename+fix to real dynamic** (dispatch through an `I`-typed param like `interface_dynamic_call_throw`) so the nothrow vtable path is genuinely covered — **strengthens** coverage rather than removing | med | run e2e interface suite; confirm vtable path emitted |

**Phase-structural duplication (driver/trait):**

| test path | covered contract | overlap reason | rec | risk | validation if changed |
|---|---|---|---|---|---|
| `driver/test_trait_method_dot_calls.py` | `use trait` gating of dot-calls (pos+neg) | overlaps `test_trait_method_resolution.py`, which pins the same gating **plus** `fn_id`/resolution internals (stronger boundary) | **merge** overlapping cases into the resolution file; keep only cases it lacks (verify case-by-case) | med | `pytest test_trait_method_resolution.py test_trait_method_dot_calls.py test_use_trait_resolution.py` |

**Keep-list (explicitly NOT targets):**
- All **51 `om_*`** fixtures — auto-generated by `__ownership_matrix__/_gen.py` ("DO NOT EDIT"); static-vs-heap_concat split is load-bearing (no-drop literal vs droppable Arc); motivated by a shipped UAF; protected by ownership-lattice change bar.
- Array-mutator basics (`array_push_pop_basic`, `array_insert_remove_basic`, `array_swap_remove_basic`, `std_array_truncate`, `std_array_extend`, `std_array_remove_range*`) — distinct intrinsic lowerings.
- All `packages/*` (38) + e2e `pkg*`/`reexport_*`/`trait_cross_module_*` — emit→consume→link→run is a distinct boundary; `reexport_{smoke,const_smoke,type_smoke}` pin different namespaces (value/const/type).
- `interface_call_throw` vs `interface_dynamic_call_throw` — static vs real vtable+unwind. `string_byte_at_method` vs `string_byte_at_rvalue` — method vs free-fn surface. `optional_on_none_throw` e2e (runtime) vs driver (checker-accept) — different boundaries.
- RESOLVED-LANGUAGE_BUG guards (`test_borrow_in_cast_no_double_free.py`, `test_array_discarded_mutator_result.py`, `test_hidden_lambda_arm_binder_collision.py`, `test_stored_lambda_in_match_arm.py`, `test_array_intrinsic_method_name_collision_chain_dup.py`, …).

---

## Cross-cutting summary & sequencing

### Tally (candidates, not yet actioned — static audit only)

**CORRECTED post-review (Findings #1, #2, #4).**

| domain | remove (immediate) | merge | rename/convert | HELD (root-cause pass) |
|---|---:|---:|---:|---:|
| Match-family | 4 | 1 | — | — |
| Exceptions | 0 | 0 (+2 borderline) | — | 3 ("twice" → §5a cluster) |
| Ownership/sanitizer | 0 | 0 | 3 notes | — |
| Concurrency | 2 | 1 | — | — |
| JSON/reload/fs-io | ~9 | ~6 | 2 + cleanups | **~18 crash-min** |
| Core language | 1 | 3 | 1 (rename-to-dynamic) | — (sampled) |
| **Total** | **~16** | **~11** | — | **~18–21** |

**Corrected headline:** the *safe immediate* dedup is **~25 of ~1730 (~1.5%)**, not
~45–50. The ~18 JSON crash-min "ladder" previously counted as removals are
**reclassified to HELD** — they pin an ownership/drop lowering defect class
(`a32fa743`: keepalive double-free of refcounted `Result::Err` payloads), NOT JSON
semantics, with no leak coverage; they need the per-fixture root-cause pass below
before any move. Mature safety suites stay near-zero (already deduped by tier).

### Recommended sequencing — RESTRUCTURED per Finding #2

**Batch 1a — truly byte-identical source/expected (verified by `diff`):**
- `e2e/concurrent_cancel_before_start_race_stress_diagnostic/` — `main.drift` `diff`-clean vs `_race_stress/`; merge the stdout/stderr `expected.json` keys into the survivor FIRST, then delete. ✅ **DONE 2026-06-18** (see Implementation log).
- ~~`e2e/import_alias/`~~ — **moved to 1b**: NOT byte-identical (only `main.drift` matches; `lib.drift`/`expected.json` differ cosmetically). Semantic duplicate, needs survivor confirmation.

**Batch 1b — strict-subset, requires EXPLICIT survivor confirmation (NOT zero-risk):**
Safe only if the survivor covers the *same boundary*; name the survivor assertion first.
- `e2e/concurrent_spawn_infers/` — only after confirming `_coerce_callback` asserts the same lambda→callback0 **inference** path (not just runs).
- `e2e/std_io_file_read_write/` — confirm `_file_builder_read_write_api/` asserts the same read+write values, not just builder API shape.
- `e2e/json_handle_share_access/`, `_clone_read/` — confirm `json_handle/` asserts the same accessor/clone-read results.
- match-family `scalar_match_const_local_value` — confirm `_local_shadows_module` exercises the **signed-local lexical** path (Finding #3).
- core-language `const_import_basic`, `string_len_*` — confirm survivor asserts the union.
- `e2e/import_alias/` — semantic duplicate (NOT byte-identical); confirm `import_basic_one_symbol/` asserts the alias-import contract before removal.

**Batch 2 — merges of pure-input-variation fixtures (assert union in survivor):**
- JSON encode-with-config (3), fn_get typed paths (4→1), duplicate-key behavior cluster, env get_set/unset.
- `e2e/std_io_configured_read_line_api_shape/` — **moved out of Batch 1** (Finding #2): it is compile/**API-shape** coverage; runtime `std_io_stdin_line_edge_matrix` may not prove the same *public API shape* → keep unless a survivor explicitly asserts that shape.
- scalar-const merges (signedness mirror, after-default).

**Batch 3 — judgement calls (medium risk, per-case validation):**
- Trait `use trait` dot-call consolidation into the resolution file.
- `interface_dynamic_call_nothrow` → rename+fix to genuine dynamic dispatch (strengthens).
- The two exceptions `_rejected` convert-to-unit (only if e2e budget matters).

**Separate review pass (NOT a batch) — JSON crash-min / "Err twice" cluster (~18–21):**
Per Finding #1, build the per-fixture root-cause column (defect → does a kept
sibling pin the **same drop path at the same/stronger boundary, with leak
coverage**). **Default = keep; preferred action = upgrade representatives to
`alloc_track_leak`** (turns crash-only repros into leak/double-free proofs). No
deletion until that pass clears each fixture individually. Treat as
LANGUAGE_BUG-adjacent under constraint #1.

### Recommended FIRST step (very small, per review)

1. ~~`e2e/import_alias/`~~ — **NOT byte-identical (premise corrected); held for Batch 1b.**
2. `e2e/concurrent_cancel_before_start_race_stress_diagnostic/` — merge `expected.json` keys into `_race_stress/`, then delete. ✅ **DONE 2026-06-18.**
3. `e2e/concurrent_spawn_infers/` — **only after** confirming `_coerce_callback` asserts the same inference path. (not started)

Hold all JSON-crash-min for the separate review pass. Do not start Batch 1b / 2
until this FIRST step is reviewed.

### Scope limits reaffirmed (Finding #4)

Core-language stays **sampled**. No broad core cleanup (generics/structs/casts/
closures/traits/packages) until a dedicated second audit pass covers them.
Implementation there is limited to survivor-confirmed subsets — nothing inferred
from the sample. (`import_alias` turned out semantic-only, not byte-identical → 1b.)

---

## Implementation log

### 2026-06-18 — first step (partial: only the truly byte-identical candidate)
Baseline `f46c9eb7`. Static-audit constraint relaxed ONLY for the two scoped items
below; nothing else touched. Git staging left to the user (used `rm`, not `git rm`).

**Identity checks recorded (per Finding #1 / step instruction):**
- `import_alias/` vs `import_basic_one_symbol/` — **NOT byte-identical.** `main.drift`
  sha256 identical (`d6070967…`); `lib.drift` DIFFERS (whitespace: one-line vs
  multi-line `add` body, identical logic); `expected.json` DIFFERS (only
  `description` string; same exit 42). → Reclassified to **semantic-duplicate /
  Batch 1b**; **NOT removed** this pass (user-confirmed).
- `concurrent_cancel_before_start_race_stress_diagnostic/` vs
  `concurrent_cancel_before_start_race_stress/` — `main.drift` **byte-identical**
  (`diff` rc=0). `_diagnostic`'s `expected.json` adds stricter `stdout:""` +
  `stderr:""` (survivor had only `exit_code:0`).

**Changes made:**
- **Edited** `concurrent_cancel_before_start_race_stress/expected.json` — merged the
  stricter `stdout:""` + `stderr:""` assertions (+ a description noting the
  absorption). Survivor is now STRICTER than before.
- **Removed (working tree)** `concurrent_cancel_before_start_race_stress_diagnostic/`
  (`main.drift`, `expected.json`) via `rm -rf`. `git status`: ` D` both files +
  ` M` the survivor's expected.json.

**Stale-index check:** none — the e2e runner discovers cases via
`case_root.iterdir()` at runtime (`runner.py:1031`); no persistent manifest lists
fixture names (grep hits for `import_alias`/`concurrent_*` in other `.py` are
unrelated import-alias *language* tests). No cache to invalidate.

**Validation:** `python -m lang.tests.codegen.e2e.runner import_basic_one_symbol
concurrent_cancel_before_start_race_stress` → **2/2 passed** (survivor passes WITH
the new stricter stdout/stderr assertions).

**Not done (held):** `import_alias/` (1b — premise corrected), all JSON crash-min.

### 2026-06-18 — first step (cont.): concurrent_spawn_infers merge
**Evidence (read-only check):** `concurrent_spawn_infers/` vs
`concurrent_spawn_coerce_callback/` — the `conc.spawn(| | => { return 7; })` call is
identical in both; after whitespace-normalization the only source differences are
the join-result binding (`var res` vs `val r`; neither reassigns → no path
difference), the match scrutinee name, and a `description` key on `infers`. Both
exercise the **same `conc.spawn` overload, the same lambda→callback0 coercion, and
the same `VirtualThread<Int>` result-type inference** from the lambda return. No
unique inference behavior in `infers`; neither has stricter assertions (both assert
only `exit_code:7`, no stdout/stderr/diagnostics). Decisive functional duplicate.

**Changes made:**
- **Edited** `concurrent_spawn_coerce_callback/expected.json` — added a description
  explicitly naming both facets (lambda→callback coercion + `VirtualThread<Int>`
  result inference from the lambda return), so the absorbed intent is documented.
- **Removed (working tree, `rm`)** `concurrent_spawn_infers/` (`main.drift`,
  `expected.json`). `git status`: ` D` both + ` M` survivor's expected.json.

**Validation:** `concurrent_spawn_coerce_callback` → **1/1 passed** (exit 7).

**Still held:** `import_alias/` (Batch 1b), all JSON crash-min (separate root-cause pass).

### 2026-06-18 — Batch 1b: import_alias merge (survivor-confirmed)
**Evidence (read-only check):** `import_alias/` vs `import_basic_one_symbol/` —
`main.drift` **byte-identical** (both `module main; import lib as lib;` …
`lib.add(40, 2)`); `lib.drift` whitespace-only diff (identical `add` logic);
`expected.json` assertions **identical** (`exit_code:42`, `stdout:""`, `stderr:""`),
only `description` differed. **Survivor confirmation: `import_basic_one_symbol`
explicitly uses `as` (`import lib as lib;`)** → it covers the alias-import syntax
path identically; the default-to-keep guard ("if survivor lacks `as`") was NOT
triggered. Neither fixture exercises a non-trivial alias (alias == module name);
that path lives in other fixtures (`const_module_alias_access` `as x`, qualified-
const `as tok`), so removal loses no alias coverage. No stricter assertions either
way.

**Changes made:**
- **Edited** `import_basic_one_symbol/expected.json` — description now explicitly
  states it covers `import lib as lib` alias syntax (absorbed `import_alias`).
- **Removed (working tree, `rm`)** `import_alias/` (`main.drift`, `lib.drift`,
  `expected.json`). `git status`: ` D` three files + ` M` survivor's expected.json.

**Validation:** `import_basic_one_symbol` → **1/1 passed** (exit 42).

**Still held:** all JSON crash-min (separate root-cause pass).

### 2026-06-18 — IO/env candidates (#1 remove, #2 merge; #3 kept)
**Evidence (read-only checks):**
- `std_io_file_read_write/` vs `std_io_file_builder_read_write_api/` — same public API
  (`file_builder().read().write().create().truncate().mode(FILE_MODE_DEFAULT).timeout().build()`
  + `buffer`/`buffer_write`/`buffer_read` + `FileWriter/Reader.write/read/close`).
  Survivor is **strictly stronger**: it verifies read-back **byte values**
  (`buffer_read==65/66/67`), the candidate only checks the byte **count**. Candidate's
  `expected.json` also used a typo key `"exit"` (runner reads `exit_code`, defaults 0
  — runner.py:401), so its exit assertion was only the default. No unique coverage lost.
- `env_get_set/` (HOME→`Some(non-empty)`) vs `env_get_unset/` (unset→`None`) —
  **complementary arms** of `std.env.get`, not subsets; identical `{"exit_code":0}`.
  → genuine 2→1 merge (both arms must survive).
- `std_io_configured_read_line_api_shape/` vs `std_io_stdin_line_edge_matrix/` —
  the matrix builds its stream via `configured_input_from_file(&f, …)` and **does NOT
  reference `stdin_builder`** (grep-confirmed). api_shape is the **sole** coverage that
  `io.stdin_builder().max_line_bytes(256).timeout(...).build().read_line()` compiles
  (called under `if false`). Per the "runtime stdin alone is not enough" guard → **KEEP**.

**Changes made:**
- **Removed (working tree, `rm`)** `std_io_file_read_write/` — survivor
  `std_io_file_builder_read_write_api/` covers the identical builder/file/buffer API
  shape with stronger byte-value assertions.
- **Added** `env_get_present_and_unset/` (`main.drift` + `expected.json`) — one fixture
  asserting BOTH arms: HOME→`Some(non-empty)` and unset→`None`. New name chosen because
  neither old single-arm name stayed accurate.
- **Removed (working tree, `rm`)** `env_get_set/` and `env_get_unset/` — only AFTER the
  combined fixture passed.
- **KEPT** `std_io_configured_read_line_api_shape/` — sole coverage for the
  `stdin_builder().max_line_bytes().timeout().build().read_line()` API shape; the matrix
  uses a different constructor and does not compile that surface.

**Validation:** `std_io_file_builder_read_write_api` → pass; `env_get_present_and_unset`
→ pass (created, ran green BEFORE the singles were removed, and again after). 2/2.

`git status`: ` D` on the 6 removed files (3 dirs), `??` on `env_get_present_and_unset/`,
no survivor edits needed. Staging left to the user (no git ops).

**Net Batch-1 tally so far:** **6 fixtures removed** —
`concurrent_cancel_before_start_race_stress_diagnostic`, `concurrent_spawn_infers`,
`import_alias`, `std_io_file_read_write`, `env_get_set`, `env_get_unset` — **1 added**
(`env_get_present_and_unset`) → **net −5 fixtures**, all survivors validated green.
**Still held:** all JSON crash-min (separate root-cause pass).

### DO NOT TOUCH (hard exclusions)

1. **Every `*_memcheck.py` (38) and every `alloc_track_*` e2e (36)** — sole leak/UAF/double-free proofs; a functional twin is not a substitute. Never delete a memcheck file "because there's an e2e," never strip `alloc_track_*` keys.
2. **The `om_*` matrix (51)** — auto-generated (`__ownership_matrix__/_gen.py`); static-vs-heap_concat split is load-bearing.
3. **VT ownership matrix** (`test_vt_result_ownership_matrix.py` R2–R8, `test_vt_drop_started_running_uaf.py` R1), registry/atexit Arc leak tests, channel close-race + destructor-deadlock valgrind tests.
4. **Package emit→consume tests** (`packages/*`, `memcheck/test_pkg_*`) — distinct `.dmp` consumer boundary; the only discriminators for the web.rest/bookkeeper cert leaks.
5. **All RESOLVED-LANGUAGE_BUG regressions** across domains (each maps 1:1 to a named root cause in MEMORY.md; no same-root replacement exists).
6. **Equivalence pins** (`stage2/test_drop_policy_standalone_matches_hir_to_mir.py`) — intentional duplication; merging defeats the test.

### Validation protocol for any later implementation slice

Per change: (a) confirm the named survivor pins the same contract at ≥ the same boundary; (b) run the survivor + any cross-referenced structural/memcheck sibling; (c) for any fixture that was the only leak proof, ensure the survivor carries `alloc_track_leak`/valgrind; (d) after a batch, run the full `scalar_match*`/`json*`/affected-domain e2e set + the relevant pytest dirs; (e) keep ABI/version untouched (test-only change).

### Spin-off cleanups surfaced (independent of dedup)

- `e2e/std_io_block_on_write_timeout/` — dead assertion (both Err arms `return 0`); tighten before relying on it.
- `test_diagnostic_json_helpers.py` — dead `_SLICE_5_PENDING = xfail(strict=True)` never applied.
- Optional renames: `test_driftc_typeenv.py` → `_ssa_type_env.py`; `interface_dynamic_call_nothrow` (misnomer).

### Honesty / coverage notes

- Core-language domain was **sampled, not exhaustive** — generics, structs, casts/narrow-int, closures, and the full trait/packages dirs were not fully read and likely hold more (B)-pattern clusters. A focused follow-up sampling pass is warranted before a core-language sweep.
- The JSON `*_local_*` and exceptions "Err twice" findings **overlap** — reconcile as a single drop-path-ladder decision.
- No numbers here are commitments; this is a candidate list for review. Nothing has been edited or deleted.

---

## JSON crash-min root-cause pass (2026-06-18) — read-only

Resolves the HELD §5a cluster **and** the overlapping Exceptions §2 "Err twice"
rows as ONE decision. Read-only; no test files changed.

### Provenance (single source)

All 29 in-scope fixtures were introduced by **one commit, `a32fa743`** ("per work
progress"), whose *compiler* changes were `llvm_codegen.py` + `stage2/hir_to_mir.py`
+ `stage1/hir_nodes.py` — **NOT the JSON parser**. That commit fixed three lowering
defects:
- **(A) keepalive by-value store double-owned refcounted values** (`llvm_codegen.py`
  `_FuncBuilder` keepalive: stored String/struct/variant/interface without
  retain/release balancing → **double-free**; fix restricts keepalive to scalar POD).
- **(B) array-grow join `LoadLocal`→`MoveOut`** (`hir_to_mir.py:3457`) — move-out/drop
  correctness for an array local.
- **(C) `&&`/`||` short-circuit lowering** (`hir_to_mir.py:1339`) — control-flow;
  **not exercised by this JSON/Err cluster** (no fixture here uses `&&`/`||`).

So every fixture below pins **(A)** (and the L-group also cleanup-authoring of a live
local on the early-return Err edge; one array-local fixture also (B)). The contract is
**ownership/drop + control-flow cleanup**, NOT parse semantics — the parse error is
just the vehicle that produces a refcounted `Result::Err` payload to drop.

### Critical coverage gap

No fixture **anywhere** exercises the `Result<…,…>::Err` payload drop with leak
tracking. The `std_json_leak_*parse*` loops parse **valid** JSON and drop on the **Ok**
branch (verified) — they prove Ok-path `JsonNode` drop is leak-free, not the Err path.
So this cluster is the **sole** Err-drop coverage, and it carries **crash-detection
only** (`exit_code:0`) for a **double-free** defect class. ⇒ the right move is to ADD
the missing leak proof (upgrade), not to delete.

### Mapping (by drop-shape group; all `a32fa743`, all `exit_code:0`, all alloc_track=0)

| group | fixtures | exact root cause it pins | subsystem | LANGUAGE_BUG? | primarily proves | same/stronger-boundary survivor? | leak/valgrind survivor? | recommendation |
|---|---|---|---|---|---|---|---|---|
| **J1** parse-Err drop, no/limited access | `json_err_twice_no_access`, `json_err_twice_same_input`, `json_err_once_object`, `json_parse_truncated_object_crash_min`, `parse_err_twice_min`, `json_two_error_parses_with_newlines_min` | (A) double-free of refcounted `JsonErrorData` Err payload on drop (no field access) | codegen keepalive + stage2 cleanup | yes (ownership/double-free class) | drop/ownership safety | NO (parse-semantics survivors are a different boundary) | NO | **keep; upgrade 1 rep w/ alloc_track** |
| **J2** parse-Err drop + field access | `json_err_twice_tag`, `json_err_line_access_crash_min`, `json_err_second_case_only_min`, `json_err_second_case_tag_only_min`, `json_parse_array_err_tag_only_no_crash`, `json_err_with_newline_crash_min`, `json_like_key_err_path_min` | (A) same, but Err `String` fields materialized via `.tag/.line/.col` access before drop | codegen keepalive + stage2 | yes | drop/ownership safety (offsets/tag are incidental) | NO | NO | **keep; upgrade 1 rep** |
| **R1** manual `Result::Err(refcounted)` drop | `result_json_err_drop_crash_min`, `result_jsonnode_err_twice_min`, `result_err_struct_with_strings_twice_min`, `result_err_large_struct_twice`, `string_struct_err_twice_min`, `string_local_then_err_twice`, `ref_plus_string_param_return_struct_twice_min`, `try_wrap_result_err_twice_min` | (A) double-free constructing+dropping `Result::Err(struct/JsonErrorData with String fields)` | codegen keepalive + stage2 | yes | drop/ownership safety | NO | NO | **keep; upgrade 1–2 reps** |
| **R2** Err forward/convert | `result_err_convert_okshape_crash_min`, `result_err_forward_same_type_min`, `canthrow_result_err_forward_crash_min` | (A) + control-flow: Err value forwarded/type-converted across a return edge | stage2 cleanup + codegen | yes | control-flow cleanup + drop | NO | NO | **keep; upgrade 1 rep** |
| **L** early-return Err past LIVE local | `loop_err_return_with_json_local_crash_min`, `cleanup_err_with_jsonnode_local_min`, `cleanup_err_with_noncopy_local_min`, `result_err_convert_with_json_local_crash_min`, `result_err_after_array_local_with_linecol_min` (also **(B)**), `try_wrapper_json_result_crash_min` | drop-on-early-return cleanup of a live `HashMap<String,JsonNode>` / array / non-Copy local + Err payload (cleanup authoring) | stage2 cleanup_authoring (+ (B) array-grow) | yes | control-flow cleanup (highest-value) | NO | NO | **keep; upgrade ≥1 rep** (the `__borrow_tmp`/scope-drop class) |
| **outlier** | `hashmap_jsonnode_empty_twice_min` | trivial: empty `HashMap<String,JsonNode>` `len()==0` twice — no Err, no drop of populated value | containers | weak/no | smoke only | n/a | n/a | defer to JSON-behavior pass (not an Err fixture) |

LANGUAGE_BUG note: none carry a bug-id (commit msg is "per work progress"), but they
guard a genuine codegen **double-free** fixed in `a32fa743`. Per constraint #1 they are
**LANGUAGE_BUG-adjacent**: do not delete without a survivor pinning the same root cause
at the same/stronger boundary **with leak coverage** — which does not exist today.

### Batch JSON-A — ✅ EXECUTED 2026-06-18 (UPGRADES ONLY, zero deletions)

**Done:** added `"alloc_track_leak": true` (+ a provenance note in `description`) to the
7 representative fixtures below — `expected.json` only, no source touched, nothing
deleted/merged. **Validation:** all **7/7 pass under the alloc-track lane** (forced
leak-check every run, runner.py:~824) → exit 0 + **no leak**. So the `a32fa743`
keepalive double-free class now has genuine leak/double-free coverage (previously
crash-only); no remaining LANGUAGE_BUG surfaced.

Upgraded: `json_err_twice_no_access`, `json_err_twice_tag`, `result_json_err_drop_crash_min`,
`result_err_struct_with_strings_twice_min`, `result_err_convert_okshape_crash_min`,
`loop_err_return_with_json_local_crash_min`, `result_err_after_array_local_with_linecol_min`.

**Key rule recorded:** parse-semantics tests (`std_json_parse_error_position` / `_tag`
/ `_policy`) are **NOT valid survivors** for the Err-drop ownership contract — they hit
the parser boundary and never construct+drop a `Result::Err`. Err-drop coverage lives
only in this cluster (now leak-proofed at the representatives above).

**JSON crash-min deletion: still DEFERRED** to Batch JSON-B. The leak proofs are now
green, which *unblocks* JSON-B, but deletion is out of scope for this upgrade-only pass
and must be evaluated per-fixture (a sibling may only be retired if it pins no distinct
control-flow shape beyond an upgraded representative).

git: ` M` on the 7 `expected.json` files only. Staging left to the user.

---

#### (original proposal, for reference)

Rationale: the defect class is a double-free with **zero** current leak coverage;
strengthen first. Add `"alloc_track_leak": true` to one representative per drop-shape
so the Err-drop path gains leak/double-free proof on every run (the runner forces
leak-checking on alloc_track fixtures):

- J1 → `json_err_twice_no_access`
- J2 → `json_err_twice_tag`
- R1 → `result_json_err_drop_crash_min` **and** `result_err_struct_with_strings_twice_min`
- R2 → `result_err_convert_okshape_crash_min`
- L  → `loop_err_return_with_json_local_crash_min` **and** `result_err_after_array_local_with_linecol_min` (covers (B) too)

**No deletions in JSON-A.** **Risk:** low — adding `alloc_track_leak` only strengthens;
the worst case is it *surfaces a latent leak* the crash-only test missed (a desirable
finding to investigate, not mask). **Validation when executed:** run each upgraded
fixture (alloc-track lane runs leak-check every run) → expect exit 0 + 0 leaks; any
leak is a real defect to file, not to paper over.

### Deferred Batch JSON-B (after JSON-A lands & is green)

Only once each shape has a leak-proof representative, evaluate **merges/deletions** of
the redundant same-shape siblings — decisive only when a sibling pins no distinct
control-flow shape beyond an upgraded representative. Expected candidates: the J1/J2
"twice" duplicates (incl. the Exceptions §2 rows `json_err_twice_same_input`,
`result_err_large_struct_twice`, `json_parse_array_err_tag_only_no_crash`) and the
trivial `hashmap_jsonnode_empty_twice_min`. **This supersedes the earlier Exceptions §2
"merge" recommendation** — those rows are Err-drop ownership fixtures, not JSON/exception
*behavior* dupes, so they follow the JSON-A/B sequence, not a behavior merge.

---

## Batch JSON-B — ✅ EXECUTED 2026-06-18 (10 decisive same-shape removals)

**Done:** removed the 10 decisive same-shape siblings (working-tree `rm`, no git ops).
The **12** held-back distinct-path fixtures and `hashmap_jsonnode_empty_twice_min` were
**not touched**; every group retains its JSON-A leak-proof rep. (Count correction: an
earlier note said "11 held-back" — that was an arithmetic slip; the enumerated list is
**12**, matching the JSON-A2 scope. See JSON-A2 below.)

**Removed (10):** `json_err_twice_same_input`, `json_err_once_object`,
`json_err_with_newline_crash_min`, `json_two_error_parses_with_newlines_min`,
`json_err_line_access_crash_min`, `json_err_second_case_only_min`,
`json_err_second_case_tag_only_min`, `json_parse_array_err_tag_only_no_crash`,
`result_jsonnode_err_twice_min`, `result_err_large_struct_twice`.

**Held-back list (UNCHANGED):** `json_parse_truncated_object_crash_min`,
`parse_err_twice_min`, `string_struct_err_twice_min`, `string_local_then_err_twice`,
`ref_plus_string_param_return_struct_twice_min`, `try_wrap_result_err_twice_min`,
`result_err_forward_same_type_min`, `canthrow_result_err_forward_crash_min`,
`cleanup_err_with_jsonnode_local_min`, `cleanup_err_with_noncopy_local_min`,
`result_err_convert_with_json_local_crash_min`, `try_wrapper_json_result_crash_min`,
+ `hashmap_jsonnode_empty_twice_min` (deferred).

**Validation:** reps `json_err_twice_no_access`, `json_err_twice_tag`,
`result_json_err_drop_crash_min`, `result_err_struct_with_strings_twice_min` +
survivors `std_json_parse_error_position`, `std_json_parse_invalid_syntax_tag` →
**6/6 passed** (reps green incl. alloc-track leak-clean; parse-semantics retained).
git: ` D` on 20 removed files (10 dirs); staging is the user's.

**Running cluster tally:** JSON-A upgraded 7 reps (leak-proof); JSON-B removed 10
siblings → **−10 fixtures** with all Err-drop shapes still leak-proofed + parse-semantics
retained.

---

#### (original static review, for reference)

## Batch JSON-B — static review (2026-06-18) — read-only, NOTHING deleted

Now that JSON-A leak-proofed one representative per drop-shape, this pass classifies
each remaining sibling as **decisive-remove** (same shape as an existing leak-proof
rep, no distinct control-flow; parse-semantics — if any — covered elsewhere) vs
**keep/upgrade** (distinct cleanup path or no leak-proof rep for that shape yet).
Leak-proof reps per group (from JSON-A) are retained in every case.

Survivor facts established (read-only):
- Ownership/Err-drop survivors = the JSON-A leak-proof reps (NOT parse-semantics tests).
- Parse-semantics survivor `std_json_parse_error_position` asserts BOTH error inputs in
  full: `{\n"a":}\n` → tag=invalid-syntax, offset=6, line=2, col=5; `[1,\n2,\n]` →
  offset=7, line=3, col=1. `std_json_parse_invalid_syntax_tag` covers the tag.

### Candidate table

| fixture | group | unique path still covered? | leak-proof rep covers same path? | rec | risk | validation |
|---|---|---|---|---|---|---|
| `json_err_twice_same_input` | J1 | none — two parse-Err discard, both `{` (trivial input variant of rep) | yes — `json_err_twice_no_access` | **remove** | low | run `json_err_twice_no_access` |
| `json_err_once_object` | J1 | none — single parse-Err discard (subset of double) | yes — `json_err_twice_no_access` | **remove** | low | run rep |
| `json_err_with_newline_crash_min` | J1/J2 | none — single parse-Err, no access | yes — `json_err_twice_no_access` | **remove** | low | run rep |
| `json_two_error_parses_with_newlines_min` | J2 | none — two parse-Err + tag; tag-value is parse-semantics | yes — `json_err_twice_tag`; values via `std_json_parse_invalid_syntax_tag` | **remove** | low | run `json_err_twice_tag` + `std_json_parse_invalid_syntax_tag` |
| `json_err_line_access_crash_min` | J2 | none — single parse-Err + line/col (Int, POD) access | yes — `json_err_twice_tag`; values via `std_json_parse_error_position` | **remove** | low | run rep + `std_json_parse_error_position` |
| `json_err_second_case_only_min` | J2 | none — single parse-Err + tag/offset/line/col | yes — rep; values via `std_json_parse_error_position` | **remove** | low | run rep + parse_error_position |
| `json_err_second_case_tag_only_min` | J2 | none — single parse-Err + tag | yes — rep; tag via parse_error_position | **remove** | low | run rep + parse_error_position |
| `json_parse_array_err_tag_only_no_crash` | J2 | none — dup of `_second_case_tag_only_min` | yes — rep | **remove** | low | run rep + parse_error_position |
| `result_jsonnode_err_twice_min` | R1 | none — JsonErrorData Err ×2 via helper | yes — `result_json_err_drop_crash_min` + `result_err_struct_with_strings_twice_min` | **remove** | low | run both reps |
| `result_err_large_struct_twice` | R1 | none — custom struct (3 String) Err ×2 | yes — `result_err_struct_with_strings_twice_min` | **remove** | low | run rep |
| `json_parse_truncated_object_crash_min` | J1 | **yes** — drops the whole *un-matched* `Result` (`val _r = parse()`), not a matched Err binding | no (rep drops via match arm) | **keep** (upgrade candidate) | — | — |
| `parse_err_twice_min` | J1 | **yes** — `std.parse.parse_int` Err (different error type/module, not json) | no | **keep** | — | — |
| `string_struct_err_twice_min` | R1 | **yes** — plain struct-with-String LOCAL drop ×2, no `Result`/match | no | **keep** (upgrade candidate) | — | — |
| `string_local_then_err_twice` | R1 | **yes** — early-return Err past a live `String` local (`out`) | no | **keep** (upgrade candidate) | — | — |
| `ref_plus_string_param_return_struct_twice_min` | R1 | **yes** — struct return-by-value + `String` by-value param consumed (keepalive on return) | no | **keep** (upgrade candidate) | — | — |
| `try_wrap_result_err_twice_min` | R1 | **yes** — Err produced via `try ... catch` (control-flow) | no | **keep** (upgrade candidate) | — | — |
| `result_err_forward_same_type_min` | R2 | **yes** — Err forwarded via match-rebind (`Err(e) => return Err(e)`), same type | uncertain (rep is convert-ok-shape) | **keep** | — | — |
| `canthrow_result_err_forward_crash_min` | R2 | **yes** — can-throw fn + whole-`Result` discard forward | uncertain | **keep** | — | — |
| `cleanup_err_with_jsonnode_local_min` | L | **yes** — **straight-line** early-return Err past live `HashMap<String,JsonNode>` (rep is the LOOP version) | no (loop rep ≠ straight-line cleanup) | **keep** (upgrade candidate — straight-line L rep) | — | — |
| `cleanup_err_with_noncopy_local_min` | L | near-dupe of `_jsonnode_local_min` (empty-HashMap drop; value type irrelevant when empty) | no leak-proof straight-line rep yet | **keep for now** → remove after a straight-line rep is upgraded | low-after-upgrade | — |
| `result_err_convert_with_json_local_crash_min` | L | **yes** — straight-line early-return + Ok-path `move fields` + whole-Result discard | no | **keep** (upgrade candidate) | — | — |
| `try_wrapper_json_result_crash_min` | L | **yes** — `try/catch` wrapping the early-return-past-live-local shape | no | **keep** (upgrade candidate) | — | — |
| `hashmap_jsonnode_empty_twice_min` | outlier | trivial `len()==0` ×2; no Err, no populated drop | n/a (not an Err fixture) | **defer** to JSON-behavior pass | — | — |

### Smallest safe deletion batch (JSON-B, decisive only)

**10 fixtures** — each is the same shape as an existing leak-proof rep, covers no
distinct control-flow, and (where it also asserted parse-semantics) those values are
independently covered by `std_json_parse_error_position` / `std_json_parse_invalid_syntax_tag`:

J1: `json_err_twice_same_input`, `json_err_once_object`
J2: `json_err_with_newline_crash_min`, `json_two_error_parses_with_newlines_min`,
    `json_err_line_access_crash_min`, `json_err_second_case_only_min`,
    `json_err_second_case_tag_only_min`, `json_parse_array_err_tag_only_no_crash`
R1: `result_jsonnode_err_twice_min`, `result_err_large_struct_twice`

Each group keeps its leak-proof rep (J1 `json_err_twice_no_access`, J2 `json_err_twice_tag`,
R1 `result_json_err_drop_crash_min` + `result_err_struct_with_strings_twice_min`).
**Risk: low.** **Required validation when executed:** after removal, run the surviving
leak-proof rep(s) for each group PLUS `std_json_parse_error_position` +
`std_json_parse_invalid_syntax_tag` → confirm green (ownership + parse-semantics retained).

### NOT deleted — distinct paths held for a future JSON-A2 upgrade pass

**12** fixtures cover a distinct cleanup/control-flow shape with **no** leak-proof rep yet
(corrected count — was mis-stated as "11"):
the whole-Result discard (`json_parse_truncated_object_crash_min`), non-json error type
(`parse_err_twice_min`), struct-local-no-Result (`string_struct_err_twice_min`),
live-String-local-on-Err-return (`string_local_then_err_twice`), return-value+param
keepalive (`ref_plus_string_param_return_struct_twice_min`), try/catch-produced Err
(`try_wrap_result_err_twice_min`), match-rebind forward (`result_err_forward_same_type_min`,
`canthrow_result_err_forward_crash_min`), **straight-line** early-return-past-live-local
(`cleanup_err_with_jsonnode_local_min` (+ near-dupe `cleanup_err_with_noncopy_local_min`),
`result_err_convert_with_json_local_crash_min`), and try/catch-wrapped L
(`try_wrapper_json_result_crash_min`). Per the rules, these are **upgrade-before-delete**
candidates, NOT deletions. `hashmap_jsonnode_empty_twice_min` is deferred to the JSON
behavior pass (not an Err fixture).

---

## Batch JSON-A2 — ✅ EXECUTED 2026-06-18 (7 upgrades, upgrade-only)

**Done:** added `"alloc_track_leak": true` (+ provenance note) to the 7 representatives
below — `expected.json` only; no source, no deletions, no merges. The 5 do-not-upgrade
fixtures (`parse_err_twice_min`, `canthrow_result_err_forward_crash_min`,
`cleanup_err_with_noncopy_local_min`, `result_err_convert_with_json_local_crash_min`,
`try_wrapper_json_result_crash_min`) were left at `alloc_track=0` (verified before & after).

**Upgraded (7):** `json_parse_truncated_object_crash_min` (A), `string_struct_err_twice_min`
(C), `string_local_then_err_twice` (D), `ref_plus_string_param_return_struct_twice_min`
(E), `try_wrap_result_err_twice_min` (F), `result_err_forward_same_type_min` (G),
`cleanup_err_with_jsonnode_local_min` (H).

**Validation:** all **7/7 pass under the alloc-track lane** → exit 0 + **0 leaks**. No
LANGUAGE_BUG surfaced. Each distinct Err-drop/cleanup shape (A,C,D,E,F,G,H) now has a
leak-proof representative. git: ` M` on the 7 `expected.json`; staging is the user's.

**JSON-C (deletions/merges) — still DEFERRED** (now unblocked, out of scope here).
Candidates: `canthrow_result_err_forward_crash_min` (→G), `cleanup_err_with_noncopy_local_min`
(→H), `result_err_convert_with_json_local_crash_min` (→H), `try_wrapper_json_result_crash_min`
(→F+H), + `hashmap_jsonnode_empty_twice_min` (JSON-behavior pass). `parse_err_twice_min`
is NOT a JSON-C candidate (kept as std.parse surface test; ownership covered by the J1 rep).

**Cluster tally:** JSON-A (7) + JSON-A2 (7) = **14 leak-proof reps**; JSON-B removed 10 →
net **−10 fixtures**, all Err-drop/cleanup shapes leak-proofed, parse-semantics retained.

---

#### (original static review, for reference)

## Batch JSON-A2 — static review (2026-06-18) — read-only, NOTHING edited

**Count discrepancy resolved:** the user's list is **12** fixtures; the JSON-B prose
said "11 held-back" — that was an arithmetic slip. Correct held-back distinct-path
count = **12** (enumerated below). `hashmap_jsonnode_empty_twice_min` is separate
(deferred outlier), not in the 12.

Goal: smallest upgrade batch — add `alloc_track_leak` only to the representatives
needed to leak-proof each **distinct runtime drop/cleanup shape**. None of the 12 has a
leak-proof survivor today (all are `exit_code:0` crash-only). Grouped by runtime shape,
several collapse to one rep (the others become JSON-C merge candidates *after* the rep
is leak-proof). Verified facts: `ParseError` is `{tag: String, offset: Int}` (String =
droppable); `try … catch` on a returned `Result::Err` triggers or_throw→catch at runtime.

### Mapping (12 fixtures → 7 distinct shapes)

| fixture | distinct shape | leak-proof survivor today? | near-dupe of | recommendation |
|---|---|---|---|---|
| `json_parse_truncated_object_crash_min` | **A** drop whole *un-matched* `Result` (`val _r = parse()`) via value-drop glue | no | — | **UPGRADE** (rep for A) |
| `string_struct_err_twice_min` | **C** struct-with-`String` LOCAL drop ×2, no `Result`/match | no | — | **UPGRADE** (rep for C) |
| `string_local_then_err_twice` | **D** early-return Err past a live `String` local | no | — | **UPGRADE** (rep for D) |
| `ref_plus_string_param_return_struct_twice_min` | **E** struct return-by-value + by-value `String` param consumed (keepalive on return value) | no | — | **UPGRADE** (rep for E) |
| `try_wrap_result_err_twice_min` | **F** Err via `try/catch` (or_throw unwind + catch-constructed Err) | no | `try_wrapper_json_result_crash_min` (F+H combo) | **UPGRADE** (rep for F) |
| `result_err_forward_same_type_min` | **G** Err forwarded via match-rebind (`Err(e)=>return Err(e)`) | no | `canthrow_result_err_forward_crash_min` | **UPGRADE** (rep for G) |
| `cleanup_err_with_jsonnode_local_min` | **H** straight-line early-return Err past live `HashMap<String,JsonNode>` | no | `cleanup_err_with_noncopy_local_min`, `result_err_convert_with_json_local_crash_min` | **UPGRADE** (rep for H) |
| `canthrow_result_err_forward_crash_min` | G + can-throw fn + whole-`Result` discard | no (G rep covers after upgrade) | `result_err_forward_same_type_min` | keep → **JSON-C** merge after G rep leak-proof |
| `cleanup_err_with_noncopy_local_min` | H with `HashMap<String,E>` (empty-map drop; value type irrelevant when empty) | no (H rep covers after upgrade) | `cleanup_err_with_jsonnode_local_min` | keep → **JSON-C** merge |
| `result_err_convert_with_json_local_crash_min` | H + Ok-path `move fields` (NOT executed when Err taken → runtime-equiv to H) + whole-`Result` discard | no (H rep covers runtime; Ok-move is compile-only delta) | `cleanup_err_with_jsonnode_local_min` | keep → **JSON-C** (note Ok-move compile coverage) |
| `try_wrapper_json_result_crash_min` | J = F + H (try/catch wrapping straight-line early-return-past-HashMap) | no (F+H reps cover jointly after upgrade) | `try_wrap_result_err_twice_min` | keep → **JSON-C** merge |
| `parse_err_twice_min` | drop `Result<Int, ParseError>::Err` ×2 — `ParseError` is String-bearing ⇒ **ownership-equivalent to J1 rep** `json_err_twice_no_access` (already leak-proof). Unique value = `std.parse` API surface, NOT a distinct drop shape | **YES** (J1 rep covers the drop; this is a parse-API surface test) | — | **KEEP** as `std.parse` surface test; **no upgrade needed** |

### Proposed JSON-A2 upgrade batch (smallest — 7 reps, one per distinct shape)

Add `"alloc_track_leak": true` (+ provenance note) to:
1. `json_parse_truncated_object_crash_min` — shape A (whole un-matched Result drop)
2. `string_struct_err_twice_min` — shape C (struct-with-String local drop, no Result)
3. `string_local_then_err_twice` — shape D (live String local on Err-return)
4. `ref_plus_string_param_return_struct_twice_min` — shape E (return-value keepalive + param consume)
5. `try_wrap_result_err_twice_min` — shape F (Err via try/catch)
6. `result_err_forward_same_type_min` — shape G (Err forward via match-rebind)
7. `cleanup_err_with_jsonnode_local_min` — shape H (straight-line early-return past live HashMap)

**No deletions in JSON-A2.** **Risk:** low — upgrade only strengthens; a surfaced leak
is a real LANGUAGE_BUG to file, not mask. **Validation when executed:** run each of the
7 under the alloc-track lane → expect exit 0 + 0 leaks; any leak → stop & report.

`parse_err_twice_min` stays as-is (surface test; ownership already covered by J1 rep).

### Deferred JSON-C (deletions/merges — only AFTER the 7 reps are leak-proof & green)

Candidates, each now covered at runtime by an upgraded rep above:
`canthrow_result_err_forward_crash_min` (→ G), `cleanup_err_with_noncopy_local_min` (→ H),
`result_err_convert_with_json_local_crash_min` (→ H; weigh the Ok-path-move compile
delta), `try_wrapper_json_result_crash_min` (→ F+H). Plus the deferred
`hashmap_jsonnode_empty_twice_min` (JSON-behavior pass). Decisive only per-fixture once
the reps are green; `parse_err_twice_min` is NOT a JSON-C candidate (kept as surface test).

### Near-dupe pairings (for JSON-C)

- **G:** `result_err_forward_same_type_min` ↔ `canthrow_result_err_forward_crash_min`
- **H:** `cleanup_err_with_jsonnode_local_min` ↔ `cleanup_err_with_noncopy_local_min` ↔ `result_err_convert_with_json_local_crash_min` (runtime-equiv)
- **F/J:** `try_wrap_result_err_twice_min` ↔ `try_wrapper_json_result_crash_min`

---

## Batch JSON-C — ✅ EXECUTED 2026-06-18 (3 decisive removals)

**Done:** removed the 3 decisive JSON-C candidates (working-tree `rm`, no git ops):
`canthrow_result_err_forward_crash_min` (G+A), `cleanup_err_with_noncopy_local_min`
(H near-dupe), `hashmap_jsonnode_empty_twice_min` (container test).

**Held back (NOT removed — medium-risk combos, still eligible for a follow-on):**
`result_err_convert_with_json_local_crash_min`, `try_wrapper_json_result_crash_min`.

**Excluded (NOT a JSON-C candidate):** `parse_err_twice_min` (std.parse API surface;
ownership covered by the J1 rep) — confirmed present, untouched.

**Validation:** survivors/proofs `result_err_forward_same_type_min`,
`json_parse_truncated_object_crash_min`, `cleanup_err_with_jsonnode_local_min`,
`hashmap_basic`, `hashmap_scope_exit_no_leak` → **5/5 passed**; the four alloc-track
ones are **leak-clean**. git: ` D` on 6 removed files (3 dirs); staging is the user's.

**Cluster tally (actual):** JSON-A(7)+JSON-A2(7)=14 leak-proof reps; removed JSON-B(10)
+ JSON-C(3) = **−13 fixtures**. 2 medium combos still held; all Err-drop/cleanup shapes
leak-proofed; parse-semantics + std.parse surface retained.

---

#### (original static confirmation, for reference)

## Batch JSON-C — static confirmation (2026-06-18) — read-only, NOTHING deleted

Confirms which of the 5 JSON-C candidates are now redundant given the JSON-A/A2
leak-proof reps. Ownership survivors only (no parse-semantics tests). A combo fixture
is removable ONLY if every constituent shape has a leak-proof rep AND no unique
compile/checker/runtime/API path remains.

Leak-proof reps in play: **G** `result_err_forward_same_type_min` · **H** (straight-line)
`cleanup_err_with_jsonnode_local_min` · **A** (whole-Result discard) + **Ok-path
move-into-Object** + early-return-past-HashMap all in `loop_err_return_with_json_local_crash_min`
and `json_parse_truncated_object_crash_min` (A) · **F** (try/catch) `try_wrap_result_err_twice_min`.
Container survivors: `hashmap_basic` (fresh `len()==0`) · `hashmap_scope_exit_no_leak`
(alloc-track HashMap scope drop).

### Confirmation table

| candidate | shape(s) | leak-proof rep(s) covering it | unique compile/checker/runtime/API path? | rec | risk | validation if removed |
|---|---|---|---|---|---|---|
| `canthrow_result_err_forward_crash_min` | G (Err forward via match-rebind) + A (`val _r=` whole-Result discard) | G `result_err_forward_same_type_min` (also has a non-nothrow forwarder → can-throw adds no checker delta) + A `json_parse_truncated_object_crash_min`/`loop_err_return` | **none** — sequential G then A; both leak-proofed | **remove** | low | run `result_err_forward_same_type_min` + `json_parse_truncated_object_crash_min` |
| `cleanup_err_with_noncopy_local_min` | H (straight-line early-return past **empty** HashMap), value `E` (custom struct) vs `JsonNode` | H `cleanup_err_with_jsonnode_local_min` | **none** — map is empty at the Err return ⇒ value type never drops; HashMap-with-droppable-value drop glue covered by the JsonNode rep | **remove** | low | run `cleanup_err_with_jsonnode_local_min` |
| `hashmap_jsonnode_empty_twice_min` | not Err-drop: empty `HashMap<String,JsonNode>` + `len()==0` ×2 | drop: `hashmap_scope_exit_no_leak` (alloc-track) + Err reps build/drop `HashMap<String,JsonNode>`; API: `hashmap_basic` asserts fresh `len()==0` | **none** — `len()==0` is value-type-agnostic; json-valued empty-map drop leak-proofed elsewhere | **remove** | low | run `hashmap_basic` + `hashmap_scope_exit_no_leak` |
| `result_err_convert_with_json_local_crash_min` | H (straight-line early-return past HashMap) + A (`_r`) + **Ok-path `move fields`→Object** (dead at runtime — Err taken) | H `cleanup_err_with_jsonnode_local_min`; A + Ok-move-into-Object both in `loop_err_return_with_json_local_crash_min` (leak-proof) | none that lacks a leak-proof rep — BUT the straight-line+Ok-move COMBINATION is split across two reps, not one | **remove (medium)** — eligible per rule; not in the smallest-safe batch | med | run `cleanup_err_with_jsonnode_local_min` + `loop_err_return_with_json_local_crash_min` + `json_parse_truncated_object_crash_min` |
| `try_wrapper_json_result_crash_min` | J = F (try/catch) + H (early-return past HashMap in `f_throwing`) + A (`_r`) + Ok-move | F `try_wrap_result_err_twice_min`; H `cleanup_err_with_jsonnode_local_min`; A + Ok-move `loop_err_return_with_json_local_crash_min` | none that lacks a leak-proof rep — but it's the most-combined fixture (F+H+A) split across three reps | **remove (medium)** — eligible per rule; not in the smallest-safe batch | med | run `try_wrap_result_err_twice_min` + `cleanup_err_with_jsonnode_local_min` + `loop_err_return_with_json_local_crash_min` |

`parse_err_twice_min` remains OUT of JSON-C (std.parse API surface; ownership already covered by J1 rep) — confirmed, no separate reason to include it.

### Smallest safe deletion batch (JSON-C) — 3 decisive (single-shape / container)

1. `canthrow_result_err_forward_crash_min` — G+A, both leak-proof, no unique path
2. `cleanup_err_with_noncopy_local_min` — H near-dupe, empty-map ⇒ value type irrelevant
3. `hashmap_jsonnode_empty_twice_min` — container test; `len()==0` via `hashmap_basic`, drop via `hashmap_scope_exit_no_leak`

**Risk: low.** **Validation when executed:** run the survivors named per row above, expect green (incl. alloc-track leak-clean for the leak-proof reps).

### Held back from the smallest-safe batch (medium-risk combos — eligible, separate step)

`result_err_convert_with_json_local_crash_min` and `try_wrapper_json_result_crash_min`:
every constituent shape (H, A, Ok-move, F) has a leak-proof rep and no unique path
remains, so they ARE removable per the rule — but each fixture's specific *combination*
lives across 2–3 reps rather than one, so they're flagged **medium-risk** and held for an
explicit follow-on rather than the smallest-safe batch. If removed, validate against the
multi-rep set named in their rows.

**Cluster tally (projected after this 3-fixture batch):** JSON-A(7)+JSON-A2(7)=14
leak-proof reps; JSON-B(−10) + JSON-C smallest(−3) = **−13 fixtures**; +2 medium combos
still eligible. All Err-drop/cleanup shapes remain leak-proofed; parse-semantics + std.parse
surface retained.
