# String ownership refactor — design-impact audit

**Commissioned:** 2026-07-05 (research-only, no implementation).
**Trigger:** `doc/refactor_triggers.md` → "Unify String/Arc ownership under one
central transfer-policy classification".
**Scope:** map the terrain so a future implementer / planning session can make an
informed scoping decision. This document does **not** recommend "do it" or
"don't"; it presents the tradeoffs completely and neutrally.

**Direction on record (2026-07-05):** ABI preservation is explicitly **not** a
goal ("zero effort to preserve it"). Where the honest answer is "this changes the
ABI," this doc says so plainly and sizes the change.

All file:line references are against the tree at audit time. Line numbers drift;
treat them as "look here," not as permanent coordinates.

---

## Executive summary (the shape of the finding)

`String` is a **compiler-privileged refcounted scalar**. In the type system it is
`TypeKind.SCALAR` with `name == "String"` (`types_core.py:2187-2191`,
`new_scalar("String")`), yet it simultaneously:

- is **Copy** (implicitly duplicable at value-use sites) — via a stdlib
  `implement Copy for String` proved through a trait query;
- **needs drop** (`has_drop(String) == True`) — via a hardcoded `name=="String"`
  branch, *not* the trait machinery;
- copies by **retain** (`drift_string_retain`) and drops by **release**
  (`drift_string_release`) — refcounted, not bitwise.

`Arc<T>` is the opposite construction: a plain **stdlib** type
(`struct Arc<T> { buf: RawBuffer<ArcBox<T>> }`, `arc.drift:105-107`) that is
**Destructible** (hence **not Copy**), whose clone/drop are ordinary Drift
functions (`_arc_clone_impl` / `_arc_destroy_impl`) reached through the generic
`destructor_fns` / `is_destructible` / ownership-ledger machinery.

The two meet at exactly **one** layer — the `DropPolicy.needs_drop` decision (the
*whether*-to-drop) — and diverge everywhere else (the *how*). Concretely:

- **Codegen is already unified.** `_emit_copy_value` and `_emit_drop_value`
  (`llvm_codegen.py:9244`, `:9653`) are single generic dispatchers: String takes
  a `drift_string_retain`/`release` branch, Arc takes a
  `destructor_fns`/destructor-call branch, structs/variants/arrays recurse. A
  struct with a `String` field already drops correctly by recursion.
- **MIR authoring is genuinely parallel.** Destructible/Arc drops are authored by
  the ledger pipeline (`ownership_ledger.py` → `drop_flags.py` →
  `match_cleanup_authoring.py` → `cleanup_authoring.py`, the sole `DropValue`
  emitter). String retain/release is authored by an entirely **separate**
  ~1740-line pass, `string_arc.py`, that runs *after* the ledger with its own
  liveness dataflow, and is **deliberately excluded** from the ledger consultation
  (long rationale at `string_arc.py:1503-1591`).
- **Type classification has a real two-authority split for String.**
  `copy_status(String)` returns `True` with the stdlib Copy-query installed but
  `False` via the structural fallback in an isolated `TypeTable`
  (`types_core.py:2670-2671` vs `:2741-2746`). This split propagates downstream to
  `DropPolicy.is_cheap_copy` → `_should_copy_value` → whether an implicit
  `CopyValue` is even emitted.

