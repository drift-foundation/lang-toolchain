# Baton message

Timestamp: 2026-08-05T22-05-50Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: corpus_fast_fail_promotion

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T22-00-21Z-52707bb0ef7b-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T22-00-21Z`
Round-5 wording corrections applied — all five active statements now state
the content-identity contract: toolchain_fingerprint_passive's docstring
(missing/unreadable locally; stale content = composite mismatch
downstream); the promote-lane docstring (passive source/tool/library
hashing + existing-artifact bytes, nothing built); _toolchain_passive's
docstring; the run_promote identity comment; the history paragraph; and
the missing-artifact test's comment (with a pointer to the separate
mtime/composite identity pins).

Verification: pytest lang/tests/tools/ = 197 passed (full directory);
git diff --check clean.  Holding for signoff before any production
verify.
