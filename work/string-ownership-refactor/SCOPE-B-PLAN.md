# String Scope B — representation & authority plan

Date: 2026-07-08 (v2 — revised same day under relaxed constraints; see §10).
Status: PLAN ONLY — no compiler/runtime changes until reviewed.
Context: Scope A landed and certified (0.33.75): String classification is structural and
mode-independent; alias-to-owned transfer is centralized (`_mark_ref_alias_if_non_bitcopy`).

**v2 constraint update (maintainer):** ABI breakage is acceptable (Drift is not in wide
public use — ABI 20→21, runtime rebuilds, pool recert are costs, not blockers), and C
direct-field interop is a compatibility preference, not a hard constraint (an explicit
accessor/conversion API for bytes/len is fine). §§1–8 below remain the factual baseline;
§10 re-ranks the options and replaces §9's recommendation under the new assumptions.

## 0. The headline finding

"Scope B" as originally sketched bundles two independent projects:

- **B-arch: authority unification** — make String retain/release stakes visible to the
  ownership ledger (today `string_arc.py` is a separate, late authority). This is the real
  architectural debt, it is the source of the audit's "authority timing" rule, and it is
  **ABI-NEUTRAL** — no representation change required.
- **B-repr: representation reshape** — change `DriftString {len, data}` and/or its heap
  block. This is the part that costs an ABI bump, a full cert rebuild, and downstream
  migration — and on inspection it buys almost nothing on its own today.

Everything below flows from keeping these separate.

## 1. DriftString representation options

Current (B0): handle = `{ drift_isize len; char* data }` (16B, by value); heap block =
`[ {atomic u64 rc; u64 flags} | bytes | NUL ]` with `data` pointing AT the bytes (header at
`data-16`); `flags&STATIC` makes retain/release no-ops; empty string = `{0, NULL}`.

