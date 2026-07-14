# string_arc deletion campaign — post-arrelide inventory & next-slice proposal

Status: STOP/REPORT CHECKPOINT — no implementation. Baseline: the committed
phase reference `build/tmp/cleanup-arrelide` (tool v1.5.0, 924/344/49,
`c3_moveout_not_owned` hard gate active and zero).

## 1. Full emission-class inventory (cleanup-arrelide)

| class | count | status |
|---|---|---|
| site_class:moveout_expansion | 1,851,213 | STRUCTURAL (MoveOut → Load+Zero+Store) — not a retain/release; ends only with B-repr |
| site_class:temp_lastuse_release | 618,744 | LIVE — largest remaining release class (SSA-temp last-use releases in `_note_use`/`_ensure_owned` bookkeeping) |
| site_class:overwrite_release | 233,519 | LIVE — StoreLocal/StoreRef/ArrayIndexStore old-value releases |
| site_class:scope_exit_release | 68,562 | LIVE — post-elision String scope-exit remainder (LIVE + PATH_DEPENDENT boundaries) |
| site_class:scope_exit_arraydrop | 4,620 | LIVE — post-elision Array sweep remainder (ALL path-dependent, itemized) |
| site_class:drop_before_overwrite_site4 | 14 | LIVE — tiny; site 4 has its own Tier-1 reporter |
| site_class:store_value_retain | 0 | TRIPWIRED (slice 4a) — deletion of the branch awaits a clean cert cycle (4a′) |
| site_class:call_arg_retain | 0 | corpus-zero fallback — 3 `_ensure_owned` call sites (Call / CallIndirect / CallIface args) |
| site_class:value_position_retain | 0 | corpus-zero fallback — 9 sites (2 explicit ctor-arg + 7 default-class: ArrayLit elems, element writes, exc-ABI strings, iface boxing, Result payloads) |
| site_class:return_retain_site3 | 0 | corpus-zero, structurally extinct since Phase 4; audit already fails any occurrence loudly (retired-C4 UNCLASSIFIED) — but the EMISSION path still exists (1 site) |
| site_class:destructor_self | 0 | NO emission site at all — enumeration-only residue of the retired site-local destructor-self path |

All 13 remaining `_ensure_owned` retain sites funnel through ONE function;
`destructor_self` has no site anywhere.

## 2. The 4a lesson applies verbatim

Slice 4a proved each "fallback" has TWO arms: a PROVEN-String RETAIN arm
(the corpus-zero, deletable part) and an untyped PASS-THROUGH arm
(`_ensure_owned` early-returns when `_is_string_value(val)` is false —
LIVE, exercised constantly, e.g. can-throw Ok-payload holders). The
corpus-zero claims above are claims about the RETAIN arm only. Any
tripwire conversion must:
- keep the pre-check move branches at every call site (owned single-use →
  moved, untouched);
- keep the pass-through for untyped values;
- keep `_ensure_owned`'s LAST-USE RELEASE bookkeeping (the
  temp_lastuse_release class is LIVE and partially emitted there);
- convert ONLY the terminal `StringRetain` emission.

## 3. Proposed next slice — "4b: central retain-arm tripwire" (smallest)

ONE change point: in `_ensure_owned`, replace the terminal `StringRetain`
emission with the existing `_dead_stake_tripwire(...)` (structured message,
site-class-carrying, converted to a clean `internal:` diagnostic at the
already-landed driver boundary). This fail-closes ALL THREE remaining
corpus-zero retain classes at once — call_arg_retain (3 sites),
value_position_retain (9 sites), return_retain_site3 (1 site) — because
every one funnels through that single retain. The store_value sites no
longer call `_ensure_owned` (4a rerouted them), so no interaction.

Plus one enumeration retirement: drop `destructor_self` from
`STRING_ARC_SITE_CLASSES` — it has no emission site; any future note()
with that tag becomes UNTAGGED, which is already a hard corpus gate
(fail-closed by construction, zero code kept alive for it).

Expected acceptance signature (vs cleanup-arrelide):
- corpus: universe identical; EVERY counter +0 (tripwires emit nothing for
  compiling code; the retired enumeration entry has no events); hard gates
  zero including c3_moveout_not_owned. Any wild firing = compile failure →
  compile-partition change → loud universe mismatch, the same double
  detection 4a has.
- pins: synthetic trigger pin per class family (a proven-String value
  forced into a call-arg / value-position / return-site3 fallback →
  structured tripwire message asserted, mirroring
  test_dead_store_value_stake_tripwire_fires); an UNTAGGED pin for a
  destructor_self note; the existing clean-diagnostic boundary pin already
  covers the driver surface.
- batteries: reporter suite; stage2 + borrow + guardrails; FULL memcheck
  (the change is emission-neutral for compiling code, but the tripwire is
  inside the ownership pass — memcheck stays in gate per standing rule).
- OPTIONAL belt-and-braces: promote the three site_class counters to
  HARD_GATES (they can only be nonzero if a tripwire somehow emitted
  instead of raising — cheap insurance; tool minor bump).

Explicitly NOT in 4b:
- deleting the 4a store_value tripwire branches (4a′ — awaits a clean cert
  cycle with zero firings, per the plan's tripwire-then-delete discipline);
- the LIVE classes (below).

## 4. Live classes — later campaign steps (for sequencing, not this slice)

1. **temp_lastuse_release (618,744)** — the migration target with the
   largest payoff: last-use SSA-temp releases belong to a generic authority
   (cleanup_authoring/ledger) rather than string_arc's use-count
   bookkeeping. Big slice; needs its own measurement first (mix of
   `_note_use` vs `_ensure_owned` emission points).
2. **overwrite_release (233,519)** — old-value releases at stores; natural
   sibling of site-4's ledger-driven drop-before-overwrite; migration
   would unify with the site-4 authority.
3. **scope_exit_release (68,562, String)** — the post-elision remainder is
   LIVE + PATH_DEPENDENT; the PATH_DEPENDENT share shrinks if/when the
   flag-refined ledger slice (recorded future emission slice) lands.
4. **scope_exit_arraydrop (4,620, all path-dependent)** — same dependency:
   flag-refined ledger OR fold into cleanup_authoring, then DELETE
   `_drop_all_arrays` outright.
5. **drop_before_overwrite_site4 (14)** — fold into (2) when it happens.
6. **moveout_expansion (1,851,213)** — structural; ends with B-repr, not
   this campaign.

## 5. STOP

Awaiting approval of the 4b slice (scope, pins, acceptance signature as
above) before any code change.
