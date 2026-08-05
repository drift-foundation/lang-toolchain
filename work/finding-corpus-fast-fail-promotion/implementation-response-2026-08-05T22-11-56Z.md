# Baton message

Timestamp: 2026-08-05T22-11-56Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: corpus_fast_fail_promotion

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T22-06-21Z-5cb273fd4c1b-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T22-06-21Z`
Round-6 items closed.

1. The mtime pin no longer touches shared state: it copies the established
archive into a PRIVATE tmp cache tree at the same cache-relative path,
monkeypatches runtime_archive_cache_root AND runtime_archive_path to that
tree, mutates only the copy's mtime (far past), and asserts exact
components-and-composite equality with the building fingerprint.  The
shared runtime cache and its metadata are never written.

2. resolve_runtime_identity computes the archive hash once and raises
RuntimeError unless it is a valid hex digest — an existing-but-unreadable
or racing artifact fails closed instead of contributing a None identity to
the composite.  New pin: _file_sha256 monkeypatched to None ⇒ RuntimeError
("could not be hashed"), with build_runtime_archive monkeypatched to
explode and untouched.

Verification: pytest lang/tests/tools/ = 198 passed (full directory);
git diff --check clean.  Holding for signoff; no production verify.
