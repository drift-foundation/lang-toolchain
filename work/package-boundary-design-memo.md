# Package Boundary Architecture: Is the Model Sound?

## Date: 2026-04-02
## Author: compiler team
## Status: research / design memo (not a plan)

---

## The question

We keep finding new boundary failures. After fixing wrapper routing
divergence, we immediately found FnResult-unwrap string lifecycle
divergence. The question is no longer "how do we fix the next boundary
bug" but:

**Are these boundary bugs implementation debt around a sound model, or
is the model itself giving the boundary too much semantic significance?**

---

## 1. What package consumption provides today

### Concrete value

| Capability | Requires semantic package consumption? |
|------------|---------------------------------------|
| **Signed provenance** (.sig sidecar, trust stores) | No. Signing verifies bytes, not semantics. Could sign source archives. |
| **Pre-compiled MIR** (skip source→HIR→MIR for stdlib) | Yes, today. But this is an optimization, not a necessity. |
| **ABI boundary enforcement** (pub visibility, wrapper generation) | No. Can be enforced with metadata annotations on source modules. |
| **Separate compilation** (compile library once, consume by many) | Yes, today. But the "once" compilation produces MIR+signatures that must be re-processed at the consumer. Not true separate compilation in the C/Rust sense. |
| **Version resolution** (dep constraints, lock files) | No. Version resolution operates on package metadata, not on the internal compilation model. |
| **Namespace trust isolation** (std.* vs app code) | No. Trust is a policy decision, not a semantic one. |

### What "separate compilation" actually means in Drift today

The package .dmp contains:
1. **Serialized MIR** for all public functions + their transitive callees
2. **Serialized signatures** with TypeExpr + raw TypeId fields
3. **Type table** (struct schemas, variant schemas, trait impls, etc.)
4. **Canonical keys** for cross-package TypeId linking

The consumer:
1. Loads the type table → links TypeIds via canonical keys → remaps
2. Loads the MIR → remaps all TypeIds in MIR instructions
3. Loads signatures → resolves TypeExpr OR remaps raw TypeIds (dual-path)
4. Runs BFS reachability from entry point
5. Synthesizes `__wrap_method` wrappers for boundary methods
6. Runs K39 (generic Destructible instantiation)
7. Runs string_arc on ALL MIR (source + package)
8. Runs codegen on ALL MIR

Steps 4-8 are essentially a full compilation pass over the package MIR.
The only saving is steps 1-3 replacing source parsing + HIR lowering +
type checking. For stdlib (~30 .drift files), this saves ~2s of parsing
on a ~12s total compile. The MIR processing, string_arc, and codegen
dominate.

**The "separate compilation" is not separate.** It's "parse once, re-process
everywhere." Every consumer re-runs string_arc, re-runs K39, re-runs
codegen over the same MIR. The MIR is not a stable compilation artifact —
it's an intermediate that still needs semantic processing.

---

## 2. Concrete costs of the dual-path model

### 2.1 Correctness failures (proven)

| Version | Bug | Mode-divergent stage |
|---------|-----|---------------------|
| 0.27.131 | FORWARD_NOMINAL divergence | Type identity (source ordering vs package pre-link) |
| 0.27.132-135 | has_drop cache staleness | Drop decisions (K39 timing vs source-mode stability) |
| 0.27.137 | copy_status structural fallback | Ownership (destructor_fns not checked in structural path) |
| 0.27.138-140 | Wrapper routing divergence | Codegen routing (source_modules vs package provenance) |
| 0.27.143+ | FnResult-unwrap string leak | String lifecycle (can-throw ABI wrapper vs direct nothrow call) |

Each of these required 1-5 compiler versions to diagnose and fix. The
FnResult-unwrap leak is still open.

### 2.2 The FnResult ABI wrapper divergence (current leak)

This is the sharpest example of the model's cost. In source mode,
`format_int` is nothrow — called directly, returns bare String.
In package-consumer mode, `format_int` crosses a package boundary —
called through a can-throw FnResult ABI wrapper.

The wrapper is semantically unnecessary (format_int is nothrow), but
the package-consumer path doesn't have the callee's nothrow proof at
call time. It only has the package signature, which declares the
function as can-throw (conservative default for cross-boundary calls).

The result: an extra retain/release pair in the caller that interacts
with the downstream serialization chain's own retain/release arithmetic,
producing a net +1 retain (leak).

This is not a wrapper routing bug. The wrapper convergence fixed
*which* wrapper to call. This is about *whether* a wrapper exists at all
— and the answer differs by mode.

### 2.3 Performance tax

