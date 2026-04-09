# Real Separate Compilation in Drift — Plan

**Status:** preliminary plan / estimation pass. Not an implementation request.
**Goal:** decide whether to schedule the work, and at what scope.
**Constraint (load-bearing):** design around a separate-compilation contract,
not a daemon/coordinator architecture. No fake `-j`. Independent compile units,
deterministic interface and codegen artifacts, explicit final link/finalize step.
**Decision:** the plan is **package-level separate compilation with content-hashed
wrapper names**, exposed as honest `drift build -j` over the package dependency
graph. Module-level units are explicitly out of scope and not a roadmap item;
they are a contingency only, with written triggers for revisiting.

---

## TL;DR

Drift is much closer to real separate compilation than the "single big driftc
invocation" surface suggests. Three load-bearing primitives are already in
production:

1. **Monomorphized generics already emit as `linkonce_odr` with COMDAT.**
   `lang/driftc/driftc.py:4221` writes an instantiation index that explicitly
   records `"linkage": "linkonce_odr", "comdat": True` for every monomorph.
   Drop-dups-at-link is the *active* codegen contract for Drift's hardest
   case, not a hypothesis I would have to propose from scratch.

2. **Cross-unit function identity is already content-hashed.**
   `FunctionKey(package_id, module_path, name, decl_fingerprint)` is the
   stable cross-unit handle for any function, computed via
   `compute_template_decl_fingerprint`. The `convergence_parity` debug check
   exists specifically to verify two independent paths through Pass1 produce
   the same `decl_fingerprint`. The primitive Drift needs for "compile in
   isolation, agree at link" is in production and load-tested.

3. **Packages already serialize the typed surface + lowered MIR.**
   `provisional_dmir_v0.py` carries `type_table`, `signatures` (including
   monomorphized `__inst__` signatures), `mir_funcs`, and `ImplMeta` records.
   The package boundary is currently the only separate-compilation boundary
   in Drift, but the payload is rich enough that consumers re-run codegen
   (HIR → MIR → SSA → LLVM IR → object) on every build. The bytes for
   skipping that re-run are essentially already there; what's missing is
   the cache and the lowered-side artifact contract.

**The plan**: cache the existing package-boundary lowered output as a
deterministic codegen artifact, content-hash every emitted symbol's mangled
name (so `linkonce_odr` works for *every* wrapper category, not just generic
monomorphs), and turn `drift build -j` into honest package-level
parallelism. Keep the existing whole-program path behind `convergence_parity`
as the from-source fallback.

**Sized:** medium, **~10–13 weeks** for one engineer at this codebase's
discipline level (regression-first, audit-validated, survives `just test`).

**Is this enough indefinitely?** Probably yes. The 0.27.x history, the
shape of Drift's actual workloads (stdlib, drift-web, single-package
consumers), and the fact that `linkonce_odr` already covers the hardest
case all point to "package-level cache hits + wrapper-name content-hashing
buys most of what finer granularity would buy, at a tiny fraction of the
surface area". Module-level units are a contingency only, gated on
specific developer-experience targets that are not on Drift's current
trajectory. See §11 for the explicit triggers.

---

## 1. Current state

### 1.1 The effective compile unit today

The effective unit *as observed from the outside* is the **package**:

- `driftc --emit-package` produces a `.dmp` containing one package's
  serialized type table, signatures, MIR, and impl metadata
  (`provisional_dmir_v0.py`). One driftc invocation consumes all source
  files of that package.
- `driftc --package-root` consumes `.dmp` artifacts as dependencies and
  produces either another `.dmp` (if also `--emit-package`) or a binary.
- `drift build` orchestrates per-artifact driftc invocations using a
  manifest + lockfile (`tools/drift_deploy/drift_build.py`).
- `drift deploy` adds the staged toolchain pipeline on top.

The effective unit *inside* a single driftc invocation is the **whole
world visible to that invocation** — all source modules of the artifact
being emitted, plus all types and signatures from every consumed package,
flattened into shared mutable state (Pass1, callable_registry,
function_keys_by_fn_id, visibility_provenance_by_id, the SSA/MIR
collections, etc.).

So the surface is "package-level separate compilation" but the
implementation inside each package is "whole-program with consumed package
slabs glued in". The HIR-level scope reconstruction work in 0.27.x and the
TypeId normalization Phase 1 work were specifically about making that
"glue" faithful — enough to not corrupt cross-package compilation.

### 1.2 Where the pipeline assumes a whole-program / whole-artifact world

