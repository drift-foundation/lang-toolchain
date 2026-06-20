# Code review: compiler-temp / user-identifier name collision (CORE_BUG, 0.33.45)

**Status for review.** Implementation complete; unit suites green (406);
full driver suite in progress (readiness gate — see §7). No ABI change.
**Reporter:** DriftQuery, M3.3 fixture loader (`dqc.env.fold_decls`).
**Severity:** CORE_BUG — silent-miscompile-class (the visible failure is a
codegen *abort*, but the underlying defect is a mis-typed local that could in
principle mis-lower rather than abort).

---

## 1. Symptom

A program that typechecks aborts during LLVM lowering:

```
NotImplementedError: LLVM codegen v1: phi with mixed incoming types {'ptr', 'drift.int'}
  ... _lower_phi
```

(or a sibling corruption: `instruction forward referenced with type 'ptr'`).
DriftQuery hit it when `dqc.env.fold_decls` declared `var t1 = -1; var t2 = -1;`
inside its declaration-folding loop; every consumer of `dqc.env` then failed to
codegen. The reporter could not reduce it to a standalone file.

## 2. Root cause

`MirBuilder.new_temp()` (`lang/driftc/stage2/hir_to_mir.py`) minted
intermediate-value ids as bare `t<N>` — `t1`, `t2`, `t3`, … A user source
variable named `t1`/`t2`/`t68` has the **same base name** as a compiler temp.

`func.local_types` is keyed by base name. Both the user local and the same-named
temp write `local_types['t2']`. In `fold_decls` the temp `t2` was
`AssignSSA(e_1)` — the receiver copy of the `&mut Env` parameter passed to an
early call — so it carried type `&mut Env` (a pointer). That **clobbered** the
user `t2`'s `Int` type in `local_types`.

Because the user `t2` was declared *inside a loop*, SSA inserts an entry-default
`ZeroValue` for it whose type is read back from `local_types['t2']` — now the
clobbered `ptr` type. That `ptr` flows into the loop-header phi's entry edge
while the real `t2 = k` assignments supply `drift.int` on the back edge →
`phi {ptr, drift.int}` → abort.

Confirmed by instrumentation: `ZeroValue(dest='t2_1', ty=377)` where type 377 =
`RefMut`, versus the identically-declared `t1` at `ty=2` (`Int`); and
`ConstString(dest='t1') / AssignSSA(dest='t2', src='e_1')` showing temp `t2`
overlapping user `t2`.

`_lower_phi` is **not** the bug — its mixed-type guard correctly refuses to emit
malformed IR. The defect is the upstream `local_types` clobber.

## 3. Why the obvious fix (`__t<N>`) is wrong

The first instinct (and the initial patch) was to prefix temps with `__`, since
other internal names use it (`__borrow_tmp`, `__array_cap_grew`, `__recv`) and
the grammar *comment* calls `__` compiler-reserved.

**The grammar does not actually reserve `__`.** `NAME` in `grammar.lark`:

```
NAME.0: /(?!type\b)(?!pub\b)(?!use\b)[A-Za-z][A-Za-z0-9_]*/ | /_[A-Za-z0-9_]*(?<!__)/
```

The `(?<!__)` lookbehind rejects names *ending* in `__`, not *beginning* with
it. Empirically, `var __t1 = 5; var __t2 = 7;` compiles and runs today, and
stdlib exports `__test_*` hooks. So `__t<N>` would merely **move** the collision
to a user `__t<N>`. (We explicitly chose NOT to start banning `__*` source
identifiers: it is not reserved today, and stdlib relies on the form.)

## 4. The fix — a non-source-expressible namespace

A source identifier is drawn entirely from `[A-Za-z0-9_]`. A name containing any
character *outside* that set therefore cannot be produced by source, regardless
of keywords or lookbehind. We use a leading `.`.

Three coordinated changes (lowering + codegen; **no ABI/runtime change**):

1. **`hir_to_mir.py` `new_temp()` → `.t<N>`.** `.` is outside `NAME`, so no
   user variable/parameter/binder — present or future — can equal `.t<N>`, nor
   any larger compiler name that embeds it (several sites build addr-taken
   locals like `__replace_old_<new_temp()>`, `__throw_ctx_str_<new_temp()>`).
   `.` is a legal LLVM identifier character; the primary value path
   `_map_value` emits it unchanged as `%.t<N>`.

2. **`llvm_codegen.py` `_alloca_name_for_local`** — preserve every char legal in
   an unquoted LLVM identifier (`[A-Za-z0-9_$.]`) instead of collapsing
   non-alphanumerics to `_`. **Why required:** addr-taken temps route through
   this helper, and the old `[^alnum]→_` collapse would map a temp `.t5` onto
   `_t5`, re-introducing the collision in the alloca-pointer namespace against a
   user `var _t5` that is also addr-taken. **Why safe:** it is a *no-op for every
   source-originated name* (those are already `[A-Za-z0-9_]`); only the new
   `.`-marked temps are affected. Verified the only value-name-affecting
   sanitizer is this one — the primary `_map_value` path does not sanitize.