| Option | Handle | data→bytes preserved | What it buys | What it costs |
|---|---|---|---|---|
| **B0 status quo** | `{len, ptr}` 16B | yes | — | — |
| **B1 ArcBox-unified** (`Arc<[Byte]>`-shaped: ptr→header at offset 0, len in header) | `{ptr}` 8B | **NO** — bytes at `ptr+hdr` | one RC machine shared with Arc; `drop_thunk`/weak slots; opens the door to String-in-stdlib-Drift (TCB shrink, ties into the parked stdlib-split) | full ABI bump; every `.data` reader and both C conventions reopened; String pays for a `drop_thunk`+weak it never uses; `len` becomes a load (perf regression for the hottest field); static literal shape reworked |
| **B2 thin handle** (`{ptr}`, len moves into the behind-header; header stays behind data) | `{ptr}` 8B | yes | smaller by-value handle (register-friendlier calls); `data` semantics intact | ABI bump anyway (handle size/borders change every signature + FnResult payloads + struct layouts); `len` becomes a load; static literal + codegen header constants change |
| **B3 SSO** (inline ≤N bytes in a 24B handle, heap beyond) | `{tagged 24B}` | only for heap case | real perf potential for short strings (allocation-free) | the big one: every helper branches; retain/release become conditional; SSO strings are effectively bitcopy → classification forks AGAIN (undoes Scope A's uniformity); **`data` pointers into a by-value-moved handle dangle** — outright hostile to conventions A/B and to `s.data` C readers; largest test/audit churn |
| **B4 builder handle** (`{len, data, cap}` 24B) | 24B | yes | in-place append/amortized concat | ABI bump; pushes toward mutation semantics String doesn't have; concat perf is better served by a separate `StringBuilder` type (no ABI cost) |

Assessment: none of B1–B4 fixes a **correctness** problem. B1 is elegant but trades String's
two hottest properties (inline `len`, direct `data`) for machinery it doesn't need. B3 is the
only option with a concrete win (perf), and it's the most dangerous for the existing interop
contract. B4's benefit is achievable ABI-neutrally with a new type.

## 2. Runtime helpers & static literal layout

All 14 exported helpers (`drift_string_literal/from_*/concat/eq/cmp/retain/release/free/
to_cstr`) take/return `DriftString` **by value** — any handle reshape changes every signature
(ABI). The static-literal layout is emitted by codegen as a hardcoded
`{ i64 rc=1, i64 flags=1, [N+1 x i8] }` constant (`_lower_const_string` +
`_emit_string_literal_value`, with a per-module literal cache) and pinned runtime-side by
`_Static_assert`s on `DriftStringHeader`. That header shape is baked into **every compiled
object** — a reshape invalidates all `.o`/`.a` artifacts even where signatures look unchanged.
Codegen has ~145 `DriftString`/`DRIFT_STRING_TYPE` references and ~30 direct helper-call
sites; 9 runtime C/header files mention `DriftString`.

## 3. C interop: `data` points-to-bytes

**Recommendation: preserve it; do not change the API.** The contract is load-bearing three
ways: (1) ~27 direct `s.data` readers inside the runtime itself (string/console/array); (2)
the by-value conventions A/B and the `DRIFT_OWNED_STRING` cleanup macro, enforced by
`test_drift_owned_string_audit.py`; (3) external FFI in certified packages (mariadb wire
protocol, net-tls) written against `{len, data}` with NUL-terminated bytes. If a future
representation must move the header, choose a shape that keeps `data` aimed at bytes
(B2-style, header still behind the pointer) — an accessor-API migration
(`drift_string_bytes(s)`) is possible but is pure downstream tax with no compensating win.

## 4. `string_arc.py` — remains, shrinks, or merges?

**Shrinks, then merges — via authority timing, not representation.** Today string_arc
(1,736 lines) is a late-rewrite pass with its own liveness/definite-assignment/moved-out
analyses, inserting `StringRetain/StringRelease` AFTER the ledger snapshot — so the ledger
cannot see String stakes (the audit §3.4 rule). Scope A's mutation-testing note (the
capture-slot marks "not provably load-bearing for String because string_arc independently
covers it") is the smell: two authorities computing overlapping answers. Two unification
paths from the audit:

- (a) **emit String retain/release early** (HIR→MIR lowering, ledger-visible) — end state:
  string_arc's analyses are deleted; the ledger + cleanup_authoring become the sole authority
  for String exactly as for Arc/Destructible. ABI-neutral.
- (b) rebuild the ledger after string_arc — cheaper but keeps two emitters; a waypoint at
  best.

Direction: (a), staged (see §8). Interacts with the already-queued **ledger-cache-safety
slice** (dirty-bit on `func._ownership_ledger`) — that should land FIRST since staged
authority moves multiply ledger rebuilds.

## 5. Copy semantics — keep retain-copy Copy

Keep it. Scope A just made "String is Copy (retain-copy) + needs-drop" **structural and
mode-independent**, unified isolated/production classification, and re-pinned the entire
stage2/drop-policy test surface on it. Move-only String would be a language redesign:
`or_throw`/format/concat chains would need explicit clones everywhere, ConstShare interplay
reopens, and the composite-Copy boundary we just defended (declared-impl `Tag{label:String}`)
would need rework. Nothing in the recurring bug class was caused by Copy-ness — every
incident was a lowering path missing the alias-marking/retain call, which Scope A's
centralization addresses. Reconsider only with a concrete ergonomics RFC, not inside a
representation project.

## 6. ABI / cert / downstream impact (if B-repr proceeds)

- `DRIFT_RT_ABI_VERSION` 20 → 21; `libdrift_rt_abi21.a`; every compiled artifact invalid.
- Full recert of the certified pool: 8 packages (mariadb-rpc, mariadb-wire-proto, microflows,
  net-tls, singular, web-client, web-jwt, web-rest) + 2 apps (mariadb-failpoint-proxy,
  uflowsd) rebuilt and re-certified.
- Downstream: DriftQuery (just unblocked on 0.33.76) recompiles; any of their C externs
  touching `DriftString` by value re-audited against the new conventions.
- B-arch alone: **none of the above** — compiler-internal, fix-and-keep versioning.

## 7. Test & migration blast radius (B-repr)

memcheck suite (38 files), ownership matrix (om_* rows), ~120 string-related e2e fixtures,
`test_drift_owned_string_audit.py`, every ASAN/Valgrind row exercising strings, DI/layout
tests, plus the codegen golden expectations that embed the literal header shape. B-arch's
radius is the compiler-side subset only (string_arc tests, ledger tests, memcheck/ownership
rows) with no fixture or runtime churn.

## 8. Smallest coherent slice

**Slice B-arch-0 (recommended next, regression-first, observational):** a differential
audit — rebuild the ledger on post-string_arc MIR and REPORT divergences between string_arc's
decisions and the ledger's verdicts (reporter-only, zero behavior change; same
reporter-before-enforcement pattern as Phase 3A). This measures exactly how far the two
authorities disagree and hands B-arch-1 its worklist.
**Slice B-arch-1:** move ONE stake shape (returned-String retain — the documented
ledger-blind case) into HIR→MIR emission; teach string_arc to recognize and skip it; delete
the corresponding special case. Repeat per shape until string_arc's own analyses are dead
code. Each step is ABI-neutral and independently certifiable.