**Correction (per review):** the recurring bug class (a new lowering path
forgetting to mark/copy a non-bitcopy String field) is related to but NOT the
same mechanism as this classification split. The three historical instances
(`refactor_triggers.md:544-657`) were each a lowering path independently
omitting a `_ref_field_temps.add()`/`_copy_if_ref_alias()` call — a missing
CALL to the shared helper, not a wrong ANSWER from `copy_status()`. Making the
classification structural closes the "isolated vs. stdlib-loaded disagree"
surprise, but does not by itself force every lowering path through the shared
marking/copy helpers — that requires a second, distinct centralization step
(see §4.1's corrected row below). Both problems share a root cause (no single
authority `String`'s ownership facts flow through), which is why fixing the
classification is a real prerequisite, not wasted motion — just not
sufficient on its own.

---

# Section 1 — Current String semantics map

## 1.1 String is minted as a `TypeKind.SCALAR`

- `types_core.py:2187-2191` — `ensure_string()` → `new_scalar("String")`.
- `types_core.py:567-569` — `new_scalar(name)` → `_add(TypeKind.SCALAR, name, [])`.
  There is no dedicated interner; the `_string_type` memo on the table is the
  singleton registry (`types_core.py:341`).
- `types_core.py:1884-1885` — name→TypeId dispatch routes `"String"` to
  `ensure_string()`.

Two distinct "is this String?" idioms exist and are **not** interchangeable:

1. **Name-based**: `td.kind is TypeKind.SCALAR and td.name == "String"` — used
   pervasively in `types_core.py`, `type_checker.py`, `hir_to_mir.py`,
   `llvm_codegen.py`.
2. **TypeId-identity-based**: `tid == string_ty` (`string_arc.py:121-122`,
   `_is_string_tid`), `ty_id == self.string_type_id` (`llvm_codegen.py:8037`).
   Relies on the `ensure_string()` singleton.

## 1.2 Where String is treated as scalar/primitive/special-cased

**Type table (`types_core.py`)**
- `:1774-1777` — `has_drop`: SCALAR needs drop iff `name=="String"` (structural,
  mode-independent).
- `:2670-2671` — `_is_copy_structural`: SCALAR-String → **not Copy** (the isolated
  fallback; see §1.5).
- `:2856` — String grouped in a primitive set literal `{"Int","Uint","Bool",
  "Float","String"}`.
- `:2960-2962` — `is_bitcopy`: SCALAR-String → **False** (never bitwise-copyable).

**Type checker (`type_checker.py`)**
- `:1584` — `_COMPILER_KNOWN_COPY_SCALARS = {"String"}` (String is the sole
  compiler-known Copy scalar).
- `:1599` — a SCALAR is structurally Copy *unless* `name == "String"`.
- `:6240-6241` — `HLiteralString` typed as the interned `String` scalar.
- `:6242+` — f-string interpolation MVP hole-type set `{Bool,Int,Uint,Float,
  String}` (String grouped with primitives).
- `:2270, :10123-10307` — Error `attrs`/`captures` map keys hard-required to be
  `String` (`name != "String"` → diagnostic). String is the privileged map-key
  type.

**HIR→MIR (`hir_to_mir.py`)**
- `:701, :754, :849, :9654, :11390, :11407` — `SCALAR and name=="String"`
  branches; `:11390/:11407` group String **with `ARRAY`** for drop purposes.
- `:848-849` (`_copy_if_ref_alias`) — String special-cased *alongside*
  STRUCT/VARIANT as "requires a runtime clone-on-read-from-`&T`" → emits
  `CopyValue`. This is exactly the historical-bug hotspot (see §3.2).

**Codegen (`llvm_codegen.py`)**
- `:1861-1862` — captures the String `TypeId` when the SCALAR-String def is seen.
- `:7716, :7998, :8123, :8466, :8941, :9247, :9386, :9647, :9705, :9789` — a large
  set of `name=="String"` branches for copy/drop/field-extract/coercion.
- `:8568, :1073` — String enumerated alongside `Int`/`Void` in FnResult named-type
  generation and the label→LLVM-type map.

**Runtime ABI (`string_runtime.h/.c`)** — see §3.3.

## 1.3 Where String is treated as Copy

- **Ground truth (stdlib):** `stdlib/std/core/core.drift:588` —
  `implement copy_mod.Copy for String { }` (in the primitive-Copy block, lines
  570-627). The `Copy` trait itself is a stdlib marker trait, not a builtin:
  `stdlib/std/core/copy.drift:21-22` (`pub trait Copy { }`). Its docstring notes
  String is Copy "because its representation is an ARC handle; copying bumps the
  refcount, no byte-level duplication."
- **Sibling capabilities:** `shareable.drift:191` `implement Frozen for String`;
  `shareable.drift:224-228` `implement ConstShare for String { const_share(&self)
  -> String { return *self; } }`. There is **deliberately no** `implement Share
  for String` (`shareable.drift:139-144`: "String stays on `copy`, not `share`").
- **Cheap clone already exists:** `core.drift:725-727`
  `clone(&self) -> String { return *self; }` — "a cheap ARC refcount increment,
  not a byte-level copy." So a move-only model's escape hatch is already cheap; it
  is just not *required* today.
- **The query hook.** `driftc.py:568-624` `_install_copy_query` looks up the
  `Copy` trait key and installs `_query_copy(tid)`. For SCALAR it fast-paths
  `True` *except* String (`driftc.py:607`), and for String it calls the trait
  prover `prove_is(world, env, {}, subject, copy_key)` (`:613`) → `True` when the
  core.drift impl is present. Installed from `_build_linked_world`
  (`driftc.py:917`). Setter: `types_core.py:2509-2514`.
- **The checker's independent name-set.** `type_checker.py:1584`
  `_COMPILER_KNOWN_COPY_SCALARS = {"String"}` makes the checker treat String as
  Copy by name, independent of any loaded impl.

So "String is Copy" is asserted in **three** places that must stay consistent: the
stdlib impl (`core.drift:588`), the checker name-set (`type_checker.py:1584`), and
the prover path (`driftc.py:607/613`). The one that **disagrees** is
`_is_copy_structural` (`types_core.py:2670`, returns False), correct only because
the query path shadows it (§1.5).

## 1.4 Where String needs drop / retain / CopyValue

- **`has_drop` (canonical, `types_core.py:1714`/`_has_drop_inner:1738`):** order
  is `destructor_fns` (Arc) → `is_destructible` (Arc) → **SCALAR name=="String"**
  (`:1774-1777`) → ERROR/INTERFACE/container recursion. String reaches "needs
  drop" via the hard name test; Arc via `destructor_fns`/`is_destructible`.
- **`DropPolicy` (5 axes, `hir_to_mir.py:270-332`; standalone mirror
  `drop_policy_compute.py:27`):** `needs_drop`, `is_bitcopy`, `is_cheap_copy`,
  `is_destructible`, `has_structural_drop`.
  - `needs_drop = has_drop or is_destructible` → String and Arc both land here.
  - `is_cheap_copy = (copy_status is True) and (is_bitcopy or is_scalar_kind or
    not has_structural_drop)` — String qualifies (SCALAR, one retain); Arc does
    not (`copy_status` False). Documented as "refcounted SCALAR types (String —
    one retain)" (`drop_policy_compute.py:78-81`).
  - A prior bug (`test_drop_policy_copy_short_circuit_bug.py`) came from treating
    `copy_status=True` as "no drop needed" — wrong for String
    (`copy_status=True` **and** `has_drop=True`). This is the design's core
    tension made concrete: for String, Copy and needs-drop are **both true**.
