# Boundary Wrapper Convergence Refactor

## Status: investigation + proposal (review before execution)
## Date: 2026-04-01

---

## 1. Current Wrapper Architecture End-to-End

### Why wrappers exist

Drift has a two-layer calling convention:

1. **Internal**: functions return their surface type directly (Int, String,
   Result<T, E>). Nothrow functions never produce an error. Can-throw
   functions return `FnResult<T, Error>` (an internal 3-field struct:
   `{is_err: i8, ok: T, err: Error*}`).

2. **Boundary/public**: all exported functions return a uniform ABI struct
   `{ok: T, err: Error*}`. Nothrow functions set `err=null`. Can-throw
   functions set `err` to the error pointer on failure. This allows callers
   to handle errors uniformly without knowing at compile time whether the
   callee can throw.

The wrapper system bridges these two layers:
- `foo__impl` is the private body (internal convention)
- `foo` is the public wrapper (boundary convention)
- `__wrap_method::foo` is a method wrapper that converts nothrow → can-throw
  ABI by wrapping the result in `ResultOk`

### What problems wrappers solve

| Problem | Solution |
|---------|----------|
| Nothrow/throw ABI mismatch | `__wrap_method::` converts nothrow return to Result ABI |
| Stable public symbol contract | `foo` is the stable public name; `foo__impl` is private |
| Cross-package call safety | Callers always use the boundary ABI, never `__impl` |
| Error propagation across packages | FnResult/Result conversion at boundaries |

### When wrappers are introduced

| Pipeline stage | What happens |
|----------------|-------------|
| `_inject_method_boundary_wrappers` (driftc.py:996) | Declares wrapper signatures for pub nothrow methods |
| Wrapper MIR synthesis (driftc.py:6961+) | Generates 3-instruction MIR: Call + ConstructResultOk + Return |
| `__impl` rename (llvm_codegen.py:432-475) | Renames exported function bodies to `foo__impl` |
| Public wrapper emission (llvm_codegen.py:478-575) | Generates LLVM wrapper under original `foo` symbol |

### When routing is decided

**Late — at LLVM codegen time** in `_resolve_call_target_symbol`
(llvm_codegen.py:6610-6670). The decision depends on:

```
is_cross_module = (caller_pkg != callee_pkg)
                  AND (force_boundary
                       OR callee_mod NOT IN source_modules
                       OR callee_mod IN explicitly_packaged_modules)
```

If `is_cross_module`: call through public wrapper (boundary ABI).
If same-module: call `__impl` directly (internal ABI).

### What data the routing decision depends on

- `source_modules`: set of module IDs compiled from source in this build
- `explicitly_packaged_modules`: modules with explicit package assignments
- `module_packages`: module → package mapping
- `is_exported_entrypoint`: whether the callee is a public export
- `export_impl_map`: function → `__impl` symbol mapping

**All of this is mode-sensitive.** `source_modules` is different in source
mode (includes stdlib) vs PEX mode (excludes stdlib). This is the root of
every mode divergence bug.

---

## 2. Legitimate vs Accidental Complexity

### Legitimate (must keep)

| Concern | Why it's real |
|---------|---------------|
| ABI adaptation at package boundaries | Packages are compiled separately; the public ABI must be stable and uniform |
| Nothrow→throw conversion | A nothrow method exported through a package must still return Result at the boundary because the consumer doesn't know it's nothrow |
| `__impl` private body | Allows same-module callers to skip boundary overhead |
| Error propagation ABI | FnResult is the internal carrier; boundary Result is the public contract |

### Accidental (should be eliminated)

| Problem | Root cause |
|---------|-----------|
| `source_modules` changes routing | Whether stdlib is source or package changes which calls go through wrappers |
| `copy_status` / `has_drop` differ between paths | Wrappers add FnResult types whose structural analysis can diverge |
| Same-module calls to exported functions use different ABI than cross-module | The `__impl` optimization creates two semantic paths for the same function |
| Match-arm codegen differs between wrapper and direct returns | FnResult → Result conversion creates different scrutinee types |
| `_should_copy_value` answers depend on which ABI the call used | The wrapper path returns a different type than the direct path |
| `string_arc._type_needs_drop` diverges from `has_drop` | Wrapper-introduced types have different drop analysis results |

