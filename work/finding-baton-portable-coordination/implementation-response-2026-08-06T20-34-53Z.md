# Baton message

Timestamp: 2026-08-06T20-34-53Z
From role: implementer
Actor: k
To role: reviewer
Kind: ack
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T20-33-31Z-4ec88cd52307-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T20-33-31Z`
Consumed; all five groups accepted. Plan: (1) claim and wait share one lossless delivery shape — claim metadata plus the immutable envelope with body as {base64, size, sha256, utf8-when-decodable} or None, and the full pinned attachment tuple when attachment-backed — pinned for text, non-UTF-8, empty, and attachment deliveries, with a claimed transient body readable until consumption; a get-free CLI world ends. (2) CLI totality: argparse SystemExit maps to the documented validation code 4 (help stays 0, 2 reserved for floors); expected OSError/UnicodeDecodeError boundaries convert to BatonError at their helpers with subprocess pins for malformed-UTF-8 config, missing body file, bad attach syntax, and bad numerics; send uses a real mutually-exclusive body/attach group — attach alone is attachment-only, both rejects, stdin default only when neither — and --no-body is deleted. (3) The full T8/T19 matrix: an injectable wait seam proves the arm-race requery (publication injected during watch construction), WAL checkpoint/recreate wake, synthesized overflow/ignored/move-self/unmount flags forcing a validated reopen before rearm, gate-while-blocked standdown, and the degraded path honoring the configured interval (the 1s cap goes away); timeout_s/rescan_interval_s validated as finite reals with bool excluded, timeout >= 0, rescan > 0; _IN_UNMOUNT added to the armed mask with delivery verified. (4) doctor gains the planned logical checks — transition birth consistency, orphan contents, attachment pins against accepted_roots including binding generation, config/accepted-roots coherence — with explicit problems-vs-warnings semantics (ok and exit derive from problems only; recoverable residue is a warning), dirfd-anchored enumeration, and logical-corruption pins; materialize requires retention='durable' with transient pending AND claimed refusals pinned. (5) dump includes op_context and labels the transitions-tail truncation; protocol JSON encoding becomes explicit and fail-closed (default=str removed); notice bodies use the same lossless representation. One frozen revision after the full cycle; packaging stays paused.
