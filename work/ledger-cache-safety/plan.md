# Ledger cache safety slice — implementation plan

**Status:** planned, NOT started.  Sequenced after mariadb v1 cert / Condvar acceptance.

**Outcome of the architectural read on 2026-05-16.**  See the chat
transcript referenced in `memory/project_ledger_cache_safety_slice.md`
for the discussion that produced this plan.

## Decision

Land a focused "ledger cache safety" slice as the next toolchain
hardening item.  **Do NOT** open a broad ownership refactor.

## Why

The recent bug pattern crossing `cleanup_authoring` /
`drop_flags` / `string_arc` / driver ledger rebuilds (commits
`fe8ca104`, `fdd1461b`, `849f00b1`, `c3344d86`) is concentrated
at the ledger-handoff boundary between passes, not inside any
single pass.  Splitting the modules differently will not move
the bug count.  The cheap, high-leverage fix is making ledger
staleness a runtime assertion instead of a discipline rule
documented in the `cleanup_authoring.py` pipeline-order
docstring.

## Sequencing rule

Do NOT put this in front of the current mariadb v1 acceptance.
Finish certifying the Condvar / ManagedConnection path first.
Land the dirty-bit guard as the NEXT toolchain hardening slice —
**unless** another stale-ledger bug appears in the mariadb cert
path first, in which case promote it.

## Target contract (K-approved framing, verbatim)

1. Any MIR mutation after a ledger build marks the function's
   ownership ledger dirty.
2. Any consumer that reads `func._ownership_ledger` must assert
   it is not dirty.
3. Rebuilding the ledger clears the dirty bit.
4. Every direct MIR mutation in stage2 ownership/cleanup passes
   must explicitly call `mark_ledger_dirty(func, reason)`.
   Reviewed and enforced by the static audit test below; the
   only escape valve is an inline allow marker carrying a free-
   text reason (for mutations before ledger attach or against
   not-yet-inserted blocks).

## Short implementation plan

### Inventory (audit before coding)

The bug class we are catching is **stale reads of
`func._ownership_ledger` after a post-HIR→MIR pass mutated MIR
without rebuilding**.  `MirBuilder.emit(...)` runs during
initial HIR→MIR construction — before any ledger is attached —
so the load-bearing surface is **explicit direct mutations** in
later passes, not the builder.  Frame the first pass around
those.

Three role categories.  Every call site in
`drop_flags.py` / `cleanup_authoring.py` /
`match_cleanup_authoring.py` / `string_arc.py` is in one of
these:

1. **MIR mutators that must mark dirty after mutating** —
   direct writes like:
   - `blk.instructions = [...]` (whole-list replacement)
   - `blk.instructions.append(...) / .insert(...) / del`
   - `func.blocks[i] = ...` (block replacement)
   - terminator rewrites (`blk.terminator = ...`)
   - block-list edits (`func.blocks.append(...)` for new BBs)

   Every such site in drop_flags, cleanup_authoring,
   match_cleanup_authoring, string_arc gets an explicit
   `mark_ledger_dirty(func, reason)` call immediately after
   the mutation.  This is the bulk of the slice's edits.

2. **Attached-ledger consumers** (read
   `func._ownership_ledger`) — route through
   `require_fresh_ledger(func, consumer_name)`:
   - `cleanup_authoring.py` — every `verdict_at` read.
   - `match_cleanup_authoring.py` — same.
   - `string_arc.py` — every `_DropVerdict` consultation.
   - `driftc.py:~7146` — observe/reporter path before
     `ownership_ledger_reporter.compare_events`.  Without
     this, debug-mode has a blind spot K flagged.

3. **Fresh-local-ledger builders** — passes that build their
   own ledger and don't read the attached one.  They do NOT
   need `require_fresh_ledger`, but they DO appear in
   category 1 because they mutate MIR after building:
   - `drop_flags.py:90` calls
     `build_ledger(func, drop_policy=drop_policy)` for
     planning, then inserts flag-store ops — mark dirty
     after those inserts.

Out of scope: pass-local data (e.g.
`func._drop_flag_managed_locals` set updates), MirBuilder
during HIR→MIR (no ledger yet), and codegen/SSA/mir_validate.

### Proposed helper surface

