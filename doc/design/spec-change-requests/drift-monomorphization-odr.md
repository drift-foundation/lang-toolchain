### Technical note: generic monomorphization and multiple definitions (MVP)

Drift uses **per-compilation-unit monomorphization** with **link-time folding** to resolve “multiple definitions” of the same generic instantiation.

#### Problem

The same generic struct/function (e.g., `Vec<T>`, `map<T,U>(...)`) may be instantiated in multiple compilation units (CUs). If each CU emits a normal (“strong”) definition, the final link would fail due to duplicate symbols, or worse, succeed with inconsistent behavior depending on link order.

#### Rule (ODR-equivalent for Drift)

For any instantiated generic, Drift defines a unique **InstantiationKey**. The final executable must contain **exactly one** definition for each InstantiationKey. Multiple emitted copies are permitted only if they are emitted in a way that the linker can fold to a single kept definition.

#### InstantiationKey

The InstantiationKey is a stable, deterministic identifier computed from:

* **Generic definition identity**

  * `(ModuleId, GenericDefId)` (or an equivalent compiler-internal stable ID)
* **Canonicalized type arguments**

  * Type aliases expanded
  * Normalized defining module identity applied (same normalization rules used for trait/impl matching)
  * Fully-qualified types include module identity; builds must reject duplicate ModuleIds
* **Canonicalized where/trait arguments** (if present in the generic header)
* **ABI-relevant flags**

  * calling convention / ABI mode
  * throw-mode (`nothrow` vs throwing) and any other signature-affecting attributes
  * layout/representation attributes that affect codegen

Two instantiations are considered “the same” if and only if their InstantiationKeys match bit-for-bit.

#### Emission model

When a CU requires an instantiation, it may emit it locally. However, the emitted symbols must be marked **link-once / COMDAT / weak ODR** so that:

* the linker accepts multiple emitted definitions with the same symbol name, and
* the linker keeps one definition and discards the rest.

This applies not only to the primary function/struct code, but to all **associated monomorphized artifacts**, including:

* method bodies for `impl` blocks on instantiated types
* drop/destructor glue
* vtables / trait method tables (if any exist in MVP scope)
* thunks/adapters generated for ABI or trait dispatch
* any compiler-synthesized helpers tied to the instantiation

#### Package templates (TemplateHIR payload)

For cross-package generics, provider packages ship **generic templates** in a separate payload (`generic_templates`). These templates are **not** DMIR; they may contain TypeVars and constraints. Each consumer instantiates the template per compilation unit into concrete DMIR/MIR, then emits linkonce/ODR-foldable symbols keyed by the InstantiationKey.

#### Naming/mangling

Monomorphized symbols must be named deterministically from the InstantiationKey (e.g., a mangled name containing a stable hash of the key plus a readable prefix for debugging). All CUs must compute identical symbol names for identical InstantiationKeys.

#### Correctness constraint

The semantics of an instantiation are defined **only** by:

* the generic body, and
* the canonicalized type/trait arguments embodied in the InstantiationKey.

Instantiation behavior must not depend on CU-local optimization choices in a way that changes observable behavior. (Different codegen is allowed as long as it is ODR-equivalent; the folding mechanism assumes all folded candidates are semantically identical for the same key.)

#### Non-goal for MVP: cross-binary ABI stability

This rule is for producing a **single final executable** from multiple CUs. For shared libraries/plugins, relying on “both sides instantiate the same generic” is not an ABI guarantee; MVP does not promise cross-binary generic instantiation compatibility.

#### Implementation notes (compiler/linker expectations)

* Object emission uses “link-once ODR” (or the closest equivalent on the target toolchain) to enable folding.
* If a target environment lacks reliable COMDAT/weak folding, Drift must fall back to a different strategy (not part of MVP).

This is the MVP definition of how Drift resolves multiple definitions arising from generic instantiation across compilation units.