- **`CopyValue` MIR op:** emitted for Copy-but-non-bitcopy values
  (`hir_to_mir.py:852, 1749, 1878, 3323, 3827, 4032, 6418`, etc.). Because Arc is
  not Copy, Arc values **never** flow through `CopyValue`. Lowering
  (`llvm_codegen.py:9247-9253`): SCALAR-String → `drift_string_retain`;
  ARRAY/STRUCT/VARIANT recurse (a String field inside a struct recurses into the
  String branch). There is **no** Arc branch in `_emit_copy_value` — Arc cannot
  appear.
- **`drift_string_retain`/`drift_string_release` emission** is String-TypeId /
  name gated everywhere: `llvm_codegen.py:9251, 9389` (retain, inside
  `name=="String"` branches), `:9707, 9791` (release, inside `name=="String"`
  branches); the `StringRetain`/`StringRelease` MIR ops are String-only by
  construction; `string_arc.py` inserts them driven by `_is_string_tid`
  (TypeId equality).

## 1.5 The isolated-vs-stdlib two-authority split (and where it propagates)

**Primary instance (Copy):** `copy_status(String)` (`types_core.py:2567`):
- With `_copy_query` installed → calls the query → prover proves the core.drift
  impl → **`True`** (`:2741-2746`).
- Without it (isolated `TypeTable`, no stdlib) → `_is_copy_structural(String)` →
  **`False`** (`:2670-2671`). A guard (`:2574-2579`) *raises* if stdlib trait
  metadata is present but no query is installed, which normally forces real
  compiles down the query path — but a bare isolated table (no `trait_worlds`)
  silently uses the `False` fallback.

**Second instance (found this pass): the split propagates into drop policy and
lowering.** Because `DropPolicy.is_cheap_copy` reads `copy_status`
(`drop_policy_compute.py:85-87`; same logic in `hir_to_mir.py:_drop_policy`), and
`_should_copy_value(ty)` returns `is_cheap_copy` (`hir_to_mir.py:776-785`), the
split flips whether an **implicit `CopyValue` is emitted for String at all**:
- stdlib-loaded: `copy_status=True` → `is_cheap_copy=True` → `val s2 = s1` emits
  `CopyValue` (retain), correct.
- isolated: `copy_status=None/False` → `is_cheap_copy=False` → String treated as
  move-only, no retain.

This is the "there may be others" the trigger anticipated: it is the *same* split,
but its blast radius reaches lowering, not just the classifier.

**Contrast — the Destructible authority (which governs Arc) has the *same shape*
split, in the other direction.** `is_destructible` (`types_core.py:2862`) depends
on an installed `_destructible_query`; without it, it raises if stdlib metadata is
present, else returns `False`. So **Arc's drop-ness is invisible in isolated
mode** — mitigated by `has_drop` checking `destructor_fns` as a *second authority*
(`types_core.py:1767-1770`) precisely to cover cross-package generic
instantiations (`Arc<T>::destroy`) the prover can't resolve.

Net: both String-Copy and Arc-Destructible have a query-vs-fallback split. The
difference is that **String's drop-ness is structural (name-based, available in
every mode) while its Copy-ness is query-dependent**, and **Arc's drop-ness is
query-dependent** with a `destructor_fns` backstop. String's needs-drop is the
*only* one of these four facts that is mode-independent today — which is exactly
what the refactor proposes to make true of String's Copy/retain classification
too.

## 1.6 String literals (static fast path)

- Typing: `HLiteralString` (`stage1/__init__.py:33`) → `record_expr(expr,
  self._string)` (`type_checker.py:6240-6241`).
- Lowering: `_visit_expr_HLiteralString` → `M.ConstString`
  (`hir_to_mir.py:2718-2721`); a cached empty-string const at `:433`. Many
  compiler-synthesized `ConstString` sites (JSON keys, event FQNs, panic
  messages).
- Codegen: `_lower_const_string` (`llvm_codegen.py:3983-4009`) emits a `private
  unnamed_addr constant` whose header carries the **static** marker so runtime
  retain/release are no-ops; `string_literal_cache` (`:691`) dedups identical
  literals. This fast path is **load-bearing** (see §4.4).

---

# Section 2 — UX dependency audit

**The one-sentence version:** Copy erases move/clone/borrow ceremony at ~120
`String` struct fields and ~300-360 by-value `String` params, plus every `val s2 =
s1` / `push(name)` / `out = out + x` reuse — none of which carry a keyword. The
`.clone()` population in the codebase (~240 sites) tracks **Arc**, not String; a
move-only String would migrate a large fraction of that ceremony onto String.

### 2.1 Implicit copies at value-use sites — **works today, would regress**

`val s2 = s1;` (String) works with no keyword: `copy_status(String)=True` →
`_require_copy_value` returns without error (`type_checker.py:3205-3207`), lowering
emits `CopyValue` → `drift_string_retain`.

Canonical fixture — `array_push_string_no_move/main.drift`:
```drift
val name = "hello";
arr.push(name);
arr.push(name);
arr.insert(1, name);
// name still valid after all operations (String is Copy)
arr.push(name);
```
Under an Arc-like move-only model this becomes (representative before→after):
```drift
val name = "hello";
arr.push(name.clone());
arr.push(name.clone());
arr.insert(1, name.clone());
arr.push(move name);          // last use may move
```
Other real reuse sites that would each need `.clone()`/`move`:
`string_concat_id_len/main.drift` (`id("a") + id("b")` where `fn id(s: String) ->
String { return s; }`), `std_regex_replace/main.drift:236`
(`expected_Xb = expected_Xb + "Xb";` — accumulator reused as its own operand),
`json.drift:2437` (`prev = best;` while `best` stays live).