| Coupling point | Where | Why it matters |
|---|---|---|
| Whole-program reachable BFS | `lang/driftc/driftc.py` around `_reachable_mir` / `_reachable_ssa` / `_all_fn_infos` | Each invocation computes the full reachable set. The plan caches the *output* of this walk, not the walk itself, so it stays whole-program inside one invocation. |
| Single `CompilationUnit` lowered as one LLVM module | `lang/driftc/driftc.py` near `_emit_codegen` | One LLVM module per package is fine for the plan; the link step combines per-package modules. |
| Global callable registry / visibility provenance | Pass1 state, threaded into Pass6 | Per-package this already only sees its own modules + imported package slabs. No structural change. |
| Wrapper synthesis (entry, K16a nothrow methods, K26 trait impl methods, K40 preamble, destroy bodies, `__wrap_method`) | Multiple post-BFS passes around `driftc.py:10380+` | **The high-leverage scope of this plan.** Wrapper mangled names must become content-hashed so `linkonce_odr` deduplication is safe across cached and from-source builds. See §5. |
| Destructor registration | `shared_type_table.destructor_fns` populated globally during Pass6 | Per-package, each package owns destructors for types it defines. The link step concatenates per-package tables (key = content-hashed type_id, value = `FunctionId` with `linkonce_odr` linkage). |
| Trait impl injection (K26) | Post-typecheck pass that injects `external_impl_metas` into combined_exports | Already cross-package today; the plan formalizes the impl ownership rule but doesn't restructure the dispatch path. |
| `intern_impl` impl_id collisions across packages | Memory item 19, fixed in 0.27.43-dev | Already a known issue; the plan reuses the existing package-local impl_id model (the 0.27.43 fix is sufficient for package-level units). |
| Runtime archive seeded as a single linkable input | `driftc.py:_run_ir_with_clang` and the dual-runtime variant resolution from the just-shipped workstream | Already separate-compilation-friendly. No change. |
| LLVM IR module flags + DI scope chain | `lang/codegen/llvm/llvm_codegen.py:1592` | Per-unit IR each declares its own compile units / source files. Already gated cleanly by `debug_enabled`. |

### 1.3 Global tables / registries

Ranked by how load-bearing they are for the plan:

1. **`function_keys_by_fn_id`** — already content-addressable. *No change.*
   `decl_fingerprint` is a SHA256 of canonical declaration form. Two
   compilations of the same declaration produce the same key. This is the
   foundation for cross-unit identity, and it works today.

2. **TypeIds in `shared_type_table`** — half-solved.
   Today they're `int` interned per-compilation. The TypeId normalization
   Phase 1 work added link-time fixup so two packages with the same `Foo`
   resolve to one canonical TypeId at link time (`type_table_link_v0.py`'s
   FORWARD_NOMINAL hardening). The plan reuses this — package-level units
   inherit the same link-time canonicalization that already works.

3. **`callable_registry` / `visibility_provenance_by_id`** — per-invocation,
   no change. Each per-package driftc invocation already only sees its own
   modules + the slabs it imported, which is the right shape.

4. **`inst_cache` / instantiation index** — already keyed by
   `FunctionKey + ABI tuple`, already emits `linkonce_odr`/`comdat`.
   *No change.* Each per-package driftc has its own inst_cache populated
   by its own demand; the linker drops duplicates across packages.

5. **`destructor_fns`** — almost type-table-local. Each package owns
   destructors for types it defines; the link step concatenates per-package
   tables.

6. **Wrapper mangled names** — **the largest delta in this plan.**
   Multiple wrapper categories currently emit names derived from
   per-compilation ordinal or interned ids. The plan content-hashes them
   so `linkonce_odr` works for every wrapper category. See §5.3.

7. **Trait `impl_id` interning** — package-local is sufficient for the
   plan. The 0.27.43-dev fix (drop `preferred=` from `intern_impl`) is
   load-bearing for the package boundary; no further work required.

---

## 2. The compile unit: package

The compile unit is the **package**, exactly as observed today.

The defense is short and entirely empirical:

- **Already exists.** Format, identity, consumer protocol are in
  production. Convergence parity check already pins agreement.
- **Already orchestrated.** `drift build`, `drift deploy`, lockfile,
  manifest, native deps, staged toolchain — all already operate at
  package granularity.
- **Already the boundary the lockfile pins.** Cache invalidation
  composes with the existing exact-version pin.
- **The cross-unit edge count is small.** A typical workspace has a
  handful of packages with a tractable dependency DAG. Link-time merge
  cost is bounded.
- **Maximum reuse, minimum new design.** Every other choice introduces
  a new boundary that the rest of the toolchain doesn't know about.

The trade-off: a single-package workspace (stdlib, drift-web, most user
projects) gets zero process-level parallelism from this plan. What it
gets instead is **content-keyed cache hits**: if no source file in the
package changed and no consumed dependency changed, the package's cached
codegen artifact is reused and driftc doesn't re-lower at all. For the
"ran tests, no source changes" case — which is the dominant inner loop —
that's the right answer.

For multi-package workspaces, the parallelism is real package-graph
parallelism: any two packages with disjoint dependency sets compile in
separate driftc processes that don't share state.

---

## 3. Required contract surface

The plan introduces two artifact families per package:

- **Interface artifact**: the existing `.dmp`, with one small extension.
  Already carries the typed surface; the plan adds an
  `imported_references` section listing the FunctionKey / TypeId /
  trait_key tuples this package consumes from upstream.
- **Codegen artifact**: new. The cached lowered output for one package,
  with a sidecar manifest describing what was emitted and what is owned.

### 3.1 Interface artifact (`.dmp` + small additions)

What today's `provisional_dmir_v0.py` already carries that we keep:

- **Public types**: every type declared by this package, full structural
  shape (fields, variants, generic parameters, traits implemented).
- **Function signatures**: every public function's typed signature, with
  throw set, can_throw, error type, FunctionKey.
- **Generic templates**: HIR body of generic functions (for use-site
  instantiation by consumers).
- **Trait impl metadata** (`ImplMeta`): `(target_type, trait)` pairs this
  package defines impls for, with the FunctionKey of every method.
- **Constants**: public constants with typed values.
- **Visibility provenance**: which module each public name lives in.

What we add:

- **Imported references manifest**: the explicit list of
  `(FunctionKey, TypeId, trait_key, impl_id)` tuples this package
  *consumes* from upstream packages. Today this is implicit; making it
  explicit gives the link step a concrete graph to validate, and gives
  the cache an exact dependency set to key on.