## 9. Recommendation (v1 — SUPERSEDED by §10 under relaxed constraints)

v1 recommended: split; defer B-repr indefinitely (ABI/downstream cost treated as decisive);
adopt B-arch after ledger-cache-safety; enter via B-arch-0. §10 revisits with ABI cost and
C-field compatibility explicitly removed as blockers.

## 10. v2 — re-ranking under relaxed constraints

### 10.1 The one fact that survives the constraint change

With ABI and C-layout costs struck from the ledger, the decision hinges purely on **which
lever moves which complexity** — and the codebase answers that unambiguously:

- Codegen ALREADY unifies String into the generic ownership dispatch:
  `_emit_copy_value(String)` → `drift_string_retain`, `_emit_drop_value(String)` →
  `drift_string_release` (`llvm_codegen.py:9383/9785`). Representation is invisible at this
  layer — a reshape swaps which helper symbol is called, nothing more.
- `string_arc.py` exists because String is **implicit-copy** (retain-copy Copy): somebody
  must AUTHOR the implicit stakes. Arc never needed an equivalent pass — not because of its
  representation, but because Arc is **clone-explicit** (`@intrinsic clone(self: &Arc<T>)`,
  `arc.drift:287`): its stakes are user-written calls, MIR-visible to the ledger from birth.
- Therefore, keeping String Copy (which we do — §10.5), **no representation whatsoever
  removes the authoring problem**. Only moving the authoring earlier (B-arch) does.

So the relaxed constraints change B-repr's *disposition* (from "defer indefinitely" to
"schedule deliberately") but cannot change the *order*: B-repr done first would mean porting
`string_arc.py`'s 1,736 lines to a new representation and then deleting them in B-arch —
paying migration twice.

### 10.2 Answers to the five questions

**Does ArcBox-unified String become attractive?** Partially — as a strategic direction, not
as the next slice, and not in its literal form. Full `ArcBox` gives String a `drop_thunk`
(waste: String's drop is statically known — codegen calls release directly, no dispatch
needed) and a weak count (no use case). What IS newly attractive with ABI off the table is
the **header-at-offset-0 discipline** and the door it opens to implementing String as a
stdlib Drift type over the same primitives as Arc (`RawBuffer` + `lang.atomic`), shrinking
the hand-written C TCB — a real win that compounds with the parked stdlib-split. The lean
form of that is B5 below, not literal `Arc<[Byte]>`.

**Thin smart handle + `drift_string_data/len` accessors — does it simplify compiler/runtime
ownership?** No. Ownership complexity lives entirely in *who retains when* (pass
architecture), not in *where len lives* (data layout). A thin `{ptr}` handle costs a memory
load on every length access — `len` is the hottest String field (bounds, eq short-circuit,
concat sizing) — and buys an 8-byte handle plus accessor indirection for C. Codegen already
treats `DriftString` as a first-class two-word LLVM struct; nothing gets simpler. Accessors
are fine as an interop STYLE (adopt them in B5 regardless of handle shape), but they are
orthogonal to ownership.

**Does moving String into Arc's ownership model reduce `string_arc`/ledger complexity enough
to justify B-repr before/with B-arch?** No — this is the crux, and the answer is a clean
negative. "Arc's ownership model" = explicit clone + ledger-visible stakes. String only
enters that model by (a) becoming clone-explicit — rejected, that's move-only ergonomics by
another name — or (b) having its implicit stakes emitted at HIR→MIR time where the ledger
sees them. (b) is exactly B-arch, and it is representation-independent. B-repr contributes
zero lines of `string_arc` deletion on its own.

