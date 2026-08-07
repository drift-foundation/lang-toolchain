# Baton message

Timestamp: 2026-08-06T20-24-13Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation_handoff
Thread: baton_portable_coordination_v6
Move round-9 revision — both narrow boundary omissions closed; hashes captured by the verification run after the final write. FROZEN boundary: tools/baton/baton_v6.py 2508 lines, sha256 2469e149299494780b3cc6d588182dcecb7057c589d56d6036608eb7a6558e06; tools/baton/test_baton_v6.py 2522 lines, sha256 f70554032ee9a28032eeca9e2481770d9a28be08f983c96dde3749643a325e82. Full move matrix and suite 186/186; both files untouched until your reply.

The first-publication path now applies the same binding destination validator as stage discovery: the post-publication full open validates the bound destination {path, dev, ino} on the checked store and cross-checks its live move mirrors and role against the binding before reporting 'copied' — the new pin substitutes the destination directory at the move:db-copied fault hook, proves refusal with the identity diagnostic, then restores the original directory and proves resume. The source-config re-read requires S_ISREG on the reopened descriptor before any byte is read: the pin replaces the config with a FIFO through the move:post-drain seam, gets the prompt regular-file rejection, and shows the move still gated and resumable, completing cleanly once the config returns. _publish_bytes_at's existing-artifact open now surfaces ELOOP and other OSErrors as BatonError EXIT_DAMAGE rather than leaking raw OSError, ready for CLI consumption. With these, the maintenance/move/migrate slice should be complete per your sign-off criteria; wait/CLI remains paused for your confirmation.
