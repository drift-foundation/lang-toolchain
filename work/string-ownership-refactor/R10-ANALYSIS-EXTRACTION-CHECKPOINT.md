# R10 — analysis-library extraction: report-only checkpoint

Status: IMPLEMENTED (Slice A of the compressed sequence), STOPPED
for static delta review. Branch: `string-arc-endgame-r10-extraction`.
Parent plan: STRING-ARC-ENDGAME-RESUME-CHECKPOINT.md rev 3, committed
as 03a9702a. Implementation report:
/tmp/drift-announce/2026-07-20T074443Z-r10-extraction-
implementation.md. This document is the DESIGN of record; §1 below is
updated to the process-compression gate (merge/release, not
development).

## 1. Certification / deployment state — MERGE/RELEASE gate (dev unblocked)

- Latest certified deployment (`~/opt/drift/certified/current/
  summary.json`): run **20260719-153613-drift-lang-79bbad3**, verdict
  `certified`, drift-lang source commit
  **79bbad34c87590f822083fb6c303abce2557eae8** = "Array sweep
  retirement ... (0.33.85, ABI 21)". Companion pins: drift-workflows
  0251b24, drift-mariadb-client 46c0c04, drift-net-tls 3315c3a,
  drift-web be5bc5c.
- The **0.33.86 / ABI 21 Arm B candidate (4c7767d6)** is **NOT the
  certified commit** — no certification run for it exists in
  `~/opt/drift/certified/snapshots/` at checkpoint time (latest
  snapshot is the 79bbad3 run above). The maintainer reported a full
  suite in flight; its run has not landed in the deployment state.
- **GATE (process-compression direction, 2026-07-20): the 0.33.86
  certification blocks MERGE/RELEASE of this branch, NOT development.**
  Implementation proceeded against current mainline; the certified
  base is 0.33.85/79bbad3. If the baseline 0.33.86 certification
  FAILS, HOLD this branch and triage the baseline first — this
  mechanical slice does not itself run a cert cycle (it is
  emission-neutral; corpus +0 and byte-identical IR stand in for
  certification of the refactor).

## 2. Scope

Mechanically extract the EIGHT R10 analysis members from
`lang/driftc/stage2/string_arc.py` into a neutral module
`lang/driftc/stage2/string_ownership_analysis.py`:

`iter_used_values`, `seed_string_dest_types`,
`is_materialized_release_family_producer`, `build_fnwide_producers`,
`compute_lastuse_release_points`, `recognize_materialized_releases`,
`compute_string_temp_liveness`, `string_operand_dispositions`.

OUT OF SCOPE (pinned): R6 and every other emission responsibility;
any algorithm/behavior change; compatibility-import policy beyond
what §5 states (deviation = stop condition). PROCESS-COMPRESSION
OVERRIDE (maintainer, 2026-07-20): `consumes_string_operand`'s
DELETION is now IN scope for this slice (dead API, zero call sites);
its dispositions-contract prose moves to the new module. The
compressed bundle also folds in the stale analysis/late-retain
authority comment cleanup (comment-only; no R3/R4 behavior change).

## 3. Exact dependency closure (AST-computed on the candidate tree)

Module-level members that move (14 = 8 public + 2 private + 4
constants):

- The eight public functions above.
- Private helpers: `_analyze_lastuse_block` (the shared core of
  `compute_lastuse_release_points` + `recognize_materialized_releases`)
  and `_is_semantic_string_tid`.
- Constants: `DISPOSITION_CONSUME`, `DISPOSITION_USE`,
  `DISPOSITION_IGNORE`, `DRIFT_STRING_HELPER_SYMBOLS`.

Imports the neutral module needs (all verified as the closure's
exact reference set):

```
from typing import Dict, Iterable, Mapping, Sequence, Set
from lang.driftc.checker import FnInfo
from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from . import mir_nodes as M
from . import cfg as _cfg
```

## 4. Import-graph acyclicity

None of the neutral module's six imports (typing, checker,
function_id, types_core, mir_nodes, cfg) imports string_arc —
verified by the standing import graph. Post-extraction:

- `string_ownership_analysis.py` → {mir_nodes, cfg, types_core,
  checker, function_id} only. It MUST NOT import string_arc (stop
  condition; enforced by a static pin, §8).
- `string_arc.py` → string_ownership_analysis (back-imports, §5).
- `string_releases.py` → string_ownership_analysis, REPLACING its
  only string_arc import — string_releases then has ZERO string_arc
  dependency (a real endgame decoupling, not just a move).
- driftc.py continues to import `insert_string_arc` from string_arc,
  unchanged.

Acyclic by construction; no other module gains an edge.

## 5. Every import that moves (enumerated)