### 2.2 Argument passing — Copy hides a real gap that Arc pays

- **By-value `String` params (~300-360 sites)** are declared freely because they
  impose no caller ceremony: `io.drift:584 file_builder(path: String)`,
  `env.drift:19 get(name: String)`, `json.drift:1511 set(..., key: String, value:
  JsonNode)`, `thread.drift:253 console_write(text: String)`. Call sites pass bare
  names/literals.
- **`&String` borrow params (~180-300 sites)** are used as an *optimization*, not
  a correctness requirement (`json.drift:382 parse(text: &String)`,
  `io.drift:1210 buffer_write_string(&mut Buffer, s: &String)`). Note
  `cli.drift:160 _string_eq_value(a: &String, b: String)` freely mixes borrow and
  by-value-Copy in one signature.
- **Arc by-value** *requires* ceremony at every handoff. From
  `lockfree_mpsc_queue_arc_clone_drop_orders/main.drift`:
  ```drift
  var root1 = root.clone();
  if not push_from_nested_clone(move root1, ...) { ... }
  var root2 = root.clone();
  if not push_from_clone(move root2, ...) { ... }
  ```
  Every Arc handoff is `.clone()` (bump) + `move` (transfer) + `.get()`/`.borrow()`
  (reach the `&T`). This is the ergonomic delta a move-only String would inherit.

**The gap Copy currently hides:** an author choosing `fn f(s: String)` for String
imposes nothing on callers; the identical choice for `Arc<T>` imposes `.clone()`
or `move` at every call. Copy makes by-value `String` params "free," which is why
they proliferate. Remove Copy and those ~300+ sites either sprout ceremony or must
be migrated to `&String`.

### 2.3 Field projection — the historical-bug area

String field read by value, struct still usable afterward, no clone:
`typed_catch_envelope_projection/main.drift:33` (`val msg: String = e.message;`
then `e.encode_compact()` still called), `std_json_canonical/main.drift:22`
(`return e.tag + "@" + e.path;` — two String fields in one expression).

Direct side-by-side contrast in `arc_struct_field_get_drop_leak/main.drift`:
```drift
struct Handle { stopped: conc.Arc<sync.AtomicBool>, value: Int }
return Handle(stopped = h.stopped.clone(), value = h.value);
```
The `Int` field is read bare; the sibling `Arc` field must be `.clone()`d. A
`String` field today reads like `value` (bare); under a move-only model it would
read like `stopped` (`.clone()`). This is precisely where the parallel-lowering
bugs live (`_copy_if_ref_alias` / `_ref_field_temps`, §3.2), because Copy lets a
field read "just work" via auto-CopyValue.

### 2.4 String literals — mostly survives

Literals are `ConstString` → static-flagged global; retain/release are runtime
no-ops. A reclassification of String's *ownership policy* does not require changing
the literal representation, **provided** the new drop/copy path still treats the
static flag as a no-op (it must, or every literal leaks/UAFs). The literal fast
path is orthogonal to Copy-ness and should survive (§4.4).

### 2.5 Concatenation — already "new allocation," Copy-independent

`drift_string_concat` allocates a fresh string and does **not** release its inputs
(`string_runtime.c:203`); the IR emits explicit releases. Concat is fundamentally
an allocating op regardless of Copy status. What Copy buys concat is only the
*operand-reuse* ergonomics (`out = out + x` where `out` is reused; §2.1), not any
concat-internal shortcut.

### 2.6 Formatting / diagnostics / JSON / error payloads — heavily Copy-dependent

The diagnostic/JSON pipeline's trait *signatures* are already move-friendly
(`&self` in, owned `String` out: `core.drift:145 Diagnostic.to_json_text`,
`log.drift:125 Debuggable`, `log.drift:168 Formatter`). The Copy reliance is
concentrated at **read sites** inside the implementations:

- `json.drift:2451-2464` `_encode_node` — `return *raw;`, `_encode_string(*v);`
  (copy borrowed variant payloads).
- `json.drift:2240 _encode_string(s: String)` takes String **by value**; every
  `*k`/`*key` caller copies a borrowed HashMap key (`:2386, :2404-2437`).
- `json.drift:2679-2708 _clone_deep_impl` — copies payloads and keys out of a
  borrowed node.
- `core.drift:819-831 ErrorParamsView { json_text: String }` with `implement Copy
  for ErrorParamsView` (`:823`) — a struct-with-String-field Copy impl that only
  compiles *because* String is Copy; `encode_compact(&self) { return
  self.json_text; }` copies the field out of `&self`.
- `log.drift:1150-1172 _payload_json_*` — `_json_escape(logger.name)` copies a
  String field out of a borrow into a by-value param.

Quantified: ~70 implicit deref-copy sites in `json.drift`, ~6 in `core.drift`,
plus the `_payload_json_*`/`_json_escape` family in `log.drift`. Only ~49 explicit
`.clone()` exist across the *entire* stdlib today. A move-only String would
multiply the clone count several-fold in these three files. Mechanical (clone is
cheap), but pervasive and verbose. The `Copy for ErrorParamsView` derive would
break outright and need rework.