### The 0.27.137 bug was accidental complexity

The cert blocker was: `copy_status(RunningServer) = True` in PEX but False
in source. This happened because:
1. PEX path routes stdlib calls through wrappers
2. This changes the TypeId universe (different instantiation hashes)
3. The structural copy analysis couldn't find the Destructible proof for
   VirtualThread at the remapped TypeId
4. Match-arm codegen treated the payload as Copy → scrutinee drop →
   VirtualThread destroyed

The wrapper routing itself was correct. The bug was in a type-system query
that gave different answers depending on which TypeIds were present — which
depends on wrapper routing — which depends on mode.

---

## 3. Design Question: Pub Declaration-Time Normalization

### What could be fixed at declaration/interface time?

**The boundary ABI contract.** Today, whether a call goes through the
wrapper is decided at each call site during LLVM codegen. Instead:

1. Every `pub` function/method could have its boundary ABI recorded at
   declaration time: "this function's public signature is
   `Result<T, Error*>`"
2. Every caller of a `pub` function would use the same ABI regardless of
   whether it's same-module, cross-module, source, or package
3. The `__impl` optimization (skipping the wrapper for same-module calls)
   would be a codegen-only optimization that doesn't change the MIR-level
   semantics

### What would this buy us?

- **One semantic path.** The MIR for calling `rest.start()` would be
  identical in source and PEX builds. The match scrutinee would always be
  the same type. `copy_status` would always see the same TypeIds.
- **No `source_modules` routing.** The call target is determined at
  declaration time, not at codegen time based on which modules are source.
- **Mode-independent MIR.** Source, package, and PEX would produce
  identical MIR for the same Drift code (modulo TypeId allocation order).
- **The `__impl` optimization becomes invisible.** Same-module calls can
  still call `__impl` at LLVM level, but MIR always uses the boundary
  signature. The optimization is a codegen rewrite, not a semantic choice.

### What would it break?

- **Performance for same-module calls.** Currently, same-module calls to
  exported functions skip the FnResult wrapper overhead. With universal
  boundary ABI, every call to a pub function would pay the FnResult cost
  at MIR level, even if codegen optimizes it away.
- **FnResult types in MIR.** Today, same-module MIR uses the surface return
  type. With universal boundary ABI, MIR would use FnResult<T, Error>
  everywhere. This changes type analysis, `copy_status`, `has_drop`, etc.
- **Existing tests.** Many tests assume same-module calls return the
  surface type directly.

### What cases still require late wrapper synthesis/routing?

- **Generic instantiation.** When a generic function is instantiated at a
  call site, the instantiation may not have a pre-declared wrapper. The
  wrapper must be synthesized on demand.
- **Dynamic dispatch.** Interface method calls go through vtable thunks
  that may need ABI adaptation.
- **Callback/closure captures.** Lambda environment types are created
  during MIR lowering; their wrappers can't be pre-declared.

---

## 4. Target Architecture

### The ideal end state

```
                    Declaration time          Codegen time
                    ──────────────            ────────────
pub fn foo() -> T   boundary_sig recorded     __impl optimization
                    MIR uses boundary_sig     (transparent to MIR)
                    one TypeId universe
                    one copy_status answer
```

**Principle**: the pub boundary contract is a property of the **declaration**,
not a per-call-site routing decision. The MIR for calling a pub function is
always the same regardless of mode. LLVM codegen may optimize same-module
calls to skip the wrapper, but this is invisible to MIR, type analysis,
and ownership tracking.

### Concrete target

1. **`FnSignature` carries `boundary_ret_type`**: for pub can-throw
   functions, this is `FnResult<T, Error>`. For pub nothrow methods, this
   is `FnResult<T, Error>` (the wrapper ABI). For non-pub functions, this
   is None (no boundary).

2. **MIR call lowering always uses `boundary_ret_type` for cross-module pub
   calls.** The match scrutinee is always the same type regardless of mode.

3. **LLVM codegen peels the boundary**: for same-module calls to known
   nothrow functions, codegen can call `__impl` and construct the FnResult
   locally. This is a transparent optimization.

