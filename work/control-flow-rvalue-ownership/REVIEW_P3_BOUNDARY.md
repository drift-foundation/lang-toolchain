# Review packet — P3 final checker-boundary closure

Slice: `work/control-flow-rvalue-ownership` (release 0.34.1, ABI 22 unchanged).
Scope of this packet: ONLY the reviewer's reopened final-P3 boundary audit.
P1.1, P1.2, and the mixed-TypeVar fix remain accepted and are untouched here.

## The defect being closed

`record_expr()` overwrites `expr_types[node_id]` on **every** visit. The initial
resolver correctly typed match arguments/receivers as values, but later passes
re-typed the same nodes with `used_as_value=False`, collapsing a `match` result
to **Void** in the *final* typed HIR. Stage2's `_cfg_result_type` arm fallback
then recovered the type at lowering, so runtime fixtures passed while the typed
HIR was silently wrong — and a wrong diagnostic surfaced.

Confirmed reproducer (invalid program — `Int` is not a `pub error`):

```drift
fn a() nothrow -> core.Result<Int, Int> { return core.Result::Err(1); }
fn b() nothrow -> core.Result<Int, Int> { return core.Result::Err(2); }
return (match true { true => { a() }, false => { b() } }).or_throw();
```

- Before: `E_REQUIREMENT_NOT_SATISFIED: Int is std.core.Throw` (wrong — weaker cascade)
- After:  `E_OR_THROW_NOT_ERROR_TYPE` (correct; matches the bind-first form)

## Changes (7 reviewer items)

All value-position typing now uses the semantic-value/deferred-use contract
`used_as_value=True, defer_value_use=True` (`_copy_use = used_as_value and not
defer_value_use` stays False, so no copy check fires — a borrow/receiver does
not consume/copy its subject, but an rvalue subject still types to the value it
produces instead of Void).

1. **or_throw preflight receiver** — `type_checker.py:10135`
   `type_expr(expr.receiver, used_as_value=False)` → value/deferred.
   New fixture `cfrv_match_receiver_or_throw_not_error_rejected` requires
   `E_OR_THROW_NOT_ERROR_TYPE` (not merely any rejection).

2. **HBorrow subject typing** — `type_checker.py` three sites:
   9279 (repeated-in-statement), 9344 (initial), 9400 (rvalue-materialization).
   `defer_value_use=True` is the correct replacement for the old
   `used_as_value=False` copy-suppression; `used_as_value=True` keeps rvalue
   subjects (match/ternary/try, ref-returning bases) typed to their value.

3. **Later real-value retyping sites** — `type_checker.py`:
   10350 (post-resolution/autoborrow receiver), 10421 (generic-require
   receiver), 10499 (method-arg retyping with expected param types).
   These visits no longer overwrite a match result with Void.

4. **Checker-BOUNDARY pin** — `lang/tests/driver/test_cfrv_match_typed_boundary.py`.
   Runs the full method/call path, then inspects `TypedFn.expr_types` and proves
   each of three STRUCTURALLY-identified contexts keeps its owned arm-result type
   (never Void), by exact shape + type + count (==3), not "all matches Node":
   - (A) a DIRECT match method receiver `(match …).size()` — receiver resolves to
     the match itself — stays `Node`;
   - (B) a match ARGUMENT to a `&Node` METHOD param `s.absorb(match …)` — the
     checker-synthesized `HBorrow(subject=HMatchExpr)` is asserted structurally —
     stays `Node`;
   - (C) a GENERIC-`require` receiver `(match …).peek()` on `Box<Int>` (peek has
     `require T is core.Copy`) — stays the owned `Box<Int>` struct.
   *Reachability-instrumented:* 10499 (method-arg retyping) and 10421
   (generic-require receiver) both execute for (B)/(C).
   *Verified adversarially:* reverting the 9400 HBorrow-subject fix leaves the
   program COMPILING (stage2 fallback masks it) yet the pin FAILS on the Void in
   `expr_types` — so the fallback can no longer hide a checker-boundary
   regression. For these shapes 10499/10421 delegate typing through the fixed
   HBorrow handler / initial `_type_user_arg` typing, so reverting them alone does
   not change the final `expr_types`.