Error payloads: every `pub error` carries by-value `String` fields
(`err.drift:35-66`: `IndexError.container_id`, `ResultError.diag_json`, etc.);
`ResultError` literally *is* a `String` JSON blob. The compiler-synthesized
`Diagnostic`/throw projections read those fields — Copy-dependent at the projection
sites.

### 2.7 Package / cross-module boundaries — no unique String dependency

`Arc<T>` fields already cross package boundaries today (Arc is a normal stdlib
generic serialized through the `.dmp`/interface-schema path). String crossing a
boundary rides the same `TypeKind.SCALAR` metadata as any scalar; there is no
String-specific serialization shortcut that Arc lacks. The package boundary is
**not** a String-Copy dependency — it is orthogonal. (The one place String is
privileged at the boundary is as the mandatory Error-map **key type**,
`type_checker.py:2270/10131/...`, which is a typing rule, not an ownership one.)

### 2.8 Bottom line — enumerated user-visible changes if String went move-only

1. `val s2 = s1; ...use s1...` → `val s2 = s1.clone();` (or `move` at last use).
2. `f(name); f(name);` (by-value param) → `f(name.clone()); f(move name);`.
3. `record.title` read into a by-value sink while `record` stays live →
   `record.title.clone()`.
4. `out = out + x` accumulator → `out = out.clone() + x` unless the checker proves
   `out`'s prior value is dead (NLL-style), which Drift's v1 borrow-checker does
   **not** do (per project memory on the v1 complexity line).
5. Every `implement Copy for <struct-with-String-field>` (e.g. `ErrorParamsView`)
   stops compiling.
6. ~300+ by-value `String` params either grow caller ceremony or migrate to
   `&String` (a signature-churn wave across stdlib + every consumer).
7. Diagnostic/JSON/log builders sprout `.clone()` at ~80 read sites.

Two spots already need a keyword even for Copy String, and would be unchanged:
`copy e.key` when reading a `&String` field by value
(`copy_ref_field_string_no_extra_load/main.drift:19`), and `move s` when capturing
String into a callback closure (capture context requires an explicit ownership
verb regardless of Copy).

---

# Section 3 — Compiler / runtime impact map

## 3.1 Code paths that would change

| Layer | File(s) | What changes |
|---|---|---|
| Type classification | `types_core.py` (`copy_status:2567`, `has_drop:1714`, `is_bitcopy:2935`, `is_destructible:2862`) | Make String's retain-copy+needs-drop **structural** (mode-independent). Introduce a central transfer-policy the classifiers consult instead of ad-hoc `name=="String"`. |
| Copy query install | `driftc.py:568-624` `_install_copy_query` | String no longer needs the prover to know it's Copy; either keep as-is or fold String into the structural default. |
| Type checker | `type_checker.py` (`_COMPILER_KNOWN_COPY_SCALARS:1584`, `_is_structurally_copy:1596`, `_require_copy_value:3205`) | Consume the central policy; the checker's independent name-set becomes redundant. |
| Drop policy | `hir_to_mir.py:_drop_policy:270-332`, `drop_policy_compute.py` | `is_cheap_copy` for String stops depending on the mode-split `copy_status`. |
| MIR lowering | `hir_to_mir.py` (`_copy_if_ref_alias:828`, `_should_copy_value:776`, all `name=="String"` branches) | Route String through the shared CopyValue/DropValue transfer path; retire per-path name checks. |
| **String ARC authoring** | `string_arc.py` (~1740 lines) | The big one. Its parallel liveness + retain/release insertion either (a) merges into the ledger/cleanup-authoring pipeline, or (b) stays but reads a central classification. See §3.4. |
| Ownership ledger / cleanup | `ownership_ledger.py`, `drop_flags.py`, `match_cleanup_authoring.py`, `cleanup_authoring.py` | If String folds in, these must handle String's retain-on-copy (currently they handle *drop* only; retain is string_arc's job). The explicit exclusion at `string_arc.py:1503-1591` documents *why this is hard* (late-rewrite authority vs ledger snapshot). |
| Codegen | `llvm_codegen.py` (`_emit_copy_value:9244`, `_emit_drop_value:9653`, `StringRetain/Release` at `:2755/2760`) | **Already unified** — minimal change. String and Arc already share these dispatchers. |
| Runtime ABI | `string_runtime.c/.h`, `atomic_runtime.c` | Only if the representation changes (§3.3). Classification-only unification needs **no** runtime change. |

## 3.2 The recurring bug class this maps onto

The "String ownership-authoring conformance matrix" trigger has fired repeatedly
because **parallel lowering paths independently re-derive "is this String
retain-copy?"** and one forgets. Concrete, still-open instances recorded in
`refactor_triggers.md:638-657` (projected-capture research §4e): three paths that
should apply the same `_ref_field_temps` aliasing mark —
`hir_to_mir.py:_copy_if_ref_alias:828-855`, the general deref path (~3395-3412),
the array-index field-projection fast path (~3535-3558), and
`_load_capture_from_env`'s REF branch (~1297-1313) — do not all agree. A single
central transfer-policy consumed by one shared read-aliasing helper is what would
collapse these.

## 3.3 Runtime representation — String vs Arc

**Same *pattern* (refcount-word prefix + payload in one heap alloc, atomic RC),
different *shape*.**

