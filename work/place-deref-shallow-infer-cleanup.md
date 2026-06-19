# Explicit `HPlaceDeref` shallow-inference cleanup — design & research

**Status:** IMPLEMENTED (checker-only, ABI unchanged). Restructure landed in
`Checker._TypingContext._infer_expr_type`; regressions added (see §7). Original
research below kept for context.
**Author:** research follow-up to the 0.33.43 borrowed-array-child fix.
**Scope:** `lang/driftc/checker/__init__.py` — `Checker._TypingContext._infer_expr_type`, the `HPlaceExpr` branch (~line 1915).
**Related:** the 0.33.43 LANGUAGE_BUG fix (`HPlaceIndex` arm) in `history.md`.

---

## 1. Background

The 0.33.43 fix added an `HPlaceIndex` arm to the shallow place walker so that
`&arr[i]`-style borrowed array children type correctly instead of degrading to
`Unknown` (which then mis-fired the string-operand diagnostic on reuse).

A reviewer correctly flagged that a *naive* `HPlaceDeref` arm would be wrong: the
walker's loop does an **unconditional leading-REF unwrap before dispatching on
every projection**, so an explicit deref arm would double-peel `&&T` (→ `T`
instead of `&T`) and reject `*p` for `p: &T` (the ref is already peeled by the
time the deref arm runs). We therefore left `HPlaceDeref` bailing to `None`
(its pre-existing behavior) and filed this follow-up.

```python
# current loop, simplified (checker/__init__.py ~1920)
for proj in expr.projections:
    cur_def = self.table.get(cur_ty)
    if cur_def.kind is TypeKind.REF and cur_def.param_types:   # <-- unconditional, every proj
        cur_ty = cur_def.param_types[0]
        cur_def = self.table.get(cur_ty)
    if   isinstance(proj, H.HPlaceField): ...
    elif isinstance(proj, H.HPlaceIndex): ...   # 0.33.43 fix
    else: return None                            # HPlaceDeref lands here today
```

---

## 2. Key finding — two place conventions, by layer (do not conflate)

There are **two** place representations in the compiler with **different deref
conventions**. Mixing them up is what makes the deref arm look harder than it is.

| Layer | Representation | Built by | Deref convention |
|---|---|---|---|
| **HIR** | `HPlaceExpr` / `HPlaceField` / `HPlaceIndex` / `HPlaceDeref` | `stage1/place_expr.py` (+ `stage1/borrow_materialize.py`) — **purely structural, no type info** | `HField→[Field]`, `HIndex→[Index]`; `HPlaceDeref` comes only from explicit unary-deref forms (`*`, and `->` normalized to `*`). Field/index carry an **implicit** one-level auto-deref of a ref base. |
| **MIR** | `Place` / `DerefProj` / `FieldProj` / `IndexProj` (distinct classes) | HIR→MIR lowering; typed by `borrow_checker_pass.py:_type_of_place` | **No** implicit auto-deref; every ref hop is an explicit deref. |

Evidence:

- `stage1/place_expr.py:58-84` maps `HField`/`HIndex`/`HUnary(DEREF)` to
  projections **structurally**, with no type information available. So
  `p.field` where `p: &T` becomes place `[Field]` (no deref); an `HPlaceDeref`
  is produced only when an explicit unary deref (`HUnary(DEREF)`) is present in
  the place chain. `stage1/borrow_materialize.py` likewise preserves/emits
  `HPlaceDeref` when rewriting an `HUnary(DEREF)` place chain (e.g. lifting an
  rvalue base to a temp) — same origin, different rewrite path. Neither inserts a
  deref for an implicit ref hop.
- `parser/__init__.py:558` normalizes `p->field` to `(*p).field` (an
  `HUnary(DEREF)`), so `->` and `*` both reduce to the same explicit unary-deref
  form before either rewriter sees them.
- The shallow value-typing of `HUnary(DEREF)` already peels exactly one ref level
  (`checker/__init__.py:2254-2260`).

**Consequence:** the shallow stub walks the **HIR** `HPlaceExpr`, whose field/index
projections legitimately need the implicit one-level unwrap. The leading unwrap is
*correct for field/index* and *wrong only for explicit deref*. The MIR-level
`_type_of_place` is the wrong model to mirror here — it's a different layer with a
different convention.

---

## 3. Proposed clean design

Remove the unconditional leading unwrap. Apply a **single-level** implicit unwrap
*inside the Field and Index arms only*; give `HPlaceDeref` an **exact one-level
peel** with no pre-unwrap.

```python
if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
    base_ty = self._infer_expr_type(expr.base) if expr.base is not None else None
    if base_ty is None:
        return None
    cur_ty = base_ty

    def _peel_ref_once(t):
        d = self.table.get(t)
        return d.param_types[0] if d.kind is TypeKind.REF and d.param_types else t

    for proj in expr.projections:
        if isinstance(proj, H.HPlaceField):
            host_ty = _peel_ref_once(cur_ty)            # implicit auto-deref, 1 level
            host_def = self.table.get(host_ty)
            if host_def.kind is TypeKind.STRUCT:
                info = self.table.struct_field(host_ty, proj.name)
                if info is None:
                    return None
                _, cur_ty = info
            elif proj.name in ("len", "cap", "capacity"):
                return checker._len_cap_result_type(host_ty)
            else:
                return None
        elif isinstance(proj, H.HPlaceIndex):
            host_ty = _peel_ref_once(cur_ty)            # implicit auto-deref, 1 level
            host_def = self.table.get(host_ty)
            if host_def.kind is TypeKind.ARRAY and host_def.param_types:
                cur_ty = host_def.param_types[0]
            else:
                return None
        elif isinstance(proj, H.HPlaceDeref):
            d = self.table.get(cur_ty)                  # EXACT one-level peel, no pre-unwrap
            if d.kind is TypeKind.REF and d.param_types:
                cur_ty = d.param_types[0]
            else:
                return None
        else:
            return None
    return cur_ty
```

