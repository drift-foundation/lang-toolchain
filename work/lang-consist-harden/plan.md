# Lexical-Scope Hardening Plan

## Goal

Harden the checker's lexical-scope enforcement so every block-introducing construct
(if, loop, try/catch, match, bare block, unsafe block) has verified isolation,
consistent diagnostic wording, and regression coverage.

## Target walkers (explicit scope)

The checker has four distinct traversal families. This plan targets walkers **A**
and **B**; walkers C and D are out of scope.

| ID | Walker                          | Location (lines)  | Manages `ctx.locals`? | In scope? |
|----|--------------------------------|--------------------|-----------------------|-----------|
| A  | `_walk_hir` (main validator)   | 2496-2789          | **yes** — save/restore | **yes**   |
| B  | throw-analysis (`walk_block`)  | 1015-1083          | no — only tracks throw | **yes** (completeness audit) |
| C  | signature-gathering (`_collect_callsite_ids`) | 377-497 | no — only collects ids | no |
| D  | inline-lambda arity checker    | 2042-2203          | no — walks calls only  | no |

**Walker A** (`_walk_hir`) is the primary target: it owns `ctx.locals` save/restore
and is where scope leakage bugs manifest.

**Walker B** (throw-analysis) doesn't manage locals, but its statement dispatch must
stay in sync with walker A. If walker A adds a new construct, walker B must also
traverse it or it silently misses throw sites. The audit in Phase 2c covers this.

## Current state

The checker (`lang/driftc/checker/__init__.py`) uses a **flat `ctx.locals` dict**
with manual save/restore at each block boundary. This works correctly today for the
constructs that are wired up, but the pattern is fragile — each new construct must
independently remember to snapshot/restore, and there is no structural enforcement.

### Constructs with save/restore today (walker A, lines 2740-2782)

| Construct       | save/restore? | Notes                                         |
|-----------------|---------------|-----------------------------------------------|
| `HIf`           | yes           | condition in outer scope, then/else isolated   |
| `HLoop`         | yes           | body isolated                                  |
| `HTry`          | yes           | body + each catch arm isolated                 |
| `HBlock`        | yes           | body isolated                                  |
| `HUnsafeBlock`  | yes           | body isolated                                  |
| `HMatchExpr`    | yes           | each arm isolated, binders seeded per arm      |
| `HLocalConst`   | n/a           | registers into `ctx.locals`, scoped by parent  |
| `HLambda`       | no walk       | lambda bodies not walked in the main validator |

Note: `HMatchStmt` does not exist as a HIR node class. Only `HMatchExpr` exists;
the `_walk_hir` walker handles it defensively as both expression and statement-level
(line 2749 + 2557). The signature-gathering walker (C) has a forward-looking
`HMatchStmt` guard that never fires.

### Catch-arm suppression

Catch arms suppress `report_unknown_names` because the catch binder (error variable)
is injected by stage2, not by the checker. The `_TypingContext.infer` path (line 1966)
already seeds `arm.binder` with `ensure_error()` type when it encounters try-expr
catch arms — but the `_walk_hir` walker's `HTry` handler does not.

---

## Phase 1: Regression tests (scope isolation)

Write negative e2e tests confirming that bindings declared inside each construct are
**not** visible after the construct exits. One test per construct.

Also add one **positive in-scope visibility** test per construct family so that
Phase 2a refactoring can verify it doesn't break valid programs.

### Tests to add (negative — expect checker error for use-after-scope)

| Test name                         | Construct tested        | What leaks?                         |
|-----------------------------------|-------------------------|-------------------------------------|
| `if_else_scope_let_leak`          | `HIf` else-branch       | `val` declared in else body         |
| `nested_if_scope_let_leak`        | `HIf` nested            | `val` from inner if                 |
| `loop_scope_const_leak`           | `HLoop`                 | `const` declared in loop body       |
| `try_body_scope_let_leak`         | `HTry` body             | `val` from try body                 |
| `catch_arm_scope_let_leak`        | `HTry` catch arm        | `val` from catch block              |
| `catch_cross_arm_leak`            | `HTry` multi-catch      | `val` from one catch arm to another |
| `match_arm_scope_let_leak`        | `HMatchExpr`            | `val` from match arm body           |
| `match_binder_scope_leak`         | `HMatchExpr`            | binder name after match exits       |
| `bare_block_scope_let_leak`       | `HBlock`                | `val` from bare `{ }` block         |
| `local_const_if_scope_leak`       | `HIf` + `HLocalConst`   | `const` from if body                |
| `local_const_loop_scope_leak`     | `HLoop` + `HLocalConst` | `const` from loop body              |

### Tests to add (positive — binding visible within scope)

One per construct family to anchor the pre/post refactor parity.

| Test name                            | Construct family  | What it confirms                         |
|--------------------------------------|-------------------|------------------------------------------|
| `if_let_visible_within_branch`       | `HIf`             | `val` usable later in same then-block    |
| `loop_let_visible_within_body`       | `HLoop`           | `val` usable within loop body            |
| `try_let_visible_within_body`        | `HTry`            | `val` usable within try body             |
| `catch_let_visible_within_arm`       | `HTry` catch      | `val` usable within catch arm block      |
| `match_binder_visible_in_arm`        | `HMatchExpr`      | binder usable inside its match arm       |
| `bare_block_let_visible_within`      | `HBlock`          | `val` usable later in same bare block    |
| `const_visible_within_block`         | `HLocalConst`     | `const` usable later in same block       |

### Existing coverage (do NOT duplicate)

- `if_arm_scope_unknown_name` — if then-branch let leak
- `loop_body_scope_unknown_name` — while-loop let leak
- `for_count_loop_scope_unknown_name` — for-count init var
- `try_catch_scope_unknown_name` — try/catch body let leak
- `local_const_nested_block` — local const in nested if (positive)