4. **`source_modules` is removed from routing decisions.** Routing is
   determined by: is the callee `pub`? If yes, use boundary ABI. Period.

5. **Wrappers are codegen artifacts only.** `__wrap_method::` and `__impl`
   exist only in LLVM IR, not in MIR. MIR never references wrapper symbols.

### What this eliminates

- `_resolve_call_target_symbol` routing logic
- `source_modules` / `explicitly_packaged_modules` in call routing
- Mode-dependent FnResult vs surface type for the same call
- Divergent TypeId universes for match scrutinees
- `copy_status` / `has_drop` differences between source and PEX paths

---

## 5. Phased Implementation Plan

### Phase A: Record `boundary_ret_type` on `FnSignature`

**Goal**: Every pub function/method knows its boundary return type at
declaration time. No behavior change yet — just metadata.

**Files**:
- `checker/__init__.py`: add `boundary_ret_type: TypeId | None` to
  `FnSignature`
- `driftc.py`: populate `boundary_ret_type` during
  `_inject_method_boundary_wrappers` and during pub function signature
  construction

**What's removed**: nothing yet.

**Regressions**: assert `boundary_ret_type` is populated for all pub
functions with wrappers. Cross-reference against existing wrapper sigs.

**Size**: Small. Additive.

---

### Phase B: MIR call lowering uses `boundary_ret_type` for cross-module calls

**Goal**: HIR-to-MIR and call lowering use `boundary_ret_type` instead of
the target's surface return type when calling a pub function from a
different module. The MIR return type is always the boundary type, regardless
of mode.

**Files**:
- `stage2/hir_to_mir.py`: call lowering uses `boundary_ret_type` when
  available and call is cross-module
- `driftc.py`: call info construction includes boundary type

**What's removed**: the mode-dependent return type divergence. Source and
PEX MIR for the same cross-module call will have the same return type.

**Regressions**:
- Source and PEX MIR for a pub function call produce the same scrutinee
  type
- `copy_status` returns the same answer for the scrutinee in both modes
- Match-arm lowering produces the same ownership decisions in both modes

**Key risk**: this is the largest behavioral change. Must validate with
drift-web and net.tls.

**Size**: Medium.

---

### Phase C: Eliminate `source_modules` from call routing

**Goal**: LLVM codegen routing no longer depends on `source_modules`. The
routing rule becomes: if callee is pub and call is cross-module (different
canonical package), use boundary ABI. No special-casing for source vs
package modules.

**Files**:
- `llvm_codegen.py`: simplify `_resolve_call_target_symbol` to use
  `boundary_ret_type` presence instead of `source_modules`

**What's removed**:
- `source_modules` set on TypeTable (or kept only for diagnostics)
- `explicitly_packaged_modules` in routing logic
- The `callee_mod not in source_modules` check

**Regressions**:
- Existing call routing tests updated for new logic
- Verify same-module calls still optimize to `__impl`
- Verify cross-module calls always use boundary ABI

**Size**: Small. Mostly deletion.

---

### Phase D: Make `__wrap_method::` a codegen artifact only

**Goal**: `__wrap_method::` wrapper MIR is no longer synthesized. The
wrapper only exists in LLVM IR, generated by the codegen wrapper emission
loop (llvm_codegen.py:478-575).

**Files**:
- `driftc.py`: remove wrapper MIR synthesis (lines 6961+)
- `driftc.py`: remove `_inject_method_boundary_wrappers` from MIR pipeline
  (keep for signature declaration only)
- `llvm_codegen.py`: wrapper emission handles nothrow→boundary conversion

**What's removed**:
- Wrapper MIR bodies
- Wrapper-specific ownership tracking (`forwarded_to_callee` in
  `param_drop_status`)
- The post-pass's wrapper handling

**Regressions**: all existing wrapper tests. This is a significant change.

**Size**: Medium. Requires careful validation.

---

### Phase E: Universal boundary ABI in MIR (optional, future)

**Goal**: ALL cross-module pub calls use boundary ABI at MIR level, not
just cross-package calls. Same-module calls within the same compilation
unit still call `__impl`, but MIR represents the call as boundary ABI.

