# Baton message

Timestamp: 2026-08-05T10-54-17Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: c748fd1b3621

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T10-48-57Z-75f47380a49c-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T10-52-24Z`
Acknowledged — this correction crossed with my commit-message reply
(implementation-response-2026-08-05T10-52-13Z), which had folded the IIFE
test change into the message and offered a split.  This supersedes that:
treat the amendment below as authoritative.

IIFE slice state: it is at its natural safe point — implementation was
already COMPLETE and handed off (thread e593c2ca51f6) before this
correction arrived: the approved one-file edit is in the working tree,
five focused gates green (65 passed).  Nothing is in flight; nothing was
broadened; the file change stays present and unstaged; no revert.

AMENDMENT to the proposed commit message: delete the final sentence of the
Tests paragraph ("test_stmt_position_iife.py renames the value-position
throwing-IIFE test accurately and adds the true statement-position
compile/run pin.") — everything else stands verbatim.

EXPLICIT EXCLUSION LIST for the preceding-slice commit (Slawomir's
selective staging):

Language-slice files TO COMMIT:
  M  doc/history.md
  M  lang/driftc/checker/call_resolver.py
  M  lang/driftc/stage2/hir_to_mir.py
  M  lang/driftc/type_checker.py
  M  lang/tests/driver/test_implicit_callback_wrap.py
  M  lang/tests/driver/test_std_json_regressions.py
  M  lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
  A  lang/tests/driver/test_assoc_call_callback_wrap.py        (untracked)
  A  lang/tests/driver/test_callback_slot_materialization.py   (untracked)
  A  lang/tests/driver/test_fnptr_borrow_materialization.py    (untracked)
  A  lang/tests/driver/test_pending_lambda_value_finalization.py (untracked)
  A  lang/tests/type_checker/test_causal_unknown_provenance.py (untracked)

EXCLUDE (next finding — commit later with its own message):
  M  lang/tests/driver/test_stmt_position_iife.py
  work/finding-true-statement-throwing-iife/**  (incl. untracked
     PROGRESS.md and implementation-response files)

EXCLUDE (not language work; separate concerns):
  AGENTS.md, AGENTS-MAILBOX-PROTO.md, AGENTS-MAILBOX-PROTO-v5.md
  tools/baton/**  (baton tooling, v4/v5)
  work/finding-causal-unknown-cascade-suppression/**  (finding evidence)
  work/finding-fiber-stack-overflow-diagnostics/**  (new, untouched by me)
  work/ message/claim/notice token files
  work/value-block-lambda-return-inference/**  (earlier-slice evidence)

Not committing anything; standing by for the queued messages and the
resume signal on the IIFE finding.
