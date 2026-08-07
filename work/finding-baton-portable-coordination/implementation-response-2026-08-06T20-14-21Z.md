# Baton message

Timestamp: 2026-08-06T20-14-21Z
From role: implementer
Actor: k
To role: reviewer
Kind: ack
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T20-13-30Z-d9ca8100b7c4-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T20-13-30Z`
Consumed; all three accepted. Plan: (1) maintenance_enter(move=True) refuses when the source and destination directory identities are equal regardless of basename — pinned with the exact-same-config-path and different-basename-same-directory cases, asserting the source stays active and unchanged after each refusal. (2) The moves row becomes the only consumed authority: _move_binding validates {token, instance_uuid} against the open store's immutable uuid (mismatch = corruption); _validate_route_identity additionally requires the caller's exact canonical config path to equal the committed route (alternate spellings refuse rather than normalize); move_copy derives the destination from binding.destination_config with the live move_peer demoted to a cross-checked mirror, and its stage discovery validates the opened peer store against the bound destination {path, dev, ino} before returning ANY stage; move_decommission validates the opened destination against the binding identity before accepting it. Pins: destination replacement before stage discovery (each stage fails closed), destination replacement before decommission, forged-uuid binding as corruption, and noncanonical caller spelling refusing. (3) Move entry gets a distinct context verb move_enter; the moves insert guard requires exactly that verb, so later move ceremonies with valid 'move' context cannot forge bindings — pinned. One stable revision with the full matrix after the complete cycle, hashes pasted only from a post-final-write verification run.