**Source ergonomics?** Unchanged: String stays Copy/retain-copy. Scope A made that
classification structural and mode-independent and re-pinned the test surface on it; every
incident in the recurring bug class was a missed retain on a lowering path, never Copy-ness
itself. Under any B-repr shape, Copy remains "bump the strong count" — semantics identical,
representation invisible to Drift source.

**Cleanest long-term architecture, optimizing for correctness and simplicity?**

1. **Single ownership authority** (B-arch end-state): String stakes are CopyValue/DropValue
   emitted at HIR→MIR, ledger-visible, authored by cleanup_authoring exactly like
   Arc/Destructible. `string_arc.py` and the private `StringRetain`/`StringRelease` MIR
   vocabulary are deleted. This is ~90% of the correctness-and-simplicity payoff.
2. **B5 "RcBytes" representation** (the v2-preferred reshape, replacing v1's B1/B2 framing):
   handle stays two words `{len, ptr}`, but `ptr` now points AT a header-at-offset-0
   `{ _Atomic u64 strong; u64 flags }` with bytes following (`bytes = ptr + 16`, exposed to
   C via `drift_string_data/len` accessors only). Keeps inline `len` (no hot-path
   regression), keeps the static-literal flag, drops the behind-the-pointer aliasing trick
   (the `data-16` header access that sanitizers, debuggers, and every new contributor find
   exotic), and gives String the same block discipline as Arc — making an eventual
   stdlib-Drift String implementation a mechanical port rather than a redesign. No
   drop_thunk, no weak count. Empty string: static empty singleton (flags=STATIC), retiring
   the `{0, NULL}` special case — one fewer branch in every helper.
3. **Explicitly rejected even under relaxed constraints:** SSO (B3) — it re-forks the
   classification Scope A just unified (some Strings become bitcopy) and reintroduces
   per-shape special cases across every helper; that is a correctness-and-simplicity
   regression bought with performance we have not measured a need for. Builder handle (B4) —
   still better served by a separate type.

### 10.2.1 B5 native model and C-string API decisions (pinned 2026-07-15)

This subsection pins the maintainer-review decisions that refine B5 from a C-layout sketch
into a Drift-native representation plus explicit C interop surface.

**Native model.** Drift `String` is an immutable UTF-8 byte string, conceptually
`ImmutableBytes<Utf8>` / `RcSlice<Byte>`, but specialized by the compiler/runtime because it
is hot:

```
pub final type String {
    len: Uint
    storage: RcBytes
}

internal final type RcBytes {
    strong: AtomicUint
    flags: RcBytesFlags
    bytes: [Byte]   // runtime tail payload, always followed by one hidden NUL byte
}
```

The C struct spelling is an implementation artifact, not the language model. Native Drift
code observes operations (`len`, byte access, compare, hash, concat, encode), not fields.
Copy remains retain-copy; Drop releases `storage`; mutable construction belongs in a
separate `StringBuilder` / `BytesBuilder` that freezes into `String`.

**Representation decisions.**
- Keep inline `len`; do not move length behind the header.
- Every String allocation reserves `len + 1` bytes and writes a hidden trailing NUL at
  `bytes[len]`. This makes borrowed C-string views zero-copy in the common case.
- `String` storage is exact, no offset/slice view inside the String handle. Shared substring
  views, if added, are a separate `StringView`-style type and do not get the zero-copy C-string
  promise.
- No SSO in B5. The correctness cost (branchy retain/release and re-forked classification)
  is not worth unmeasured allocation wins.
- `RcBytesFlags` starts conservative: `STATIC` / `IMMORTAL` and interior-NUL cache state
  (`NO_INTERIOR_NUL_KNOWN`, `HAS_INTERIOR_NUL`) are the only planned semantics. Other bits are
  reserved; no weak count and no drop thunk.

**C interop posture.** C never relies on the native `String` field layout. C code uses explicit
borrowed or owned APIs whose names carry lifetime/ownership.

Length-aware borrowed bytes always work, including strings containing `NUL`:

```
with_bytes(s, |ptr, len| { ... })
```

Checked borrowed C strings are Rust-like: native strings may contain interior NUL, but C-string
conversion is fallible. The checked helpers validate or use the cached interior-NUL flag, then
borrow the hidden trailing-NUL storage:

```
with_cstr(s, body)  -> Result<T, CStringError>
with_cstr2(a, b, body) -> Result<T, CStringError>
with_cstr3(a, b, c, body) -> Result<T, CStringError>
with_cstr4(a, b, c, d, body) -> Result<T, CStringError>
```

`CStringError` includes `InteriorNul(index: Int)`. In throwing code, callers use
`.or_throw()` / auto-try; in `nothrow` code they match the `Result`. The arity helpers exist
specifically so common multi-parameter C calls do not require nested `match` ladders.

Unchecked C-string helpers are intentionally named unsafe and do no scan:

```
with_cstr_unsafe(s, body)
with_cstr2_unsafe(a, b, body)
with_cstr3_unsafe(a, b, c, body)
with_cstr4_unsafe(a, b, c, d, body)
```

They are memory-safe under the callback lifetime, but semantically unsafe: if a String contains
an interior NUL, C observes only the prefix. They are for callers that have already validated
or constructed the inputs under a stronger invariant.

For complex cases, provide an opaque `CStringScope`:

```
with_cstring_scope(|scope| {
    val p = scope.cstr(...);     // checked Result
    val q = scope.cstr_unsafe(...);
    val argv = scope.argv(...);
    ...
})
```

The scope owns internal pins/temps. Users never index into `pins`; they receive raw pointers
or opaque scoped handles (`CArgv`) valid only until the callback returns. Owned C allocations
(`OwnedCStr` / `OwnedCBytes`) are separate APIs reserved for handoff to C libraries that take
ownership.

### 10.3 v2 recommendation: **B-arch first, then B-repr(B5) as a committed follow-on — sequenced, not combined**

1. **Order is forced by the dependency structure, not by ABI caution:** B-repr before
   B-arch ports `string_arc.py` to a new layout and then deletes it; combined B-arch+B-repr
   stacks an authority migration, a representation cutover, a full-pool recert, and ~120
   fixture updates into one gate — maximal blame-ambiguity when something regresses, against
   everything this project's slice discipline has practiced. Sequenced, each lands with its
   own clean verification story.
2. **What changes vs v1: B-repr(B5) graduates from "deferred pending a driver" to a
   scheduled slice** with its own entry criteria, immediately after B-arch completes: ABI
   20→21, `libdrift_rt_abi21.a`, pool recert (8 pkgs + 2 apps), DriftQuery recompile,
   accessor-based C API (`drift_string_data/len`, conversions), static-literal layout
   regenerated. All of it is now budgeted cost, not blocker.
3. **Sequencing (v2.1 — ledger-cache-safety verified already in-tree, §11.1):**
   B-arch-0 (differential reporter) → B-arch-1..n (per-shape stake migration until
   `string_arc.py` is dead) → **B-repr(B5)**. The reporter's divergence inventory doubles as
   the B-repr test worklist: every site it names is a site the reshape must re-verify under
   ASAN/Valgrind.
4. **Success criteria for the pair:** after B-arch, `string_arc.py` deleted and the memcheck
   + ownership-matrix suites green with the ledger as sole authority; after B-repr(B5), the
   only String-specific special case left in the compiler is the static-literal emitter, and
   the C TCB's string surface is a candidate for stdlib-Drift reimplementation (stdlib-split
   prerequisite satisfied for String).

