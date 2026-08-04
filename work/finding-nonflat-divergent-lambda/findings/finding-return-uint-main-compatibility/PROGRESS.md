# PROGRESS: finding-return-uint-main-compatibility

STATUS: RESOLVED — Slawomir approved the edit (relayed in
review-2026-08-04T03-38-51Z: "as long as it preserves the spec then YES.
We (Drift) is POSIX C-like so our main has to align with C main").  The
one-line fixture edit is applied; `main` keeps `nothrow -> Int`; no
implicit Uint -> Int coercion was added.  Focused gates:
bitwise_uint_ops e2e ok (exit 254 contract preserved) + return-boundary
driver pins green.

## Assessment (agrees with the reviewer)

Stale fixture, not a compiler coercion gap.  The spec requires
`main -> Int`, distinguishes Int/Uint, provides explicit `cast<T>(...)`,
and nothing authorizes implicit non-literal `Uint -> Int` at a return
boundary.  The shared return authority's rejection is CORRECT; no compiler
change will be made for this finding.

## Proposed edit (exact, one line)

In `lang/tests/codegen/e2e/bitwise_uint_ops/main.drift`:

```
-	return x;     // 254
+	return cast<Int>(x);     // 254
```

All Uint bitwise/augmented-assignment coverage is untouched; only the
entrypoint boundary becomes explicit.

## Log

- 2026-08-04: Reproduced focused: `bitwise_uint_ops: FAIL (unexpected
  checker diagnostics: return type 'Uint' does not match declared type
  'Int')` on the committed tree.
- 2026-08-04: Out-of-tree probe of the proposed edit (scratchpad
  uintprobe/): compiles clean, runs, EXIT 254 — contract preserved.
- 2026-08-04: Negative boundary coverage check: the shared return
  authority's non-literal mismatch pins in
  lang/tests/driver/test_lambda_return_inference_boundary.py already make
  "non-literal Uint cannot return from -> Int without a cast" unambiguous;
  no new pin needed (plan item 6).
- 2026-08-04: Proposal recorded; APPROVAL-PENDING raised in parent root.
  AFTER approval: apply the one-line edit, run the focused e2e case +
  return-boundary driver tests, fold result into the parent handoff.
