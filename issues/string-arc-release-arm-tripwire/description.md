Summary: intake for firings of the string_arc release-arm tripwire
(`internal: string ownership stake contract failure (string_arc
release-arm tripwire [lastuse_release_arm]: ...)`)

What this error means
- The in-pass LAST-USE RELEASE arm in `_note_use` is FAIL-CLOSED
  (2026-07-16, after TLR-7 closed the family ladder): every measured
  last-use release population — 618,744 lifetime releases across all
  producer families (ConstString, StringConcat, proven non-throw
  String-returning calls, StringFrom*, ExcGet*Json, CopyValue) and all
  CFG shapes (in-block, straight-line cross-block, joins, per-iteration
  intra-loop) — is authored by the `string_releases` materialization
  pass and suppressed at string_arc's recognition before this arm can
  fire.  The arm is scheduled for deletion (with the 4a'/4b' dead-stake
  branches) after a clean certification cycle with zero firings.
- A firing therefore means the compiler reached a release-emission path
  the whole 924-fixture corpus claimed unreachable — a LANGUAGE_BUG,
  NOT a user error.  The user's source is valid.
- The payload's `family=` flag distinguishes the two defect classes:
  - family=True — a STALE UNMIGRATED family temp: the string_releases
    pass failed to materialize a qualified temp's release, or
    string_arc's recognition failed to see it (producer-resolution or
    placement-validation defect);
  - family=False — a NON-FAMILY owned producer (e.g. StringRetain /
    VariantGetField / ArrayIndexLoad dests) reached a non-consuming
    drain: a new emission shape that needs family review before any
    code change.

Required repro info (all of it is in the diagnostic message)
- The full diagnostic line, including: fn symbol, block/index, value,
  producer kind + family flag, and the live_out bit.
- The compiling source (the fixture or app module that triggered it).

Triage
- Do NOT work around in stdlib/app code; the fix belongs in
  string_releases/string_arc (or, for family=False, in a reviewed
  family extension per the TLR ladder discipline — release-arm
  tripwire design, recorded in the TLR entries of doc/history.md).