## 11. Execution details (v2.1, pre-B-arch-0 review items)

### 11.1 Ledger-cache-safety prerequisite: **SATISFIED — already in-tree**

The plan's earlier "ledger-cache-safety slice → B-arch-0" sequencing was based on a stale
roadmap note. Verified in-tree: `stage2/ledger_cache.py` (231 lines) implements the full
design — dirty-bit runtime assertion (`mark_ledger_dirty` after every direct MIR mutation in
the four scoped passes; `require_fresh_ledger`/`maybe_fresh_ledger` on every read;
`build_and_attach_ledger` clears the bit) — and both companion tests exist and run in the
driver suite (`test_ledger_cache_safety_dirty_bit.py`,
`test_ledger_cache_safety_mutation_audit.py`, the static mutation-pattern audit with inline
allow markers). All four consumers are wired: `cleanup_authoring`, `match_cleanup_authoring`,
`drop_flags`, `string_arc`. **There is no remaining ledger-cache slice.** The sequence
collapses to: B-arch-0 → B-arch-1..n → B-repr(B5). (The `work/ledger-cache-safety/` plan dir
was wiped at branch close, per work/-is-ephemeral policy; the module docstring carries the
design summary and names the motivating regressions fdd1461b/849f00b1/c3344d86/fe8ca104.)