New module `lang/driftc/stage2/ledger_cache.py`:

```python
def build_and_attach_ledger(
    func: MirFunc,
    drop_policy: DropPolicy,
    *,
    reason: str = "fresh-build",
) -> OwnershipLedger:
    """Standard driver path: build the ledger AND attach to
    `func._ownership_ledger`, clearing the dirty bit.  Returns
    the new ledger.  Use this anywhere the current code does
    `ledger = build_ledger(...); setattr(func,
    '_ownership_ledger', ledger)` — driftc.py has four such
    sites today (7032/7056/7101/7129).

    `reason` is recorded for diagnostics; it does NOT mark dirty
    — the ledger is fresh on return.
    """

def attach_ledger(func: MirFunc, ledger: OwnershipLedger) -> None:
    """Lower-level: attach an externally-built ledger.  Clears
    the dirty bit.  Prefer `build_and_attach_ledger` for the
    driver path; this exists for tests and pass-local cases."""

def mark_ledger_dirty(func: MirFunc, reason: str) -> None:
    """Mark `func._ownership_ledger` as stale.  Call IMMEDIATELY
    AFTER any direct MIR mutation in stage2 ownership/cleanup
    passes.  `reason` is free-text (`"drop_flags.insert_flag_store"`
    style); it appears in the staleness assertion message.  No-op
    if no ledger is attached."""

def require_fresh_ledger(
    func: MirFunc, consumer: str
) -> OwnershipLedger:
    """Assert the ledger is attached AND not dirty, then return
    it.  AssertionError message names `consumer` and the last
    `reason` that marked dirty.  Use at every read of
    `func._ownership_ledger` in stage2 ownership/cleanup
    consumers AND in the driftc.py observe/reporter debug
    path."""

def maybe_fresh_ledger(
    func: MirFunc, consumer: str
) -> Optional[OwnershipLedger]:
    """Returns the ledger if attached AND fresh, else None.  For
    paths that currently no-op when no ledger is attached (e.g.
    optional observe paths, test helpers that may run pre-build).
    Default-on in the driver should be `require_fresh_ledger`;
    only convert to `maybe_fresh_ledger` with an explicit
    justification per call site."""
```

`MirFunc` gains two attributes (already partially present —
`_ownership_ledger` is read at `driftc.py:7146`):
- `_ownership_ledger: Optional[OwnershipLedger]`
- `_ledger_dirty_reason: Optional[str]` (None when fresh;
  non-None ⇒ dirty)

### First-pass enforcement scope

Edits land in these files only:
- `lang/driftc/stage2/ledger_cache.py` — new module, the five
  helpers above.
- `lang/driftc/stage2/drop_flags.py` — `mark_ledger_dirty` after
  every direct MIR mutation; no `require_fresh_ledger` (builds
  fresh local ledger).
- `lang/driftc/stage2/cleanup_authoring.py` — both
  `require_fresh_ledger` at every `verdict_at` read AND
  `mark_ledger_dirty` after every emission.
- `lang/driftc/stage2/match_cleanup_authoring.py` — same as
  cleanup_authoring.
- `lang/driftc/stage2/string_arc.py` — `require_fresh_ledger` at
  `_DropVerdict` reads; `mark_ledger_dirty` after MoveOut
  expansion and retain/release inserts.
- `lang/driftc/driftc.py` —
  - Convert the four `build_ledger(...); setattr(...,
    '_ownership_ledger', ledger)` sites to
    `build_and_attach_ledger(...)`.
  - Route the observe-reporter debug-block read at line ~7146
    through `require_fresh_ledger` (K's medium finding —
    closes the debug-mode blind spot).

NOT yet:
- `MirBuilder` auto-dirty hooks.  MirBuilder runs during initial
  HIR→MIR; no ledger exists at that point.  Adding the hook
  would be a no-op against the bug class we are targeting.  If
  later refactors move builder calls into post-build passes,
  revisit then.
- `hir_to_mir.py` consumer-side edits — same reason; this pass
  builds the initial MIR before any ledger exists.
- Codegen / SSA / mir_validate / unrelated modules.

### Regression test

`lang/tests/driver/test_ledger_cache_safety_dirty_bit.py`:

**Negative path (direct-mutation marks dirty):**
1. Construct or load a small `MirFunc` fixture.
2. `ledger = build_and_attach_ledger(func, drop_policy)`.
3. Apply a direct list mutation (e.g.
   `func.blocks[0].instructions.append(some_op)`) followed by
   `mark_ledger_dirty(func, "test_mutation")` — mirrors the
   discipline the real passes follow.
4. Call `require_fresh_ledger(func, "test_consumer")`.
5. Assert raises `AssertionError` whose message includes both
   `"test_consumer"` AND `"test_mutation"`.

**Negative path (forgot to mark dirty after mutation):**

The dirty bit alone cannot detect a mutation that fails to call
`mark_ledger_dirty` — by itself, this is a discipline failure
outside the runtime-assertion contract.  The **static audit
test** (below) covers this gap by scanning the four scoped
files for mutation patterns and requiring nearby evidence of
either a `mark_ledger_dirty` call or an inline allow marker.
"Forgot the rebuild" becoming "forgot the dirty mark" is the
failure mode K flagged in the 2026-05-16 review; the static
audit closes it for the in-scope files.

**Positive path:**
1. `build_and_attach_ledger(func, drop_policy)`.
2. `require_fresh_ledger(func, "test_consumer")` returns the
   ledger without error.

**Observe-path coverage:**
3. After step 2, simulate a mutation + mark_dirty, then call the
   driftc-style observe block (or a thin shim mimicking lines
   ~7146).  Assert the require_fresh_ledger call there also
   raises — proves the debug-mode blind spot is closed.

### Static audit test (the discipline-side guard)

`lang/tests/driver/test_ledger_cache_safety_mutation_audit.py`.

Scans the four in-scope files —
`lang/driftc/stage2/drop_flags.py`,
`lang/driftc/stage2/cleanup_authoring.py`,
`lang/driftc/stage2/match_cleanup_authoring.py`,
`lang/driftc/stage2/string_arc.py` —
for the mutation patterns that constitute the bug surface, and
requires nearby evidence of either a `mark_ledger_dirty(` call
or an inline allow marker.  This is the *reviewable* part of
the contract: a code reviewer or future Claude can run one test
to catch "forgot the dirty mark" before it ships.

**Patterns the scanner flags** (line-based regex; no AST):

```
\.instructions\s*=          # whole-list replacement
\.instructions\.append\(    # append
\.instructions\.insert\(    # insert
\bdel\s+.*\.instructions    # del (slice or element)
\.terminator\s*=            # terminator rewrite
\bfunc\.blocks\[.*\]\s*=    # block replacement
\bfunc\.blocks\.append\(    # new block appended
\bfunc\.blocks\.insert\(    # new block inserted
```

(Calibrate the list against the actual mutation sites at audit
time; tighten only if false-positives appear.  Better to flag
too much and add allow-markers than to miss a real site.)

**Pass criteria for each flagged line:**

A flagged line is acceptable if EITHER:
1. A `mark_ledger_dirty(` call appears within ±5 source lines.
   The proximity window mirrors how the discipline is actually
   read: the audit catches "the mutation and its bookkeeping
   should be readable as one unit."
2. An inline allow marker appears on the same line OR the line
   immediately before, with a free-text reason:
   ```python
   blk.instructions = []  # ledger-cache-safety-audit: allow <reason>
   ```
   Allow-reason vocabulary (open-ended, not enum-typed; the
   reviewer reads the reason):
   - `pre-attach` — runs before any ledger is attached to the
     function.
   - `new-block` — mutating a freshly-constructed block not yet
     inserted into `func.blocks`.
   - `local-ledger-only` — mutation tracked against a
     pass-local ledger that is never attached.
   - free-text — anything else, with a one-line explanation.

**Output on failure:** list each offending file:line:pattern
with the surrounding source snippet, and a one-line hint:
"add `mark_ledger_dirty(func, '...')` after the mutation OR an
inline allow marker with a reason."

**Implementation sketch (~80 LOC):**