This is the full "normalize at pub" vision. It eliminates the last source
of MIR divergence between same-module and cross-module paths.

**Key risk**: performance. Every pub call at MIR level would pay FnResult
overhead. The codegen optimization must reliably eliminate this for
same-module calls.

**Size**: Large. Long-term.

---

## 6. Specific Questions Answered

### Can pub declaration-time normalization replace some current wrapper routing?

Yes — phases A-C replace `source_modules`-based routing with
`boundary_ret_type`-based routing. The declaration carries the contract;
the call site doesn't need to figure it out.

### Which wrappers are truly ABI-mandated vs legacy?

**ABI-mandated:**
- `__impl` + public wrapper pair for exported functions (different calling
  conventions at the LLVM level)
- `__wrap_method::` for nothrow methods that must present a can-throw ABI
  at package boundaries

**Legacy/accidental:**
- `__wrap_method::` as MIR-level entities (should be codegen-only)
- `source_modules`-dependent routing (should be declaration-driven)
- Mode-conditional `is_cross_module` checks

### Can wrapper insertion be made mode-independent after ingress?

Yes — after Phase C. If routing depends only on `boundary_ret_type` (set at
declaration time) and canonical package membership (set at ingress time),
the routing is mode-independent.

### What invariants should hold between direct and wrapper paths?

1. **Same MIR return type.** A cross-module call to a pub function always
   produces the same MIR return type regardless of mode.
2. **Same `copy_status`.** The scrutinee type from a pub function call has
   the same Copy analysis in all modes.
3. **Same ownership decisions.** Match-arm lowering for the result of a pub
   function call produces the same `arm_scrut_payload_moved` flag.
4. **Same `has_drop`.** No mode-dependent has_drop divergence for types
   that appear in pub function signatures.

### What should become an assertion/error if violated?

- `copy_status` returning different answers for the same (name, module)
  type across compilation modes → error (partially done in 0.27.137)
- `has_drop` returning different answers → error (done in 0.27.136)
- MIR return type for a pub cross-module call differing between source and
  PEX → new assertion in call lowering

---

## 7. Non-Goals

- **No mode-conditional semantics.** Routing must not depend on source vs
  package vs PEX.
- **No package-name/std special cases.** `source_modules` containing or
  excluding stdlib must not change call routing behavior.
- **No keeping two semantically different call paths.** If both direct and
  wrapper paths exist, they must produce the same MIR-level semantics.
- **No wrappers whose behavior depends on invocation mode.** The
  `__wrap_method::` contract is the same whether the caller is source,
  package, or PEX.

---

## 8. Relationship to Prior Refactor Plans

This plan is the **third leg** of the anti-mode-divergence work:

| Plan | What it fixes | Status |
|------|---------------|--------|
| Semantic ingestion refactor | Type identity / declaration ordering | Proposed |
| Drop insertion refactor | Ownership / has_drop stability | Phases A-D partially done (0.27.135-0.27.137) |
| **Wrapper convergence** | **Call routing / ABI path divergence** | **This plan** |

All three share the principle: **mode affects inputs and packaging, not
compiler meaning.** The wrapper convergence plan is specifically about
ensuring that the *call path* to a pub function is the same regardless of
which modules are source-compiled.

The 0.27.137 fix (`copy_status` checking `destructor_fns`) is a
point-fix within this space. Phases A-C of this plan would eliminate the
class of bugs where `copy_status` / `has_drop` / ownership analysis gives
different answers because of mode-dependent TypeId universes created by
wrapper routing.

---

## Summary

| Phase | Goal | Removes | Size |
|-------|------|---------|------|
| **A** | `boundary_ret_type` on FnSignature | Nothing (additive) | Small |
| **B** | MIR uses boundary type for cross-module pub calls | Mode-dependent scrutinee types | Medium |
| **C** | Remove `source_modules` from call routing | Mode-conditional routing | Small |
| **D** | `__wrap_method::` becomes codegen-only | Wrapper MIR synthesis | Medium |
| **E** | Universal boundary ABI in MIR (future) | All same/cross-module ABI differences | Large |

Execution order: A → B → C → D. E is optional long-term.
A is the prerequisite. B is the payoff. C is the cleanup. D is the
simplification.