### 11.2 B-arch-0 reporter contract (bounded, not open telemetry)

Reality check driving the design: the ledger has NO event model for
`StringRetain`/`StringRelease` (they are string_arc's private MIR vocabulary, invisible to
`ownership_ledger.py`), and the existing `ownership_ledger_reporter.py` is drop-verdict
oriented (`DisagreementRecord`, `classify_verdicts`, `compare_events`, `collecting_emit`).
B-arch-0 EXTENDS that reporter pattern; it does not fork a new framework and does not add
stake events to the ledger (that is B-arch-1's job, per shape).

**Event model.** One record per string_arc-emitted instruction:
`StringStakeEvent = (fn_id, block, post_index, kind, subject, site_class)` with
`kind ∈ {RETAIN, RELEASE, MOVEOUT_EXPANSION}` and `site_class` drawn from the CLOSED
enumeration of string_arc's emission sites (tagged at the emission point, not inferred):
`{call_arg_retain, overwrite_release, scope_exit_release, return_retain_site3,
drop_before_overwrite_site4, moveout_expansion, destructor_self}`. An emission point that
fits no tag is itself a finding (`UNTAGGED`) — the enumeration is part of the deliverable.

**Snapshots.** Exactly two, both already legal under the dirty-bit machinery:
`L_pre` = the fresh ledger attached when string_arc starts (today's input), and
`L_post` = `build_and_attach_ledger` re-run on string_arc's OUTPUT MIR. No intermediate
per-mutation snapshots.

**Divergence classes (closed set; each is one comparison, not a dataflow analysis):**
- **C1 release-vs-verdict**: for each RELEASE at a scope-exit/return program point, does the
  ledger verdict at that point say MUST_DROP? Records `release_without_must_drop`
  (double-authority / over-release candidate) and `must_drop_without_release`
  (leak candidate) per local.
- **C2 retain-vs-copy-visibility**: for each RETAIN, is there a ledger-visible copy-consume
  (CopyValue / consumed LoadLocal) of the same subject at that point in the PRE MIR? Records
  `invisible_stake` — the exact inventory B-arch-1 migrates, shape by shape.
- **C3 moveout-vs-state**: each MOVEOUT_EXPANSION where `L_pre` state of the local is not
  Owned at that point.
- **C4 known-divergence allowlist**: `return_retain_site3` is EXPECTED divergent (the
  documented ledger-blind returned-String case) — counted under its own bucket, never
  reported as a failure. The allowlist is explicit in code and in the report.
- **UNCLASSIFIED**: anything else. Hard-capped at 50 detailed records per class per corpus
  run (counts always exact, details truncated) — the anti-telemetry-creep bound.

**Report artifact.** Off by default. Enabled by `DRIFT_STRING_ARC_AUDIT=1` (mirroring the
`drift_debug` conventions): per-fn JSONL records + an end-of-compile aggregate
(counts per class × site_class) to stderr or `DRIFT_STRING_ARC_AUDIT_FILE`. The corpus run's
aggregate goes into `work/string-ownership-refactor/B-ARCH-0-INVENTORY.md`.

**Pass/fail criteria.** B-arch-0 is observational; its gate is:
1. Reporter off: full suite bit-identical behavior (it must add zero instructions and zero
   diagnostics).
2. Reporter on over the compile corpus (driver + e2e + memcheck test sources): every
   divergence lands in C1–C4; UNCLASSIFIED = 0 is the target, and any UNCLASSIFIED entry is
   triaged (allowlisted with rationale, or promoted to a new named class) BEFORE B-arch-1
   starts.
3. Deliverable: the inventory doc with per-class counts and the ranked B-arch-1 shape
   worklist (expected ranking: C4 site-3 return first — already root-caused — then C2
   invisible-stake shapes by count).
B-arch-0 makes NO fixes: any genuine bug it uncovers (a C1 leak candidate that reproduces,
say) files as its own regression-first slice, not as scope creep on the reporter.