The extra retain/release churn is measurable. For the bookkeeper's
logger pattern (format_int → map literal → logger.info):

- Source mode: 5 retains + 6 releases for the format_int string
- PEX mode: 6 retains + 6 releases (should be 7 releases, but one is missing → leak)

Even if the leak is fixed, PEX mode has ~20% more ARC operations per
string value flowing through a cross-package generic function.

### 2.4 Complexity cost

Mode-sensitive code in the compiler (non-exhaustive):

| Area | Mode-sensitive code | Purpose |
|------|-------------------|---------|
| `_build_package_consumer_unit` (~700 lines) | Entirely package-specific | MIR loading, TypeId remapping, BFS, wrapper synthesis |
| `type_table_link_v0.py` (~1200 lines) | Package-only | Canonical key computation, TypeId linking, cross-package type resolution |
| Signature reconstruction (~200 lines in driftc.py) | Dual-path: TypeExpr resolution vs raw TypeId remapping | Rebuilding FnSignature from package payload |
| `__wrap_method` synthesis (~150 lines) | Package-boundary only | FnResult ABI wrappers for nothrow methods |
| BFS reachability (~120 lines) | Package-consumer only | Pruning package MIR to entry-reachable functions |
| K39 post-instantiation rescan | Package-consumer timing differs | Generic Destructible instantiation scanning |
| string_arc Call handler | Mode-sensitive via fn_infos availability | Retain/release decisions depend on whether fn_infos has the callee |

Total: ~2500+ lines of code that exist solely because of the dual-path
model. Every one of these is a potential divergence site.

---

## 3. Remaining boundary seams after wrapper convergence

### Seam 1: FnResult ABI wrapping (ACTIVE — current leak)

**Stage**: MIR construction (wrapper synthesis) + string_arc + codegen
**Why it diverges**: Package-boundary nothrow methods get `__wrap_method`
wrappers that return FnResult. Source-mode calls the method directly.
The FnResult unwrap produces a different SSA value that string_arc
handles with an extra retain/release pair.

**The concrete mechanism** (`driftc.py:2416`): wrapper synthesis always
emits `Call(can_throw=False)` + `ConstructResultOk` — wrapping the
result in FnResult **regardless of the target's `declared_can_throw`**.
The `declared_can_throw` field IS serialized in the package payload
(`provisional_dmir_v0.py`), but the wrapper synthesis code does not
read it. This is why even provably nothrow functions like `format_int`
get FnResult-wrapped at the package boundary.

**User-visible**: String leak (22 bytes per call through this path).
**Status**: Proven. Reproducer exists.

### Seam 2: Generic instantiation timing

**Stage**: K39 (generic Destructible scan) + MIR lowering
**Why it diverges**: In source mode, all types are in the TypeTable before
any generic instantiation. In package-consumer mode, generic templates
from packages are instantiated after TypeId linking. The K39 scan runs
at a different point relative to type table finalization. `destructor_fns`
may be incomplete when string_arc's `_type_needs_drop` runs.
**User-visible**: Missing drops for generic Destructible types (the original
ScopeGuard hypothesis). Not proven for this specific case, but the seam
exists.
**Status**: Partially mitigated by 0.27.135 cache clear. Structural fix
pending (drop-insertion refactor Phase A).

### Seam 3: fn_infos population for package functions

**Stage**: string_arc pass
**Why it diverges**: In source mode, `fn_infos` contains checker-populated
FnInfo for all source functions, with complete param_type_ids derived
from the checker's type inference. In package-consumer mode, fn_infos
for package functions is reconstructed from serialized signatures +
TypeId remapping. Generic instantiation signatures (`__inst__`) use
raw TypeId + tid_map, not TypeExpr resolution. If the remapped TypeId
for a String param doesn't match the canonical String TypeId, `_param_is_string`
returns false and string_arc doesn't consume the String argument.
**User-visible**: String leak or use-after-free depending on
`_note_use(consume=True)` vs `_note_use(consume=False)` path.
**Status**: Not proven as the cause of this specific leak, but the
mechanism exists. The string_arc Call handler's `else` branch (line 1121-1122)
appends args without `_note_use` when param type is not recognized as
String or ref.

### Seam 4: Signature TypeId resolution dual-path

**Stage**: Package consumer signature reconstruction
**Why it diverges**: Package signatures are resolved through TWO paths:
TypeExpr resolution (preferred) and raw TypeId + tid_map remapping
(fallback). For `__inst__` and generic signatures, only the raw path
is used. These paths can produce different TypeIds for the same logical
type — proven by the 8.2 assertion infrastructure that was needed to
validate convergence.
**User-visible**: TypeId mismatches in SSA validation (the Optional<ScopeGuard>
SSA error from the app team, fixed in 0.27.137-0.27.140). Also feeds
into seam 3 (fn_infos with wrong TypeIds).
**Status**: Mitigated by the dual-path assertion + boundary_ret_type_id.
Structural fix would be eliminating one of the two paths.

