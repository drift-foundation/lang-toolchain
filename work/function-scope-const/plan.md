# Function-Scope `const` Support Plan (Evaluation Only)

## Goal
Evaluate the complexity and design impact of adding **function-scope `const` declarations** in Drift (e.g. inside `fn main() { const X: Uint = 1; }`).

This document is planning-only. **No code changes** are included.

## Problem Statement
Current behavior rejects `const` in function scope at parse time (unexpected token), while module-scope `const` is supported. This creates a language ergonomics gap for users who expect local immutable compile-time constants.

---

## Step 1: Current-State Audit

### Where `const` is accepted today

`const` is a module-level `item` only. The grammar rule chain:

```
program: module_stmt item*
item: ... | const_def | ...
const_def: CONST NAME COLON type_expr EQUAL expr TERMINATOR    (grammar.lark:147)
```

It does **not** appear in any block-level rule:
- `stmt` → `compound_stmt | simple_stmt TERMINATOR` (line 209)
- `simple_stmt` → `let_stmt | return_stmt | ... | expr_stmt` (line 220)
- `compound_stmt` → `if_stmt | while_stmt | ... | block` (line 212)

Attempting `const` inside a function body produces a Lark `UnexpectedToken` error at parse time.

### Module-const pipeline (traced end-to-end)

| Stage | File | What happens |
|-------|------|-------------|
| **Parse** | `parser/parser.py:896` | `_build_const_def()` creates `ConstDef` AST node |
| **Program AST** | `parser/ast.py:510` | Stored in `Program.consts` list |
| **Const eval** | `parser/__init__.py:3990` | `_eval_const_value(expr)` — accepts `Literal` or unary `+`/`-` on numeric literal; returns Python value or `None` |
| **Type validation** | `parser/__init__.py:4001–4057` | Checks declared type matches literal kind (Byte range, Int, Uint, Bool, String, Float); rejects mismatches |
| **TypeTable registration** | `core/types_core.py:425` | `define_const(module_id, name, type_id, value)` stores `(TypeId, value)` keyed by `"module::name"` |
| **Type checker** | `type_checker.py:5224–5231` | HVar with `binding_id is None` → `lookup_const()` → returns typed value; enforces Copy |
| **Borrow checker** | `borrow_checker_pass.py:655–675` | Const places always `PlaceState.VALID` (never uninitialized/moved) |
| **HIR→MIR lowering** | `stage2/hir_to_mir.py:1384–1427` | `lookup_const()` → emit `ConstInt`/`ConstString`/etc. MIR literal node (no storage, inlined at use) |
| **LLVM codegen** | `llvm/llvm_codegen.py:2034+` | `ConstInt` → `add i64 0, <val>`, `ConstString` → global unnamed_addr + GEP, etc. |

**Key observation:** consts are **compile-time values with no runtime storage**. They are fully resolved by the checker/TypeTable and inlined as MIR literal instructions. The LLVM codegen never sees a "const declaration" — only literal emission nodes.

### Const name resolution order (type_checker.py:5220–5231)
1. Local/param bindings (lexical scopes, by `binding_id`)
2. Module-qualified const symbols (`mod::NAME`)
3. Unqualified const names within current module

This is important: local bindings with `binding_id != None` take precedence over consts.

---

## Step 2: Surface Design Proposal

### Recommended semantics: identical to module const

Function-scope `const` should be a **compile-time literal alias with no storage**, identical to module-scope `const` except:
- Scoped to the enclosing block (not module-visible)
- Not exportable
- Participates in normal block-level name shadowing rules
- **Reusable at every use site (including non-Copy literals)**. A local const must not behave like a one-shot moved local.

Required behavior example:

```drift
const S: String = "hi";
use_string(S);
use_string(S);  // must also work
```

This must remain valid by treating local const like module const (re-materialized/inlined per use), not like a consumable `val`.

Syntax: `const NAME: Type = <literal>;`

Same initializer restrictions as module const: compile-time literal only (integer, float, bool, string, byte) with optional unary `+`/`-` on numerics. No constant-expression evaluation (`1 + 2`), no function calls, no variant constructors.

Rationale:
- Keeps the const contract simple: const = compile-time known literal value, period.
- Avoids introducing a const-expression evaluator (which is a large feature with cascading complexity).
- Matches user expectations from C/C++ `const int X = 42;` at local scope.
- The existing pipeline already handles const inlining; local const just needs a different registration path.

### Grammar change

Add `const_def` as a `simple_stmt` alternative (it already has a `TERMINATOR`):

```lark
simple_stmt: let_stmt
           | const_def                   ← NEW
           | return_stmt
           | ...
```

However, `const_def` already includes its own `TERMINATOR`, and `simple_stmt` adds one via `stmt: simple_stmt TERMINATOR`. This would double-terminate. Two options:

**Option A — New rule `local_const_stmt`** (recommended):
```lark
local_const_stmt: CONST NAME COLON type_expr EQUAL expr
simple_stmt: let_stmt
           | local_const_stmt
           | ...
```
The `TERMINATOR` comes from `stmt: simple_stmt TERMINATOR` as usual. This avoids touching the module-level `const_def` rule.