| | String | Arc\<T\> |
|---|---|---|
| Handle (by value) | `struct DriftString { drift_isize len; char *data; }` (`string_runtime.h:10-13`) — `data` points **at the payload** | `Arc<T> = { buf: RawBuffer<ArcBox<T>> }` = `{ptr, cap}` (`arc.drift:105-107`) — `ptr` points **at the header (offset 0)** |
| Refcount location | prefix header **behind** the data ptr: `data - 16` (`string_runtime.c:56-72`) | header **at** offset 0 of the pointee |
| Header | `{ _Atomic uint64_t refcount; uint64_t flags; }` (16B) | `{ AtomicInt strong; AtomicInt weak; Fn drop_thunk; }` (~24B, `arc.drift:78-82`) |
| Heap block | `[ refcount | flags | bytes… | NUL ]` | `[ strong | weak | drop_thunk | value ]` |
| Static/no-op | `flags & DRIFT_STRING_FLAG_STATIC` → retain/release skip | moved-from raw-null sentinel → drop no-op; **no** static-literal concept |
| Destroy | inline `free(hdr)` on `prev==1` | per-T `drop_thunk` runs `Destructible::destroy` then `mem.dealloc` |
| RC atomicity | atomic C11 (relaxed add / release sub + acquire fence) | atomic C11 via `drift_atomic_fetch_{add,sub}_int` |
| Provenance | hand-written C runtime (`drift_string_*`) | pure stdlib Drift over `RawBuffer`+`lang.atomic`; only the atomic RMW is a C call |

They are **not layout-identical**: String's handle points *past* a 16-byte
`{rc,flags}` header and carries an inline `len`; Arc's handle points *at* a
24-byte `{strong,weak,drop_thunk}` header with no inline length (payload type is
static). Unifying the *representation* (not just the classification) would mean
either reshaping String into an `ArcBox`-style block (gaining a `drop_thunk`/weak
slot it doesn't need, losing the payload-relative `data` pointer that
`drift_string_to_cstr` and slicing rely on) or generalizing `ArcBox` to carry an
inline length — a real runtime redesign, and the point where an ABI bump becomes
mandatory.

## 3.4 The parallel MIR-authoring pipelines (the core of the work)

**Destructible / Arc drop authoring** (order per `cleanup_authoring.py:1-30`):
```
build ledger
drop_flags PLANNING          (drop_flags.py)
rebuild ledger
match_cleanup_authoring      (site 2, match_cleanup_authoring.py)
rebuild ledger
cleanup_authoring            (site 1 — SOLE DropValue emitter, cleanup_authoring.py)
string_arc                   (String/Array retain/release — SEPARATE authority)
```

**String retain/release authoring** is `string_arc.py`: a self-contained pass with
its own use/def liveness (`:626-665`), definite-assignment (`:667-703`),
moved-out (`:705-737`), and owned/move-only tracking (`:739-791`), inserting
`StringRetain`/`StringRelease` and expanding `MoveOut`. It runs **after** the
ledger and is **deliberately not** folded into the ledger consultation. The
rationale (`string_arc.py:1548-1591`) is the crux for any unification effort:

> `string_arc` is a **late-rewrite** pass that synthesises retain/release *after*
> the ledger is built. For a returned String the handler retain-wraps the return
> value (caller gets a fresh +1; the function still owns the local's original +1
> and MUST release it). The ledger — built on pre-rewrite MIR — sees a plain
> `LoadLocal+Return` and transitions the local to `MOVED_OUT`, which is the
> **wrong** predicate for "skip the function-exit release." Arc, whose clone/destroy
> are MIR-first (visible to the ledger at build time), flows through the ledger
> correctly.

So the genuine difficulty is **authority timing**: String's refcount stakes are
created by a pass that runs after the ledger snapshot, so the ledger cannot see
them. Unifying means either (a) moving String retain/release *earlier* (into
HIR→MIR, visible to the ledger), or (b) rebuilding/extending the ledger after
string_arc. The architectural rule already stated in-code
(`string_arc.py:1580-1591`): "ledger authority is valid only for ownership effects
visible in the MIR snapshot used to build the ledger; any late pass that
creates/releases refcount stakes remains its own authority unless we rebuild the
ledger after it or move those effects earlier." That rule *is* the scoping
constraint for this refactor.

## 3.5 ABI-visible surface (honest answer: **yes, an ABI bump if representation
changes; no bump if classification-only**)

Two very different scopes hide inside "unify String/Arc":

**Scope A — classification-only** (make String's retain-copy+needs-drop structural;
route through shared `CopyValue`/`DropValue` where semantics coincide; keep the
`DriftString {len,data}` representation and the `drift_string_*` runtime). This is
**ABI-neutral**: no exported-helper signature changes, no struct-layout change, no
calling-convention change. Codegen already dispatches String and Arc through the
same `_emit_copy_value`/`_emit_drop_value`. This is the scope the trigger's
"Improvement" bullet literally describes.

**Scope B — representation unification** (make String an `Arc<[Byte]>`-shaped
handle, or otherwise reshape its heap block / handle to match `ArcBox`). This
**changes the ABI**, and the direction says take the bump. The ABI-visible surface
that would change:
- The **`DriftString` struct** (`string_runtime.h:10-13`) — its size/field layout
  is part of the calling convention (passed/returned **by value**; `%DriftString`
  in LLVM, `llvm_codegen.py:219`). Any reshape changes every function signature
  that takes/returns a String.
- The **by-value DriftString calling convention** itself — `string_runtime.h:40-62`
  documents two conventions (A: caller `retain; extern(s); release`, callee
  releases the extra stake; B: intrinsic receivers pass the stake direct). This is
  String-specific convention machinery (Arc has none). A representation change
  reopens all of it and the `DRIFT_OWNED_STRING` cleanup-attribute audit
  (`test_drift_owned_string_audit.py`).