### 3.2 Codegen artifact (new)

A directory next to the existing `.dmp` containing:

- **One LLVM IR module or `.o` file** for the whole package. Contains:
  - All non-generic functions defined in the package, with external
    linkage where they're public, internal otherwise.
  - All wrappers owned by this package (per the rules in §5), with
    `linkonce_odr` for every wrapper category that has a stable
    content-hashed mangled name.
  - All monomorphized generics demanded by this package, with
    `linkonce_odr` linkage (already the case today via the
    instantiation index).
  - All vtables for `(target_type, trait)` pairs whose impl is owned
    by this package, with `linkonce_odr`.
  - Module flags + (optional) `!dbg` metadata aligned with the active
    runtime lane (from the just-shipped dual-runtime workstream).
- **A sidecar manifest** describing:
  - The codegen artifact's content hash (cache key).
  - The list of symbols defined (mangled name) and a flag for each:
    `unique` vs `linkonce_odr`.
  - The list of symbols *referenced but not defined* (the package's
    imports — must resolve to another package or the runtime archive
    at link time).
  - The list of `(impl_id, trait_key, target_type_id)` tuples this
    package owns.
  - The list of `(target_type_id, FunctionId)` destructor entries this
    package owns.
  - The compiler version (`DRIFTC_VERSION`).
  - The runtime ABI version (`DRIFT_RT_ABI_VERSION`).
  - The active runtime lane (`normal` / `debug-style`).
  - `drift_main_owned: bool` (only one package per program may be true).

### 3.3 What consumers get

A package `C` compiling against packages `A`, `B` reads:

- `A.dmp`, `B.dmp`: typed surface, FunctionKeys, type definitions,
  trait impls, generic templates.
- `A.codegen.json`, `B.codegen.json`: list of symbols `A` and `B` will
  resolve at link time. `C` doesn't need the actual `.o` files for type
  checking — it only needs to know the symbol *will exist*.

`C` then lowers its own source against the imported interface, emits its
own codegen artifact, and the link step is responsible for combining
`A.o + B.o + C.o + runtime.a` into a binary.

This is exactly what a C/Rust developer expects from "real separate
compilation".

---

## 4. Ownership rules

Every category of generated code is assigned to exactly one package (or,
for shared/dedup-eligible code, to N packages with linker dedup).

### 4.1 Generic monomorphizations

**Already solved.** `linkonce_odr` + COMDAT in the existing instantiation
index. Each package that demands `Vec<Int>::push` emits its own copy and
the linker keeps one. Verified at `driftc.py:4221`.

**Demand-side rule: use-site instantiation.** A consumer package `C`
reads the template HIR from `A.dmp`, runs its own
instantiation+lowering, emits its own `linkonce_odr` symbol. `A` may
also emit the same instantiation if it happens to use it. The linker
picks one. Fully local, no cross-unit demand graph required.

The define-site model (consumer writes a request, link step routes it
back to `A`, `A` re-runs to fulfill it) is rejected: it's daemon-shaped
and the use-site model is strictly better because `linkonce_odr` already
handles the dedup.

### 4.2 Trait impls

**Orphan rule + package-local impl_id (already in place).**

The defining package owns:

- The impl record itself (already an `ImplMeta` in the payload).
- The vtable for `(target_type, trait)` if the trait is dispatched
  dynamically (the package can't always know, so emit it always with
  `linkonce_odr`).
- The method-body wrappers for the impl methods, with `linkonce_odr`
  and content-hashed mangled names (see §5.3).

Two packages may not define the same `(target_type, trait)` impl.
Today this is enforced lexically per-package and the cross-package case
was hardened in 0.27.43-dev (memory item 19). The plan inherits this
state.

**Discovery:** consumers find impls via the `.dmp`'s
`(target_type_id, trait_key) → impl_id` index. The link step builds the
union of all per-package indexes and validates uniqueness as a
belt-and-suspenders check.

### 4.3 Wrappers — the high-leverage scope

Drift emits at least these wrapper categories:

| Wrapper | Currently emitted by | Ownership in the plan |
|---|---|---|
| `drift_main` (OS entry) | The build that produces the binary | **Unique-per-program.** Owned by the binary-producing package. Only that package may emit it. Validated at link step. |
| K16a nothrow method wrappers | The build that consumes the package containing the method | **Per-callsite, `linkonce_odr`.** Each consumer package emits its own copy; linker dedups. **Mangled name must be content-hashed** so two packages produce the same name without coordination. |
| K26 trait impl method wrappers | The build that injects the impl | **Owned by the impl-defining package.** `linkonce_odr` for safety. **Mangled name content-hashed.** |
| K40 preamble wrappers | The build that consumes the dependency | **Per-callsite, `linkonce_odr`.** **Mangled name content-hashed.** |
| Destroy-body wrappers | Whichever build first sees the destructor type graph reach this body | **Owned by the type-defining package.** `linkonce_odr`. **Mangled name content-hashed.** |
| External method wrappers (`__wrap_method`) | The build that consumes the package providing the externally-callable method | **Per-callsite, `linkonce_odr`.** **Mangled name content-hashed.** |
| ABI version stamp + sentinels | The runtime archive | **Already separate.** No change. |

**Only `drift_main` is unique-per-program.** Every other wrapper category
can be `linkonce_odr` if its mangled name is a content hash that two
packages agree on without coordination.

