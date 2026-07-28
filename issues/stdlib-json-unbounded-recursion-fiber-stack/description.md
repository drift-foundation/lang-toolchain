Summary: std.json's DEFAULT parse depth is unbounded (`JsonLimits.max_depth =
None`), and the recursive parser's stack use scales with client-supplied
nesting — on the 256 KiB default virtual-thread stacks every web-rest-style
service parses untrusted bodies on, that is a remote-triggerable process crash
(fiber guard-page SIGSEGV).

Provenance
- Surfaced by drift-workflows cert run 20260727-043732 (debug lane): uflowsd
  SIGSEGV'd in `std.json::_parse_literal`, fault 8 bytes below RSP inside the
  serve fiber's 256 KiB stack guard page.  gdb-verified; full report:
  drift-announce 2026-07-27T062000Z (drift-workflows).
- Bracketing EXONERATED the 0.33.89 candidate (identical crash on 0.33.88,
  on old web pkgs, with and without draining) — this is a latent platform
  hazard, not a regression.  drift-workflows fixed their services by
  installing a 2 MiB-stack process-default executor.

Why this is a drift-lang issue anyway
- Their debug-lane overflow needed only a 3-level document on fat frames.  In
  RELEASE lane the same mechanism is reachable by a client that sends a
  deeply-nested body (depth scales linearly with input bytes: `[[[[...`).
  Any service that parses untrusted JSON on a default-stack fiber inherits
  the hazard.
- The LIMIT MECHANISM ALREADY EXISTS: `JsonLimits.max_depth`
  (stdlib/std/json/json.drift:213) with per-depth accounting in the parser
  (:1378) and a structured `JsonError` carrying the failing depth.  Only the
  DEFAULT (:247, `Optional::None()`) is unbounded.

Proposed fix (small, semantic)
- Change the default `max_depth` from unbounded to a bounded value sized so
  the worst-case parser stack at the limit fits comfortably inside a 256 KiB
  fiber IN DEBUG LANE (measure frames, then pick; other parsers commonly use
  64-1024).  Explicit `JsonLimits` overrides remain available for callers who
  genuinely need deeper documents.
- Behavioral change: previously-accepted pathological documents beyond the
  default become a structured parse error (fail-closed, not a crash).  Needs:
  boundary tests at limit/limit+1 in both lanes, a doc note in
  effective-drift's JSON section, a history entry, and normal corpus
  attribution for the stdlib delta.

Sequencing
- POST-certification follow-up: filed during the 0.33.89 cert cycle
  (candidate affaae95); must not invalidate it.  Not a blocker — downstream
  services can (and drift-workflows did) mitigate via executor stack policy.

Related
- issues/runtime-fiber-stack-overflow-diagnostics (the same incident's
  observability gap).
- std.concurrent default `stack_bytes = 262144`
  (stdlib/std/concurrent/conc