**Option B — Make `const_def` terminator-free and add TERMINATOR at both use sites:**
This would require restructuring the `item` rule too. More invasive, not recommended.

### Name resolution

- Local const is **not** registered in `TypeTable.consts` (module-scoped dict). Instead, it gets a function-scope entry — either:
  - (a) ~~Synthesized as a `val` binding with a special `is_const: True` flag and the literal value pre-evaluated.~~ **Rejected:** a `val` binding for non-Copy types (e.g. `String`) would be consumed on first move, breaking multi-use semantics. Local const must be re-materializable at every use site.
  - (b) Registered in a new `local_consts: dict[binding_id, (TypeId, value)]` on the lowering context, consulted at HVar resolution time. Each use emits a fresh `ConstInt`/`ConstString`/etc. MIR literal — identical to how module consts work via `lookup_const()`.

**Recommendation: Option (b).** The parser emits a new `HLocalConst` HIR node (or an `HLet` with `is_const` + pre-evaluated value). The checker validates the literal constraint and records `(binding_id, TypeId, value)` in a local-const table. Stage2 consults this table when visiting an HVar: if the binding_id matches a local const, emit the appropriate `ConstInt`/`ConstString`/etc. MIR literal directly (no `LoadLocal`, no storage allocation). Each use site gets an independent materialization. Downstream (MIR/SSA/codegen) sees no difference from a module const.

**Critical invariant:** a local const reference must never lower to `LoadLocal`. It must always re-materialize the literal value, so non-Copy types like `String` can be used at multiple sites without move/consume semantics.

### Shadowing rules

- Local `const` may shadow module-level consts (same as `val` shadowing).
- Local `const` may shadow outer-block `val`/`var`/`const` names (same as `val` shadowing).
- Duplicate `const` in the same block is rejected (same as duplicate `val`).
- `const` name may not conflict with a `val`/`var` in the same scope.

---

## Step 3: Complexity Estimate by Subsystem

### Parser/AST — **Low**
| File | Change |
|------|--------|
| `grammar.lark` | Add `local_const_stmt` rule (~1 line); add to `simple_stmt` alternatives |
| `parser/ast.py` | Add new `LocalConstStmt` node (name, type_expr, value, loc) |
| `parser/parser.py` | Add `_build_local_const_stmt()` builder (~15 lines); wire into `_build_simple_stmt()` |

~30 lines of new code. No ambiguity risk — `CONST` token at priority `.2` is unambiguous in statement position.

### Checker — **Low**
| File | Change |
|------|--------|
| `parser/__init__.py` | Add validation for local-const initializers: same `_eval_const_value()` + type-match logic used for module consts, invoked during statement processing |
| `checker/__init__.py` | Handle `LocalConstStmt` during statement processing: validate initializer via `_eval_const_value()`, type-match, record `(binding_id, TypeId, value)` in a local-const table passed to stage2 |

~20 lines. The existing `_eval_const_value()` function is reusable as-is.

### Type checker — **Low**
| File | Change |
|------|--------|
| `type_checker.py` | Local-const bindings get typed during checker pass (same as `val`). No special type-checker logic needed — the const identity is carried by the local-const table, not the type system. `&mut` rejection is inherited from immutable binding semantics. |

~5 lines.

### HIR→MIR lowering (Stage2) — **Low-Medium**
| File | Change |
|------|--------|
| `hir_to_mir.py` | When visiting an HVar that resolves to a local-const binding: emit `ConstInt`/`ConstString`/etc. directly instead of `LoadLocal`. This requires the lowering pass to know the binding's const value — either stored on the HIR node or looked up from a function-scope const table |

~25 lines. The MIR emission code for `ConstInt`/`ConstString`/etc. already exists — it just needs a new trigger path.

Stage2 receives the `local_consts: dict[binding_id, (TypeId, value)]` table from the checker. When visiting an HVar whose `binding_id` appears in this table, it emits a fresh MIR literal (same as the module-const path in `_visit_expr_HVar` lines 1401–1427). No `LoadLocal`, no `StoreLocal`, no local slot allocated.

### MIR/SSA/codegen — **No change**
These layers only see `ConstInt`, `ConstString`, etc. MIR nodes. They don't know or care whether the const came from module scope or function scope. Zero changes needed.

### Borrow checker — **No change**
Local consts are never lowered to locals — they produce MIR literals directly. The borrow checker never sees a local slot for them, so no place-tracking or move-tracking applies. If an HVar resolves to a local const, it never reaches the borrow checker as a place.

### Diagnostics — **Low**
One new diagnostic needed: "local const initializer must be a compile-time literal in v1" (reuse existing wording from module-const check).

### Total estimate: **Small** (~80-100 lines of new code across 4 files)

---

## Step 4: Risk Analysis

### Parser ambiguity risk: **None**
`CONST` at priority `.2` is unambiguous in statement position. No existing `simple_stmt` alternative starts with `CONST`. Lark will route to `local_const_stmt` deterministically.

### Constant-expression evaluation drift risk: **None (if we hold the line)**
By keeping the same literal-only constraint as module const, we avoid introducing a const-expression evaluator. The risk only materializes if we later relax to allow `1 + 2` or `SIZE * 2` — which should be a separate, deliberate feature.