The mangled-name work is the largest single chunk of new code in this
plan. It is mechanical (sometimes painful) work, not architectural work:
for each wrapper category, replace whatever per-compilation ordinal /
interned id is currently in the name with a content hash derived from
the same inputs that determine semantic identity. `decl_fingerprint` is
the existing analogue and the right model.

This work is **explicitly in scope** for the plan. The previous draft of
this document scoped it as Phase 2 work; on reflection that was wrong —
without it, `linkonce_odr` doesn't reach all wrapper categories, and the
cache becomes unsafe for any package that produces wrappers (which is
most of them).

### 4.4 Vtables (for `dyn Trait`)

Owned by the impl-defining package. `linkonce_odr` so multiple packages
that both happen to define the same impl get deduped.

**Vtable layout must be deterministic** independent of compilation order.
Today the K26 path injects vtable rows during whole-program walk; the
row order may depend on the order impls were discovered. The plan pins
**method index = position in the trait declaration's method list**, so
two packages defining the same impl produce identical vtable bytes. This
is a small constraint on the existing K26 code.

### 4.5 What is safe to duplicate

- Anything `linkonce_odr` is safe by construction.
- Inline / always-inline functions are safe (LLVM handles it).
- Header-equivalent metadata (type definitions in interface artifacts).
- The runtime archive sentinel and ABI stamp are not duplicated.

### 4.6 What is NOT safe to duplicate

- The OS entry (`drift_main`). Exactly one package emits it.
- (Drift currently has no module-init / static-init functions, which
  simplifies this category to zero.)
- Symbols other packages link against by name and assume a single
  definition site (none today; worth a sweep during implementation).

### 4.7 Destructor table

`shared_type_table.destructor_fns` is the most "global state" thing in
the type table. Per-package, each package registers destructors for
types it owns. The link step concatenates per-package tables — key =
content-hashed type_id, value = `FunctionId` of the destroy body
(which is `linkonce_odr` and lives in the type-owning package).

A duplicate key with different values is a hard error at the link step
(orphan-rule violation: two packages claim to own the destructor for
the same type). With content-hashed names this should be impossible;
the validation catches it as a sanity check.

---

## 5. Type identity / interface identity

This is the section that evaluates "compile independently, drop dups at
link" most directly.

### 5.1 What needs canonical identity before link

