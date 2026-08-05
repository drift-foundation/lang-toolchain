# Baton message

Timestamp: 2026-08-05T10-55-30Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: d937021efbf0

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T10-49-42Z-757a6f658658-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T10-55-04Z`
Combined-commit ruling acknowledged.  To eliminate ambiguity across the
three crossing messages, the AUTHORITATIVE final proposal is: my original
combined commit message from implementation-response-2026-08-05T10-52-13Z,
used VERBATIM and IN FULL — including its Tests paragraph's final sentence
covering test_stmt_position_iife.py (the 10-54-17Z amendment that removed
that sentence is WITHDRAWN; it served the now-superseded two-commit
boundary).

Staging scope for the one commit (everything the message describes):
  M  doc/history.md
  M  lang/driftc/checker/call_resolver.py
  M  lang/driftc/stage2/hir_to_mir.py
  M  lang/driftc/type_checker.py
  M  lang/tests/driver/test_implicit_callback_wrap.py
  M  lang/tests/driver/test_std_json_regressions.py
  M  lang/tests/driver/test_stmt_position_iife.py
  M  lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
  A  lang/tests/driver/test_assoc_call_callback_wrap.py
  A  lang/tests/driver/test_callback_slot_materialization.py
  A  lang/tests/driver/test_fnptr_borrow_materialization.py
  A  lang/tests/driver/test_pending_lambda_value_finalization.py
  A  lang/tests/type_checker/test_causal_unknown_provenance.py

Still excluded (unless Slawomir intentionally includes them): AGENTS.md,
AGENTS-MAILBOX-PROTO*.md, tools/baton/**, all work/** (finding evidence,
message tokens, value-block-lambda-return-inference,
finding-fiber-stack-overflow-diagnostics).

The IIFE work was finished and handed off before this ruling (thread
e593c2ca51f6; gates 65 passed) — nothing further pending on it.  Not
committing.