```python
SCOPED_FILES = [
    "lang/driftc/stage2/drop_flags.py",
    "lang/driftc/stage2/cleanup_authoring.py",
    "lang/driftc/stage2/match_cleanup_authoring.py",
    "lang/driftc/stage2/string_arc.py",
]
MUTATION_PATTERNS = [...]  # the regexes above
ALLOW_RE = re.compile(r"#\s*ledger-cache-safety-audit:\s*allow\s+(\S.*)")
MARK_RE = re.compile(r"\bmark_ledger_dirty\s*\(")

def test_ledger_cache_safety_mutation_audit():
    offenders = []
    for path in SCOPED_FILES:
        lines = Path(ROOT / path).read_text().splitlines()
        for i, line in enumerate(lines):
            for pat in MUTATION_PATTERNS:
                if not pat.search(line):
                    continue
                # Look for an allow marker on same line or prev line.
                if ALLOW_RE.search(line):
                    continue
                if i > 0 and ALLOW_RE.search(lines[i - 1]):
                    continue
                # Look for mark_ledger_dirty within ±5 lines.
                window = lines[max(0, i - 5) : min(len(lines), i + 6)]
                if any(MARK_RE.search(w) for w in window):
                    continue
                offenders.append((path, i + 1, line.strip()))
    if offenders:
        msg = "ledger-cache-safety audit failed; add mark_ledger_dirty(...) or an inline allow marker:\n"
        for path, lineno, src in offenders:
            msg += f"  {path}:{lineno}  {src}\n"
        raise AssertionError(msg)
```

**Limits — what this audit does NOT catch:**

- Mutations via aliases (e.g. `xs = blk.instructions;
  xs.append(...)`) — the regex misses these.  Mitigated by
  forbidding the alias pattern in the audit's allow-marker
  documentation; reviewers should flag it manually.
- Mutations in files outside the scoped set.  First-pass scope
  is the four files where the recent bugs concentrated; expand
  the SCOPED_FILES list if a new offending site appears.
- Semantic correctness of the `mark_ledger_dirty` reason
  string.  The audit verifies presence, not accuracy.

These limits are acceptable for a first-pass guard: it catches
the literal-write shapes that the recent bug commits exhibited,
which is the failure mode we have evidence for.

### Effort estimate

Total LOC: ~280 (helper module ~70 + ~10-15 `mark_ledger_dirty`
call sites in drop_flags / cleanup_authoring /
match_cleanup_authoring / string_arc + ~5-8
`require_fresh_ledger` call sites in attached-ledger consumers
+ driftc.py 4-site `build_and_attach_ledger` migration + the
~50-line runtime regression test + the ~80-line static audit
test).  No migration risk — the dirty bit is additive; existing
rebuild calls in `driftc.py` continue to work and now flow
through `build_and_attach_ledger`, which clears the bit as a
side effect.  Bugs already in flight (any stale-ledger
consultation in current
code) surface as assertions at test-time, which is the intended
outcome.

### Explicitly out of scope

- Wrapping every `func.blocks[i].instrs.list` access in an
  opaque type.  First pass uses explicit
  `mark_ledger_dirty(func, reason)` calls at the few
  direct-mutation sites; convert to wrappers in a follow-up only
  if drift continues.
- Static type-level enforcement (Python type system doesn't
  support "fresh vs dirty" as a type-level distinction).
  Runtime assertion is the contract.
- Extending the dirty bit to stage4 / SSA / codegen.  If
  evidence emerges that those also need it, expand later.
- "Finishing the Phase 4 migration" (strings/arrays
  return-source legacy alias-walk, moved_out_locals /
  explicitly_dropped_locals holdouts).  Independent track; do
  not couple.

### Design decisions (settled by K, 2026-05-16)

1. **`mark_ledger_dirty` does NOT detach the ledger.**  It only
   flips the dirty bit and records the reason.  Detach-on-mutation
   would be a stricter follow-up if drift persists.
2. **Reason strings are free-text.**  Convention:
   `"<pass_name>.<action>"` style (e.g.
   `"drop_flags.insert_flag_store"`).  Used only in the
   staleness AssertionError message; no test should match on the
   exact string.
3. **Hard assertion is the driver-path default.**  `maybe_fresh_ledger`
   exists for test helpers and intentional no-op-when-unattached
   paths only — every use of it in non-test code requires an
   inline justification comment.  If a real consumer needs the
   soft form, that is a signal the caller should rebuild instead.