### Hidden boundary expansion risk: **None**
Local consts desugar to the same MIR literal nodes already used by module consts. No new MIR node types, no new codegen paths, no new SSA handling.

### Name resolution collision risk: **Low**
Local consts get a `binding_id` like any other local binding, so they participate in the same block-scoping and shadowing rules. The only new check is the initializer constraint. Shadowing rules already work correctly for `val`.

### Cross-module visibility risk: **None**
Local consts are block-scoped. They are never registered in `TypeTable.consts`, never exported, never visible outside the function.

---

## Step 5: Regression-First Test Plan

### Positive tests (e2e)

| Test name | Description | Expected |
|-----------|-------------|----------|
| `local_const_int` | `const X: Int = 42;` in function body, `return X;` | exit_code: 42 |
| `local_const_uint` | `const X: Uint = 7;` in function body, use in arithmetic | exit_code: 7 |
| `local_const_byte` | `const B: Byte = 255;` | exit_code: 255 (via Int cast) |
| `local_const_bool` | `const T: Bool = true;` in conditional | exit_code: 1 |
| `local_const_string` | `const S: String = "hello";` passed to `byte_length()` | exit_code: 5 |
| `local_const_float` | `const F: Float = 2.0; return (F > 1.0) as Int;` (fixed predicate) | exit_code: 1 |
| `local_const_unary_neg` | `const N: Int = -1;` | exit_code: appropriate |
| `local_const_nested_block` | `const` inside `if` / `while` body | exit_code: correct |
| `local_const_shadow_module` | Local `const X` shadows module `const X` | uses local value |
| `local_const_bitwise` | `const A: Uint = 0xFF; const B: Uint = 0x0F; return (A & B);` | exit_code: 15 |
| `local_const_string_multi_use` | `const S: String = "hi"; use_string(S); use_string(S);` — two consuming uses of non-Copy const | exit_code: 0 (both calls succeed) |

### Negative tests (e2e)

| Test name | Description | Expected diagnostic |
|-----------|-------------|---------------------|
| `local_const_nonliteral_rejected` | `const X: Int = 1 + 2;` | "initializer must be a compile-time literal" |
| `local_const_call_rejected` | `const X: Int = foo();` | "initializer must be a compile-time literal" |
| `local_const_var_ref_rejected` | `val y = 1; const X: Int = y;` | "initializer must be a compile-time literal" |
| `local_const_type_mismatch` | `const X: String = 42;` | "declared type does not match initializer" |
| `local_const_byte_oob` | `const B: Byte = 256;` | "out of range" or type mismatch |
| `local_const_duplicate_rejected` | Two `const X` in same block | duplicate name error |

### Boundary guardrails

| Test | Purpose |
|------|---------|
| `local_const_not_exported` | Verify `pub const` is rejected in function scope (parse error) |
| `local_const_no_mut_borrow` | `&mut` of local const value is rejected (already covered by `val` semantics) |

---

## Step 6: Recommendation

### Feasibility: **Yes — straightforward**

Function-scope `const` is feasible and low-risk. The existing const pipeline (eval → TypeTable → MIR literal inlining) provides all the machinery; the only new work is grammar acceptance and routing local-const declarations through the same validation path.

### Recommended MVP surface

1. **Syntax:** `const NAME: Type = <literal>;` inside any block (function body, if/while/for body, bare block).
2. **Semantics:** compile-time literal alias, no storage, same initializer restrictions as module const (literal or unary `+`/`-` on numeric literal).
3. **Scoping:** block-scoped, same shadowing rules as `val`.
4. **Not exportable:** `pub const` in function scope is a parse error.
5. **Implementation approach:** new `LocalConstStmt` AST/HIR node; checker validates literal constraint and records `(binding_id, TypeId, value)` in a local-const table; stage2 consults table and emits MIR literal at each use site (no local slot, no `LoadLocal`). Each reference re-materializes the value, so non-Copy types work at multiple sites.

### Staged follow-ups (post-MVP, separate features)

- **Constant expressions** (`const X: Int = A + B;` where `A`, `B` are other consts) — requires a const-expression evaluator. Medium-to-large effort. Add when there's demonstrated need.
- **Const in match patterns** — allowing const names as pattern match arms. Requires pattern-resolution changes. Separate feature.
- **Const generic parameters** — `fn foo<const N: Int>()`. Entirely separate feature, very large.

### Spec update required

`docs/design/drift-lang-spec.md` must be updated:
- §4 (Constants): currently describes module-scope `const` only. Add block-scope `const` with same literal-only constraint, block scoping, and re-materialization semantics.
- §9 (Keywords): `const` is already listed as a language keyword — no change needed.
- Clarify that `const` in function scope is not exportable and does not participate in module re-export resolution.

### Effort estimate: **2-3 focused sessions**
- Session 1: Grammar + parser + AST changes + basic checker validation.
- Session 2: Stage2 lowering path + all positive/negative e2e tests.
- Session 3: Spec update, edge cases, `local_const_string_multi_use` regression confirmation.