### Canonical diagnostic string

All scope-leak negative tests must expect this exact error message from the checker:

```
unknown name '{name}'
```

(This is what the checker currently emits via `report_unknown_names`. If the wording
changes in Phase 4, update all expected files in one pass.)

---

## Phase 2: Checker walker audit & normalize

### 2a. Extract scope helper (behavior-sensitive refactor)

Replace the repeated save/restore pattern with a context-manager:

```python
@contextmanager
def _scoped_locals(ctx):
    saved = dict(ctx.locals)
    try:
        yield
    finally:
        ctx.locals = saved
```

Then each construct becomes:

```python
elif isinstance(stmt, H.HIf):
    walk_expr(stmt.cond)
    with _scoped_locals(ctx):
        walk_block(stmt.then_block)
    if stmt.else_block:
        with _scoped_locals(ctx):
            walk_block(stmt.else_block)
```

**This is NOT a pure refactor.** Save/restore centralization can change behavior if
any branch relied on accidental leakage or if the `finally` exception-safety changes
an error-path outcome. Treat it as behavior-sensitive:

- Run all Phase 1 regressions (both negative and positive) before and after.
- Require exact diagnostic parity: same errors, same spans, same ordering.
- If any existing test changes output, investigate before proceeding.

Benefits:
- Single place to add future scope-enter/exit hooks (diagnostics, debug tracing)
- `finally`-based restore is exception-safe (current code is not — if `walk_block`
  raises before restore, locals leak into the next construct)

### 2b. Catch-arm binder seeding

**MVP type policy (pinned):** Always seed `arm.binder` as `Error` type
(`tt.ensure_error()`) in the `_walk_hir` walker, matching the existing behavior in
`_TypingContext.infer` (line 1966-1968). Do NOT attempt to resolve the specific
exception event type — that is a post-MVP enhancement.

Concrete steps:
1. In `_walk_hir`'s `HTry` handler, before `walk_block(arm.block)`, if `arm.binder`
   is not None, seed `ctx.locals[arm.binder] = tt.ensure_error()`.
2. Remove the `report_unknown_names = False` suppression for that arm.
3. The save/restore (or `_scoped_locals`) around the arm block already cleans up
   the binder after the arm exits.
4. Add two regression tests:
   - `catch_binder_visible_in_arm` (positive: binder usable inside catch arm)
   - `catch_binder_scope_leak` (negative: binder not visible after try/catch)

### 2c. Walk audit

For both walker A (`_walk_hir`) and walker B (throw-analysis):

1. Enumerate all `HStmt` subclasses from `lang/driftc/stage1/hir_nodes.py`.
2. For each subclass, verify it appears in the walker's `isinstance` chain.
3. For walker A: verify block-introducing constructs use `_scoped_locals`.
4. For walker A: verify leaf nodes (`HReturn`, `HBreak`, `HContinue`, `HThrow`,
   `HExprStmt`) are documented as not needing scoping.
5. For walker B: verify every block-containing construct recurses into its sub-blocks
   so throw sites are not missed.
6. Confirm `HMatchExpr` is handled in both expression and statement paths of walker A.
   (`HMatchStmt` does not exist; no action needed for it.)

---

## Phase 3: Local-const scope hardening

### 3a. Const binding_id isolation

Verify that `ctx.locals[int(binding_id)]` entries for local consts are properly
removed by the save/restore mechanism (they should be, since save/restore replaces
the entire dict — but confirm with a test).

### 3b. Const-in-catch / const-in-match

Add tests that a `const` declared inside a catch arm or match arm is not visible
outside. Verify the checker rejects the use.

---

## Phase 4: Diagnostic consistency

### 4a. Error message audit

Collect all "unknown name" / "not defined" / "out of scope" diagnostics emitted by
the checker and normalize wording to a single pattern:

```
unknown name '{name}'
```

This is already the current wording. Verify no variant spellings exist.

### 4b. Span accuracy

For each scope-leak rejection test, verify the diagnostic span points to the **use
site** (not the declaration site). Add span assertions to the expected output.

---

## Ordering & dependencies

```
Phase 1 (regressions: negatives + positives per construct)
  |
  ├─> Phase 2a (scope helper extraction — behavior-sensitive, pre/post parity required)
  |     ├─> Phase 2b (catch-arm binder seeding, type policy: always Error)
  |     └─> Phase 2c (walk audit: walker A + walker B)
  |
  └─> Phase 3 (local-const scope)

Phase 4 (diagnostics) — can run in parallel with Phase 2/3
```

Phase 1 must land first so that Phases 2-3 have regressions to validate against.
Phase 2a requires pre/post regression parity (not a pure refactor).
Phase 2b changes diagnostics and needs its own regressions before/after.

---

## Acceptance criteria (done checklist)

- [x] All new negative scope-leak tests pass (bindings rejected after scope exit)
- [x] All new positive in-scope tests pass (bindings accepted within scope)
- [x] No new internal diagnostics or assertion failures
- [ ] Full driver + e2e shard for affected tests green (user runs on farm)
- [x] Walker A and walker B dispatch chains cover all `HStmt` subclasses
- [x] Catch-arm binder seeded as `Error` type; `report_unknown_names` suppression removed
- [x] Canonical diagnostic string `unknown name '{name}'` used consistently

---

## Out of scope

- Lambda body walking (separate work item — tied to callable model roadmap #3)
- Composite const types (roadmap #1)
- Stage2 scope-stack changes (stage2 already has `_push_scope`/`_pop_scope`)
- Borrow-checker scope model (already has NLL-lite + escape levels)
- Catch-arm event-specific typing (post-MVP enhancement to 2b)
