# Function-Scope `const` — Implementation Progress

## Status: COMPLETE

## Session 1: Grammar + Parser + AST + Checker + Stage2

### Step 1: Grammar change
- [x] Add `local_const_stmt` rule to `grammar.lark`
- [x] Add to `simple_stmt` alternatives

### Step 2: AST node
- [x] Add `LocalConstStmt` to `parser/ast.py`
- [x] Add `LocalConstStmt` to `stage0/ast.py` (needed for main compilation path)

### Step 3: Parser builder
- [x] Add `_build_local_const_stmt()` to `parser/parser.py`
- [x] Wire into `_build_stmt` dispatch
- [x] Add `_convert_local_const()` to `parser/__init__.py` (stage0 adapter)

### Step 4: HIR node + stage1
- [x] Add `HLocalConst` HIR node to `stage1/hir_nodes.py`
- [x] Wire AST→HIR lowering in `stage1/ast_to_hir.py`
- [x] Export `HLocalConst` from `stage1/__init__.py`
- [x] Handle `HLocalConst` in `stage1/normalize.py` (rewriter, _scan_block, _assign_block)
- [x] Handle `HLocalConst` in `stage1/borrow_materialize.py`
- [x] Handle `HLocalConst` in `stage1/place_canonicalize.py`
- [x] Handle `HLocalConst` in `stage1/capture_discovery.py`

### Step 5: Checker validation
- [x] Add `_eval_hir_const_value()` helper in `checker/__init__.py`
- [x] Validate local-const initializer (literal-only + type-match + Byte range)
- [x] Handle `HLocalConst` in 4 walker functions in checker
- [x] Handle `HLocalConst` in `type_checker.py` (type_stmt, walk_stmt, stmt_can_throw)
- [x] Register `local_const_binding_ids` set; skip Copy check for local-const HVar uses
- [x] Handle `HLocalConst` in `driftc.py` (5 locations: walk_stmt, _collect_local_names, _remap_lambda_local_collisions, _scan_stmt, _remap_stmt)

### Step 6: Stage2 HVar resolution
- [x] Add `_local_consts: dict[int, tuple[TypeId, value]]` to HIR→MIR lowering
- [x] Add `_emit_local_const()` — emits MIR literal per type
- [x] Add `_visit_stmt_HLocalConst()` — records binding in local-const table
- [x] Consult local-const table at top of `_visit_expr_HVar` (before module-const lookup)
- [x] Add local-const type resolution in `_infer_expr_type` for HVar
- [x] Handle `HLocalConst` in `stmt_can_throw` and expr walker
- [x] Add local-const materialization in `_lower_addr_of_place` for method auto-borrow

### Step 6b: Borrow checker
- [x] Add `local_const_binding_ids: Set[int]` field to `BorrowChecker` dataclass
- [x] Scan HIR block for `HLocalConst` in `from_typed_fn` and collect binding_ids
- [x] Update `_state_for` to return `PlaceState.VALID` for local-const bindings
- [x] Handle `HLocalConst` in `_collect_binding_ids_for_name_in_block`

## Session 2: e2e tests (all 18 passing)

### Step 7: Positive e2e tests (11/11)
- [x] local_const_int (exit 42)
- [x] local_const_uint (exit 7)
- [x] local_const_byte (exit 255)
- [x] local_const_bool (exit 1)
- [x] local_const_string (exit 5)
- [x] local_const_float (exit 1)
- [x] local_const_unary_neg (exit 1)
- [x] local_const_nested_block (exit 10)
- [x] local_const_shadow_module (exit 7)
- [x] local_const_bitwise (exit 15)
- [x] local_const_string_multi_use (exit 0) — MUST-HAVE non-Copy regression

### Step 8: Negative e2e tests (5/5)
- [x] local_const_nonliteral_rejected
- [x] local_const_call_rejected
- [x] local_const_var_ref_rejected
- [x] local_const_type_mismatch
- [x] local_const_byte_oob
- Note: `local_const_duplicate_rejected` dropped — Drift allows shadowing in same block (same as `val`).

### Step 9: Boundary guardrails (2/2)
- [x] local_const_not_exported (parse error: `pub const` in function scope)
- [x] local_const_no_mut_borrow (`&mut` of immutable binding rejected)

## Session 3: Spec update (complete)

### Step 10: Spec update
- [x] Update §3.9 (Constants) in drift-lang-spec.md
- [x] Clarify block-scope, non-exportable, re-materialization semantics
- [x] Add block-scope syntax example and non-Copy multi-use example

---

## Log

### 2026-02-23 — Session 1
- All grammar/parser/AST/HIR/checker/type_checker/stage2 changes implemented.
- Hit borrow checker issue (`use of uninitialized 'X'`) — local consts have no `StoreLocal`.
- Partially fixed: added `local_const_binding_ids` scanning in `from_typed_fn`.

### 2026-02-23 — Session 2
- Completed borrow checker fix: added `local_const_binding_ids` field to `BorrowChecker` dataclass, updated `_state_for` to return VALID.
- Discovered method auto-borrow issue: `S.byte_length()` needs `&S` but local const has no local slot. Fixed by adding local-const materialization in `_lower_addr_of_place`.
- Created and verified all 18 e2e tests (11 positive, 5 negative, 2 boundary guardrails).
- Full e2e suite regression check: passed (exit 0, no failures).

### 2026-02-23 — Session 3
- Updated §3.9 (Constants) in `docs/design/drift-lang-spec.md` to describe block-scope `const`.
- Added block-scope syntax example, non-Copy multi-use example, shadowing rules, `pub` restriction.
- All implementation complete.

### Key design decisions
- Local const = compile-time literal alias, no storage, re-materialized at each use site.
- Non-Copy types (String) work at multiple use sites because each use emits a fresh ConstString.
- Method calls on local consts: auto-borrow materializes const into a temp local, takes address.
- Shadowing in same block is allowed (consistent with `val` behavior).
