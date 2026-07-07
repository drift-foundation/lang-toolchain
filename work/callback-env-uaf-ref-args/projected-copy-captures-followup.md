# Projected Copy Captures Follow-up

> **STATUS: HISTORICAL (2026-07-07).** The `Copy && is_bitcopy` projected-capture
> surface shipped in 0.33.70 (see `REPORT-0.33.70-projected-capture-lowering.md`).
> The one remaining piece — widening to Copy-but-NON-bitcopy (String-bearing)
> fields — is NOT a standalone slice: it is the final step of the String
> Scope A transfer-policy work (`work/string-ownership-refactor/NEXT-PHASE-PLAN.md`),
> because relaxing the gate without Scope A's alias centralization produced a
> confirmed ASAN UAF during 0.33.70 review.

## Decision

Split the work.

The closure-env UAF fix should land as a conservative LANGUAGE_BUG fix: reject
implicit boxed-callback captures whose final capture kind is MOVE and whose
capture key has a field projection. This closes the unsafe non-Copy projected
capture path that double-dropped the field owner.

Support for Copy-typed projected captures, such as `p.count: Int`, is deferred.
That support is a real lowering feature, not a small checker tweak.

## Why Deferred

`capture_discovery.py` already represents captures by `HCaptureKey`, including
field projections. Env construction can also store the projected value. The
missing piece is that lambda-body lowering does not consistently treat a
projected capture key as its own body-visible binding.

The risky path found during review:

- `capture_as_move=True` classifies a boxed-callback read of `p.count` as a
  MOVE-kind projected capture.
- A type-aware checker downgrade from MOVE to COPY can make env construction
  store only the `Int` value of `p.count`.
- `_emit_lambda_capture_prologue()` binds captures by root local id/name and
  ignores `key.proj`, so the prologue can materialize local `p` from an env slot
  that contains only the projected `Int`, not the full `Prepared` value.
- The body still contains source expressions such as `p.count`, so later
  inference/lowering can treat `p` as the original root type or as the projected
  slot inconsistently.

That means Copy-projected capture support must make projected capture keys
first-class across body lookup, type inference, and lowering. It should not be
folded into the UAF fix unless this behavior is required immediately.

## Current Fix Check-in Gate

Before checking in the UAF fix:

- Reject every final `MOVE` capture with `key.proj` before MIR lowering.
- Add a MIR/lowering assertion if a projected MOVE capture leaks through either
  lambda env construction path.
- Keep the positive `std.mem.replace` control: extract the field into a local,
  then `captures(move local)`.
- Do not keep a positive `p.count` Copy-projected callback test in this patch;
  that belongs to this follow-up.
- Align `doc/history.md` and `REPORT.md` with the conservative split.
- Keep `DRIFTC_VERSION` bumped for the behavior-changing diagnostic; no ABI bump
  unless a later projected-capture lowering feature changes a boundary contract.

## Follow-up Scope

Implement Copy-projected captures intentionally:

- Define the body-visible semantics for a captured projection key. A capture of
  `p.count` must not imply the whole root binding `p` is available.
- Update lambda prologue/lowering so projected capture slots are addressed by the
  exact capture key, not only by root binding id.
- Ensure exact `HField` and canonical `HPlaceExpr` reads of the captured
  projection load from the env slot and infer the projected field type.
- Decide diagnostics for attempts to use the uncaptured root or sibling fields
  inside the lambda body when only one projection was captured.
- Preserve rejection for non-Copy projected MOVE captures unless full partial
  move-and-zero-back semantics are implemented.

## Required Regressions

- Positive: root struct is non-Copy, projected field is Copy.
  Example: `Prepared { count: Int, execute: CallbackThrow1<...> }`, boxed
  callback reads `p.count`, compiles and runs.
- Negative: projected field is non-Copy.
  Example: boxed callback passes `p.execute` by value and gets the projected
  move diagnostic.
- Negative: only `p.count` is captured, but the lambda body tries to use `p` or
  `p.execute`.
- Shape coverage for both `HField` and canonical `HPlaceExpr` if both forms can
  reach lambda body lowering.

## Status

- UAF root cause: identified.
- Conservative fix: should be checked in first.
- Copy-projected capture support: deferred follow-up.