Notes:

- **Single-level** (not a `while`) matches the language's single-level auto-deref:
  `b.field` where `b: &&Struct` is not valid Drift (it needs an explicit `(*b)`),
  so we never need to peel two levels implicitly.
- `TypeKind.REF` covers **both** `&T` and `&mut T` — mutability is a `ref_mut`
  bool on `TypeDef`, there is no separate `REF_MUT` kind — so **`&mut` needs no
  special case**.
- The place arms return the field/element **type only**; like the 0.33.43
  `HPlaceIndex` arm they must **not** emit a Copy diagnostic (place projection is a
  borrow, not a copy).

---

## 4. Acceptance / failing cases

Place context = a normalized `HPlaceExpr` (borrow/assign/move target) that gets
shallow-typed in value position.

| Place | Base type | Current | Proposed |
|---|---|---|---|
| `*p`  `[Deref]` | `p: &T`  | `None` (bail) | `T` |
| `*p`  `[Deref]` | `p: &&T` | `None` | `&T` (exact, no double-peel) |
| `(*p).field`  `[Deref, Field]` | `p: &T`  | `None` | field type |
| `(*pp).field` `[Deref, Field]` | `pp: &&T` | `None` | Deref `&&T→&T`, Field auto-deref `&T→T` → field type |
| `(*p)[i]` `[Deref, Index]` | `p: &Array<T>` | `None` | `T` |
| `&mut` variants of all above | — | `None` | identical to `&` (REF kind) |
| **non-Copy `T` payload** (any of the above) | — | — | returns the type; **no Copy diagnostic** emitted (no false accept — typing ≠ acceptance) |

These would become driver regressions (compile + run + leak-clean) modeled on
`lang/tests/driver/test_borrowed_local_field_string_copy.py`.

---

## 5. User-visibility — narrow, non-blocking

Verified by compile (`/tmp/deref_read.drift`, exit 0): explicit-deref **value
reads** are already correct today, including reuse —

```drift
val name = p->text + "";   val again = name + "";    // OK
val star = (*p).text + ""; val star2 = star + "";    // OK
```

— because a value read `p->text` / `(*p).text` is `HField(HUnary(DEREF, p), text)`
and is typed by the `HUnary(DEREF)` + `HField` path (exact one-level peel), **not**
the `HPlaceExpr` branch.

The remaining gap only bites when an explicit `HPlaceDeref` appears inside a
**normalized `HPlaceExpr` place target** (borrow/assign/move) that is shallow-typed
in value position and whose `Unknown` result propagates into a strict op (e.g. a
string binop). That shape is awkward to even construct naturally (it generally
wants a `&&T`), so impact is low. **No current program is blocked.**

---

## 6. Recommendation: small checker cleanup, NOT a refactor trigger

- ~25 LOC in one function, no contract/ABI/runtime change, mirrors existing intent.
  Land it opportunistically (next checker-area slice) with the §4 regressions.
- Do **not** file a broad "unify all place walkers" refactor trigger: the
  HIR-implicit vs MIR-explicit deref conventions differ **legitimately** by layer,
  so forced cross-layer unification would be incorrect.
- If a *third HIR-level* place walker ever appears, the right mitigation is a
  shared **HIR-only** place-typing helper, not cross-layer consolidation.

---

## 7. Implementation outcome

Landed exactly as designed in §3: removed the unconditional leading-REF unwrap;
implicit single-level unwrap now lives inside the `HPlaceField`/`HPlaceIndex` arms
(`_peel_ref_once`); `HPlaceDeref` peels exactly one level with no pre-unwrap.
Checker-only — no MIR/lowering/runtime/ABI change.

**Correction to §5's impact estimate:** the user-visible gap turned out *easier*
to trigger than predicted — a single-ref **bare deref reborrow** `&(*p)`
(`p: &T`) already normalizes to place `[Deref]` and reproduces the cascade; it
does not require a `&&T`. Verified by emulating the pre-fix deref bail
(`return None`) and recompiling: `&(*p)` and `&(*p).children[0]` borrows both fail
with `E-AUTO-f6706407` at the second concat pre-fix, and compile+run clean
post-fix. (Plain value reads `p->text`/`(*p).text` were and remain fine — they
never enter the `HPlaceExpr` walker.) Still non-blocking — no shipped program is
known to use this shape — but the surface is a touch wider than first thought.

**Tests added:**
- `lang/tests/checker/test_place_deref_shallow_inference.py` (9) — pins inferred
  `TypeId`s directly via `_TypingContext.infer`: `*p`(&T)→T, `*pm`(&mut T)→T,
  `*pp`(&&T)→&T (exact one-level peel, the case the old leading-unwrap got wrong),
  `(*p).text`→String, `(*pp).text`→String, `(*p).children[0]`→Node, ref-array
  index, ref-struct field, and a non-Copy-payload case asserting **no diagnostic**
  (no Copy-check false accept/reject).
- `lang/tests/driver/test_borrowed_local_deref_field_string_copy.py` (3) —
  compile+run+leak-clean for `&(*p)`, `&(*p).children[0]`, and the `->` sugar
  `&p->children[0]`; the borrowed local's String field is reused in a second
  string op (the cascade that mis-fired pre-fix).

**Regression sweep:** checker+stage1+type_checker (287) and the
string/array/borrow/field/index/place/deref driver set all green; original
0.33.43 `test_borrowed_local_field_string_copy.py` still green.