### Seam 5: Type table completeness at MIR lowering time

**Stage**: HIR-to-MIR + codegen
**Why it diverges**: In source mode, struct instances are populated during
HIR lowering for all source modules. In package-consumer mode, struct
instances come from the package type table linking. If linking misses
an instance (e.g., for a cross-package generic instantiation), `has_drop`
returns False for the struct (line 1499 in types_core.py), and the
array drop helper's `emit_drop` for structs silently returns without
emitting field drops (line 9067-9068 in llvm_codegen.py).
**User-visible**: Silent leak of struct fields. Not proven for this
specific case, but the mechanism is documented.
**Status**: No mitigation. The silent return at line 9068 should at
minimum be a diagnostic.

---

## 4. The hard alternative: why not eliminate semantic package consumption?

### Option A: Keep both paths, enforce immediate post-ingress convergence

**Concept**: Package ingress loads type table + signatures + MIR, then
immediately normalizes everything into the same internal state as
source-mode compilation. After normalization, no code path checks
"is this from a package?"

**What this means concretely**:
- After TypeId linking, the type table is identical to source mode
- After signature reconstruction, fn_infos is identical to source mode
- MIR from packages goes through the same string_arc as source MIR
- No `__wrap_method` wrappers — nothrow functions are called directly
  regardless of origin

**Benefits**:
- Eliminates seams 1, 3, 4 (FnResult ABI, fn_infos divergence, dual-path signatures)
- Reduces seam 2 (K39 timing) to a cache-clear discipline
- Seam 5 becomes the only structural risk
- Preserves package distribution/signing/versioning
- Preserves compilation time savings from pre-parsed MIR