3. **`llvm_codegen.py` `_bb` → `.bb.<name>`** — the block-label helper carried
   the identical false `__bb_` "guarantees no collision" claim. Block labels and
   SSA value names share one LLVM namespace, so a user local `__bb_entry` could
   collide with a block label. Switched to the same non-source `.`-marker. This
   is a consistent prefix swap across all 8 `_bb` call sites; the phi-predecessor
   fixup constructs its search pattern via `_bb(...)` so it tracks the new prefix,
   and the replacement side uses independent sub-block names (untouched).

## 5. Known residual (documented, deferred — not in this change)

Codegen-internal SSA names minted by `_fresh` (`%{hint}{n}`, e.g.
`%arr_dup_done5`), **including the sub-block labels it generates**, remain in the
source-collidable `[A-Za-z0-9_]` space. This is the same class but:

- **Lower severity:** it requires a user to name a local *exactly* like a
  `_fresh` hint+counter (`arr_dup_done5`) — far less plausible than `t1`/`t2`.
- **Fails loudly:** a duplicate `%name` is an LLVM verifier error at compile
  time, not a silent miscompile.

Closing it means routing all `_fresh`/sub-block names through a non-source marker
— a broad codegen change (~100 call sites) with its own validation surface. We
deliberately did **not** bundle that into a CORE_BUG fix. Recommend a focused
follow-up. (`new_temp` was the one producing *silent* type corruption, so it is
the urgent half.)

## 6. Files changed

| File | Change |
|---|---|
| `lang/driftc/stage2/hir_to_mir.py` | `new_temp()` → `.t<N>` (+ rationale docstring) |
| `lang/codegen/llvm/llvm_codegen.py` | `_alloca_name_for_local` preserves `[A-Za-z0-9_$.]`; `_bb` → `.bb.<name>` |
| `lang/versions.py` | `DRIFTC_VERSION` 0.33.44 → 0.33.45 (ABI stays 17) |
| `history.md` | CORE_BUG entry |
| `lang/tests/stage2/test_mir_temp_name_reserved_namespace.py` | new — invariant test (3 cases) |
| `lang/tests/driver/test_temp_user_name_collision_codegen.py` | new — e2e repro (4 cases) |

## 7. Test coverage & validation

**Invariant (stage2 unit, grammar-independent):**
`test_mir_temp_name_reserved_namespace.py` — every `new_temp()` output contains a
char outside `[A-Za-z0-9_]` (sufficient condition for "not a source identifier");
is the chosen `.t<N>` form; and never equals `t1`/`t2`/`t68`/`t163`/`__t1`/
`__t2`/`_t1`/`_t5`.

**End-to-end (driver), parametrized over user-name shapes:**
`test_temp_user_name_collision_codegen.py` — the minimal standalone repro the
original report could not isolate (early `&mut`-param call → ptr temp on `t2`;
user `Int` sentinels declared in an outer loop, used as post-loop indices):
- `t1`/`t2` (original bug) → returns 80
- `__t1`/`__t2` (proves it is not a `__` fix) → returns 80
- `_t1`/`_t2` (exercises the alloca-name path) → returns 80
- `__bb_entry`/`__bb_if_then` (the `_bb` hardening) → returns 18

**Pre-fix confirmation:** reverting `new_temp` to bare `t<N>` reproduces the
codegen abort for the `t1`/`t2` form (and the DriftQuery `dqc.env` build, with
the M3.3 `ND_SIG` branch, reproduced `phi mixed {ptr, drift.int}` pre-fix and
lowers+links clean post-fix — validated on a throwaway copy, not in the
DriftQuery repo).

**Regression suites:**
- stage2 + stage3 + stage4 + codegen + checker units: **406 passed**.
- full driver suite (`lang/tests/driver`, ~1856 tests; the phi-heavy validation
  that exercises the `_bb` change): **in progress** — this is the gate before we
  call it ready for a staged toolchain.

## 8. Risk assessment

- **ABI:** none. Pure internal value-id strings and codegen name emission.
- **Blast radius of `new_temp`:** value-id string only; no MIR shape change. SSA
  versioning appends `_<v>` to the base, unaffected by the `.`.
- **Blast radius of `_alloca_name_for_local`:** provably a no-op for all existing
  names; only preserves the new marker.
- **Blast radius of `_bb`:** all label emission + phi-fixup go through `_bb`
  consistently; the driver suite is the regression check (phi-heavy programs).
- **Golden-name tests:** stage units that hand-build MIR use `t1`/`t2` as their
  own *inputs* (unaffected by `new_temp`'s *output*); 406 unit tests green
  confirms no golden-name breakage.

## 9. Readiness

Ready to cut a staged toolchain for DriftQuery to validate M3.3 **pending the
full driver suite finishing green** (§7). The unblock on the DriftQuery side is
on their tree (re-add the `ND_SIG`/`ND_FUNC` loader branches + re-enable M3.3
assertions against a ≥0.33.45 toolchain).
