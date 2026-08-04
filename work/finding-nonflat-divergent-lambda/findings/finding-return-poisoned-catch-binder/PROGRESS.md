# PROGRESS: finding-return-poisoned-catch-binder

STATUS: FIX IN TREE — focused gates green; folding into parent handoff.

## Fix

1. DELETED the function-wide `binding_names.items()` fallback in
   `type_checker.py` HVar resolution (was ~:7334).  Names resolve only via
   binding identity or the ACTIVE lexical scopes; a comment pins the
   no-history-resolver contract at the site.
2. Phase-1's unknown-name diagnostic now carries the stable code
   `E-UNKNOWN-NAME` — the same code the phase-2 checker uses for the same
   meaning.  (Surfaced by test_catch_binder_sibling_name_reuse.py::
   test_catch_binder_not_visible_after_arm, which pins the exact code:
   with the fallback gone, phase 1 reaches the unresolved use first and
   must speak the same diagnostic language.)

## Audit trail (why the fallback existed)

`git log -L` attributes it to commit 6fda4df3 — a large mixed commit
(Diagnostic by-ref migration + stdlib/e2e sweep), no rationale text, no
dedicated test.  It predates `_scope_lookup_binding_id` and the
`scope_bindings` walk that now own lexical resolution.  Battery evidence
below shows nothing legitimate depended on it.

## New pins

lang/tests/driver/test_out_of_scope_name_resolution.py (NEW file):
- post-catch binder use → `unknown name 'e'`, NO return-mismatch cascade;
- non-catch companion: block-local after its scope → unknown name;
- in-scope typed catch binder still resolves as the Error type incl.
  field projection (compile+run positive);
- shadowing positive: inner binder wins in-block, outer restored after.

## Verification

- e2e `catch_binder_scope_leak`: ok (was the full-suite FAIL).
- New pins + full test_catch_binder_sibling_name_reuse.py: 7 passed.
- type_checker/checker/stage1/stage2/parser suites: 1096 passed BOTH
  before and after the E-UNKNOWN-NAME stamp; e2e re-verified ok after it.
- catch/binder/scope/lambda/capture driver battery (34 files): 265 passed,
  1 failed pre-fix (the sibling code pin above — now green).
- No global "prior diagnostic exists" suppression involved; the return
  authority's existing Unknown suppression handles the cascade (acceptance
  criterion).