**Costs**:
- Must prove that type table linking produces EXACTLY the same state as
  source-mode lowering (this is the semantic-ingestion refactor's goal)
- Must eliminate `__wrap_method` entirely, which requires the consumer
  to know the callee's nothrow status (currently only known in source mode)

**Migration**:
- Phase 1: Serialize nothrow status in package signatures (additive, no behavior change)
- Phase 2: Consumer uses nothrow status to skip FnResult wrapping (eliminates seam 1)
- Phase 3: Validate fn_infos equivalence between source and package paths (eliminates seam 3)
- Phase 4: Remove dual-path signature resolution (eliminates seam 4)

**Bug classes eliminated**: all five seams above, plus any future seam
in the same family.

**Bug classes NOT eliminated**: bugs in type table linking itself (seam 5),
bugs in string_arc that affect both modes equally.

### Option B: Reduce packages to distribution/provenance only

**Concept**: Packages contain source code (or a canonical serialized
AST/HIR), not MIR. The consumer always compiles from this representation
using the same pipeline as source mode. Packages provide versioning,
signing, trust, and distribution — not compilation semantics.

**What this means concretely**:
- .dmp contains serialized HIR (or even source text) instead of MIR
- Consumer runs HIR-to-MIR + string_arc + codegen on package code
  exactly as it does on source code
- No TypeId remapping — consumer's type table is populated by lowering
  the package HIR in the consumer's context
- No signature reconstruction — signatures come from the checker, same
  as source mode
- No `__wrap_method` — all functions are compiled together

**Benefits**:
- Eliminates ALL five seams. There is no "package mode" after loading.
- Dramatically simpler compiler (~2500 lines of mode-sensitive code removed)
- Debugging/certification: one compilation path to validate
- Performance: no ARC churn at boundaries, no wrapper overhead

**Costs**:
- Compilation time: re-parsing + re-checking stdlib for every consumer.
  Current stdlib: ~30 files, ~2s parse time, ~10s total compile.
  With pre-parsed HIR: ~0.5s load, same MIR/codegen time.
  Net impact: +1.5s compile time. Acceptable for correctness.
- Package format change: breaking change to .dmp format. All packages
  must be rebuilt. Since packages currently require exact compiler version
  match anyway, this is a normal version bump.
- ABI boundary enforcement (pub visibility) must be handled differently —
  metadata-driven rather than structural. This is strictly easier than
  the current approach.

**Migration**:
- Phase 1: Add HIR serialization to package producer (alongside MIR)
- Phase 2: Consumer loads HIR and compiles it through the standard pipeline
- Phase 3: Remove MIR serialization/deserialization + all consumer-side
  MIR processing (TypeId remapping, BFS, wrapper synthesis, etc.)
- Phase 4: Remove type_table_link_v0.py entirely

**Bug classes eliminated**: all boundary bugs, permanently. The model
cannot produce mode divergence because there is only one mode.

**Bug classes NOT eliminated**: bugs in the core compilation pipeline
(string_arc, codegen, etc.) that affect all code equally.

### Option C: Hybrid — package MIR for non-generic code, source re-instantiation for generics

**Concept**: Package MIR is used for concrete (non-generic) functions.
Generic templates are serialized as parameterized HIR and re-instantiated
at the consumer in the consumer's type context. This eliminates the
generic instantiation seam while preserving some compilation savings.

**Benefits**: eliminates seams 2, 3 (generic-related). Preserves MIR
savings for the ~70% of stdlib functions that are non-generic.

**Costs**: still has seams 1, 4, 5 for non-generic code. Two
serialization formats. Arguably the worst of both worlds in complexity.

**Verdict**: not recommended. The savings are marginal and the complexity
is high.

---

## 5. Assessment: is the model sound?

### No. The model is unsound for the compiler as it exists today.

The package boundary is a **semantic cliff** — code on one side of the
boundary is processed differently from code on the other side. Every
processing stage that has mode-sensitive behavior is a potential
divergence site. We have found five such sites. There are likely more.

The fundamental problem is that the .dmp package format serializes
**MIR** — an intermediate representation that is NOT a stable compilation
artifact. MIR requires further semantic processing (string_arc, K39,
codegen) that is sensitive to the TypeTable state, fn_infos population,
and ownership model at the time it runs. The package-consumer path
reconstructs these inputs from serialized data, and the reconstruction
is inherently an approximation of what source-mode compilation would
have produced.

Every time we fix one approximation gap, we find another. This is not
implementation debt around a sound model — it is the predictable
consequence of a model that requires two different paths to produce
identical semantics.

### The core trade

Package consumption saves ~2s of compilation time (source parsing +
type checking) at the cost of:
- ~2500 lines of mode-sensitive compiler code
- A new boundary bug every 1-2 compiler versions
- 1-5 versions per bug to diagnose and fix
- ARC performance overhead at boundaries
- Certification complexity (two paths to validate)

**This trade is not worth it.** The compilation time savings are
marginal. The correctness and complexity costs are substantial and
recurring.

---

## 6. Recommendation

### Immediate (current release): fix the FnResult-unwrap leak

Serialize `declared_can_throw` / nothrow status in package signatures.
Consumer uses this to skip FnResult wrapping for provably nothrow
cross-package calls. This eliminates the current leak and seam 1.

### Short-term (next 2 releases): Option A — enforce post-ingress convergence

Complete the semantic-ingestion refactor (Phase A-E) and drop-insertion
refactor (Phase A-D). These eliminate seams 2-4 by making the type table
and ownership facts mode-independent after package loading.

### Medium-term (3-4 releases): Option B — reduce packages to distribution only

Replace MIR serialization with HIR serialization. Compile all code
through one pipeline. Eliminate the dual-path model entirely. This is
the architectural end-state that prevents future boundary bugs by
construction rather than by testing.

### What to preserve from the package model

- Signed provenance (trust stores, .sig sidecars)
- Version resolution (dep constraints, lock files)
- Namespace trust isolation
- Distribution format (.dmp as a container)
- ABI boundary metadata (pub visibility annotations)

None of these require semantic package consumption. They require a
distribution container with metadata. The container can hold HIR (or
source) instead of MIR without losing any of these capabilities.

---

## 7. Appendix: the bookkeeper leak as case study

The bookkeeper leak illustrates why the dual-path model fails:

1. **Same source code** (`logger.info("event", {"port": fmt.format_int(port)})`)
2. **Different compilation paths** (source: direct nothrow call; PEX: FnResult wrapper)
3. **Different string lifecycle** (source: 5 retains, 6 releases; PEX: 6 retains, 6 releases)
4. **Different outcome** (source: clean; PEX: 22-byte leak per call)

The leak is not in the logger, the HashMap, the serialization, or the
destroy chain. All of those are identical between modes. The leak is in
the **boundary itself** — the FnResult wrapping that exists only in
package-consumer mode produces an extra retain that has no matching
release.

This is not a bug in any single component. It is a bug in the model
that says "cross-package calls must go through FnResult ABI wrappers."
The wrapper convergence made the wrapper routing mode-independent, but
the wrapper *existence* remains mode-dependent. The model's requirement
for wrappers at package boundaries is the root cause.