- The **exported runtime helpers** `drift_string_literal/from_cstr/from_utf8_bytes/
  from_int64/from_uint64/from_f64/from_bool/concat/eq/retain/release/cmp/free/
  to_cstr` (`string_runtime.h:15-38`) — signatures take/return `DriftString` by
  value; a reshape changes all of them.
- The **static-literal global layout** emitted by `_lower_const_string`
  (`llvm_codegen.py:3983-4009`) — the `{i64,i64,...}` header shape is baked into
  emitted objects; changing it invalidates every compiled artifact (hence the
  bump).
- `DRIFT_RT_ABI_VERSION` (`lang/versions.py`, stamped into
  `libdrift_rt_abi<N>.a` and `abi_version_stamp.c`) — increments; all bundles
  rebuild through cert.

**Sizing guidance:** the honest recommendation to the future planner is to decide
**A vs B first**, because they are different projects. A is a classification /
authoring-consolidation refactor (compiler-internal, ABI-neutral — see the
corrected framing in §4.1: classification fixes the policy split, and is a
real prerequisite for closing the recurring bug class, but only the
centralization half of A actually closes it). B is a runtime-representation
redesign (ABI bump, cert rebuild, downstream recompile). The trigger's own
"Scope when triggered" text reads as Scope A ("central transfer-policy enum …
String's classification made structural … sharing one `CopyValue`/`DropValue`
path"), with the ABI-bump caveat attached defensively in case the *right*
answer turns out to require B.

**DECISION (2026-07-05, recorded here for the implementer):** the next
compiler refactor targets **Scope A only** — make String's retain-copy +
needs-drop classification structural and mode-independent (closes the
isolated-vs-stdlib-loaded policy split), THEN centralize the MIR helper paths
that mark borrowed String/non-bitcopy aliases (`_ref_field_temps`) and apply
`CopyValue` at ownership-transfer boundaries (`_copy_if_ref_alias`) — both
steps required, per the §4.1 correction, to actually close the recurring bug
class. Keep String Copy. Do **not** reshape the runtime representation
(Scope B) as part of this work.

Scope B (`DriftString` toward an `ArcBox`-style representation) is explicitly
a **separate, later project**. ABI preservation is not a constraint for
Scope B when/if it's taken up, but its UX/source blast radius (§4.2 — every
by-value String param, every place Copy-ness is currently leaned on) is still
a real constraint on THAT project's scope; representation work must not be
bundled into either the projected-capture follow-up
(`work/callback-env-uaf-ref-args/research-copy-projected-captures.md`) or
this classification/centralization cleanup. Keep the three tracks separate
even though they share root cause.

## 3.6 Test blast radius (rough)

Ripgrep over `*.drift` (`stdlib` + `lang/tests` + `examples`) and `*.py` tests:

| Signal | Approx count |
|---|---|
| `Arc<` usages | ~48-50 files (~220 sites) |
| `.clone()` calls (tracks Arc, not String) | ~240 (69 files) |
| `move <ident>` (all types) | ~360 (stdlib) / ~920 (with tests) |
| `: String` fields/params | ~230 (stdlib) / ~350+ (with tests) |
| by-value `String` params | ~70 (stdlib) / ~356 (with tests) |
| `&String` borrow params | ~180 (stdlib) / ~300 (with tests) |
| Python tests touching string move/copy/`drift_string` | ~45 |

Scope A churns primarily the ~45 Python compiler tests plus the ownership/leak
memcheck suite (`string_arc`, cleanup-authoring, the conformance-matrix fixtures).
Scope B additionally churns every `.drift` fixture that constructs/passes String by
value and the entire `drift_string_*` runtime + ABI-audit test — and forces a
downstream recompile of every consumer package.

---

# Section 4 — Risk matrix

## 4.1 What gets simpler

| Item | Detail |
|---|---|
| The recurring double-free/leak bug class | **Correction (per review): classification-only does NOT by itself close this bug class.** One central "String is retain-copy+needs-drop" authority fixes the POLICY split (no more isolated-vs-stdlib-loaded disagreement about the classification) — but the actual missed-retain/missed-alias-marking bugs (the `_ref_field_temps`/`_copy_if_ref_alias` gaps, the `_visit_expr_HField` fast-path omissions) are only closed once a SECOND, distinct step forces every lowering path to go through shared alias/transfer helpers, rather than each path re-implementing its own marking. Classification is a prerequisite for that centralization, not a substitute for it. See `refactor_triggers.md:544-657` (two fired instances + the three-path projected-capture finding) — none of those three were caused by the classification split itself; all three were a lowering path independently forgetting to call the marking/copy helper. |
| The `copy_status` mode-split for String | Made structural → `is_cheap_copy`/`_should_copy_value`/`_copy_if_ref_alias` stop flipping between isolated and stdlib-loaded compiles. Removes a class of "works in the real compile, wrong in the unit-test table" surprises. |
| Classifier redundancy | Three independent "String is Copy" assertions (`core.drift:588`, `type_checker.py:1584`, `driftc.py:607/613`) collapse toward one. |
| `string_arc.py` (potentially) | If String folds into the ledger/cleanup pipeline (Scope A's harder variant), ~1740 lines of parallel liveness+authoring shrink or merge into `cleanup_authoring.py`. **Caveat:** the authority-timing problem (§3.4) means this is the *hardest* part, not free. |
| Future refcounted handles | A central transfer-policy category means the next `String`-like type (a `Rope`, an interned symbol, a GPU handle) fits an existing lane instead of getting its own bespoke special-casing. |

## 4.2 What gets stricter / less ergonomic (only if String loses Copy — Scope B or a
Copy-removing variant of A)