Production:
- `string_releases.py:76` — its ONE import STATEMENT of five unique
  names (`build_fnwide_producers`, `compute_lastuse_release_points`,
  `compute_string_temp_liveness`, `iter_used_values`,
  `seed_string_dest_types`) retargets to the neutral module.
  `test_string_arc_audit_reporter.py` carries SEVEN more retargeted
  import statements (verified: lines 809/901/935/974/1029/1090/2125),
  covering FIVE unique moved names:
  `compute_lastuse_release_points`, `seed_string_dest_types`,
  `build_fnwide_producers`, `string_operand_dispositions`,
  `DISPOSITION_CONSUME`. Its eighth string_arc import —
  `insert_string_arc` — stays. Total touched import statements = 9
  (8 consumer retargets + string_arc's one new back-import).
- `string_arc.py` — BACK-IMPORTS from the neutral module exactly the
  SIX members its remaining code references (AST-verified):
  `iter_used_values`, `seed_string_dest_types`,
  `build_fnwide_producers`, `compute_string_temp_liveness`,
  `recognize_materialized_releases`, `DRIFT_STRING_HELPER_SYMBOLS`.
  (`string_operand_dispositions` + `DISPOSITION_CONSUME` were the
  deleted `consumes_string_operand` wrapper's only consumers, so
  they are NOT back-imported; the unused `Sequence` typing import was
  dropped in the same cleanup.)

Tests (CORRECTED inline per the process-compression direction):
- ONLY `lang/tests/stage2/test_string_arc_audit_reporter.py` imports
  moved R10 names — the five unique names
  `compute_lastuse_release_points`, `seed_string_dest_types`,
  `build_fnwide_producers`, `string_operand_dispositions`,
  `DISPOSITION_CONSUME` — and retargets.
- The three lastuse memcheck carriers mention the family predicate
  in DOCSTRINGS only — no import, no retarget.
- There are SIX actual test importers of string_arc (verified:
  test_drop_before_overwrite_swap, test_move_from_ref_string_arc_
  contract, test_string_arc_audit_reporter, test_string_arc_
  recursive_type_guard, test_string_arc_return_swap,
  test_zero_storage_drop_safe); FIVE remain unchanged because they
  use only non-R10 names (`insert_string_arc`,
  `variant_zero_tag_drop_safe`).

COMPATIBILITY-IMPORT POLICY (pinned): NO re-export shims — every
consumer retargets in the same slice; a lingering
`from .string_arc import <R10 name>` anywhere is a stop condition
(§8). Any desire to deviate (e.g. a transitional alias) is itself a
stop condition requiring maintainer sign-off.

## 6. Mechanical-preservation contract

- Function bodies, `_analyze_lastuse_block`, constants, and
  docstrings move VERBATIM (byte-identical modulo the module header
  and import block). No emission, classification, placement,
  liveness, producer-resolution, or MoveOut change of any kind.
- `consumes_string_operand`: DELETED in this slice per the
  process-compression override (dead API, zero call sites — proof
  standing from rev 3). The dispositions CONTRACT prose (the
  "TLR-2a shared contracts" block, whose Contract 1 now names
  `string_operand_dispositions` rather than the deleted wrapper)
  moves with the library as its documentation.
- TLR-8 / materialized-release behavior preserved EXACTLY: the
  family predicate (MoveOut membership included), fn-wide producer
  resolution (duplicate-dest fail-closed), release placement +
  recognition (incl. the terminator-drained arm and the
  release-recognition fail-closed checks), and the
  `materialized_lastuse_release` counter at **618,744** — all
  unchanged by construction (verbatim bodies) and re-proven by the
  acceptance gates below.

## 7. Acceptance (pinned)

- Corpus vs the standing reference (`build/tmp/flagret`): EVERY
  counter +0 (14 keys; `materialized_lastuse_release` stays 618,744;
  events 2,772,976); universe identical; all hard gates zero.
- Emitted LLVM IR byte-identity where practical: a spot probe
  (compile 2-3 fixtures pre/post, diff the LLVM IR modulo the
  embedded build timestamp) — the extraction touches no emission
  path, so byte-identity is the expectation, not just +0 counters.
- Reporter battery, stage2 FULL, drop_flags/cleanup_authoring
  batteries, ledger-cache guardrails: identical counts. Standalone
  memcheck: 105 + 1 skip, unchanged.
- New static pins — AST-BASED (a textual `.string_arc` scan is
  insufficient): (a) the neutral module never imports string_arc —
  `import ...string_arc`, `from ...string_arc import X`, AND the
  bare-package `from . import string_arc` (module empty, `string_arc`
  as an imported name); (b) no production/test module REACHES a moved
  R10 member through string_arc, closing BOTH escapes — the
  ImportFrom form (relative/absolute/bare-package, aliased or not,
  single-line or multiline naming a moved member) AND the
  module-alias + attribute-access form (`import ...string_arc as sa`
  / `from ...stage2 import string_arc as sa`, then `sa.<MOVED>`): the
  pin binds every local name resolving to the string_arc module and
  flags any `Attribute` on it whose attr is a moved member.
  Fail-closed; teeth proven on both escapes.
- Compiler stays **0.33.86**, ABI stays **21** — a pure internal
  move ships inside the existing candidate version; NO version bump
  (if the maintainer prefers stamping, that is a review decision,
  not a default).

## 8. Stop conditions

- Any import cycle (the neutral module referencing string_arc, or
  any dependency of it acquiring a string_arc edge).
- Any non-mechanical source change surfacing during the move (a
  body edit needed to make the extraction work = design gap → stop
  and report).
- Any compatibility-import decision beyond §5's no-shim policy.
- Any nonzero corpus counter delta, any MIR byte difference in the
  spot probe, any battery/memcheck count change.
- The §1 certification gate (merge/release, NOT development): a
  baseline 0.33.86 certification FAILURE → HOLD this branch and
  triage the baseline first before merge/release (the mechanical
  slice itself runs no cert cycle).

## 9. STOP

IMPLEMENTED (Slice A) and STOPPED for static delta review — see the
implementation report
(2026-07-20T074443Z-r10-extraction-implementation.md) and the
review-closure round. Acceptance is green (corpus +0, IR
byte-identical modulo the build timestamp, batteries green); merge/
release remains gated on the baseline 0.33.86 certification.
