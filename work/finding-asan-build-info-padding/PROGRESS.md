# Progress

Implementer-owned progress journal for the ASAN `.drift_build_info` padding
finding. `lang.implementer` owns this file exclusively; the reviewer must not
create or update it. Entries are append-only and stamped in UTC.

## Status

- **State:** AUTHORIZED AND ACTIVE as of 2026-08-11T06:29Z. This is now the
  serial implementer item. (Previously QUEUED; superseded — see the journal.)
- **Classification:** LANGUAGE_BUG (compiler-produced linked ASAN executable
  violates Drift's own exact-byte executable metadata contract).
- **Gate reached:** Gate 0 complete on authorization items; scheduling,
  fresh re-read, refactor-trigger rescan, and test-edit confirmation are all
  satisfied. Gates 1-5 in progress starting at Gate 1.
- **Test-edit authorization:** GRANTED. Slawomir explicitly approved editing
  the existing `lang/tests/driver/test_abi_version_stamp.py`.
- **Active serial finding:** this one. `work/finding-fiber-stack-overflow-diagnostics/`
  is the other finding folder in the tree and is not displaced by implementation
  here beyond taking the serial slot, which Slawomir granted.
- **Tree at logging time:** branch `main`, `DRIFTC_VERSION = 0.35.0`,
  `DRIFT_RT_ABI_VERSION = 22`.

## Baton identifiers

Handoff that created this file:

| Field | Value |
| --- | --- |
| message_id | `629168b45f58957811a431809abcd691` |
| claim_id | `13c087b9ad643598f3f4f893e0a218d3` |
| thread_id | `asan-build-info-padding` |
| kind | `finding_handoff` |
| outcome | `queued_awaiting_serial_slot` |
| from | `lang.reviewer` |
| to | `lang.implementer` |
| created | 2026-08-11T06:18:42Z |
| claimed | 2026-08-11T06:18:49Z |

Upstream origin (per `FINDING.md`): Baton publication
`fcfad62b3e95c44286a900209a9c425c`, sent by `pushcoin.reviewer` to
`lang.reviewer` and `human.slawomir`. The byte-exact durable report is
materialized in this folder as
`report-2026-08-11T05-58-56Z-ac530b2bf623ea2401d819da4556fde1.md` with its
references leaf preserved beside it.

Mailbox: Baton 1.0.0 / protocol 10, instance `/home/sl/src/mailbox/baton.json`.

## Gate 0 — ownership and scheduling

- [x] `lang.implementer` creates and exclusively owns `PROGRESS.md` — done
      2026-08-11T06:19Z (this file).
- [x] Slawomir confirms this finding is the next serial item — 2026-08-11T06:28:45Z,
      relayed 06:29:35Z.
- [x] Read the whole folder fresh at scheduling time — 2026-08-11T06:29Z.
      `FINDING.md`, `REPRO.md`, and `PLAN.md` are byte-identical to the versions
      read at logging time (mtimes unchanged at 2026-08-11T06:18:11Z UTC-equivalent
      local 00:18:11); no reviewer addendum or review journal was added.
- [x] Repeat the `doc/refactor_triggers.md` scan and record the result —
      2026-08-11T06:31Z, no matching trigger (see journal).
- [x] Obtain Slawomir's explicit confirmation before editing the existing
      `lang/tests/driver/test_abi_version_stamp.py` — GRANTED ("test edit OK").

## Standing constraints acknowledged

Recorded here so they survive a context reset; the authority is `FINDING.md`
and `PLAN.md`, which must be re-read fresh when this is scheduled.

- Regression-first: land and observe `test_build_info_survives_asan_link` RED
  before any producer/codegen change. A synthetic padded-byte test is
  insufficient — the defect appears after the exact IR constant is emitted, so
  the regression must go through a real ASAN link and the production
  `extract_build_info` reader.
- Do not weaken the exact-byte reader: no NUL trimming, no tolerance path, no
  length-prefix compatibility shim, no second contract in
  `validate_build_info_payload`.
- No app/stdlib/PushCoin-side workaround. The fix is producer-side.
- Do not edit the language specification; this is compiler/tooling conformance.
- Version: expected `0.35.0` → `0.36.0` minor bump unless already folded into a
  newer unreleased minor train.
- ABI: leave `DRIFT_RT_ABI_VERSION` at 22 for a producer-only sanitizer/codegen
  correction with unchanged boundary signatures and layouts. Reassess against
  the actual final diff.
- Agents do not run `run_all_tests.sh`; report readiness for Slawomir's
  full-suite run.

## Journal

### 2026-08-11T06:19Z — finding logged as queued

Claimed Baton handoff `629168b45f58957811a431809abcd691` from `lang.reviewer`
(claim `13c087b9ad643598f3f4f893e0a218d3`) and read `FINDING.md`, `REPRO.md`,
and `PLAN.md` in full. Created this file to satisfy Gate 0 item 1 and to record
the handoff identifiers.

No implementation, no test authoring, no independent reproduction, and no
revalidation of the downstream evidence was performed — all of that is
explicitly deferred to scheduling time per the handoff and `PLAN.md` Gate 0.

Downstream evidence carried forward unverified, to be independently
revalidated when scheduled: normal binary section `0x2c7`/align 1 accepted;
ASAN binary section `0x380` (896)/align 32 rejected at JSON byte 704, being
704 canonical JSON bytes followed by 192 NUL bytes.

Open questions inherited from `FINDING.md`, none investigated yet: the first
boundary at which the section changes (compiler IR vs ASAN-instrumented object
vs final link); the supported LLVM mechanism for keeping this metadata global
uninstrumented while preserving `@llvm.used` retention across the repository's
clang/LLVM matrix; whether non-ASAN or combined sanitizer profiles share the
defect; and whether the eventual fix touches any compiler/runtime ABI boundary.

Next action: none until Slawomir schedules this finding. On scheduling, re-read
this folder fresh and start at Gate 0 item 2.

### 2026-08-11T06:29Z — AUTHORIZED; implementation starts

Claimed Baton `implementation_authorization` `38e95db23e7fcaf29de77a105a4283db`
from `lang.reviewer` (claim `84fd319bfd514d36d9870f02c04a7ddc`, outcome
`approved_proceed`).

Slawomir's approval is recorded in Baton response
`1fa5a45717884ef2d63a2f2722c38794` (reviewer claim
`1da83024f23f37ba26f69dfdb66531ed`) at 2026-08-11T06:28:45Z, verbatim:

> work approved
> test edit OK
> proced

This authorizes all three decisions: (1) this finding is the next serial
implementer item, proceed now; (2) edit the existing
`lang/tests/driver/test_abi_version_stamp.py` to add the mandatory red-first
`test_build_info_survives_asan_link`; (3) proceed under the current
`FINDING.md`, `REPRO.md`, and `PLAN.md` with no requested plan changes.

Fresh re-read performed before this entry: the three reviewer-owned files are
unchanged since logging, so the context recorded in the 06:19Z entry still
holds. No later reviewer evidence or review journal exists in the folder.

Constraints restated as still binding under the authorization: regression-first
(red observed before any producer change), no weakening of the exact-byte
reader, no downstream/app/stdlib workaround, no full repository suite from
agents, and red-regression plus implementation evidence go to `lang.reviewer`
on Baton thread `asan-build-info-padding`.

This entry was written BEFORE any source or test edit, as the authorization
requires. Next: refactor-trigger rescan, then independent reproduction through
the production reader, then Gate 1.

### 2026-08-11T06:31Z — refactor-trigger rescan: no matching trigger

Rescanned `doc/refactor_triggers.md` (sha256
`0e2bf1cc6965ff9a5551e68ef07c3887643808721eb9391623201e24b369a520`, last
modified 2026-08-04, i.e. unchanged since before this finding was filed, so the
reviewer's 2026-08-11 scan result still stands independently reconfirmed).

Ten entries are present. None names executable metadata sections, sanitizer
padding or re-alignment of metadata globals, `.drift_build_info`, `@llvm.used`
retention, or an equivalent shape. Keyword sweep for
asan/sanitiz/build_info/align/metadata/section/llvm.used/elf/linker returned
only:

- line 683 — "accepted row base + ASan + memcheck clean" (String
  ownership-authoring conformance matrix; a verification note)
- line 728 — "re-running under ASAN and Valgrind produced no observable
  difference" (same entry; also a verification note)

Both are historical verification prose inside an unrelated entry, not triggers.

Conclusion: this fix does not ride an existing refactor trigger, and no new
trigger is proposed yet. Reassess at Gate 3 once the actual producer-side
mechanism is known — if the fix reveals a general "metadata globals must be
exempt from sanitizer transforms" pattern with more than one site, that is
trigger-shaped and should be filed then.

Gate 0 is now complete. Next: independent reproduction through the production
reader.

### 2026-08-11T06:36Z — Gate 1 RED observed; defect reproduced and boundary pinned

**Independently reproduced.** Downstream evidence is confirmed as a real
producer defect on this tree, at 0.35.0 / ABI 22, clang 20.1.8
(Ubuntu clang version 20.1.8 (0ubuntu4)).

Trivial `main` (the existing `_compile_simple_program` fixture), linked twice
through the repository's real lanes:

| Lane | Section size | Align | Trailing NULs | Production reader |
| --- | ---: | ---: | ---: | --- |
| normal (`default` archive) | 267 (`0x10b`) | 1 | 0 | accepted |
| ASAN (`asan` archive, `-fsanitize=address -g`) | 352 (`0x160`) | 32 | 85 | REJECTED |

Reader diagnostic, verbatim:

    lang.build_info.BuildInfoError: build-info payload is not valid JSON:
    Extra data: line 1 column 268 (char 267)

This is the same shape PushCoin reported and the arithmetic corroborates their
numbers exactly, which retro-validates their observation: ASAN's redzone rule
is `RZ = max(32, 32 * (size/32/4 + 1))` plus `alignTo(size, 32) - size`. For our
267-byte document: `alignTo(267,32)=288`, so `288-267=21`, `RZ=64`, total
`267+64+21 = 352`. For PushCoin's 704-byte document: `alignTo(704,32)=704`,
so `0`, `RZ=192`, total `704+192 = 896` — precisely their reported `0x380`
with 192 NULs. Their report is accurate and needs no correction.

**First mutation boundary: the ASAN IR transform, at compile time.** The
linker is NOT implicated. Evidence, in order:

1. driftc emits exactly what the contract requires —
   `@__drift_build_info = internal constant [267 x i8] [...], section
   ".drift_build_info", align 1`.
2. `clang -fsanitize=address -S -emit-llvm` over that IR rewrites it to
   `@__drift_build_info = internal constant { [267 x i8], [85 x i8] }` and
   emits an `@__asan_global___drift_build_info` descriptor recording
   `size=267, size_with_redzone=352`. The custom section name is carried onto
   the padded struct, so the section itself grows.
3. The ASAN-instrumented `.o` already shows size 352 / align 32 / 85 NULs,
   before any linking.
4. The non-sanitized `.o` shows 267 / align 1 / 0 NULs.

So ASAN's global instrumentation does not honor the exact-length, `align 1`,
custom-section shape the metadata global was emitted with. `.drift_build_info`
is not addressable program data — nothing ever loads it at runtime — so a
redzone around it buys no memory-safety coverage and only breaks the contract.

**Affected profile scope** (section facts from the instrumented object):

| Profile | Size | Align | NULs | Reader |
| --- | ---: | ---: | ---: | --- |
| none | 267 | 1 | 0 | OK |
| asan | 352 | 32 | 85 | WOULD REJECT |
| ubsan | 267 | 1 | 0 | OK |
| asan+ubsan | 352 | 32 | 85 | WOULD REJECT |

Standalone UBSAN is clean; only the ASAN-bearing profiles are affected. The fix
must therefore cover ASAN and ASAN+UBSAN and must not expand beyond them.

**Gate 1 regression landed RED.** Added
`lang/tests/driver/test_abi_version_stamp.py::test_build_info_survives_asan_link`
beside the existing normal-link regression, under the explicit test-edit
authorization. It uses the real `asan` runtime archive variant and driftc's own
`-fsanitize=address -g` flags, and reads through production
`extract_build_info` — not a synthetic padded byte string, which could not
reproduce a defect introduced after the exact IR constant is emitted. Run
alone:

    PYTHONPATH=. .venv/bin/python -m pytest \
      lang/tests/driver/test_abi_version_stamp.py::test_build_info_survives_asan_link -x -q
    -> 1 failed in 18.85s
       lang.build_info.BuildInfoError: build-info payload is not valid JSON:
       Extra data: line 1 column 268 (char 267)
       (raised at lang/build_info.py:345, through extract_build_info)

No producer or codegen file has been modified at this point. The only tree
change is the new test.

**Gate 2 candidate mechanism, validated OUT OF TREE only** (no compiler edit
yet): LLVM's `no_sanitize_address` global attribute. Applied to the emitted
global in a scratch copy of the IR and re-run through
`clang -fsanitize=address -g`:

- section returns to size 267 / align 1 / 0 trailing NULs;
- the rest of the module stays ASAN-instrumented (`asan.module_ctor` /
  `__asan_init` still present) — this does not disable the sanitizer;
- `@__drift_build_info` remains in `@llvm.used`, so retention is preserved.

That is the expected patch shape for Gate 3: emit the attribute from
`LlvmModuleBuilder.emit_build_info` rather than changing the reader. Still to
prove before landing: that the attribute's textual spelling is accepted across
every clang/LLVM version this repository supports, not just 20.1.8 on this
host.

Next: send the RED evidence to `lang.reviewer` before any producer change.

### 2026-08-11T06:45Z — Gate 3 fix applied; Gate 4 focused verification GREEN

**Producer fix (the whole change).** `LlvmModuleBuilder.emit_build_info` now
emits `no_sanitize_address` on the stamp global:

    @__drift_build_info = internal constant [N x i8] [...],
      section ".drift_build_info", align 1, no_sanitize_address

`lang/build_info.py` is UNTOUCHED — no tolerance path, no NUL trimming, no
compatibility reader, no second contract. No stdlib, application, or
PushCoin-side change. The exact section name, byte length, and alignment
contract are unchanged; only the sanitizer exemption is added.

**Post-fix section evidence, from real driftc output across profiles:**

| Profile | Size | Align | NULs | Reader | ASAN active |
| --- | ---: | ---: | ---: | --- | --- |
| none | 267 | 1 | 0 | OK | n/a |
| asan | 267 | 1 | 0 | OK | yes — 4320 instrumented globals |
| ubsan | 267 | 1 | 0 | OK | n/a |
| asan+ubsan | 267 | 1 | 0 | OK | yes — 4320 instrumented globals |

Narrowness proved: `@__asan_global___drift_build_info` is gone (this one global
is exempt) while 4320 other globals remain instrumented and
`asan.module_ctor` / `__asan_init` are still emitted — the sanitizer is NOT
disabled. `@__drift_build_info` remains in `@llvm.used`, so retention holds.
Standalone UBSAN behavior is unchanged.

**Regression results:**

    PYTHONPATH=. .venv/bin/python -m pytest \
      lang/tests/driver/test_abi_version_stamp.py::test_build_info_survives_asan_link \
      lang/tests/driver/test_abi_version_stamp.py::test_build_info_survives_link -q
    -> 2 passed in 46.33s     (new test RED -> GREEN; normal link still green)

    PYTHONPATH=. .venv/bin/python -m pytest -n16 \
      lang/tests/codegen/test_build_info_stamp.py \
      lang/tests/driver/test_abi_version_stamp.py -q
    -> 63 passed in 76.76s    (pre-version-bump)
    -> 63 passed in 81.59s    (post-version-bump, re-run unfiltered)

Both build-info test files were run WHOLE, not name-filtered. This covers the
schema, malformed-payload, duplicate-section, and trailing-content negatives:
they remain green, which is the direct evidence that the reader was not
weakened.

**End-to-end CLI smoke — the downstream blocker is cleared.** Built the trivial
program through the real driver on both lanes and inspected through the
supported interface:

    driftc app/main.drift -o hello-normal
    DRIFT_ASAN=1 driftc app/main.drift -o hello-asan

    drift inspect build-info hello-normal --json   -> exit 0, profile "optimized"
    drift inspect build-info hello-asan   --json   -> exit 0, profile "asan"

Both documents are complete and canonical at `abi:22`. Both binaries still run
(exit 0), so the exemption did not disturb the ASAN runtime. This is exactly
the PushCoin adoption step that was blocked.

**Gate 5 version/ABI decision.**

- `DRIFTC_VERSION` `0.35.0` -> `0.36.0`. `lang/versions.py` states the rule:
  before 1.0, actual or suspected user-visible impact bumps MINOR and resets
  PATCH. ASAN artifacts becoming inspectable through the supported interface is
  user-visible, so PATCH is not available.
- `DRIFT_RT_ABI_VERSION` stays **22**. The diff adds one LLVM attribute to one
  internal metadata global. No runtime-exported helper signature, no data
  layout crossing the boundary, no calling convention, and no ownership/drop
  contract changes. Verified against the actual final diff, which touches only
  `llvm_codegen.py` (emitter), `versions.py` (version string), and the test
  file. Grep confirmed no test pins the literal `0.35.0`; the three hits are
  prose in comments.

**Compiler-version evidence, stated narrowly.** Verified on this host's
deployed compiler only: `Ubuntu clang version 20.1.8 (0ubuntu4)`, which is the
only clang installed here (`/usr/bin/clang` = `clang-20`; the repository
discovers it with a bare `shutil.which("clang")` and declares no floor). I am
NOT claiming a broader version matrix. `no_sanitize_address` as a textual
global attribute requires LLVM >= 15; if the supported floor is ever declared
below that, this needs revisiting.

Diff summary:

    lang/codegen/llvm/llvm_codegen.py           | 15 ++++++-
    lang/tests/driver/test_abi_version_stamp.py | 61 +++++++++++++++++++++++++++++
    lang/versions.py                            |  2 +-

Not done, and deliberately left to the human: `run_all_tests.sh` (agents do not
run the full suite), release notes / announcement, and the downstream PushCoin
handoff. Ready for Slawomir's full-suite run.

### 2026-08-11T06:51Z — Gate 5 closure artifacts published

Reviewer pass `de0ab1346d513574ee36327d9b261b15` (claim
`6146787fd21411262f1d18c70591fc61`, outcome `changes_requested_gate5`) approved
the code/test/version slice with no blocking code finding, and independently
re-ran the focused suites on the final diff (`63 passed in 82.60s`). It
requested four Gate 5 closure artifacts. All four are now done, and NONE of
them introduced a source-tree diff — the diff is still exactly the three files
recorded in the 06:45Z entry.

**1. Release notes published (local file, per the no-external-publishing rule):**

    /tmp/drift-announce/2026-08-11T06-50-31Z-drift-lang-release-notes.md

Covers the 0.36.0 ASAN build-info fix, ABI 22 unchanged, root cause and the
compile-time boundary, the RED-to-GREEN regression, focused results from both
implementer and reviewer, the narrow clang-20.1.8-only version claim, and
explicit readiness for Slawomir's full-suite run.

**2. Downstream handoff sent to `pushcoin.reviewer`:**

| Field | Value |
| --- | --- |
| message_id | `ce31849ddffc3f7369b94387d75fe375` |
| publication_id | `df8077189531c87bc9e9529d7daee0bf` |
| kind | `downstream_fix_notice` |
| outcome | `fixed_pending_full_suite_and_release_gate` |
| thread | `asan-build-info-padding` |

It confirms their evidence was accurate (their 704-byte/896-byte numbers
corroborate the ASAN redzone arithmetic exactly), names the pinned regression,
gives the version/ABI result, tells them no PushCoin-side change is needed and
the held app-side workaround should not be adopted, and states explicitly that
their blocked `drift inspect build-info ... --json` gate may resume only AFTER
0.36.0 clears the full suite and the release gate — it is not released yet.

**3. This entry records those identifiers** (requirement 3).

**4. Gate 5 handoff returned to `lang.reviewer`** — see the reply on claim
`6146787fd21411262f1d18c70591fc61` below.

**Final state: awaiting Slawomir's full-suite run.** Nothing staged, nothing
committed, `run_all_tests.sh` not run. The git index remains Slawomir's. No
`doc/history.md` entry was added because the approving review asked for no
further source-tree edits at Gate 5; if a history entry is wanted before
staging, that is a fresh source diff and should be requested explicitly.

Baton identifier ledger for this finding, in order:

| # | id | direction | kind |
| --- | --- | --- | --- |
| 1 | `629168b45f58957811a431809abcd691` | reviewer -> me | finding_handoff |
| 2 | `a1127215fad768190fb32c4d497b30c3` | me -> reviewer | finding_ack |
| 3 | `c62d4700a1530e20075358f15b6e6958` | reviewer -> me | finding_ack_received |
| 4 | `38e95db23e7fcaf29de77a105a4283db` | reviewer -> me | implementation_authorization |
| 5 | `b15bd3b200cb201a0374d99eadae9f3d` | me -> reviewer | authorization_ack |
| 6 | `a6dde5be45df38fe8a1a12908fd13767` | reviewer -> me | authorization_ack_received |
| 7 | `c40e4fdf2dff86cbbfbe253f2d500793` | me -> reviewer | red_regression_evidence |
| 8 | `80cc7242feb245cad2a8956f4e8c89a3` | reviewer -> me | red_gate_review |
| 9 | `d62570697295dcb4ca9070ce7b8ddb35` | me -> reviewer | implementation_handoff |
| 10 | `de0ab1346d513574ee36327d9b261b15` | reviewer -> me | implementation_review |
| 11 | `ce31849ddffc3f7369b94387d75fe375` | me -> pushcoin | downstream_fix_notice |

Slawomir's approval: `1fa5a45717884ef2d63a2f2722c38794` at 2026-08-11T06:28:45Z.

### 2026-08-11T06:55Z — permanent 0.36.0 history entry added (history-only diff)

Reviewer pass `e28071bfa766a16afb0c5447ae5d5359` (claim
`31d383c2eeb1e9b4baef0ec885d89a80`, outcome `changes_requested_history`)
verified all four Gate 5 artifacts and requested one bookkeeping change: the
permanent 0.36.0 entry in `doc/history.md`, on the grounds that the repository's
durable release ledger would otherwise contradict a shipped
`DRIFTC_VERSION = 0.36.0`. The `/tmp/drift-announce` note is the cross-team
announcement and does not replace in-repository history.

Added at the top of `doc/history.md`, above the 0.35.0 entry, matching the
established entry style (dated heading with version and ABI status, then
LANGUAGE_BUG / root cause / Fix / Tests / Version and ABI / Verification scope
prose). Content covers the required points: the exact-byte failure and its
downstream impact; the compile-time ASAN global-redzone root cause with the
267 -> 352 / align 32 / 85-NUL measurements; the producer-only
`no_sanitize_address` fix with the fail-closed reader explicitly unchanged and
the exemption's narrowness; the RED-to-GREEN linked-ASAN regression and the
63-passed focused verification including the negatives; the version/ABI
rationale with no downstream migration; and an accurate full-suite status
("run by the maintainer and had not yet run when this entry was written").

Standalone-after-cleanup requirement honored: the entry references NO finding
folder, NO Baton identifiers, and NO `/tmp` announcement path. Verified by grep
over the new entry — clean. It reads correctly once `work/` is deleted.

Diff is history-only; no compiler, reader, runtime, stdlib, or test file was
touched in this step:

    doc/history.md                              | 76 +++++++++++++++++++++++++++++
    lang/codegen/llvm/llvm_codegen.py           | 15 +++++-      (unchanged since 06:45Z)
    lang/tests/driver/test_abi_version_stamp.py | 61 +++++++++++++++++++++      (unchanged)
    lang/versions.py                            |  2 +-          (unchanged)

`git diff --check` passes. Nothing staged, nothing committed,
`run_all_tests.sh` not run. Still awaiting Slawomir's full-suite run.

### 2026-08-11T06:58Z — downstream review found a real coverage gap; regression strengthened

`pushcoin.reviewer` (message `e5fc0723c79949d33a19ab13a73968f3`, claim
`eb80b1324735152c757102ed4458dd55`, outcome
`changes_requested_pending_release`) accepted the root fix in principle and
independently verified the producer diff, the regression's use of the real ASAN
archive and production reader, `1 passed in 19.27s` on the final diff, the
0.36.0/ABI-22 result, and that nothing is staged or released.

**Their finding is correct and is now fixed.** The regression linked with
`-fsanitize=address` but COMPILED in the normal lane, so the stamped document
read `"build":{"profile":"optimized"}`. `_resolve_build_profile`
(lang/driftc/driftc.py:78) resolves the profile during CODEGEN from the
`DRIFT_ASAN` environment signal — not at link time — so linking a
normal-profile module could never produce `profile: "asan"`.

Consequence, stated plainly: the byte-contract root fix and its RED-to-GREEN
evidence were never in doubt — the test genuinely pinned the ASAN transform and
link path that caused the padding — but `REPRO.md` acceptance criterion 3
("assert the full document, including an ASAN build profile") was NOT met, and
I reported Gate 4 green without it. The reported CLI smoke did show
`profile: "asan"`, but a smoke run is evidence, not a pinned regression. The
gap was mine.

**Correction applied** to `test_build_info_survives_asan_link`: `DRIFT_ASAN=1`
is now set (via `monkeypatch`, scoped and restored) around the COMPILE step so
the whole ASAN lane is exercised, and the test asserts
`doc["build"]["profile"] == "asan"` alongside the existing driftc/ABI
assertions. The docstring records why linking alone leaves the profile
assertion vacuous.

Evidence for the strengthened test:

- passes on the fixed tree: `1 passed in 19.17s`;
- still RED with the producer fix temporarily reverted: `1 failed in 20.29s`,
  same `BuildInfoError` at `lang/build_info.py:345` — so strengthening the
  assertions did NOT weaken the byte-contract pin. The producer file was
  restored from a byte-exact backup immediately afterwards and re-verified
  (`git diff --stat` back to 14 insertions, `no_sanitize_address` present).
- both build-info files re-run whole afterwards: `63 passed in 81.61s`.

Diff is now four files: the three previously approved plus `doc/history.md`,
with `lang/tests/driver/test_abi_version_stamp.py` amended by this correction.
No compiler, reader, runtime, or stdlib file changed in this step. Because
`lang.reviewer`'s history pass stated that no further test change was
requested, this test amendment is being disclosed to them for a fresh review
rather than folded in silently.

Still not staged, not committed, `run_all_tests.sh` not run.

### 2026-08-11T07:02Z — EVIDENCE CORRECTION: PushCoin payload is 706 bytes + 190 NULs

`lang.reviewer` message `0cf0337c3abde95cfda4873076621cb0` (claim
`b59b38a1159ed859dd720347072a6b1c`), relaying PushCoin
`d902e39734aa14caae971af4fbb48c53`, corrects the downstream forensic
measurement. **This supersedes every earlier statement in this file that the
PushCoin payload was 704 bytes with 192 NULs**, including the arithmetic in the
06:19Z, 06:36Z and 06:45Z entries. Earlier entries are append-only and are not
edited; this entry is the authority.

Corrected downstream measurement:

| Fact | Value |
| --- | --- |
| ELF section | 896 bytes, align 32 |
| canonical JSON | **706 UTF-8 bytes** |
| decoded JSON | 704 characters |
| ASAN padding | **190 NUL bytes** |
| embedded profile | `asan` |

Cause of the error: Python's `JSONDecodeError` reports a CHARACTER offset, which
was read as a raw ELF BYTE offset. One em dash in the payload is one decoded
character encoded as three UTF-8 bytes, hence the two-byte difference.

**Independently verified here, and the correction is self-proving.** I built
synthetic globals of several sizes, instrumented each with
`clang -fsanitize=address`, and measured the resulting section:

| Payload | Section | NUL padding |
| ---: | ---: | ---: |
| 267 (our fixture) | 352 | 85 |
| 704 | 864 | 160 |
| 706 (PushCoin) | 896 | 190 |
| 896 | 1120 | 224 |

A 704-byte payload instruments to an **864**-byte section, so the original
"704 bytes + 192 NULs in an 896-byte section" was internally impossible; 706
reproduces PushCoin's 896/190 exactly.

This also corrects the redzone formula I stated in the 06:36Z entry and in
Baton messages `c40e4fdf2dff86cbbfbe253f2d500793` and
`ce31849ddffc3f7369b94387d75fe375`. Those messages are immutable and are not
edited; the correct model, verified at all four points above, is:

    RZ    = max(32, (n // 32 // 4) * 32)
    total = alignTo(n + RZ, 32)

My earlier informal formula agreed on the 267-byte fixture by coincidence and
disagrees at 706 (it predicts 928).

What the correction does NOT change: the 896-byte / align-32 section
observation, the LANGUAGE_BUG classification, our independent 267 -> 352 / 85
NUL reproduction, the compile-time ASAN global-redzone root cause, the narrow
`no_sanitize_address` producer fix, or the fail-closed reader contract.

Artifacts updated to carry the corrected evidence:

- this `PROGRESS.md` entry;
- the release announcement
  `/tmp/drift-announce/2026-08-11T06-50-31Z-drift-lang-release-notes.md`, whose
  downstream-impact paragraph now reads 706 bytes + 190 NULs, with an explicit
  correction note and the verified redzone model table;
- the second PushCoin notice will carry the corrected numbers (the first one,
  already sent, is immutable and is superseded on this thread).

`doc/history.md` needed no change: it never stated the downstream byte numbers,
only our own 267 -> 352 / 85 NUL fixture measurements, which are unaffected.
Confirmed by grep over the new entry.

### 2026-08-11T07:05Z — reviewer's 8 required changes: final status

`lang.reviewer` `a16b63fa7dec893a3839a73ff8432149` (claim
`728dec88859dab1c317ba58152956f1f`) independently confirmed PushCoin's coverage
finding and listed eight required changes. Status of each:

1. **Compile with the ASAN profile signal active** — DONE, in exactly the
   expected shape: pytest `monkeypatch` fixture plus
   `monkeypatch.setenv("DRIFT_ASAN", "1")` before `_compile_simple_program`,
   with `delenv` afterwards so the link step and the rest of the session are
   unaffected.
2. **Keep the real ASAN archive and `-fsanitize=address -g` link path** — kept,
   unchanged.
3. **Assert `doc["build"]["profile"] == "asan"`** — DONE, alongside the
   existing driftc-version and ABI assertions.
4. **Re-run the corrected test and both complete files** — exact results:
   - `test_build_info_survives_asan_link` alone: **1 passed in 26.48s**
   - both build-info files whole, `-n16`: **63 passed in 143.14s**
   - additionally, with the producer fix temporarily reverted, the corrected
     test still fails (`1 failed in 20.29s`, `BuildInfoError` at
     `lang/build_info.py:345`), proving the strengthened assertions did not
     weaken the byte-contract pin; the producer file was restored from a
     byte-exact backup and re-verified.
5. **Update `PROGRESS.md`** — this entry, plus the 06:58Z correction entry and
   the 07:02Z evidence correction.
6. **Second durable PushCoin notice** — sent; see the identifier recorded in
   the next entry.
7. **Permanent 0.36.0 `doc/history.md` entry** — done at 06:55Z and approved by
   `lang.reviewer` in `0134abf0627cd1a40278c3ee017b0b9d` ("History entry
   approved as written; `git diff --check` passes").
8. **Combined handoff for final review** — returned on claim
   `728dec88859dab1c317ba58152956f1f`.

Producer fix and reader untouched by all of this. Nothing staged, nothing
committed, `run_all_tests.sh` not run.

Current diff — four files, 168 insertions:

    doc/history.md                              | 76 ++++++++++
    lang/codegen/llvm/llvm_codegen.py           | 15 +++++-
    lang/tests/driver/test_abi_version_stamp.py | 77 +++++++++++
    lang/versions.py                            |  2 +-

`git diff --check` passes.

### 2026-08-11T07:07Z — second PushCoin notice sent; all claims resolved

Second durable notice to `pushcoin.reviewer` sent as response
`(reply to claim 49aa328cb5c792d961e4a88429f7f6e1)`, kind
`downstream_second_notice`, outcome
`coverage_closed_evidence_corrected_still_held`. It closes the coverage
discrepancy (profile now pinned, with the reverted-fix negative control),
carries the corrected 706-byte / 190-NUL evidence with the verified redzone
model table, states where the durable upstream evidence was corrected, and
retains their hold explicitly — no third notice until final review AND the
full-suite/certification gate.

Also resolved this round:

- `0134abf0627cd1a40278c3ee017b0b9d` (history entry APPROVED as written,
  `git diff --check` passes) — closed, outcome `history_approval_noted`.
- `0cf0337c3abde95cfda4873076621cb0` (byte evidence correction) — replied,
  outcome `evidence_corrected_propagated`.
- `a16b63fa7dec893a3839a73ff8432149` (reopened profile regression, 8 items) —
  replied with the combined handoff, outcome
  `corrections_complete_awaiting_final_review`.
- `eb80b1324735152c757102ed4458dd55` (PushCoin coverage finding) — replied,
  outcome `coverage_corrected_still_held`.
- `debb184678a9de0dae0019e19a63732d` (PushCoin evidence correction) — replied
  with the second notice above.

Queue empty; no claim held. Final state: awaiting `lang.reviewer`'s final
review and Slawomir's full-suite run. Nothing staged, nothing committed.

### 2026-08-11T07:09Z — DRIFT_ASAN environment-lifetime cleanup applied

`lang.reviewer` `76cfeadbbf87012abd31ccdd0b31bbad` (claim
`12e311f78c6b8ba23d860501ffffa460`) verified the strengthened profile
regression — including their own `63 passed in 87.14s` on the amended diff —
and requested one test-hygiene change: drop the explicit
`monkeypatch.delenv("DRIFT_ASAN", raising=False)` after IR generation.

Their three reasons are all correct, and the third is a real defect rather than
style:

1. the `monkeypatch` fixture already restores the prior environment at
   teardown, so the delenv was redundant;
2. deleting the lane signal mid-test made the "whole ASAN lane" claim
   internally inconsistent;
3. it caused the later `sanitizer_timeout(120)` call to miss its documented
   ASAN multiplier, and when the suite ITSELF runs with `DRIFT_ASAN=1` it
   temporarily erased the outer lane value — precisely the under-scaled
   subprocess timeout that `sanitizer_timeout` exists to prevent, i.e. a
   latent flake I introduced under the sanitizer lane.

`DRIFT_ASAN=1` now stays set for the whole test body, with scoped restoration
left to `monkeypatch` teardown. The comment records why the lifetime matters
(codegen-time profile resolution AND the timeout multiplier) so it is not
"cleaned up" back to a delenv later.

Re-run of the corrected single test, as requested: **1 passed in 20.02s**. The
reviewer stated the full 63-test run was not needed for this one-line
environment-lifetime change, and no other substantive diff appeared, so it was
not re-run.

Diff — four files, 170 insertions; `git diff --check` passes:

    doc/history.md                              | 76 +++++++++++
    lang/codegen/llvm/llvm_codegen.py           | 15 +++++-
    lang/tests/driver/test_abi_version_stamp.py | 79 +++++++++++
    lang/versions.py                            |  2 +-

Producer, reader, version, ABI, runtime and stdlib untouched by this change.

### 2026-08-11T07:10Z — REVIEWER SIGN-OFF; finding complete pending Slawomir's full suite

`lang.reviewer` `85a38941eafbc2012b659caf387d0c48` (claim
`eaba59af9426b2cc5b7b9e35cbca7cb1`, outcome `signed_off_ready_for_full_suite`):
**no open code, test, version, ABI, history, evidence, or handoff finding
remains.**

Independent verification recorded by the reviewer across the whole arc:

- regression RED before the producer change: `1 failed in 22.55s`,
  `Extra data: line 1 column 268 (char 267)`;
- both build-info files whole on the strengthened diff: `63 passed in 87.14s`;
- the directly affected regression after the environment-lifetime cleanup:
  `1 passed in 19.09s`.

Authoritative downstream measurement of record: 706 canonical UTF-8 bytes /
704 decoded characters plus 190 NUL bytes in an 896-byte, align-32 ASAN
section. Our own ASCII fixture's 267 -> 352 / 85 NUL reproduction is unchanged.

**Final state of the work: COMPLETE, awaiting Slawomir's full-suite run.**
Nothing staged, nothing committed, `run_all_tests.sh` not run by any agent, and
the git index remains Slawomir's.

Final diff — four files, 170 insertions, `git diff --check` clean:

    doc/history.md                              | 76 +++++++++
    lang/codegen/llvm/llvm_codegen.py           | 15 +++++-
    lang/tests/driver/test_abi_version_stamp.py | 79 +++++++++
    lang/versions.py                            |  2 +-

Remaining steps are Slawomir's, in order: run the full suite; if green, stage,
commit, and take 0.36.0 through the release/certification gate; then a third
durable notice releases PushCoin's hold so they can refresh the tracked
toolchain, rebuild `bookkeeper-asan`, and verify the external stamp reports
`profile: "asan"` before resuming their parent battery.

One open item deliberately NOT resolved by this finding, for the record: the
`no_sanitize_address` spelling was verified only against this host's clang
20.1.8, the sole installed compiler, and the repository declares no clang
floor. The attribute requires LLVM >= 15. If a supported floor below that is
ever declared, this fix needs revisiting.

### 2026-08-11T13:14Z — PushCoin accepts the coverage correction; hold retained

`pushcoin.reviewer` `495aae2c5d672483c6cf57df2ad72bea` (claim
`1c11d30a51cf5a2a1f5a315df61526f3`, outcome `coverage_accepted_still_held`)
independently re-ran the corrected regression — `1 passed in 22.12s` — and
resolved their coverage objection. They confirm the ASAN runtime/link path is
retained and the fail-closed reader is unchanged. Their journal:
`pushcoin.source:work/finding-driftc-0350-adoption/findings/finding-asan-build-info-stamp/review-2026-08-11T13-13-53Z.md`.

Their conditions list still named three outstanding items (Lang review, the
release/certification gate, and the second durable notice). Two of the three
were already satisfied and their message appears to predate them, so I replied
with a factual correction rather than letting the stale list stand: Lang review
signed off at 07:10Z, and the second durable notice went out at 07:07Z as the
reply to their evidence-correction claim. I offered to re-send that content if
it did not surface on their side.

Only the full-suite run plus the 0.36.0 release/certification gate remain, and
both are Slawomir's. Restated explicitly to PushCoin that nothing in the reply
is authorization: their hold stands, and the THIRD notice is the one that
releases it.

Tree state unchanged since sign-off: four files, 170 insertions, nothing
staged, nothing committed, no full-suite run has occurred.

### 2026-08-11T13:15Z — PushCoin confirms the second notice landed; evidence closed

`pushcoin.reviewer` `87c7fcba32a32eb0a50c8839e8b074c0` (claim
`35be7c843298b92279e0acf05fa086a3`, outcome `evidence_closed_still_held`) — the
second notice DID surface on their side; their earlier three-item conditions
list was simply stale, so no re-send was needed.

They independently confirmed the corrected 706-byte / 704-character / 190-NUL
measurement in this `PROGRESS.md` and in the release announcement, including
the redzone table that disproves the superseded 704/192 arithmetic, and
confirmed `doc/history.md` carries only the unaffected 267 -> 352 / 85-NUL
ASCII fixture. They explicitly decline to treat the ordinal "second notice" as
a release notice.

Evidence closure is therefore complete on both sides. The finding is fully
reviewed by `lang.reviewer` (signed off 07:10Z) and by `pushcoin.reviewer`
(coverage accepted 13:14Z, evidence closed 13:15Z).

**Sole remaining action, and it is Slawomir's: run the full suite, then take
0.36.0 through the release/certification gate.** After certification, the third
durable notice releases PushCoin's hold and their downstream verification
begins.

No claim held; queue empty. Tree unchanged: four files, 170 insertions,
nothing staged, nothing committed.

### 2026-08-11T13:17Z — full suite RUNNING; status reported to PushCoin

`pushcoin.reviewer` `0c8529c8b29762ba704750955d0c645a` (claim
`228d4ba26514a3a3f74d81e45029e0ae`, outcome `response_requested`), at
`human.slawomir`'s request, asked for a durable gate status distinguishing
running / complete / not-started, plus an explicit mapping onto PushCoin's
seven original-bug criteria.

**Slawomir's full suite is RUNNING.** Verified directly rather than assumed:
`run_all_tests.log` is being written, started 2026-08-11T13:02:29Z, currently
executing `lang/tests/type_checker` under `pytest -n16 --dist=worksteal`.

The running suite IS testing the final Lang-reviewed diff: `HEAD` is
`c7fa71c4` with exactly the four reviewed files modified and uncommitted, and
the environment-lifetime cleanup (07:08Z) predates the suite start by ~6 hours.
The on-disk test contains only `monkeypatch.setenv("DRIFT_ASAN","1")`, no
`delenv`.

Criteria mapping reported: 1-5 SATISFIED with evidence already recorded in this
file; 6 (full suite) NOT AVAILABLE — running; 7 (staging/commit/certification)
NOT STARTED, with no certified artifact identity in existence to name.

Care taken on criterion 6: completed phases in the log show ok/PASS and no
failure, but that is progress and was explicitly NOT characterized as a result.
The one log line matching a failure pattern is `nm_failed=18` inside a
lane-audit line whose verdict is `PASS` — a skipped-symbol-probe counter, not a
test failure. Confirmed by inspection before reporting.

In-tree identity confirmed for the report: `DRIFTC_VERSION = "0.36.0"`,
`DRIFT_RT_ABI_VERSION = 22`.

### 2026-08-11T13:18Z — PushCoin accepts criteria 1-5; 6-7 open pending the live suite

`pushcoin.reviewer` `51950d726c03a8b12dfa020fdce7b417` (claim
`8bfdfc0ece763f6e0a1bda2663928907`, outcome `criteria_1_5_accepted_6_7_open`)
accepted the gate report and independently confirmed the final four-file diff,
a clean diff check, the live `run-all-tests.sh`, and the current driver-suite
phase. They read the same discipline on partial progress: PASS entries at ~2%
are progress, not a green gate.

Criteria 1-5 are now CLOSED by both reviewers. Criteria 6 (full-suite result)
and 7 (release/certification + named artifact) remain open and are Slawomir's.

A completion watcher is armed on the running suite so the result can be
reported to `lang.reviewer` and `pushcoin.reviewer` as soon as the run exits,
rather than being discovered on the next inbound message. No agent is running
or has run `run_all_tests.sh`; the watcher only waits on the existing process
and reads its log.

Queue empty, no claim held.

### 2026-08-11T15:20Z — full-suite run ABORTED by host OOM; inconclusive, rerun required

Slawomir's full-suite run (started 13:02:29Z) did **not** complete. It stopped
at 49% with no pytest summary line, and the suite process was gone by
15:19:08Z. Counts before the abort: 3896 PASSED lines, 2 FAILED lines.

**Cause: system-wide memory exhaustion, not a test failure.** The kernel OOM
killer fired at 2026-08-11T15:18:11Z (`Out of memory: Killed process ...
(wireplumber)`, `constraint=CONSTRAINT_NONE`, `global_oom`), three minutes
after the last log write at 15:15:29Z. Slawomir independently reports the
machine was overloaded and will retry.

The two failures observed at ~48%, immediately before the OOM:

- `lang/tests/driver/test_logger_no_attrs_overload.py::test_info_no_attrs`
- `lang/tests/driver/test_borrow_in_cast_no_double_free.py::test_borrow_directly_bound_still_ok`

Both test FILES pass in isolation on the same tree, run immediately afterwards:
`7 passed in 196.17s`.

Observation recorded WITHOUT drawing a conclusion:
`test_logger_no_attrs_overload.py:46` uses a hardcoded `timeout=10` for the
binary run, while line 43 correctly uses `sanitizer_timeout(60)`. A hardcoded
subprocess budget under `-n16` on a starving host is the documented
false-`TimeoutExpired` shape. This is pre-existing test code, untouched by this
finding.

**Position taken, per `pushcoin.reviewer`'s explicit instruction and my own
judgement: this run is INCONCLUSIVE.** It is not a green gate, and it is not
evidence that the build-info patch is innocent either. The in-isolation passes
and the OOM record are strong evidence about HOST conditions; only a clean
rerun on the same reviewed diff settles criterion 6. I will not label the
failures patch-caused or harmless before that.

Evidence preserved for audit at
`work/finding-asan-build-info-padding/evidence-2026-08-11T15-19Z-overloaded-run.md`
(counts, verbatim FAILED lines, kernel OOM lines, the isolation rerun, and the
hardcoded-timeout observation).

No agent started or will start the rerun; the full suite is Slawomir's to run.
Criteria 6 and 7 remain OPEN. Tree unchanged: four files, 170 insertions,
nothing staged, nothing committed.

### 2026-08-11T16:43Z — rerun COMPLETED GREEN, but scope does NOT satisfy criterion 6

The `just test` rerun finished cleanly. Driver exited 16:43:09Z.

    plan: uniform-pytest-lanes | jobs_n: 16 | elapsed_s: 225.627
    12/12 jobs ok, all exit_code=0, 0 failing
    borrow 1.44s · core 1.46s · method_registry 0.63s · packages 56.12s ·
    parser 1.63s · repo_audits 2.35s · stage1 3.88s · stage2 84.82s ·
    stage3 1.47s · stage4 7.47s · traits 1.55s · type_checker 62.81s
    kernel OOM events in the window: 0

Tree under test was confirmed identical to the signed-off diff at start
(`HEAD c7fa71c4` + the four files, 170 insertions).

**This is NOT the full suite, and it must not be read as satisfying criterion
6.** The 225-second elapsed time against the aborted run's 2+ hours was the
tell; I checked the plan rather than accepting the green.

`uniform-pytest-lanes` runs eleven `lang/tests/*` subtrees plus a two-file
repo-audit job — 271 test files. The following are NOT in the plan (569 test
files):

| Subtree | Test files | Contains |
| --- | ---: | --- |
| `lang/tests/driver` | **483** | **this finding's own regression**, and **both tests that failed in the aborted run** |
| `lang/tests/memcheck` | 50 | |
| `lang/tests/checker` | 17 | |
| `lang/tests/tools` | 11 | |
| `lang/tests/codegen` | 6 | **`test_build_info_stamp.py`**, the other build-info file |
| `lang/tests/modules`, `lang/tests/gdb` | 2 | |

So the rerun did not execute `test_build_info_survives_asan_link`, did not
execute `test_build_info_stamp.py`, and did not retest
`test_logger_no_attrs_overload.py::test_info_no_attrs` or
`test_borrow_in_cast_no_double_free.py::test_borrow_directly_bound_still_ok`.
The two prior failures therefore remain unretested by a suite run.

What IS covered on this exact tree, from my earlier targeted runs: both
build-info files whole (`63 passed`), and both previously-failing files
(`7 passed`). That is targeted evidence, not a suite result, and I am not
substituting it for one.

**Criterion 6 remains OPEN.** Criterion 7 remains NOT STARTED. No third
certification notice. Reported to both reviewers so a scope-limited green
cannot be mistaken for a release gate.

### 2026-08-11T16:47Z — CORRECTION: the rerun is still running; I reported a child step

My 16:43Z entry and the `rerun_result` message
(`5983dbce4616c5263533ac1d95aeb790`) framed the uniform plan's 12/12 completion
as the rerun's terminal result. **That framing was wrong and is corrected
here.** `pushcoin.reviewer` had already required that the terminal report cover
top-level `just test` rather than the uniform child; they were right.

`just test` is an eleven-step recipe:

     1. review-cleanup            5. lang-driver-test      9. ext-e2e-smoke
     2. ownership-matrix-check    6. lang-codegen-test    10. ext-e2e-boundary
     3. lang-uniform-pytest       7. lang-gdb-test        11. ownership-matrix-pkgb
     4. lang-llvm-test            8. drift-deploy-test

`tools/drift_test_run.py --plan uniform.plan.json` is **step 3 of 11**. The
top-level recipe (PID 2293419) was still running the whole time and is now on
**step 5, `lang-driver-test`**:
`pytest -n 16 --dist=worksteal -m "not perf" -v lang/tests/driver`.

**What my error changed:** the claim that the rerun "excludes `lang/tests/driver`
and `lang/tests/codegen`, so a different run would be needed" was wrong. Those
subtrees ARE covered, as steps 5 and 6. The exclusion is a property of the
uniform PLAN, not of the run. `just test` is on track to exercise
`test_build_info_survives_asan_link`, `test_build_info_stamp.py`, and both
previously-disputed failures.

**What it did not change:** criterion 6 was open then and is open now — for the
simpler reason that the run had not finished. The 12/12 uniform figure remains
accurate as a STEP result and insufficient as a gate. Nothing was released,
staged, certified, or notified on the strength of it.

Root cause of my error: I armed the completion watcher on the child driver PID
(`drift_test_run.py`) instead of the top-level recipe PID, so the child's exit
looked like the run's exit. Watcher is re-armed on PID 2293419 (`just test`).

Correction sent to both reviewers as publication
`6406e949f6dbe32c5d3294cae5c84fd1`.

### 2026-08-11T18:10Z — targeted run STOPPED on Slawomir's notice; machine reserved

Global notice from `human.slawomir`: "machine busy running ASAN full compiler
test suite - refrain from running heavy (driftc) tests until this finishes."

My in-flight targeted verification (the four files: our regression,
`test_build_info_stamp.py`, and both previously-disputed driver tests) was
exactly the kind of heavy driftc workload the notice prohibits, so it was
**killed immediately** rather than allowed to finish. No result was produced
and none is claimed. Confirmed no leftover pytest workers of mine remain.

This also removes a competing memory consumer from the host, which matters
given the 15:18:11Z OOM.

Follow-up owed on this thread: the targeted results were promised to both
reviewers in `0d9d838c8e1dea9a415547b9fad716b9` /
`c39a1c6bf984dacbc40d8fc992a201e3`. They will be produced only after Slawomir's
suite finishes and the machine is released — and only if still useful, since a
completed top-level `just test` would supersede them.

Gate state unchanged: criterion 6 UNRESOLVED pending the pts/16 console verdict
(exit code + `lang tests: Success.` marker) for the run that exited 18:05:31Z;
criterion 7 NOT STARTED. Nothing staged or committed.

### 2026-08-11T18:11Z — reviewer concurs: withhold verdict, await maintainer console

`lang.reviewer` `6a64bf32c40abd805899abb8bf7be9a2` (claim
`24b1d8073f7b16d1b89da6153b4fd332`, outcome
`unresolved_awaiting_maintainer_console`) independently confirmed PID 2293419
is gone and that `HEAD c7fa71c4`, the four-file diff, and the clean diff check
are unchanged. They explicitly endorse withholding the verdict without the exit
code / `lang tests: Success.` marker, are requesting Slawomir's console
evidence, and state that a targeted follow-up cannot substitute for it — which
also makes the stand-down of my killed targeted run the right outcome rather
than a loss.

Nothing further is actionable by me. The finding is complete and idle pending
exactly one input: the pts/16 console verdict for the top-level `just test`
that exited 2026-08-11T18:05:31Z.

State: criterion 6 UNRESOLVED, criterion 7 NOT STARTED, PushCoin held, nothing
staged or committed, no workload running on the machine.

### 2026-08-11T18:16Z — commit message drafted (text only; no Git mutation)

`lang.reviewer` `07c8c23ad8c06b10a698111e05ce53eb` (claim
`805217c521647c185e2c1c833d658db0`) requested a draft commit message covering
the producer fix, the regression, 0.36.0, ABI 22 unchanged, and the history
entry, stating focused tests green and the top-level ASAN verdict unresolved,
returned for reviewer approval and Slawomir — explicitly with no Git mutation.

Draft saved to `work/finding-asan-build-info-padding/COMMIT_MSG.draft.txt` and
returned in full on the thread (`f6bcb65687e70bd33f8cd32d6f432dc7`).

**No Git command was run.** Nothing staged, nothing committed, no branch
touched, no `git add`. Slawomir reserves all Git mutations; this is text he can
take or discard.

Style follows the repository's existing convention (subsystem-prefixed subject,
prose body, explicit "Verification:" paragraph), matching `aa625723`.

Deliberate choices, flagged to the reviewer:

- The closing paragraph records that the top-level `DRIFT_ASAN=1 just test`
  verdict is UNRESOLVED and warns against treating the commit as
  full-suite-verified. It is written to be DELETED and replaced with the real
  result once the verdict lands, not amended around.
- No downstream/reporter names, Baton identifiers, finding paths, or `/tmp`
  paths appear, so the message stands alone after `work/` is deleted.
- No trailers added: recent history carries none and the committer is
  Slawomir.

Gate state unchanged: criterion 6 UNRESOLVED, criterion 7 NOT STARTED, PushCoin
held, no workload running on the machine.

### 2026-08-11T18:17Z — index state changed: files are now STAGED (maintainer action)

Correction to the standing "nothing staged" claim carried by every earlier
entry, including the 18:16Z one immediately above.

`git status --short` now shows the four tracked files and the finding folder in
the INDEX:

    M  doc/history.md
    M  lang/codegen/llvm/llvm_codegen.py
    M  lang/tests/driver/test_abi_version_stamp.py
    M  lang/versions.py
    A  work/finding-asan-build-info-padding/...   (finding artifacts)
    ?? work/finding-asan-build-info-padding/COMMIT_MSG.draft.txt   (untracked)

**This staging was NOT performed by me.** My git usage in this finding has been
read-only throughout — `status`, `diff`, `diff --check`, `diff --stat`, `log`,
`rev-parse`. No `add`, no `commit`, no branch operation. Slawomir reserves all
Git mutations and this is consistent with his commit-message request.

Nothing is COMMITTED: `HEAD` is still `c7fa71c4`. The working-tree content is
unchanged from the reviewed diff; only the index changed.

Gate state is otherwise unchanged: criterion 6 UNRESOLVED pending the pts/16
console verdict, criterion 7 NOT STARTED, PushCoin held. Committing before that
verdict is recorded would make the commit's own closing paragraph the accurate
description — that is Slawomir's call.

### 2026-08-11T18:19Z — commit text APPROVED; index not commit-ready; whitespace fixed

`lang.reviewer` `9760b84a3a0641f1c8eb6215b9484722` (claim
`279a4a0897fb8d5073b2930097c803db`, outcome `text_approved_index_not_ready`):

- **Commit-message text: APPROVED** for the intended four-file change, with the
  live-verdict paragraph retained and to be replaced by the actual result if
  Slawomir gets a terminal verdict before committing.
- **Current staged index: NOT COMMIT-READY** — 29 staged paths / 2141
  insertions rather than the four implementation+history files, the whole
  ephemeral finding folder staged, `PROGRESS.md` staged as `AM` (stale
  snapshot), and `git diff --cached --check` failing on two trailing spaces.

Actions taken, strictly within what the reviewer permitted (fix the working
file, keep `PROGRESS.md` current, do NOT stage or commit):

- Stripped the trailing whitespace on lines 13-14 of
  `evidence-2026-08-11T15-19Z-overloaded-run.md`. The spaces were verbatim from
  pytest `-v` output, which terminates each line with a space; only the
  trailing whitespace changed, the quoted text is otherwise untouched.
- `git diff --check` on the WORKING TREE is now clean, and no trailing
  whitespace remains anywhere in the finding folder.
- `git diff --cached --check` still reports the two lines, because the INDEX
  holds the pre-fix snapshot. That is expected and is Slawomir's to resolve by
  re-staging or by narrowing the staged scope; I did not stage.

Still not staged or committed BY ME; `HEAD` remains `c7fa71c4`. Final staged
scope is Slawomir's decision, including the standing rule that finding folders
are removed after merge/closure with no permanent references — which argues
against committing `work/finding-asan-build-info-padding/` at all.

### 2026-08-11T18:20Z — reviewer verified the fix; my ephemerality framing corrected

`lang.reviewer` `3655d04aadd356650014d30362ca979b` (claim
`6dbcf80e9a5a8126cbf91e651c4987f1`, outcome `working_clean_index_stale`)
verified: working `git diff --check` clean, cached check still failing,
`evidence` and `PROGRESS.md` both `AM`, no reviewer Git action.

**Correction to my own framing, recorded so it does not propagate:** I wrote
that committing the finding folder "would contradict" the ephemerality rule.
That was overstated. The accurate rule, per the reviewer, is that a live
finding MAY be tracked on the branch provided it is removed after merge/closure
and leaves no permanent references. Staging the folder is therefore a
legitimate option, not a violation. My objection reduces to the narrower point
that `PROGRESS.md` keeps changing and any snapshot committed now goes stale
immediately.

Final staged scope is Slawomir's call. No Git mutation by me at any point;
`HEAD` remains `c7fa71c4`.

Blocked on exactly one input: the pts/16 console verdict for the top-level
`DRIFT_ASAN=1 just test` that exited 2026-08-11T18:05:31Z.

### 2026-08-11T18:25Z — Slawomir committed 0a86af72 and staged 0.36.0+abi22; artifact verified

`lang.reviewer` `ea66487c8b3467c94f7e2c7d38b93fca` (claim
`0a75af189cb694d6b8fd7513745f2398`): Slawomir committed the change as
`0a86af72` — using the drafted subject line — and staged
`/home/sl/opt/drift/staged/toolchain/drift-0.36.0+abi22` (build 18:21:02Z),
sending pre-cert trials to `pushcoin.reviewer` (`b0bd6633`) and `web.reviewer`
(`cc3223b5`). Not certified; suite verdict still unresolved.

Working tree is now clean apart from an untracked reviewer journal, so the
finding folder was included in the commit — the maintainer's chosen scope.

**Independent verification of the STAGED ARTIFACT** (read-only; nothing under
`~/opt/drift/**` modified, per the frozen-artifact rule):

- identity: `driftc --version --json` reports `driftc 0.36.0`, `abi 22`,
  `git 0a86af72`; staged `lang/versions.py` agrees;
- the fix is present in the staged compiler:
  `lang/codegen/llvm/llvm_codegen.py:1478  f'no_sanitize_address'`;
- end-to-end with the staged toolchain, trivial program built twice:
  `drift inspect build-info hello-normal --json` -> exit 0, profile
  `optimized`; `hello-asan` -> exit 0, profile `asan`; both canonical at
  `abi:22`, `git:"0a86af72"`;
- raw ELF facts for the ASAN binary's `.drift_build_info`: **size=295,
  align=1, trailing_NULs=0** — the defect's exact signature, absent.

So the staged artifact genuinely fixes the reported downstream bug, which is
what PushCoin's pre-cert trial exercises.

This does NOT close criterion 6: verifying the staged artifact is not the
top-level `DRIFT_ASAN=1 just test` verdict, which still needs the pts/16
console exit code and `lang tests: Success.` marker for the run that exited
18:05:31Z. Staging is not certification; no third notice issued; PushCoin's
hold stands.