| Regression | User-felt cost |
|---|---|
| `val s2 = s1` reuse | needs `.clone()` or `move` (§2.1). |
| by-value `String` params | callers add `.clone()`/`move`, or APIs migrate to `&String` (§2.2). |
| String struct-field reads | `record.title` → `record.title.clone()` (§2.3). |
| `out = out + x` accumulators | need `out.clone()` unless NLL proves the old value dead — Drift v1 does not (§2.8). |
| `implement Copy for <struct with String field>` | stops compiling (`ErrorParamsView`, §2.6). |
| Diagnostic/JSON/log builders | ~80 `.clone()` insertions across `json.drift`/`core.drift`/`log.drift`. |

**Important:** the trigger's *stated* improvement (make classification structural,
share the CopyValue/DropValue path) does **not** require removing Copy — it can
keep String Copy while unifying the *mechanism*. The §4.2 regressions only
materialize if the refactor also decides String should become move-only like Arc.
Whether to do that is a separate, larger language-design question the trigger does
not mandate.

## 4.3 What might break downstream packages

- **Scope A (classification-only):** source-compatible. No user syntax changes. Risk
  is compiler-internal correctness (ownership authoring), caught by the leak/UAF
  memcheck suite and the conformance matrix — not by downstream source.
- **Scope B (representation) or Copy-removal:** **source-breaking** for every
  consumer that reuses a String, passes String by value, reads a String field into
  a live sink, or derives Copy on a String-bearing struct. Given
  drift-mariadb-client / drift-net-tls / drift-web all build error/diagnostic
  payloads and pass connection/query strings around, they would need a `.clone()`/
  `move`/`&String` migration wave. This is the "several-fold clone multiplication"
  the diagnostics audit quantified, applied to third-party code.
- **ABI bump (Scope B):** every certified bundle rebuilds through cert per the ABI
  policy; same-ABI candidates cannot be tested against the old bundle because the
  layout changed. This is the expensive, but explicitly-sanctioned, path.

## 4.4 What should remain intentionally special (load-bearing, not a defect)

| Keep | Why |
|---|---|
| **Static-literal fast path** | `_lower_const_string`'s static-flagged globals + the `DRIFT_STRING_FLAG_STATIC` retain/release no-op (`string_runtime.c:233-272`) make `"foo"` allocation-free and zero-refcount-traffic. Any unified drop/copy path MUST preserve the static-flag no-op or every literal leaks/UAFs. This is independent of Copy-ness and should survive verbatim. |
| **Non-atomic-vs-atomic is already aligned** | Both String and Arc use atomic C11 RC; no change needed. |
| **String as the mandatory Error-map key type** | `type_checker.py:2270/10131/...` — a typing rule orthogonal to ownership; leave it. |
| **`String` staying Copy** (arguably) | The entire diagnostic/JSON/log/error-payload ergonomics (§2.6) and ~300+ by-value-param APIs rest on it. The trigger's improvement can be achieved *while keeping Copy* (unify the mechanism, not the Copy bit). Removing Copy is a much bigger, separately-justified decision. |
| **The `_copy_if_ref_alias` CopyValue-on-ref-read behavior** | The behavior is correct; only its *scattered re-derivation* across parallel paths is the defect. Centralize the classification, keep the behavior. |
| **`String`'s payload-relative `data` pointer** | `drift_string_to_cstr`, slicing, and C-interop rely on `data` pointing at the bytes (not at a header). If Scope B reshapes toward `ArcBox`, this interop contract must be preserved or re-provided. |

---

## Appendix — key coordinates for the implementer

- Two-authority Copy split: `types_core.py:2670-2671` (fallback False) vs
  `:2741-2746` (query True); guard `:2574-2579`.
- Structural drop for String: `types_core.py:1774-1777` (`has_drop`).
- Drop policy 5-axis: `hir_to_mir.py:270-332`; standalone mirror
  `drop_policy_compute.py:27` (`is_cheap_copy` reads `copy_status`, `:85-87`).
- Copy query install: `driftc.py:568-624`; String Copy impl `core.drift:588`;
  Copy trait `copy.drift:21-22`.
- Shared codegen dispatch: `_emit_copy_value` `llvm_codegen.py:9244`;
  `_emit_drop_value` `:9653` (String vs `destructor_fns`/Arc branches).
- Parallel String authoring: `string_arc.py` (whole file); authority-timing
  rationale `:1548-1591`.
- Ledger/cleanup pipeline: `cleanup_authoring.py:1-30` (pass order),
  `ownership_ledger.py`, `drop_flags.py`, `match_cleanup_authoring.py`.
- Runtime: `string_runtime.h:10-13` (`DriftString`), `:40-62` (by-value ABI
  conventions), `string_runtime.c:56-72` (header), `:233-272` (retain/release +
  static no-op); Arc `arc.drift:78-107` (`ArcHeader`/`ArcBox`/`Arc`),
  `:225-275` (clone/destroy), `atomic_runtime.c:72-74` (atomic RMW).
- Historical bug class: `doc/refactor_triggers.md:544-657` (conformance matrix +
  projected-capture three-path finding).
</content>
</invoke>