5. **Legitimate `used_as_value=False` inventory left untouched** — place/
   mutability traversal, scrutinees, HMove/HCopy place subjects (9612/9688),
   assignment targets, statement expressions, documented callback-call probes
   (411/4103/4135/7522/7524/7761/7795), place bases (5500/5537/5544), and two
   intentional structural probes:
   - `type_checker.py:3397` — fresh-type/idempotency probe restricted to
     `HUnary`/`HMethodCall`/`HVar` (checks whether the node's already-fresh type
     matches the formal before wrapping a deref; HMatchExpr is not in scope).
   - `type_checker.py:10294` — borrowed-projection consume-safety classification
     of a non-call receiver (guarded `not isinstance(receiver, HCall/HMethodCall/
     HInvoke)`); a type check, not a value use.

6. **`_type_user_arg` is now the single source** — 12 call-resolver sites that
   spelled `used_as_value=True, defer_value_use=True` directly now route through
   the helper; only the helper body spells the flags.

7. **Stale comments refreshed** — the two ConstShare-walker comments (call args
   are typed as values with deferred use, not `used_as_value=False`), the
   HBorrow rvalue-subject comment, and `test_autoborrow_receiver_place.py`'s
   docstring.

Coupled regression fixed while routing receivers (prior round, still relevant):
`E_IFACE_FIELD_COPY` gated on `used_as_value` instead of `_copy_use`, so a
borrowed interface-field receiver tripped it once receivers became
`used_as_value=True`. Re-gated on `_copy_use`.

## Where to look first (review hot-spots)

- `type_checker.py:10135` or_throw preflight — the headline fix.
- `type_checker.py` HBorrow handler (~9267–9402) — three subject sites + the
  rewritten rvalue-subject comment. Confirm `defer_value_use` genuinely
  replaces the copy-suppression that `used_as_value=False` used to provide.
- `type_checker.py:10350/10421/10499` — the "don't overwrite with Void" retyping
  sites; confirm each still computes the value it needs (recv_ty / arg record).
- `call_resolver.py:56` `_type_user_arg` definition + the 14 call sites routed
  through it (receivers 2183/2155/4347/4608; args/kwargs/ctor fields).
- Boundary pin's adversarial property (revert 9400 → compiles but pin RED).

## Verification (gates)

- `_cfg_result_type` unit pins: 6 passed (incl. mixed-TypeVar fail-loud).
- Broad `checker + stage2 + borrow` + boundary pin + autoborrow test:
  **769 passed** (0 fail).
- or_throw match-receiver → `E_OR_THROW_NOT_ERROR_TYPE`; bind-first control
  unchanged; `result_or_throw_ok` green.
- All P3 accept fixtures base + ASan + **memcheck clean** (6/6, incl.
  result_or_throw_ok).
- Broad e2e sweep (523 borrow/proj/receiver/result/mem/ctor cases):
  **519 successful, 4 skipped, 0 failed**.
- Coverage round: 19 focused pins green (strengthened boundary + or_throw
  exact-code driver + 6 `_cfg_result_type` unit + autoborrow); e2e or_throw
  fixture ok. Full suite: USER-run on the frozen tree.

## Fixtures / tests added this round

- `cfrv_match_receiver_or_throw_not_error_rejected` (e2e, exit 1, message match on
  the precise or_throw public-error diagnostic).
- `test_or_throw_match_receiver_diag.py` (driver) — asserts the EXACT code
  `E_OR_THROW_NOT_ERROR_TYPE` and explicitly excludes the old
  `E_REQUIREMENT_NOT_SATISFIED` cascade (the e2e runner matches message text
  only, not codes).
- `test_cfrv_match_typed_boundary.py` (driver) — checker-boundary expr_types pin,
  three structurally-identified contexts (see item 4).

## Not in this packet

P5 (restore `E_REDUNDANT_ARG_BORROW` where the bare form is now accepted, `&mut`
bind-first pins, ownership matrix, `doc/history.md`) — begins after the two
in-flight gates report clean.