For the link step to be safe (in the sense of "produces the same binary
the whole-program build would produce"), these must be identical across
units:

1. **Type identity.** `TypeId` for `Foo` must resolve to the same
   canonical id in every package that sees `Foo`. Today the
   `type_table_link_v0` FORWARD_NOMINAL pass does this at link time.
   The plan **reuses** this — no new work; per-package codegen artifacts
   carry their package-local TypeIds, the link step canonicalizes.
2. **Function identity.** `FunctionKey` for `f` must be the same. Already
   true via `decl_fingerprint`.
3. **Impl identity.** `impl_id` for `impl Display for MyType` must be
   the same. **Already true at the package boundary** after the
   0.27.43-dev fix.
4. **Trait identity.** `trait_key` for `Display` is `(package_id,
   module_path, name, version)`. Already content-stable.
5. **Mangled symbol names.** Every emitted symbol whose linkage is
   `linkonce_odr` must have the same mangled name in every package
   that emits it. **Today this is partial** — monomorphized generics
   are content-hashed; wrappers are not. **The plan extends content-
   hashing to every wrapper category.** This is the §4.3 work.

### 5.2 What is link-time-merged

- Vtables (`linkonce_odr`).
- Monomorphized generics (`linkonce_odr`).
- All wrapper categories (`linkonce_odr`, after §4.3).
- Type table fragments (union, with FORWARD_NOMINAL canonicalization).
- Impl tables (union, with the orphan-rule validation as a
  belt-and-suspenders check).
- Destructor tables (union, content-hashed keys).

### 5.3 What CANNOT be link-time-merged

- The entry point. `drift_main` must come from one package.

### 5.4 The "compile independently, drop dups at link" model

**This is the architecture.** It works for Drift specifically because:

- LLVM `linkonce_odr` + COMDAT is the standard mechanism; clang and lld
  both implement it correctly.
- Drift already uses `linkonce_odr` for monomorphized generics. The
  hardest case is solved.
- The remaining cases (every wrapper category) are mechanically the
  same shape: deterministic mangled name + `linkonce_odr` linkage.
- `decl_fingerprint` already pins function identity; `impl_id` is
  package-local-stable; `TypeId` canonicalization at link time is
  already in production.

**Where it breaks down — and how the plan handles each**:

1. **Mangled name drift.** If two packages produce different mangled
   names for the same wrapper, the linker doesn't dedup and you get
   two distinct symbols. **Fix:** §4.3 wrapper-name content-hashing.
   This is the load-bearing piece of new work in the plan.

2. **TypeId drift in the IR.** If one package emits `%type_42` and
   another `%type_17` for the same type, the IR doesn't link cleanly
   even if symbols match. **Fix:** types are structural in the LLVM
   IR (the compiler lowers TypeIds to LLVM struct types, not to int
   IDs in the IR), so the type identity is established at codegen
   time within one package and the link step doesn't see TypeIds at
   all. The int IDs are a front-end concern only. *Audit needed
   during implementation* to confirm no place leaks the int into the
   IR.

3. **Impl method dispatch.** If two packages disagree on the method
   index in a vtable, the binary is silently wrong. **Fix:** §4.4
   pins method index = position in trait declaration. Small
   constraint on existing K26 code.

4. **Orphan-rule violations.** If two packages accidentally define
   the same impl, the link step has to detect it. **Fix:** the
   orphan rule is already enforced at the package boundary (memory
   item 19, 0.27.43-dev). The link-step union-and-validate is a
   belt-and-suspenders check.

5. **Throw checking transitivity.** If package `A` declares `f()
   throws E` and package `B` declares the same `f` with `throws E2`,
   that's a real semantic conflict. **Fix:** the `decl_fingerprint`
   for an imported FunctionKey already encodes the throw set; if
   the import-site's expected fingerprint disagrees with the
   exporter's published fingerprint, the cache miss surfaces as a
   re-typecheck which catches it. The link step also validates as
   a final guard.

**Verdict:** drop-dups-at-link is realistic for Drift. The work is
concentrated in making mangled names content-hashed for every wrapper
category and in moving cross-unit validation from "Pass1 sees
everything" to "link step verifies the union". This is medium-sized
invasive work, not architectural rewrite.

---

## 6. Link / finalize phase

### 6.1 What it consumes

- N codegen artifacts (one per package), each containing:
  - LLVM IR or `.o` for the package.
  - A sidecar manifest of defined symbols, referenced symbols, owned
    impls, owned destructors, throw declarations, lane.
- The interface artifacts (`.dmp`) for any package the link step
  needs to validate against.
- The runtime archive for the active lane (already separate).
- The lockfile (already at `tools/drift_deploy/lockfile.py`).

### 6.2 What it does

1. **Validate the artifact graph.** Every package's `referenced
   symbols` list must resolve to exactly one package's `defined
   symbols` list (or to the runtime archive). Failure → unresolved
   reference error with a clear message.
2. **Validate orphan rules.** Union of per-package impl tables.
   Duplicate `impl_id` with different bodies → orphan-rule violation
   (hard error). Duplicate `impl_id` with the same body → drop one
   (`linkonce_odr` will handle it at the .o layer, but the link step
   rejects it earlier for clarity).
3. **Validate the destructor table** the same way.
4. **Validate cross-package signature agreement.** Every imported
   `FunctionKey` must have the same signature in the importer and
   the exporter. The cache key already includes this; the link step
   verifies as belt-and-suspenders.
5. **Build the global type table** by union of per-package fragments.
   Apply the existing FORWARD_NOMINAL canonicalization. Duplicate
   `TypeId` with different shapes → fatal.
6. **Pick the unique package owning `drift_main`** and verify it
   exists. Multiple owners → fatal. Zero owners → the user is
   producing a library, no `drift_main` required.
7. **Validate the lane.** Every codegen artifact must declare the
   same lane (normal or debug-style). Mixing is a hard error. This
   reuses the conftest sentinel-audit pattern from the just-shipped
   workstream.
8. **Invoke the linker** (clang) on all `.o` files + the runtime
   archive + the link libs already enumerated by the existing driftc
   clang link path.
9. **Post-link verification:** the existing sentinel audit + gdb-index
   etc., unchanged from this workstream.

### 6.3 What the link step is NOT

- Not a daemon. Not a long-running coordinator.
- Not a deduplicator. `linkonce_odr` + COMDAT handles dedup; the
  drift-side link step's job is *validation*, not symbol-table
  surgery.
- Not stateful. A single process that reads N files, validates,
  invokes the existing clang link command, writes the binary.

---

## 7. Incrementality and caching

### 7.1 Cache key (per package)

For a package's **codegen artifact**, the cache key includes:

- The hash of the package's source files (Drift sources + native deps).
- The hash of every interface artifact this package consumes
  (transitively pinned by the lockfile, so no transitive walk needed
  at build time).
- The compiler version (`DRIFTC_VERSION`).
- The runtime ABI version (`DRIFT_RT_ABI_VERSION`).
- The active runtime lane (normal vs debug-style — already a content
  axis from the dual-runtime workstream).
- The set of compile-time flags that affect codegen (`-O`, `-g`,
  sanitizer state, target word bits).
- The trust store identity (if signature verification is enabled, a
  changed trust store invalidates downstream consumer caches).

For a package's **interface artifact**, the cache key is the same
minus the codegen-only flags. Interface artifacts are reusable across
optimization modes; codegen artifacts are not.

### 7.2 What invalidates a package

- Source change → package rebuilds.
- Any consumed interface artifact's hash changes → package's codegen
  rebuilds (interface may or may not need to rebuild, depending on
  whether the consumed change affected the package's public surface).
- Compiler version bump → everything rebuilds.
- ABI bump → everything rebuilds.
- Lane flip (`DRIFT_DEBUG=1` toggled) → codegen artifacts rebuild
  (different cache key); interface artifacts are reused.
- Trust store change → cached signature-dependent artifacts
  invalidate.

### 7.3 Useful incremental rebuilds

This architecture *naturally* enables:

- Edit a function body inside a package → only that package's codegen
  artifact rebuilds. Interface artifact rebuilds only if the public
  surface changed. Every other package is reused from cache.
- Edit a public type → that package's interface artifact rebuilds;
  every downstream consumer's codegen rebuilds; binary relinks.
- Add a new function to a package → that package's interface and
  codegen rebuild; consumers are unaffected unless they import the
  new function.
- Toggle `DRIFT_DEBUG=1` → all codegen artifacts rebuild (different
  lane key); all interface artifacts are reused.
- Run tests with no source changes → every package is a cache hit;
  driftc does no work; the link step is the only cost.

That last bullet is the dominant inner-loop case for most workflows
and the highest-value win in the plan.

### 7.4 Where the cache lives

The runtime archive cache (`build/runtime_libs/<variant>/`) is already
content-addressable in spirit — different cflags produce different
variants in different subdirs. The same model extends to per-package
codegen: `build/units/<package_id>/<cache_key>/{interface,codegen}`.

The lockfile already pins exact versions for cross-package
dependencies (`tools/drift_deploy/lockfile.py`). Extending it to also
pin interface artifact hashes is small.

---

## 8. Parallelism model

### 8.1 Where real parallelism comes from

- **Across packages.** Any two packages with disjoint dependency sets
  compile in separate driftc processes that don't share state. For a
  workspace with 5 leaf packages and 3 root packages, this is
  meaningful.
- **Within a package: not in this plan.** Inside one driftc invocation
  everything continues to serialize through the existing whole-program
  walk. The plan does not touch intra-package concurrency.

### 8.2 Operational shape (one driftc per package)

```
$ drift build -j 4 my-workspace
# computes package DAG: [pkg-a, pkg-b] depend on [pkg-c, pkg-d]
# step 1: schedule pkg-c and pkg-d (4 cores → 2 in parallel)
#   process: driftc --emit-package pkg-c
#     → writes pkg-c.dmp + pkg-c.o + pkg-c.json
#   process: driftc --emit-package pkg-d
#     → writes pkg-d.dmp + pkg-d.o + pkg-d.json
# step 2: pkg-a and pkg-b can now start (pkg-c, pkg-d are ready)
#   process: driftc --emit-package pkg-a --package-root .build/pkgs
#   process: driftc --emit-package pkg-b --package-root .build/pkgs
# step 3: drift link --validate .build/pkgs/*.json --output app.bin
#   (validates the artifact graph, then invokes clang on the union of .o files)
```

Each process is a real driftc, processing one package. The link step
is one process at the end. No daemon. No threads inside driftc.

For a single-package workspace (stdlib, drift-web), step 1 has one
process, step 2 is empty, step 3 invokes the linker. Parallelism is
zero, but **cache hits** mean the typical "ran tests, no source
changes" inner loop produces a step-1 cache hit and a fast step-3
relink.

### 8.3 Honest answer to "no fake -j"

The user's constraint maps cleanly:

- **"Threads inside one process pretending to parallelize the AST
  passes"** → fake. Not in this plan.
- **"Multiple driftc processes producing independent artifacts
  according to a real contract"** → real. This is the plan.
- **"One driftc process emitting cached artifacts that are
  independently skippable on next build"** → real. Even though it's
  one process, the *test* is whether the artifacts are reproducible
  and independently invalidatable, which they are.

---

## 9. Migration scope (the plan as work items)

The plan as discrete work items, in dependency order:

### 9.1 Define the artifact contracts (~1–2 weeks)

1. Codegen artifact directory layout next to the existing `.dmp`.
2. Sidecar manifest schema (defined symbols, referenced symbols,
   owned impls, owned destructors, lane, cache key, version pins).
3. Interface artifact extension (the `imported_references` list).
4. Cache key derivation function and the inputs it depends on.
5. Regression scaffolding (mirror the dual-runtime workstream's
   regression-first discipline): a small set of tests that pin the
   contract before the producer/consumer code lands.

### 9.2 Wrapper-name content-hashing (~3 weeks)

For each wrapper category, replace the per-compilation ordinal /
interned id in the mangled name with a content hash:

- K16a nothrow method wrappers
- K26 trait impl method wrappers
- K40 preamble wrappers
- Destroy-body wrappers
- External method wrappers (`__wrap_method`)

Each category needs:

- The hash inputs identified (typically: target type fingerprint,
  trait fingerprint, method name, signature fingerprint).
- A regression test that asserts the same wrapper from two
  independent compilations produces the same mangled name byte-for-byte.
- A linker test that asserts two packages emitting the same wrapper
  link cleanly under `linkonce_odr`.

This is the largest single chunk in the plan. It is mechanical work
but has to be done carefully — wrapper synthesis paths have a track
record of post-BFS reachability bugs (memory items 21, 22, 30).

### 9.3 Codegen artifact emission (~1 week)

Make `driftc --emit-package` *also* emit the codegen artifact when it
has access to the lowered IR (which is when building from source).
Mostly plumbing — the IR already exists at the point in driftc.py
where the package is serialized.

### 9.4 Consumer-side cache hit (~2–3 weeks)

When `driftc` is invoked to build a binary that depends on package A,
and A's cache entry is present and the cache key matches:

- Skip A's HIR → MIR → SSA → IR re-run.
- Include A's `.o` directly in the final clang link command.
- Validate A's sidecar manifest against the importing package's
  imported_references list.

Largest non-trivial change in this group. Local to driftc's
package-load path. This is where the 0.27.x latent bugs are most
likely to surface.

### 9.5 Link step (~1 week)

A new `drift link` subcommand (or an extension to `drift build`'s final
phase) that:

- Walks the union of per-package codegen manifests.
- Validates the artifact graph (defined/referenced symbol resolution).
- Validates orphan rules (impl + destructor uniqueness).
- Validates lane consistency.
- Picks the unique `drift_main` owner.
- Invokes clang with the `.o` files + runtime archive + link libs.
- Runs the existing post-link sentinel audit unchanged.

### 9.6 drift build parallelism + DAG scheduling (~1 week)

Convert `drift build`'s serial per-artifact loop into a process pool
that respects DAG order. Honor `-j N`. Default to `-j 1` initially so
the first wave of users opts in (`-j auto` after a stabilization
window). Mirror how `DRIFT_OPTIMIZED` was introduced in this
workstream before being retired.

### 9.7 End-to-end validation (~2 weeks)

The inevitable wave of latent-coupling fixes that surface when the
cached path produces a binary that disagrees with the from-source
path. The dual-runtime workstream's experience suggests this is at
least a week of triage on top of any initial implementation. Budget
two weeks for the same discipline level here.

The validation gate is:

- `convergence_parity` continues to pass everywhere.
- `just test` is fully green in both lanes (normal and debug-style).
- `just test` with the cache primed produces the same outputs as
  `just test` from a clean build.
- `drift build -j N` produces the same binaries as `drift build -j 1`
  for `N ∈ {1, 2, 4, 8}`.

### 9.8 Total

- Artifact contracts + scaffolding: 1–2 weeks
- Wrapper-name content-hashing: 3 weeks
- Codegen artifact emission: 1 week
- Consumer-side cache hit: 2–3 weeks
- Link step: 1 week
- drift build parallelism: 1 week
- End-to-end validation: 2 weeks
- **Total: ~10–13 weeks** for one engineer at this codebase's
  discipline level.

Faster if more engineers work in parallel (the wrapper-name work and
the artifact-emission work are independent enough to split). Slower
if the wave of latent couplings in §9.7 is deeper than expected.

---

## 10. Risk assessment

### 10.1 Likely hardest subsystems

Ranked by where I expect to hit the most surprising coupling:

1. **Wrapper-name content-hashing.** Drift's wrapper synthesis paths
   have a track record of post-BFS reachability bugs (memory items 21,
   22, 30). Each one is evidence that wrapper ownership is implicit
   and order-dependent. The work is to make wrapper *names* content-
   hashed; the side effect is that any place where wrapper synthesis
   depended on order will surface as a content-hash collision or a
   cache miss. Both are detectable; both will need fixes during
   implementation.

2. **The `convergence_parity` check itself.** It compares two paths
   through Pass1. Adding a third "from-cache" path means it has to
   compare three paths, OR the cached path has to be byte-equivalent
   to the from-package path so the existing two-way check still pins
   it. The latter is easier and is what I'd recommend. Either way,
   it will likely surface at least one place where the two existing
   paths disagree in ways that have been masked by the whole-program
   flatten.

3. **Visibility provenance + module_id stability.** The "module_id"
   assigned to each module is a per-compilation int that the compiler
   writes into various places. For the cached path, two builds have
   to assign the same module_id to the same module. Either we hash
   it, or we ensure assignment order is deterministic. Has to be
   audited.

4. **Generic template body serialization round-trip.** Already
   happens (templates are in the package payload), but the round-trip
   fidelity has been the source of multiple bugs. The plan inherits
   this; making it bulletproof in the cached path is a smaller
   version of the same audit.

5. **The interaction between `inst_cache` and K20** (suppressing
   `__inst__` monomorphizations when a generic template is present).
   For cached consumers, K20 suppression has to happen against
   templates *visible from the cached interface*. Slightly more
   nuanced than today.

### 10.2 Likely hidden coupling points

- **Trust store and signature verification.** The package signature
  verification path runs at consumption time. Cached artifacts need
  to re-verify against the same trust store, and the cache key has
  to include the trust store identity. Easy to miss.

- **`stage2/string_arc.py` and other ARC lowering passes.** They emit
  release/retain calls based on whole-program data flow. Per-package
  the data flow is bounded, but cross-package reference counting has
  to be preserved. Should fall out naturally from the linker
  resolving the helper symbols, but worth verifying.

- **Destructor type graph reachability.** Memory item 22 documents a
  past bug where destroy-body wrappers were missed by the BFS. The
  cached path has to handle "the type that needs the destructor lives
  in package A, but the use site that demands the destructor lives in
  package B". Either A pre-emits all destructors for its public types
  (eager + `linkonce_odr`), or the link step performs a final
  reachability walk over the union of artifacts. The plan picks the
  eager-emit option because it composes better with `linkonce_odr`.

- **Trait dispatch through cached impls.** If C calls `t.method()`
  where `t: dyn SomeTrait`, the call goes through a vtable that may
  live in any package. The link step's union-of-impl-tables has to
  be queryable at the right point in the cached path. Should be
  straightforward but worth verifying.

### 10.3 Sizing

- **Size:** medium.
- **Total:** ~10–13 weeks for one engineer at this codebase's
  discipline level (regression-first, audit-validated, survives
  `just test`).
- **Confidence:** medium-high. The artifact format is mostly already
  there, the link-time validation logic is bounded, and the wrapper-
  name work is mechanical. The largest source of estimate uncertainty
  is §9.7 (the latent-coupling wave), which the dual-runtime
  workstream's experience suggests is real but bounded.
- **Risks:** the convergence_parity check will surface latent
  disagreements; some wrapper categories will be harder to content-
  hash than expected; scope creep into module-level units is the
  biggest non-technical risk (resist it — see §11).

### 10.4 What this plan does NOT promise

- It does not promise sub-package incremental rebuilds. If you edit
  one function in a 50-module package, the whole package re-lowers.
  What it does promise is that *every other package in the workspace*
  is a cache hit, and that the inner loop of "ran tests, no source
  changes" is essentially free.
- It does not promise intra-package parallelism. One driftc per
  package; everything inside one driftc is serial.
- It does not promise an LSP-style live feedback path. That's a
  different problem.

---

## 11. Out of scope (and what would force revisiting)

The following are explicitly **not** in this plan, and not on the
roadmap:

- **Module-level compile units.** A package is one unit. Each driftc
  invocation handles one whole package. Modules inside a package are
  not separately cached, not separately compiled, not separately
  parallelized.
- **Content-hashed `impl_id`.** Package-local impl_ids (the 0.27.43-dev
  fix) are sufficient for package-level units. Content hashing is a
  prerequisite for module-level units, so it stays out.
- **Demand-driven trait dispatch resolved at link time.** The type
  checker continues to see the impl table at type-check time. The
  link step's union-and-validate is belt-and-suspenders, not the
  primary dispatch mechanism.
- **A daemon, a coordinator, or any long-lived compiler process.**
  Categorically out.
- **Threads inside driftc parallelizing AST passes.** Categorically
  out.
- **A removal of the whole-program walk inside one driftc invocation.**
  The walk stays. The plan caches its output.

### 11.1 What would force revisiting

These are the explicit triggers for considering finer granularity or
additional architecture work. None of them are on Drift's current
trajectory; the section exists so we have *written* triggers, not
vibes:

1. **A single package routinely takes more than ~30 seconds to compile
   from source on representative hardware AND no other package in the
   workspace is on the critical path.** That's the case where per-
   package parallelism doesn't help and the cache hit is unavailable
   (because you're actively editing the package). At that point,
   sub-package units start to matter.

2. **A stated developer-experience target that this plan can't hit.**
   Examples: "edit-to-feedback under 500 ms in stdlib for a one-line
   change", "live LSP type-check on every keystroke for a 100k-line
   package". These targets require finer-grained units; this plan
   targets the seconds-to-tens-of-seconds band, not sub-second.

3. **The package count stays small but per-package size grows by an
   order of magnitude.** A hypothetical 500-module stdlib would
   defeat per-package cache hits during active editing.

4. **A toolchain shift toward live type-checking** (drift-lsp,
   drift-check, IDE integration). This plan is build-system-shaped,
   not editor-shaped. An editor integration would have its own
   architectural needs.

If any of these triggers fire and the chosen response is "split
packages into modules", the plan's artifact contract is designed so
that a future "module" is just a "single-module package", which means
the existing artifact format extends additively. We don't have to
re-architect; we have to introduce a new boundary inside what is
currently one driftc invocation. That's a real engineering project,
but it's not blocked by anything in this plan.

---

## Open questions

These have to be answered before implementation starts:

1. **What is the exact ownership rule for `__wrap_method` external
   method wrappers?** The wrapper synthesis is order-dependent today
   (memory items 21–22). The cached path has to produce identical
   sets of wrappers and identical mangled names across two runs of
   the same package. Verification requires running the same package
   build twice and diffing the wrapper symbol tables.

2. **Do any tests in the suite read TypeIds as ints with specific
   values (e.g., assertions on ordinal)?** A grep would answer this;
   if any do, they have to be updated. The cached path inherits
   whatever exists today.

3. **How does the lockfile interact with the cache key?** The
   lockfile pins exact dependency versions. The cache key probably
   wants the lockfile hash as one input; that way a `drift prepare`
   that moves a transitive dep invalidates downstream consumers.

4. **What does the convergence_parity check do once a third path
   exists?** Recommendation: make the cached path byte-equivalent
   to the from-package path so the existing two-way check still
   pins it.

5. **Default `-j` value.** Recommendation: default to `-j 1`
   initially, mirror the `DRIFT_OPTIMIZED` rollout pattern from
   the dual-runtime workstream, flip to `-j auto` after a
   stabilization window.

6. **Trust store cache key inclusion.** The trust store path or
   trust store hash has to be in the cache key for any artifact
   whose validity depends on signature verification. Easy to miss.

7. **The eager-vs-lazy destructor emission decision.** The plan
   recommends eager emission of destructors for every public type a
   package owns, with `linkonce_odr` for safety. Confirming this is
   correct under the existing ARC lowering passes is a small audit.

---

## Recommended direction

**Schedule the plan as written.** Package-level separate compilation
with content-hashed wrapper names is the right architecture for Drift.
Phase boundaries are dropped: the entire plan is one workstream of
~10–13 weeks at this codebase's discipline level. Module-level units
are explicitly **not** a follow-up; they are a contingency only, with
written triggers in §11 that are not on the current trajectory.

Reject the daemon model unconditionally.
Reject intra-process AST-pass threading unconditionally.
Reject "fake -j" unconditionally.
Reject scope creep into module-level units during this workstream.

The plan plays well with the dual-runtime workstream that just shipped
(the codegen artifact is per-lane and the existing audit hooks already
verify cross-lane integrity), reuses every cross-unit primitive that's
already in production (`FunctionKey`, `decl_fingerprint`,
`linkonce_odr` monomorphs, FORWARD_NOMINAL canonicalization, the
package format), and delivers honest `drift build -j` over the real
package dependency graph with real cache hits on unchanged packages.

For the typical inner loop ("ran tests, no source changes"), this
makes driftc do essentially zero work. That's the highest-value win
in the plan and the reason package-level units are sufficient.
