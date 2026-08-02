# P5.1 — Frozen source contract (control-flow-rvalue borrows), 0.34.1

Empirically established, not assumed. Two probes:
- **Compile probe** (`scratchpad/p5/truthtable.py`): 48 cells = **4 CF producers**
  (match, ternary, try, unsafe-block) × **3 subject shapes** (whole, field, index)
  × **4 spelling/modes** (shared bare, shared explicit `&`, mutable bare,
  mutable explicit `&mut`). Full driftc compile+run; records outcome + primary
  diagnostic code.
- **Structural probe** (`scratchpad/p5/matprobe.py`): runs stage1
  `BorrowMaterializeRewriter._rewrite_expr` directly and inspects whether a
  `__tmp_borrow*` local is minted (⇒ `materialized_rvalue=True`).

### Temp-accounting — two DISTINCT pipeline paths (do not conflate)
- **stage1 `materialized_rvalue` flag** — set only when stage1 sees a
  SOURCE-WRITTEN `HBorrow` and `_split_lift_place_chain` mints a `__tmp_borrow*`
  local (structural rule; NOT a hard-coded enumeration of the four CF kinds).
  Applies to the **explicit `&`/`&mut`** cells.
- **lowered owner materialization (stage2)** — the SHARED BARE accepted forms
  carry no stage1 borrow; the CHECKER later synthesizes
  `HBorrow(source_written=False, allow_rvalue=True)` and **stage2** materializes/
  lifts its owner. These forms do NOT acquire the stage1 `materialized_rvalue`
  flag; their ownership is proven from checked-HIR/MIR + the P5.3 runtime matrix.
- **mutable bare** rejects the non-place BEFORE synthesizing an `HBorrow` or
  reaching lowering, so its temp status is **N/A** (no temp is minted).

The direct `BorrowMaterializeRewriter._rewrite_expr` probe is a **programmatic
shape/provenance probe** — it proves the underlying expression is *liftable when
wrapped in an HBorrow*; it does NOT prove that the actual bare-source cell
traversed stage1 borrow materialization.

## The frozen contract

| mode / spelling | shapes | CF producers | contract | actual (probe) | primary code | stage1 `materialized_rvalue` | stage2 lowered owner |
|---|---|---|---|---|---|---|---|
| **shared, bare** | whole/field/index | match/ternary/try/unsafe | ACCEPT, drop-once | OK rc=0 (12/12) | — (accepted; synth `source_written=False` borrow) | **no** (no stage1 borrow) | **yes** — checker-synth HBorrow, stage2 lifts owner (proven from checked HIR/MIR + P5.3) |
| **shared, explicit `&`** | whole/field/index | all 4 | REJECT redundant | `E_REDUNDANT_ARG_BORROW` (12/12¹) | `E_REDUNDANT_ARG_BORROW` | **yes** | — (rejected) |
| **mutable, explicit `&mut`** | whole/field/index | all 4 | REJECT bind-first | `E_MUT_RVALUE_ARG_BINDING_REQUIRED` (12/12¹) | `E_MUT_RVALUE_ARG_BINDING_REQUIRED` | **yes** — code is gated on the flag (type_checker.py:3195), so its appearance *proves* the temp | — (rejected) |
| **mutable, bare** | whole/field/index | all 4 | REJECT bind-first | "borrow requires an addressable place; bind to a local first" (12/12) | `E_MUT_RVALUE_ARG_BINDING_REQUIRED` (0.34.1: aligned, see below) | **N/A** — rejects before HBorrow synthesis / lowering | **N/A** |

¹ The `&match {…}` / `&mut match {…}` *whole* cells without parentheses are a
  PROBE ARTIFACT — `&match` does not parse (bare `&` before the `match` keyword).
  Re-probed with parentheses (`&(match {…})` / `&mut (match {…})`): they yield
  `E_REDUNDANT_ARG_BORROW` / `E_MUT_RVALUE_ARG_BINDING_REQUIRED` as expected. Not
  a compiler gap.

The structural probe directly proved ternary + match are liftable (minted=True);
try + unsafe are proven transitively via the mutable-explicit
`E_MUT_RVALUE_ARG_BINDING_REQUIRED` code (gated on the flag).

### Reject-cell fix-it consistency
- shared explicit `&` → "pass '<operand>' directly" (deletion yields the accepted
  canonical bare spelling). Correct — the value alone satisfies the `&T` formal.
- mutable explicit `&mut` → "bind it to a `var` first". Correct bind-first.
- mutable bare → "bind to a local first". Correct bind-first.
- **No mutable cell offers a "pass directly" fix-it.** ✓

## Preserved controls (where `&` changes typing — NOT redundant)
- `&Concrete → &Interface` widening: classified `coercion`, ACCEPTED (deleting
  `&` changes typing → not redundant).
- Generic-by-value formals: `&` is meaningful; not flagged redundant.
- Existing W0 exemptions (declaration-origin classifier `declared_ref_formal` /
  `build_declared_ref_mask`) unchanged.

## W0 totality scope (correction folded in)
W0 totality validates only **surviving source-written** borrows:
- shared-explicit-redundant and mutable-rvalue cells are proven by **exact
  rejection-code pins** (they are rejected, so they do not "survive").
- accepted **bare** forms are checker-synthesized `source_written=False`
  borrows — correctly **outside** W0 totality.
- genuine coercion / exemption forms that survive are what exercise totality.

## Scope caveat
This contract table establishes **acceptance + diagnostic behavior only**. It
does NOT establish ownership safety — the P5.3 base/ASan/memcheck matrix remains
mandatory for every accepted (shared-bare) row.

## Resolved decision — stable-code alignment (APPROVED + implemented)
The **mutable-bare** rvalue rejection previously emitted the generic
"borrow requires an addressable place; bind to a local first" with an
auto-hashed `E-AUTO-…` code. Per reviewer approval it now carries the stable
`E_MUT_RVALUE_ARG_BINDING_REQUIRED` category — same as the explicit `&mut`
form — via a shared constructor `_mut_rvalue_binding_required_diag(message,
span)` used by all three argument paths (explicit + BOTH bare resolution-path
branches, formerly ~3277 and ~3492). Each keeps a context-appropriate message.
- Argument-scoped only: `_autoborrow_mut_failure` for real places, immutable
  bindings, and mutable METHOD RECEIVERS is **not** relabeled (verified —
  `mk().bump()` receiver keeps the generic gate).
- ABI-neutral (diagnostic category only).
- Tests/docs asserting the old bare-vs-explicit code distinction updated
  intentionally (see P5.2 / P5.4).
