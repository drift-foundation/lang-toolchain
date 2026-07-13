Summary: intake for firings of the string_arc dead-stake tripwire
(`internal: string ownership stake contract failure (string_arc dead-stake
tripwire [<site-class>]: ...)`)

What this error means
- `string_arc`'s store-path stake fallback (`_ensure_owned` with site class
  `store_value_retain`) is FAIL-CLOSED as of string-cleanup slice 4a
  (2026-07-13): the RETAIN arm of that fallback — reached only for a
  proven-String stored value that is neither move-only nor an owned
  single-use temp — was driven to zero corpus-wide (B-arch-1d, 7,624 → 0;
  the C2 ZeroValue fix removed the last wild carrier) and is scheduled for
  deletion after a clean certification cycle.
- A firing therefore means the compiler reached a stake-emission path that
  the whole 924-fixture corpus plus a full cert cycle claimed unreachable:
  a LANGUAGE_BUG in stake classification (string_stakes/string_arc
  producer contracts), NOT a user error. The user's source is valid.
- Values WITHOUT String type metadata are NOT tripwired — they keep the
  fallback's historical pass-through. If you are reading this because of a
  firing, the value was proven String and would have received a
  late (ledger-invisible) retain.

Required repro info (all of it is in the diagnostic message)
- The full diagnostic line, including: site class, fn symbol,
  `block '<name>'[<index>]`, the SSA value id, the store target, and the
  best-effort producer instruction name.
- The complete compiling source (all files of the compilation unit) and the
  exact driftc invocation.
- Toolchain version + ABI (`driftc --version`).

Triage starting points
- `lang/driftc/stage2/string_arc.py` — `_dead_stake_tripwire` and the three
  store arms (StoreLocal / StoreRef / ArrayIndexStore).
- `lang/driftc/stage2/string_stakes.py` — the pass that OWNS store staking;
  a firing usually means a producer shape its `_is_string_value_view` /
  owned-at-extraction classification does not cover.
- `lang/tests/stage2/test_string_arc_audit_reporter.py` — the tripwire
  message pin and the owned-at-extraction classification pins.

Status: no known firings. This directory intentionally contains no repro —
it is the intake location the diagnostic points users at.
