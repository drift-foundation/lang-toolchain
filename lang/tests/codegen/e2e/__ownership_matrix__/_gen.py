#!/usr/bin/env python3
"""
Ownership-transfer matrix generator.

Motivation
----------
The drift-net-tls v0.3.14 certification UAF (fixed in 0.27.192) revealed
a systematic coverage gap: we had regressions for specific incidents but
no matrix over the axes that actually matter for ownership-transfer
bugs.  Three axes drove the bug:

  * TRANSFER SITE — which MIR instruction transfers ownership
    (Array.push/insert/extend, ArrayLit, StructCtor, VariantCtor, …)
  * VALUE SHAPE  — what the source expression looks like at HIR
    (HVar local, HCall rvalue, projected HPlaceExpr)
  * TYPE + SOURCE FLAVOR — Copy vs move classification, and for
    refcount-ARC types (String), whether the buffer is static or heap.

Without a systematic matrix, a bug in any single point could — and did —
ship undetected.  This generator emits a compact table of fixtures that
exercises each point independently.

Design
------
The generator drives off three dictionaries at the top of this file:
SITES, SHAPES, TYPE_INFO.  Each SITES entry is a small code emitter:
given a shape and type_info, it returns the Drift body that stands up
the value, performs the transfer, and asserts post-transfer integrity.
Adding a new site is a single dictionary entry.

Each emitted fixture covers one (site, type[, source_flavor]) combo and
exercises all applicable shapes inside the fixture as separate fns.
This keeps fixture count bounded (~30–50) while still exercising every
shape × site × type point.

Regenerate with:

    PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/_gen.py

(or: `just ownership-matrix-gen`)

Output
------
For each (site, type[, flavor]) combo, creates:

    lang/tests/codegen/e2e/om_<site>_<type>[_<flavor>]/main.drift
    lang/tests/codegen/e2e/om_<site>_<type>[_<flavor>]/expected.json

Fixtures are picked up automatically by the standard e2e runner's
shallow scan of `lang/tests/codegen/e2e/*` (`__`-prefixed directories
like this generator's home are skipped).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Fixtures are emitted as sibling directories to the generator's folder
# so the standard e2e runner (which does a shallow scan of
# lang/tests/codegen/e2e/*) picks them up automatically.  The generator
# itself lives in __ownership_matrix__/ (prefix causes the runner to
# skip it as a fixture dir).
FIXTURES_ROOT = HERE.parent

# --------------------------------------------------------------------
# TYPES
# --------------------------------------------------------------------
# Each type descriptor provides the Drift snippets needed to:
#   - decl_ty(): the Drift type syntax (e.g. "String").
#   - build_heap_expr(flavor): expression that yields a HEAP-backed value
#     of this type (non-static where applicable).  The `flavor` argument
#     is only consulted for String; other types ignore it.
#   - assert_value(expr): Drift snippet that asserts the given expression
#     matches the expected heap value.  Returns drift code usable inside
#     an `if` with a per-scenario return code offset.
#   - replacement_expr(flavor): expression producing a DIFFERENT value of
#     the same type (used for HVar-reassignment tests to verify the
#     source local's refcount is truly independent from the destination).
#   - is_bitcopy: used to skip flavor expansion for pure-value types.

def _string_build_heap(flavor: str) -> str:
    if flavor == "static":
        return '"xAAAA"'   # literal — DRIFT_STRING_FLAG_STATIC, release no-op
    if flavor == "heap_concat":
        return '"x" + "AAAA"'   # drift_string_concat → heap buffer
    raise ValueError(f"unknown string flavor: {flavor!r}")


def _string_replacement(flavor: str) -> str:
    if flavor == "static":
        return '"yBBBBB"'
    if flavor == "heap_concat":
        return '"y" + "BBBBB"'
    raise ValueError(f"unknown string flavor: {flavor!r}")


# Note on String source flavors: the matrix covers `static` and
# `heap_concat` because those are the two user-observable buffer
# classes at the runtime level — static-flagged (release no-op) vs.
# heap-allocated (release decrements refcount).  A `utf8_bytes` flavor
# was considered but is redundant: the only stdlib path to build a
# String from raw bytes is the `@intrinsic string_from_utf8_bytes`
# which requires an `unsafe` block and a buffer construction
# boilerplate, and the runtime-side heap semantics are identical to
# `drift_string_concat`'s output.  The `.clone()` method on a static
# literal would not help either — drift_string_retain on a static-
# flagged buffer is a no-op, yielding the same behavior as `static`.


TYPE_INFO = {
    "string": {
        "decl_ty": "String",
        "build_heap": _string_build_heap,
        "replacement": _string_replacement,
        "assert_eq_heap": lambda expr: f'if ({expr}).byte_length() != 5 {{ return 1; }}',
        # `expr` evaluates to this type; project it down to an Int that
        # the test can compare.  `expected_int` is the value
        # `extract_int(build_heap(...))` should produce.  Used by the
        # fn_arg / return_value sites that need an Int result code.
        "extract_int": lambda expr: f"({expr}).byte_length()",
        "expected_int": 5,
        # `replacement()` yields a DIFFERENT value — used by
        # match_bind's `reassignment_before_match` scenario.
        # `replacement_expected_int` is `extract_int(replacement(...))`.
        "replacement_expected_int": 6,   # "yBBBBB".byte_length() == 6
        "flavors": ["static", "heap_concat"],
        "is_bitcopy": False,
        "needs_import_core": False,
    },
    # `diag_entry` retired in Slice 7a (0.31.62, 2026-05-05) along
    # with the DV/DiagnosticEntry public surface; the parameterization
    # remains illustrated in the comment block below for cross-reference
    # but no `om_*_diag_entry` test is generated.
    "int": {
        "decl_ty": "Int",
        "build_heap": lambda _f: "42",
        "replacement": lambda _f: "99",
        "assert_eq_heap": lambda expr: f'if ({expr}) != 42 {{ return 1; }}',
        "extract_int": lambda expr: f"({expr})",
        "expected_int": 42,
        "flavors": [None],
        "is_bitcopy": True,
        "needs_import_core": False,
    },
    # Non-Copy destructor-bearing type.  See TOKEN_PREAMBLE and
    # TOKEN_SITES below for the per-site scenario bodies.  The Copy-
    # type scaffolding fields (build_heap, extract_int, …) are unused
    # for Token; the generator takes a separate dispatch path keyed on
    # `ty_name == "token"`.
    "token": {
        "decl_ty": "Token",
        "flavors": [None],
        "is_bitcopy": False,
        "needs_import_core": True,
        "is_non_copy_destructor": True,
    },
}

# --------------------------------------------------------------------
# SHAPES
# --------------------------------------------------------------------
# Each shape is a function (ty_info, flavor) -> (decls, access_expr).
#   - decls: Drift statements that set up the source expression in a
#            local.  May be empty for rvalue shapes.
#   - access_expr: Drift expression that represents V (the value to
#            transfer) in the test body.
#
# HVar local:      set up `val src = <heap>`; V = `src`
# HCall rvalue:    no decl; V = `build_heap()`
# Projection:      set up `val holder = Holder(field = <heap>)`;
#                  V = `holder.field`

def shape_hvar_local(ty_info: dict, flavor: str | None) -> tuple[str, str]:
    heap = ty_info["build_heap"](flavor)
    decls = f"\tval src: {ty_info['decl_ty']} = {heap};\n"
    return decls, "src"


def shape_hcall_rvalue(ty_info: dict, flavor: str | None) -> tuple[str, str]:
    return "", ty_info["build_heap"](flavor)


def shape_projection(ty_info: dict, flavor: str | None) -> tuple[str, str]:
    heap = ty_info["build_heap"](flavor)
    decl_ty = ty_info["decl_ty"]
    # Drift ctor syntax for generic structs: `Holder(field = ...)`, with
    # the type parameter inferred from the annotation on the binding.
    # Explicit `Holder<T>(...)` on the RHS is rejected by the parser.
    decls = (
        f"\tval holder: Holder<{decl_ty}> = Holder(field = {heap});\n"
    )
    return decls, "holder.field"


SHAPES = {
    "hvar_local": shape_hvar_local,
    "hcall_rvalue": shape_hcall_rvalue,
    "projection": shape_projection,
}

# --------------------------------------------------------------------
# SITES
# --------------------------------------------------------------------
# Each site is a function (shape_name, ty_info, flavor) -> drift body
# for a scenario function.  Must start with a nothrow scenario returning
# Int (0 on success, non-zero scenario-specific code on failure).
#
# Site emitters return the BODY of the scenario fn.  The driver wraps
# them in `fn scenario_<site>_<shape>() nothrow -> Int { ... }`.


def site_array_push(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    assert_snippet = ty_info["assert_eq_heap"](f"arr[0]")
    return f"""
	var arr: Array<{decl_ty}> = [];
{decls}	arr.push({access});
	if arr.len != 1 {{ return 1; }}
	{assert_snippet}
"""


def site_array_insert(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    assert_snippet = ty_info["assert_eq_heap"](f"arr[0]")
    return f"""
	var arr: Array<{decl_ty}> = [];
{decls}	arr.insert(0, {access});
	if arr.len != 1 {{ return 1; }}
	{assert_snippet}
"""


def site_array_set(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    # `arr.set(index, value)` per the reconciled public API contract.
    # Seed the array with one slot via push of a fresh (rvalue) value
    # so set(0, ...) has a valid index to overwrite.  The shape axis
    # then varies how the OVERWRITE value is supplied.
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    seed = ty_info["build_heap"](flavor)
    assert_snippet = ty_info["assert_eq_heap"](f"arr[0]")
    return f"""
	var arr: Array<{decl_ty}> = [];
	arr.push({seed});
{decls}	arr.set(0, {access});
	if arr.len != 1 {{ return 1; }}
	{assert_snippet}
"""


def site_array_extend(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    # NOTE on shape axis:
    # `extend(&src)` always takes a borrow of an Array<T> as its
    # transfer site; the value shapes (HVar/HCall/projection) here do
    # not vary the shape AT THE EXTEND CALL — they only vary how the
    # source array's single element gets populated upstream (via push).
    # Truly varying extend's source-shape (e.g. `dest.extend(&local)`
    # vs `dest.extend(&holder.array_field)` vs `dest.extend(&fn_call())`)
    # is a follow-up — not all of those forms are even legal Drift
    # surface today.  For now the axis is APPROXIMATE for extend: the
    # three sub-fns differ in their setup phase only.  Documenting
    # this honestly so the matrix accounting is not overstated.
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    assert_snippet = ty_info["assert_eq_heap"](f"dest[0]")
    return f"""
	// Setup phase varies by shape; transfer site is always
	// `dest.extend(&src_arr)` with src_arr a borrowed local array.
	var src_arr: Array<{decl_ty}> = [];
{decls}	src_arr.push({access});
	var dest: Array<{decl_ty}> = [];
	dest.extend(&src_arr);
	if dest.len != 1 {{ return 1; }}
	{assert_snippet}
	if src_arr.len != 1 {{ return 2; }}
"""


def site_array_literal(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    assert_snippet = ty_info["assert_eq_heap"](f"arr[0]")
    return f"""
{decls}	val arr: Array<{decl_ty}> = [{access}];
	if arr.len != 1 {{ return 1; }}
	{assert_snippet}
"""


def site_struct_ctor(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    assert_snippet = ty_info["assert_eq_heap"](f"bag.v")
    return f"""
{decls}	val bag: Bag<{decl_ty}> = Bag(v = {access});
	{assert_snippet}
"""


def site_variant_ctor(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    assert_snippet = ty_info["assert_eq_heap"](f"inner")
    return f"""
{decls}	val m: Msg<{decl_ty}> = Msg::Payload(v = {access});
	match m {{
		Msg::Payload(inner) => {{ {assert_snippet} }},
		default => {{ return 2; }}
	}}
"""


def site_result_ok(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    assert_snippet = ty_info["assert_eq_heap"](f"v")
    return f"""
{decls}	val r: core.Result<{decl_ty}, Int> = core.Result::Ok({access});
	match r {{
		core.Result::Err(_) => {{ return 3; }},
		core.Result::Ok(v) => {{ {assert_snippet} }},
	}}
"""


def site_result_err(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    assert_snippet = ty_info["assert_eq_heap"](f"e")
    return f"""
{decls}	val r: core.Result<Int, {decl_ty}> = core.Result::Err({access});
	match r {{
		core.Result::Ok(_) => {{ return 3; }},
		core.Result::Err(e) => {{ {assert_snippet} }},
	}}
"""


def site_fn_arg(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    # Function call by-value arg: `sink(value)`.  The sink fn is a
    # per-fixture top-level helper (see SITE_HELPERS) that takes the
    # element type by value and returns Int (via the type's
    # `extract_int` projection).  This isolates `_lower_call_arg`'s
    # decision-making and the callee-side by-value reception from the
    # array-store / constructor composite paths.
    decls, access = SHAPES[shape_name](ty_info, flavor)
    expected = ty_info["expected_int"]
    return f"""
{decls}	val n = sink({access});
	if n != {expected} {{ return 1; }}
"""


def site_return_value(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    # Return-value transfer: callee returns a value built per the
    # named shape; caller receives and asserts.  Each shape gets its
    # own producer function (see SITE_HELPERS) so the matrix can
    # isolate "callee returns owned local" vs "callee returns
    # rvalue-direct" vs "callee returns projected place".
    decl_ty = ty_info["decl_ty"]
    expected = ty_info["expected_int"]
    extract = ty_info["extract_int"]("v")
    return f"""
	val v: {decl_ty} = produce_{shape_name}();
	if {extract} != {expected} {{ return 1; }}
"""


def site_extend_source(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    # True source-array-shape axis for `extend(&src)`: vary the
    # ITERABLE expression at the extend call, not the upstream
    # populate-via-push step.  Complements `array_extend` (which
    # documented the populate-shape axis as approximate).
    #
    # - hvar_local: `dest.extend(&src_arr)` — borrow of a local var.
    # - hcall_rvalue: `dest.extend(&make_one_element_array())` — borrow
    #   of an rvalue.  May be rejected by Drift's borrow rules; if so,
    #   the failure is the contract — document and move on.
    # - projection: `dest.extend(&holder.items)` — borrow of a struct
    #   field via projection.
    decl_ty = ty_info["decl_ty"]
    seed = ty_info["build_heap"](flavor)
    assert_snippet = ty_info["assert_eq_heap"](f"dest[0]")
    if shape_name == "hvar_local":
        setup = f"\tvar src_arr: Array<{decl_ty}> = [];\n\tsrc_arr.push({seed});\n"
        extend_call = "dest.extend(&src_arr);"
        post_check = "\tif src_arr.len != 1 { return 4; }\n"
    elif shape_name == "hcall_rvalue":
        # Build a single-element array from a function call return.
        # The borrowed-rvalue form is uncommon and likely rejected by
        # the borrow checker; bind to a local first to keep the
        # transfer site at extend rather than failing surface syntax.
        setup = f"\tval src_arr: Array<{decl_ty}> = make_one_element_array();\n"
        extend_call = "dest.extend(&src_arr);"
        post_check = "\tif src_arr.len != 1 { return 4; }\n"
    elif shape_name == "projection":
        # `move <expr>` requires an addressable place; bind the call
        # result to a local first, then move it into the struct field.
        setup = (
            f"\tvar bag_items: Array<{decl_ty}> = arr_lit_factory();\n"
            f"\tval bag: ArrBag<{decl_ty}> = ArrBag(items = move bag_items);\n"
        )
        extend_call = "dest.extend(&bag.items);"
        post_check = "\tif bag.items.len != 1 { return 4; }\n"
    else:
        raise ValueError(f"unknown shape: {shape_name!r}")
    return f"""
{setup}	var dest: Array<{decl_ty}> = [];
	{extend_call}
	if dest.len != 1 {{ return 1; }}
	{assert_snippet}
{post_check}"""


def site_for_loop_bind(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    # `for x in <iterable>` — element binder gets `&T` (a borrow) per
    # the existing for-loop semantics; iteration must NOT consume the
    # iterable, and the binder per-iteration must read intact data.
    # The shape axis varies the ITERABLE expression, not the binder
    # (binder is always `&T`).  After the loop, the iterable must
    # still own its elements.
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    expected = ty_info["expected_int"]
    extract_x = ty_info["extract_int"]("(*x)")
    return f"""
	// Build a single-element source array.
{decls}	var iterable: Array<{decl_ty}> = [];
	iterable.push({access});
	var seen = 0;
	for x in iterable {{
		if {extract_x} != {expected} {{ return 1; }}
		seen = seen + 1;
	}}
	if seen != 1 {{ return 2; }}
	// Iterable must still own its element after the loop.
	if iterable.len != 1 {{ return 3; }}
"""


def site_local_assign(shape_name: str, ty_info: dict, flavor: str | None) -> str:
    # Local reassignment: `dst = source_expr;` where dst is a `var`
    # local previously bound to a placeholder of the same type.  This
    # site is distinct from struct/variant/array stores: it's the
    # ownership-transfer step a `var x = ...; ... ; x = ...;` pattern
    # implies, plus the implicit drop of the previous dst value.
    # Pre-fix in the drift-net-tls TLS regression, exactly this kind
    # of var-reassignment-after-push triggered the UAF (the bad push
    # released a shared view; the next assignment to the source local
    # freed the buffer the array slot still pointed at).
    decls, access = SHAPES[shape_name](ty_info, flavor)
    decl_ty = ty_info["decl_ty"]
    placeholder = ty_info["build_heap"](flavor)
    expected = ty_info["expected_int"]
    extract_dst = ty_info["extract_int"]("dst")
    return f"""
	var dst: {decl_ty} = {placeholder};
{decls}	dst = {access};
	if {extract_dst} != {expected} {{ return 1; }}
"""


SITES = {
    "array_push": site_array_push,
    "array_insert": site_array_insert,
    "array_set": site_array_set,
    "array_extend": site_array_extend,
    "array_literal": site_array_literal,
    "struct_ctor": site_struct_ctor,
    "variant_ctor": site_variant_ctor,
    "result_ok": site_result_ok,
    "result_err": site_result_err,
    "fn_arg": site_fn_arg,
    "return_value": site_return_value,
    "local_assign": site_local_assign,
    "for_loop_bind": site_for_loop_bind,
    "extend_source": site_extend_source,
    # match_bind is a meta-site: its emitter is not called through the
    # per-shape scenario loop in `render_fixture`.  Instead,
    # `_render_fixture_match_bind` (Copy types) and the Token-path
    # equivalent generate one scenario fn per MATCH_BIND_PATTERN.
    # The SITES entry exists so the outer iteration in `emit_all` /
    # `--check` covers this site like any other.
    "match_bind": None,
}


# SITE_HELPERS — per-site helpers emitted at module scope (above the
# scenario fns).  Some sites (fn_arg, return_value) need top-level
# function declarations that take/return the element type; those are
# not expressible inside the per-shape scenario body alone.
#
# Each entry is a callable `(ty_info, flavor) -> str` returning Drift
# code to splice in at module scope.  Sites without top-level helpers
# omit themselves from this dict.
SITE_HELPERS: dict[str, "object"] = {}


def helpers_fn_arg(ty_info: dict, flavor: str | None) -> str:
    # `sink(x: T) -> Int` — consumes x by value, returns the Int
    # projection (byte_length / key.byte_length / x).  Doesn't depend
    # on flavor; same sink for all flavors of the type.
    decl_ty = ty_info["decl_ty"]
    extract = ty_info["extract_int"]("x")
    return f"""
fn sink(x: {decl_ty}) nothrow -> Int {{
	return {extract};
}}
"""


def helpers_return_value(ty_info: dict, flavor: str | None) -> str:
    # Three producers, one per value shape.  Each producer rebuilds
    # the value internally using its shape's setup with this fixture's
    # flavor.  The caller (scenario) just calls `produce_<shape>()`.
    decl_ty = ty_info["decl_ty"]
    out: list[str] = []
    for shape_name, shape_emit in SHAPES.items():
        decls, access = shape_emit(ty_info, flavor)
        out.append(
            f"""
fn produce_{shape_name}() nothrow -> {decl_ty} {{
{decls}	return {access};
}}
"""
        )
    return "".join(out)


def helpers_extend_source(ty_info: dict, flavor: str | None) -> str:
    # `make_one_element_array() -> Array<T>` for hcall_rvalue;
    # `arr_lit_factory() -> Array<T>` for projection (used to fill
    # bag.items via struct ctor).  Same heap-built single element
    # both times.  Plus the ArrBag<T> struct decl for projection.
    decl_ty = ty_info["decl_ty"]
    seed = ty_info["build_heap"](flavor)
    return f"""
pub struct ArrBag<T> {{
	pub items: Array<T>,
}}

fn make_one_element_array() nothrow -> Array<{decl_ty}> {{
	var xs: Array<{decl_ty}> = [];
	xs.push({seed});
	return move xs;
}}

fn arr_lit_factory() nothrow -> Array<{decl_ty}> {{
	var xs: Array<{decl_ty}> = [];
	xs.push({seed});
	return move xs;
}}
"""


SITE_HELPERS["fn_arg"] = helpers_fn_arg
SITE_HELPERS["return_value"] = helpers_return_value
SITE_HELPERS["extend_source"] = helpers_extend_source


# --------------------------------------------------------------------
# MATCH-ARM BIND AXIS (`match_bind` site)
# --------------------------------------------------------------------
# Rationale: the existing variant_ctor / result_ok / result_err sites
# exercise match binding INCIDENTALLY — the assert `{assert_eq_heap}`
# runs inside a `Msg::Payload(inner)` arm and thus depends on both
# variant construction and match extraction working correctly.  When
# 0.27.197 surfaced multiple match-specific ownership bugs
# (value-producing tombstone store, partial-move cleanup for subset
# binds, generic variant re-finalization), the matrix caught some but
# not all of them because it couldn't isolate payload EXTRACTION from
# payload CONSTRUCTION.
#
# The match_bind site emits one fixture per (type, flavor) combo whose
# INTERNAL scenarios are patterns of match binding, not value shapes.
# Each pattern below targets a distinct binder-lifetime / arm-ownership
# property the 0.27.197 cluster could have regressed.
#
# Patterns:
#   - `named_bind`: baseline `Msg::Payload(v) => use v`.
#   - `wildcard_ignore`: `Msg::Payload(_) => { }` — the ignored payload
#     must drop exactly once, no leak on the wildcard discard path.
#   - `arm_bind_vs_ignore`: `Pair::Left(v) => use v`,
#     `Pair::Right(_) => discard`.  Both arms run (two separate
#     scenarios within the same fn) so per-arm ownership divergence is
#     exercised.
#   - `reassignment_before_match`: `var r = old_value; r = new_value;
#     match r { Payload(v) => ... }` — binder must reflect the NEW
#     value; old value must be dropped by the `var`-reassignment path
#     (exercises the `string_arc.py` MoveOut-discard hardening).
#   - `nested_match`: outer arm binds inner variant; inner match
#     destructures its own payload.  Verifies tombstone / partial-move
#     propagation through two layers of match lowering.
#   - `value_producing_match`:
#     `val x = match r { Payload(v) => v, default => fallback }` —
#     result takes ownership of the arm-bound payload.
#     * For Copy / Copy-non-bitcopy (String):
#       verifies that the bound `v` carries a retained / owned payload
#       after the arm, and the scrutinee's scope-drop does NOT free it
#       early.
#     * For Token (non-Copy): verifies `sess.drops` does NOT increment
#       until the returned Token's OWN scope ends, pinning the
#       value-producing tombstone store from 0.27.197 against silent
#       regression to "destroy fires at arm-end".
#
# The match_bind site's body is assembled by `_render_fixture_match_bind`
# (Copy types) and `_render_fixture_token_match_bind` (Token), both of
# which bypass the per-shape scenario loop used by other SITES.


def _match_bind_build_heap(ty_info: dict, flavor: str | None) -> str:
    return ty_info["build_heap"](flavor)


def _match_bind_replacement(ty_info: dict, flavor: str | None) -> str:
    """Different value of the same type for reassignment-before-match."""
    return ty_info["replacement"](flavor)


def _match_bind_pattern_named_bind(ty_info: dict, flavor: str | None) -> str:
    decl_ty = ty_info["decl_ty"]
    heap = _match_bind_build_heap(ty_info, flavor)
    assert_v = ty_info["assert_eq_heap"]("v")
    return f"""fn scenario_named_bind() nothrow -> Int {{
	val r: Msg<{decl_ty}> = Msg::Payload(v = {heap});
	match r {{
		Msg::Payload(v) => {{ {assert_v} }},
		default => {{ return 2; }}
	}}
	return 0;
}}
"""


def _match_bind_pattern_wildcard_ignore(ty_info: dict, flavor: str | None) -> str:
    # Wildcard binder `_` — payload must drop exactly once.  On plain
    # mode, success is just exit 0; ASAN / memcheck catch the leak /
    # double-free if cleanup goes wrong.
    decl_ty = ty_info["decl_ty"]
    heap = _match_bind_build_heap(ty_info, flavor)
    return f"""fn scenario_wildcard_ignore() nothrow -> Int {{
	val r: Msg<{decl_ty}> = Msg::Payload(v = {heap});
	match r {{
		Msg::Payload(_) => {{ /* payload discarded via wildcard; must drop exactly once */ }},
		default => {{ return 2; }}
	}}
	return 0;
}}
"""


def _match_bind_pattern_arm_bind_vs_ignore(ty_info: dict, flavor: str | None) -> str:
    # Two-ctor `Pair<T>` variant; construct one of each and run both
    # match shapes.  The binding arm and the wildcard arm must each
    # handle ownership correctly without relying on the sibling arm's
    # behavior.
    decl_ty = ty_info["decl_ty"]
    heap_l = _match_bind_build_heap(ty_info, flavor)
    heap_r = _match_bind_build_heap(ty_info, flavor)
    assert_v = ty_info["assert_eq_heap"]("v")
    return f"""fn scenario_arm_bind_vs_ignore() nothrow -> Int {{
	val left: Pair<{decl_ty}> = Pair::Left(v = {heap_l});
	match left {{
		Pair::Left(v) => {{ {assert_v} }},
		Pair::Right(_) => {{ return 3; }}
	}}
	val right: Pair<{decl_ty}> = Pair::Right(v = {heap_r});
	match right {{
		Pair::Left(_) => {{ return 4; }},
		Pair::Right(_) => {{ /* payload discarded via wildcard */ }}
	}}
	return 0;
}}
"""


def _match_bind_pattern_reassignment_before_match(ty_info: dict, flavor: str | None) -> str:
    # Exercises the MoveOut-then-reassign code path in string_arc.py
    # that was hardened in 0.27.197.  The var holds `old`; the
    # subsequent `= new` reassignment must drop `old` cleanly BEFORE
    # the new value is committed.  Match then observes the NEW value.
    decl_ty = ty_info["decl_ty"]
    heap_old = _match_bind_build_heap(ty_info, flavor)
    heap_new = _match_bind_replacement(ty_info, flavor)
    # Assert against the replacement's expected projection; `replacement`
    # is a different value from `build_heap`, so we can't reuse
    # assert_eq_heap directly.  Use extract_int to get a concrete Int.
    extract = ty_info["extract_int"]("v")
    replacement_extract_expected = ty_info.get("replacement_expected_int")
    if replacement_extract_expected is None:
        # Fallback: compare against `expected_int` assuming replacement
        # projects to the same int (true for types where replacement
        # differs in content but not in projection length).  Overridden
        # via TYPE_INFO["replacement_expected_int"] when the replacement
        # yields a distinct Int.
        replacement_extract_expected = ty_info["expected_int"]
    return f"""fn scenario_reassignment_before_match() nothrow -> Int {{
	var r: Msg<{decl_ty}> = Msg::Payload(v = {heap_old});
	// Qualified ctor on the reassignment RHS: the checker cannot infer
	// `T` from the bare `Msg::Payload(...)` call because there is no
	// expected-type context at a `var` reassignment (E-QMEM-CANNOT-INFER).
	r = Msg<{decl_ty}>::Payload(v = {heap_new});
	match r {{
		Msg::Payload(v) => {{
			if {extract} != {replacement_extract_expected} {{ return 1; }}
		}},
		default => {{ return 2; }}
	}}
	return 0;
}}
"""


def _match_bind_pattern_nested_match(ty_info: dict, flavor: str | None) -> str:
    # Outer variant `Nest<T>::Wrap(inner: Msg<T>)` wraps the inner
    # `Msg<T>::Payload(v)`.  Outer match binds `inner` (consumes
    # ownership of the inner variant); inner match destructures its
    # payload.  Each match layer's scrutinee must be cleaned up
    # correctly — no scrutinee-scope-drop double-destroys the payload
    # moved through two binder layers.
    decl_ty = ty_info["decl_ty"]
    heap = _match_bind_build_heap(ty_info, flavor)
    assert_v = ty_info["assert_eq_heap"]("v")
    return f"""fn scenario_nested_match() nothrow -> Int {{
	val inner: Msg<{decl_ty}> = Msg::Payload(v = {heap});
	val outer: Nest<{decl_ty}> = Nest::Wrap(inner = move inner);
	match outer {{
		Nest::Wrap(i) => {{
			match i {{
				Msg::Payload(v) => {{ {assert_v} }},
				default => {{ return 3; }}
			}}
		}},
		default => {{ return 4; }}
	}}
	return 0;
}}
"""


def _match_bind_pattern_value_producing_match(ty_info: dict, flavor: str | None) -> str:
    # Value-producing match: `val x = match r { Payload(v) => v,
    # default => fallback }`.  For Copy / Copy-non-bitcopy types this
    # verifies the result owns a valid retained/copied payload AFTER
    # the arm completes — the scrutinee's scope-drop must not free the
    # payload the result now holds.
    #
    # Ownership independence proof: build the scrutinee inside a helper
    # fn and return the match result; the scrutinee goes out of scope
    # at the helper's return boundary.  If the arm leaked an alias of
    # the scrutinee's payload into the returned value, the caller's
    # read after helper-return would UAF under ASAN / memcheck.
    decl_ty = ty_info["decl_ty"]
    heap = _match_bind_build_heap(ty_info, flavor)
    fallback = _match_bind_build_heap(ty_info, flavor)
    assert_x = ty_info["assert_eq_heap"]("x")
    return f"""fn _vpm_helper() nothrow -> {decl_ty} {{
	val r: Msg<{decl_ty}> = Msg::Payload(v = {heap});
	// Scrutinee `r` goes out of scope at this fn's return boundary;
	// if the arm produces an alias of r's payload, the caller's read
	// below will UAF under ASAN.
	return match r {{
		Msg::Payload(v) => {{ v }},
		default => {{ {fallback} }},
	}};
}}

fn scenario_value_producing_match() nothrow -> Int {{
	val x: {decl_ty} = _vpm_helper();
	{assert_x}
	return 0;
}}
"""


MATCH_BIND_PATTERNS = {
    "named_bind": _match_bind_pattern_named_bind,
    "wildcard_ignore": _match_bind_pattern_wildcard_ignore,
    "arm_bind_vs_ignore": _match_bind_pattern_arm_bind_vs_ignore,
    "reassignment_before_match": _match_bind_pattern_reassignment_before_match,
    "nested_match": _match_bind_pattern_nested_match,
    "value_producing_match": _match_bind_pattern_value_producing_match,
}


_NEST_DECL = """\
pub variant Nest<T> {
	Wrap(inner: Msg<T>),
	@tombstone NestTombstone,
}
"""


_PAIR_DECL = """\
pub variant Pair<T> {
	Left(v: T),
	Right(v: T),
}
"""


_SHAPE_LINE_MATCH_BIND = (
    "// Scenarios are MATCH-ARM BIND PATTERNS, not value shapes:\n"
    "// named_bind, wildcard_ignore, arm_bind_vs_ignore,\n"
    "// reassignment_before_match, nested_match, value_producing_match.\n"
    "// Each isolates a distinct binder-lifetime / arm-ownership\n"
    "// property that the existing variant_ctor / result_* sites\n"
    "// exercise only incidentally.\n"
    "//\n"
)


def _render_fixture_match_bind(ty_name: str, ty_info: dict, flavor: str | None) -> str:
    """Dedicated renderer for the match_bind site on Copy types."""
    lines: list[str] = []
    lines.append(
        _HEADER.format(
            site="match_bind",
            ty_name=ty_name,
            flavor_line=_flavor_line(flavor),
            shape_line=_SHAPE_LINE_MATCH_BIND,
        )
    )
    if ty_info.get("needs_import_core") or ty_name == "string":
        lines.append("import std.core as core;\n")
    lines.append("\n")
    lines.append(_MSG_DECL)
    lines.append(_PAIR_DECL)
    lines.append(_NEST_DECL)
    lines.append("\n")
    for pattern_name, pattern_emit in MATCH_BIND_PATTERNS.items():
        lines.append(pattern_emit(ty_info, flavor))
        lines.append("\n")
    # Main dispatcher — one call per pattern, offset 100 * idx.
    main_body = []
    for idx, pattern_name in enumerate(MATCH_BIND_PATTERNS.keys(), start=1):
        offset = idx * 100
        main_body.append(
            f"\tval r{idx} = scenario_{pattern_name}();\n"
            f"\tif r{idx} != 0 {{ return {offset} + r{idx}; }}\n"
        )
    lines.append(
        "pub fn main() nothrow -> Int {\n"
        + "".join(main_body)
        + "\treturn 0;\n"
        + "}\n"
    )
    return "".join(lines)


# Token match_bind patterns — explicit ownership model per the
# reviewer's guidance: each pattern MUST assert `sess.drops` at
# carefully-chosen observation points so the scenario exercises the
# TOMBSTONE PATH (value-producing match tombstone store from 0.27.197)
# rather than accidentally only proving arm-end drop.

def _token_match_bind_named_bind() -> str:
    # Bind `v` in the arm; `v` drops at arm-end → sess.drops == 1.
    # Scrutinee `m` was consumed by the move; its scope-drop must be
    # a tombstone-store no-op.
    return """fn scenario_named_bind() nothrow -> Int {
	var sess: Session = Session(drops = 0);
	{
		val m: TokenMsg = TokenMsg::Payload(t = make_token(&mut sess));
		match m {
			TokenMsg::Payload(v) => {
				// Inside arm: Token is live as `v`.
				if sess.drops != 0 { return 1; }
			},
			default => { return 2; }
		}
		// Arm-end dropped `v` → sess.drops == 1.
		if sess.drops != 1 { return 3; }
	}
	// Scrutinee scope-drop tombstone no-op; sess.drops stays 1.
	if sess.drops != 1 { return 4; }
	return 0;
}
"""


def _token_match_bind_wildcard_ignore() -> str:
    # Wildcard `_` discards the payload; it must still drop exactly
    # once.  Unlike named_bind, there is no binder name to observe
    # in-arm; the drop happens inside the arm as part of scrutinee
    # consumption.  After the match, sess.drops == 1.
    return """fn scenario_wildcard_ignore() nothrow -> Int {
	var sess: Session = Session(drops = 0);
	{
		val m: TokenMsg = TokenMsg::Payload(t = make_token(&mut sess));
		match m {
			TokenMsg::Payload(_) => {
				// Wildcard: no binder.  Payload destructor fires
				// as part of the match arm / scrutinee consumption.
			},
			default => { return 2; }
		}
		if sess.drops != 1 { return 3; }
	}
	if sess.drops != 1 { return 4; }
	return 0;
}
"""


def _token_match_bind_arm_bind_vs_ignore() -> str:
    # Two ctors, two scenarios; each observes `sess.drops` per
    # its own Token lifetime.  Both arms must drop exactly once
    # regardless of which branch is taken.
    return """fn scenario_arm_bind_vs_ignore() nothrow -> Int {
	var sess_left: Session = Session(drops = 0);
	var sess_right: Session = Session(drops = 0);
	{
		val left: TokenPair = TokenPair::Left(t = make_token(&mut sess_left));
		match left {
			TokenPair::Left(v) => {
				if sess_left.drops != 0 { return 1; }
			},
			TokenPair::Right(_) => { return 3; }
		}
		if sess_left.drops != 1 { return 4; }
	}
	if sess_left.drops != 1 { return 5; }
	{
		val right: TokenPair = TokenPair::Right(t = make_token(&mut sess_right));
		match right {
			TokenPair::Left(_) => { return 6; },
			TokenPair::Right(_) => {
				// Wildcard discard; destructor fires as part of
				// scrutinee consumption.
			}
		}
		if sess_right.drops != 1 { return 7; }
	}
	if sess_right.drops != 1 { return 8; }
	return 0;
}
"""


def _token_match_bind_reassignment_before_match() -> str:
    # `var r: TokenMsg = Msg::Payload(t_old); r = Msg::Payload(t_new);`
    # The reassignment must drop `t_old` cleanly (sess_old.drops == 1)
    # BEFORE committing t_new.  The match then observes t_new.
    return """fn scenario_reassignment_before_match() nothrow -> Int {
	var sess_old: Session = Session(drops = 0);
	var sess_new: Session = Session(drops = 0);
	{
		var r: TokenMsg = TokenMsg::Payload(t = make_token(&mut sess_old));
		// Pre-reassignment: old token alive.
		if sess_old.drops != 0 { return 1; }
		r = TokenMsg::Payload(t = make_token(&mut sess_new));
		// Post-reassignment: old token dropped (sess_old.drops == 1),
		// new token alive (sess_new.drops == 0).  Exercises the
		// MoveOut-zero-then-StoreLocal hardening in string_arc.py —
		// the drop-before-overwrite must fire on the OLD value, not
		// on the synthesized zero bytes.
		if sess_old.drops != 1 { return 2; }
		if sess_new.drops != 0 { return 3; }
		match r {
			TokenMsg::Payload(v) => {
				if sess_new.drops != 0 { return 4; }
			},
			default => { return 5; }
		}
		// Arm-end dropped new-token binder.
		if sess_new.drops != 1 { return 6; }
	}
	// Scrutinee tombstone no-op.
	if sess_old.drops != 1 { return 7; }
	if sess_new.drops != 1 { return 8; }
	return 0;
}
"""


def _token_match_bind_nested_match() -> str:
    # Outer `TokenNest::Wrap(inner: TokenMsg)` wraps the inner
    # `TokenMsg::Payload(t: Token)`.  Outer match binds `inner` —
    # consuming the outer scrutinee and moving the inner variant
    # into the outer arm's binder.  Inner match then destructures
    # the Token payload out of the binder.  Both scrutinee layers
    # must be cleaned up correctly without double-destroying the
    # Token.
    return """fn scenario_nested_match() nothrow -> Int {
	var sess: Session = Session(drops = 0);
	{
		val inner: TokenMsg = TokenMsg::Payload(t = make_token(&mut sess));
		val outer: TokenNest = TokenNest::Wrap(inner = move inner);
		match outer {
			TokenNest::Wrap(i) => {
				match i {
					TokenMsg::Payload(v) => {
						if sess.drops != 0 { return 1; }
					},
					default => { return 3; }
				}
				// Inner match done; arm dropped `v` → sess.drops == 1.
				if sess.drops != 1 { return 4; }
			},
			default => { return 5; }
		}
		// Outer match done; both scrutinee layers tombstoned.
		if sess.drops != 1 { return 6; }
	}
	if sess.drops != 1 { return 7; }
	return 0;
}
"""


def _token_match_bind_value_producing_match() -> str:
    # `val returned: Token = match m { Payload(v) => move v, default
    # => make_token(&mut sess_fb) }`.  The Payload arm MOVES `v` out
    # of the scrutinee and returns it as the match result → `returned`
    # owns the Token.
    #
    # Key assertion: `sess.drops` stays 0 while `returned` is alive
    # (i.e. destroy does NOT fire at arm-end, pinning the tombstone
    # store from 0.27.197 against silent regression to "destructor
    # fires when the binder `v` goes out of scope at arm-end").
    # sess.drops increments to 1 only when `returned` ITSELF goes out
    # of scope.  Fallback session guards against accidental default-
    # arm dispatch.
    return """fn scenario_value_producing_match() nothrow -> Int {
	var sess: Session = Session(drops = 0);
	var sess_fb: Session = Session(drops = 0);
	{
		val m: TokenMsg = TokenMsg::Payload(t = make_token(&mut sess));
		{
			val returned: Token = match m {
				TokenMsg::Payload(v) => { move v },
				default => { make_token(&mut sess_fb) },
			};
			// Crucial assertion: the returned Token is live; destroy
			// has NOT fired.  If the compiler incorrectly dropped `v`
			// at arm-end (instead of moving it into `returned`), this
			// would already read sess.drops == 1.
			if sess.drops != 0 { return 1; }
			// Default arm was not taken → fallback session untouched.
			if sess_fb.drops != 0 { return 2; }
		}
		// `returned` scope-dropped here → destroy fires once.
		if sess.drops != 1 { return 3; }
		// Scrutinee `m` was consumed by the Payload arm's move; its
		// scope-drop below reads the tombstone bytes and is a no-op.
		if sess_fb.drops != 0 { return 4; }
	}
	// Post-scrutinee-scope: tombstone route means sess.drops stays 1.
	if sess.drops != 1 { return 5; }
	if sess_fb.drops != 0 { return 6; }
	return 0;
}
"""


TOKEN_MATCH_BIND_PATTERNS = {
    "named_bind": _token_match_bind_named_bind,
    "wildcard_ignore": _token_match_bind_wildcard_ignore,
    "arm_bind_vs_ignore": _token_match_bind_arm_bind_vs_ignore,
    "reassignment_before_match": _token_match_bind_reassignment_before_match,
    "nested_match": _token_match_bind_nested_match,
    "value_producing_match": _token_match_bind_value_producing_match,
}


_TOKEN_PAIR_DECL = """\
pub variant TokenPair {
	Left(t: Token),
	Right(t: Token),
}
"""


_TOKEN_NEST_DECL = """\
pub variant TokenNest {
	Wrap(inner: TokenMsg),
	@tombstone TokenNestTombstone,
}
"""


def _render_fixture_token_match_bind() -> str:
    """Dedicated renderer for the match_bind site on Token."""
    lines: list[str] = []
    lines.append(
        _HEADER.format(
            site="match_bind",
            ty_name="token",
            flavor_line="",
            shape_line=_SHAPE_LINE_MATCH_BIND,
        )
    )
    lines.append("import std.core as core;\n")
    lines.append(TOKEN_PREAMBLE)
    lines.append(_TOKEN_MSG_DECL)
    lines.append(_TOKEN_PAIR_DECL)
    lines.append(_TOKEN_NEST_DECL)
    lines.append("\n")
    for pattern_name, pattern_emit in TOKEN_MATCH_BIND_PATTERNS.items():
        lines.append(pattern_emit())
        lines.append("\n")
    main_body = []
    for idx, pattern_name in enumerate(TOKEN_MATCH_BIND_PATTERNS.keys(), start=1):
        offset = idx * 100
        main_body.append(
            f"\tval r{idx} = scenario_{pattern_name}();\n"
            f"\tif r{idx} != 0 {{ return {offset} + r{idx}; }}\n"
        )
    lines.append(
        "pub fn main() nothrow -> Int {\n"
        + "".join(main_body)
        + "\treturn 0;\n"
        + "}\n"
    )
    return "".join(lines)


# --------------------------------------------------------------------
# NON-COPY TOKEN AXIS
# --------------------------------------------------------------------
# The Copy-type matrix above guards retain / drop balance for Copy
# (possibly non-bitcopy) values.  The other half of ownership
# correctness is MOVE/DROP-EXACTLY-ONCE for truly non-Copy values
# with a `core.Destructible` impl.  Different obligations:
#   - Callee must move the source, not copy it.
#   - Source locals must not be dropped again after being moved.
#   - Destructors run at the destination's lifetime end, not at
#     transfer time (no premature drop).
#
# The Token type carries an observable side channel (`&mut Session`
# where `Session.drops: Int`) that every `destroy()` increments.
# Each scenario asserts `sess.drops == 0` while the token is "in
# flight" through the transfer site and `sess.drops == 1` after the
# destination container has gone out of scope.
#
# SHAPES — two for Token (projection of a non-Copy struct field is
# complex and deferred):
#   - hvar_move:    `val tok = make_token(&mut sess); <site>(move tok);`
#   - hcall_rvalue: `<site>(make_token(&mut sess));`
#
# SITES supported for Token (see TOKEN_SITES dict below):
#   - struct_ctor, variant_ctor
#   - result_ok
#   - return_value
#   - local_assign
#   - fn_arg
# Array sites (array_push/insert/set/literal/extend) and for_loop_bind
# are skipped: `Array<Token>` is rejected by the type system today
# ("owning Array cannot contain borrowed aggregate element type in
# v1") because Token carries a `&mut Session` field.  Extending the
# non-Copy axis to Array sites requires a different side-channel
# design (e.g. a shared refcount `Int` cell instead of `&mut
# Session`); deferred.
#
# Negative contract test for the "missing move" case
# (`match tok { Token => ... }` or `sink(tok)` without `move`) is
# also deferred.  The generator's positive Token fixtures all use
# explicit `move` for HVar scenarios.


TOKEN_PREAMBLE = """
pub struct Session {
	pub drops: Int,
}

pub struct Token {
	pub session: &mut Session,
}

implement core.Destructible for Token {
	pub fn destroy(self: Token) nothrow -> Void {
		self.session.drops = self.session.drops + 1;
	}
}

fn make_token(sess: &mut Session) nothrow -> Token {
	return Token(session = sess);
}
"""


TOKEN_SHAPES = ("hvar_move", "hcall_rvalue")


def token_access(shape_name: str, sess_expr: str = "&mut sess") -> tuple[str, str]:
    """Return (setup-decls, access-expression) for a Token-transfer
    scenario.  `sess_expr` is the expression that yields `&mut Session`
    — at the scenario level where `sess` is a `Session` local this is
    `&mut sess`, but inside a helper that receives `sess: &mut Session`
    directly, the caller passes just `sess`.
    """
    if shape_name == "hvar_move":
        return (f"\tval tok: Token = make_token({sess_expr});\n", "move tok")
    if shape_name == "hcall_rvalue":
        return ("", f"make_token({sess_expr})")
    raise ValueError(f"unknown token shape: {shape_name!r}")


def token_scenario_wrap(site_body: str) -> str:
    """Wrap a Token transfer-site body in a Session setup + drops
    assertion.  The body is expected to create the destination
    container (array/struct/variant/etc.), transfer the Token into
    it, and let the container go out of scope at the body's end.
    """
    return f"""
	var sess: Session = Session(drops = 0);
{site_body}	// destination container scope-dropped above; destructor
	// must have run exactly once.
	if sess.drops != 1 {{ return 90; }}
"""


# Per-(site, shape) Token scenario bodies.  Each returns the *inner*
# body (without the Session-setup wrapper applied by token_scenario_wrap).
# Array<Token> sites are omitted — see TOKEN_SITES comment below.
def token_site_struct_ctor(shape: str) -> str:
    decls, access = token_access(shape)
    return f"""	{{
{decls}		val bag: TokenBag = TokenBag(t = {access});
		if sess.drops != 0 {{ return 1; }}
	}}
"""


def token_site_variant_ctor(shape: str) -> str:
    decls, access = token_access(shape)
    return f"""	{{
{decls}		val m: TokenMsg = TokenMsg::Payload(t = {access});
		match m {{
			TokenMsg::Payload(inner) => {{
				if sess.drops != 0 {{ return 1; }}
			}},
			default => {{ return 2; }},
		}}
	}}
"""


def token_site_result_ok(shape: str) -> str:
    decls, access = token_access(shape)
    return f"""	{{
{decls}		val r: core.Result<Token, Int> = core.Result::Ok({access});
		match r {{
			core.Result::Err(_) => {{ return 2; }},
			core.Result::Ok(v) => {{
				if sess.drops != 0 {{ return 1; }}
			}},
		}}
	}}
"""


def token_site_return_value(shape: str) -> str:
    decls, access = token_access(shape)
    # produce_<shape>(sess) returns a Token built via the named
    # shape; caller receives it and lets it drop at the end of the
    # wrapping block.
    return f"""	{{
		val v: Token = produce_{shape}(&mut sess);
		if sess.drops != 0 {{ return 1; }}
	}}
"""


def token_site_local_assign(shape: str) -> str:
    # Build dst via an extra session so the initial-drop doesn't
    # pollute `sess`.  Then reassign dst from the shape-provided
    # Token over `sess`.  sess.drops only increments from the
    # shape-provided Token's destructor at block scope exit.
    decls, access = token_access(shape)
    return f"""	{{
		var dst_sess: Session = Session(drops = 0);
		var dst: Token = make_token(&mut dst_sess);
{decls}		dst = {access};
		if sess.drops != 0 {{ return 1; }}
	}}
"""


def token_site_fn_arg(shape: str) -> str:
    decls, access = token_access(shape)
    # `sink(t: Token)` consumes the Token by value; the Token drops
    # at sink's scope end before sink returns.
    return f"""{decls}	sink({access});
	// After sink returns, destructor must have run exactly once.
"""


# NOTE on array_* sites for Token:
# Token carries a `&mut Session` reference field as its observable
# side channel.  Drift's type system rejects `Array<Token>` in v1 with
# "owning Array cannot contain borrowed aggregate element type in v1"
# — arrays of borrow-bearing structs are not legal surface syntax.
# The Copy-type matrix covers array_push/insert/set for Copy element
# types extensively; for the non-Copy axis, the move-and-drop-once
# guarantee is exercised by the non-Array sites (struct/variant ctor,
# result ok, return value, local assign, fn arg) which are all that
# the type system permits for a borrow-bearing Token.  Extending the
# non-Copy axis to Array<Token> requires a different side-channel
# design (e.g. a shared refcount Int cell instead of `&mut Session`)
# and is left as a follow-up.
TOKEN_SITES: dict[str, "object"] = {
    "struct_ctor": token_site_struct_ctor,
    "variant_ctor": token_site_variant_ctor,
    "result_ok": token_site_result_ok,
    "return_value": token_site_return_value,
    "local_assign": token_site_local_assign,
    "fn_arg": token_site_fn_arg,
    # match_bind is routed through `_render_fixture_token_match_bind`,
    # which does not go through the per-shape emitter loop.  The
    # entry here keeps the outer site iteration (emit_all / --check)
    # from skipping Token × match_bind.
    "match_bind": None,
}


_TOKEN_BAG_DECL = """\
pub struct TokenBag {
\tpub t: Token,
}
"""


_TOKEN_MSG_DECL = """\
pub variant TokenMsg {
\tPayload(t: Token),
\t@tombstone Tombstone,
}
"""


def token_helpers_fn_arg() -> str:
    # `sink(t: Token)` consumes the Token by value.  Token's destructor
    # fires at sink's scope end before sink returns; the caller then
    # observes `sess.drops == 1`.
    return """
fn sink(t: Token) nothrow -> Void {
\t// consume by value; Token destructor fires at this fn's scope end.
\treturn;
}
"""


def token_helpers_return_value() -> str:
    # Per-shape producers.  Parameter `sess: &mut Session` is already a
    # reference, so `make_token(sess)` passes it through without adding
    # another `&mut`.
    out: list[str] = []
    for shape in TOKEN_SHAPES:
        decls, access = token_access(shape, sess_expr="sess")
        out.append(
            f"""
fn produce_{shape}(sess: &mut Session) nothrow -> Token {{
{decls}\treturn {access};
}}
"""
        )
    return "".join(out)


TOKEN_SITE_HELPERS: dict[str, "object"] = {
    "fn_arg": token_helpers_fn_arg,
    "return_value": token_helpers_return_value,
}


# KNOWN MATRIX GAPS — per-(site, type) fixture-level skips.
# Some (site, type) combos trip pre-existing compiler bugs unrelated
# to the ownership-transfer-drop regression the matrix is primarily
# guarding.  The generator elides the whole fixture for these combos
# and tracks them below so the omission is visible and auditable.
#
# (site, ty_name) -> short reason / follow-up handle.
KNOWN_SKIP_COMBOS: dict[tuple[str, str], str] = {
    # ("array_literal", "diag_entry"): fixed in 0.27.193 — ArrayLit
    # lowering now emits CopyValue + paired DropValue (when the source
    # is an owned rvalue temp) for Copy non-bitcopy struct elements,
    # mirroring the push/insert/set/extend path's _ensure_array_elem_copy
    # treatment.  Re-enabled.
    # ("array_insert", "diag_entry"): fixed in 0.27.193 — UnboundLocalError
    # in call_resolver.py was an OK-path early-return that referenced an
    # `info` symbol built only at the success exit.  Re-enabled.
}

# --------------------------------------------------------------------
# FIXTURE ASSEMBLY
# --------------------------------------------------------------------


_HEADER = """\
// Auto-generated by lang/tests/codegen/e2e/__ownership_matrix__/_gen.py.
// DO NOT EDIT MANUALLY.  To regenerate:
//     just ownership-matrix-gen
// (or:  PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/_gen.py)
//
// Site    = {site}
// Type    = {ty_name}
{flavor_line}//
{shape_line}// Fails under DRIFT_ASAN or DRIFT_MEMCHECK on any ownership gap in
// the generated site.

module m;
"""


_SHAPE_LINE_COPY = (
    "// Exercises all three value shapes (HVar local / HCall rvalue /\n"
    "// projected place) that `_lower_call_arg` (or its ownership-transfer\n"
    "// peers) classify distinctly.\n"
    "//\n"
)


_SHAPE_LINE_TOKEN = (
    "// Exercises both Token value shapes (hvar_move via explicit\n"
    "// `move tok`, and hcall_rvalue via `make_token(&mut sess)`\n"
    "// directly); projection is deferred for non-Copy types.  Every\n"
    "// scenario observes `sess.drops` via an `&mut Session` side\n"
    "// channel to assert the Token is destroyed exactly once.\n"
    "//\n"
)


def _flavor_line(flavor: str | None) -> str:
    if flavor is None:
        return ""
    return f"// Flavor  = {flavor}\n"


_HOLDER_DECL = """\

pub struct Holder<T> {
	pub field: T,
}
"""


_BAG_DECL = """\
pub struct Bag<T> {
	pub v: T,
}
"""


_MSG_DECL = """\
pub variant Msg<T> {
	Payload(v: T),
	@tombstone Tombstone,
}
"""


def render_fixture(site: str, ty_name: str, ty_info: dict, flavor: str | None) -> str:
    if ty_name == "token":
        return _render_fixture_token(site, ty_info)
    if site == "match_bind":
        return _render_fixture_match_bind(ty_name, ty_info, flavor)
    lines: list[str] = []
    lines.append(
        _HEADER.format(
            site=site,
            ty_name=ty_name,
            flavor_line=_flavor_line(flavor),
            shape_line=_SHAPE_LINE_COPY,
        )
    )
    if ty_info.get("needs_import_core") or site in ("result_ok", "result_err") or ty_name == "string":
        lines.append("import std.core as core;\n")
    # Struct/variant decls.  Conditional on sites that need them.
    lines.append(_HOLDER_DECL)
    if site in ("struct_ctor",):
        lines.append(_BAG_DECL)
    if site in ("variant_ctor",):
        lines.append(_MSG_DECL)
    lines.append("\n")

    # Top-level helpers (sink fn for fn_arg, produce_X fns for
    # return_value, etc.) emitted before the scenario fns so they
    # are in scope.
    helpers_emit = SITE_HELPERS.get(site)
    if helpers_emit is not None:
        lines.append(helpers_emit(ty_info, flavor))

    emit_body = SITES[site]
    scenarios: list[str] = []
    for shape_name in SHAPES.keys():
        fn_name = f"scenario_{shape_name}"
        body = emit_body(shape_name, ty_info, flavor)
        scenarios.append(
            f"fn {fn_name}() nothrow -> Int {{{body}\treturn 0;\n}}\n"
        )
    lines.extend(scenarios)

    # Main dispatcher.
    main_body = []
    for idx, shape_name in enumerate(SHAPES.keys(), start=1):
        offset = idx * 100
        main_body.append(
            f"\tval r{idx} = scenario_{shape_name}();\n"
            f"\tif r{idx} != 0 {{ return {offset} + r{idx}; }}\n"
        )
    lines.append(
        "\npub fn main() nothrow -> Int {\n"
        + "".join(main_body)
        + "\treturn 0;\n"
        + "}\n"
    )
    return "".join(lines)


def _render_fixture_token(site: str, ty_info: dict) -> str:
    if site == "match_bind":
        return _render_fixture_token_match_bind()
    lines: list[str] = []
    lines.append(
        _HEADER.format(
            site=site,
            ty_name="token",
            flavor_line="",
            shape_line=_SHAPE_LINE_TOKEN,
        )
    )
    lines.append("import std.core as core;\n")
    lines.append(TOKEN_PREAMBLE)
    if site == "struct_ctor":
        lines.append(_TOKEN_BAG_DECL)
    if site == "variant_ctor":
        lines.append(_TOKEN_MSG_DECL)
    lines.append("\n")

    helpers_emit = TOKEN_SITE_HELPERS.get(site)
    if helpers_emit is not None:
        lines.append(helpers_emit())

    emit_body = TOKEN_SITES[site]
    scenarios: list[str] = []
    for shape_name in TOKEN_SHAPES:
        fn_name = f"scenario_{shape_name}"
        body = token_scenario_wrap(emit_body(shape_name))
        scenarios.append(
            f"fn {fn_name}() nothrow -> Int {{{body}\treturn 0;\n}}\n"
        )
    lines.extend(scenarios)

    main_body = []
    for idx, shape_name in enumerate(TOKEN_SHAPES, start=1):
        offset = idx * 100
        main_body.append(
            f"\tval r{idx} = scenario_{shape_name}();\n"
            f"\tif r{idx} != 0 {{ return {offset} + r{idx}; }}\n"
        )
    lines.append(
        "\npub fn main() nothrow -> Int {\n"
        + "".join(main_body)
        + "\treturn 0;\n"
        + "}\n"
    )
    return "".join(lines)


def fixture_name(site: str, ty_name: str, flavor: str | None) -> str:
    if flavor is None:
        return f"om_{site}_{ty_name}"
    return f"om_{site}_{ty_name}_{flavor}"


def render_expected(site: str, ty_name: str, flavor: str | None) -> str:
    flavor_suffix = f", flavor={flavor}" if flavor else ""
    if ty_name == "token":
        desc = (
            f"Auto-generated ownership-transfer regression: site={site}, "
            f"type=token (non-Copy, core.Destructible).  Token carries an "
            f"observable `&mut Session {{ drops: Int }}` side channel; every "
            f"destroy() increments sess.drops.  Each scenario asserts "
            f"sess.drops == 0 while the token is in flight through the "
            f"transfer site, then sess.drops == 1 after the destination "
            f"container goes out of scope.  Shapes: hvar_move (`move tok`) "
            f"and hcall_rvalue (`make_token(&mut sess)` directly).  Must "
            f"pass plain + ASAN + memcheck."
        )
        return json.dumps({"description": desc, "exit_code": 0}, indent=2) + "\n"
    if site == "array_extend":
        # Honest accounting: extend's transfer site is borrow-of-array;
        # the value shapes only vary the upstream setup (one push of a
        # heap value into src_arr) before extend runs.
        shape_note = (
            "Value shapes vary the upstream setup (single push into "
            "src_arr) only — extend's transfer site is always "
            "`dest.extend(&src_arr)` with src_arr a borrowed local."
        )
    elif site == "extend_source":
        # Honest accounting: extend_source varies the source-array
        # shape at the extend call site, but direct borrow-of-rvalue
        # is not legal today — the HCall-rvalue scenario binds the
        # call result to a local first and then extends from
        # `&src_arr` (effectively the same borrow shape as the HVar
        # scenario).  The projection scenario DOES vary the actual
        # call shape: `dest.extend(&bag.items)` borrows a projected
        # struct field rather than a local.  The matrix value from
        # this site is the projection cell; HVar and HCall differ
        # only in upstream populate.
        shape_note = (
            "Shape axis varies the source-array expression at the "
            "extend call site: HVar local (`&src_arr`), HCall rvalue "
            "BOUND TO A LOCAL first then `&src_arr` (because direct "
            "borrow-of-rvalue is rejected by the borrow checker "
            "today), and projected field (`&bag.items`).  The "
            "projection case is the only scenario that varies the "
            "actual call-site borrow shape; HVar and HCall rvalue "
            "differ only in their upstream populate path."
        )
    else:
        shape_note = (
            "Exercises HVar local, HCall rvalue, and projected-place value "
            "shapes through the named transfer site."
        )
    desc = (
        f"Auto-generated ownership-transfer regression: site={site}, "
        f"type={ty_name}{flavor_suffix}. {shape_note} "
        f"Post-transfer integrity checked via byte_length / key.byte_length / "
        f"value comparison. Must pass plain + ASAN; memcheck applies to "
        f"heap-backed string subset."
    )
    return json.dumps({"description": desc, "exit_code": 0}, indent=2) + "\n"


def emit_all(root: Path) -> tuple[list[Path], list[tuple[str, str]]]:
    written: list[Path] = []
    skipped: list[tuple[str, str]] = []
    for site in SITES:
        for ty_name, ty_info in TYPE_INFO.items():
            if (site, ty_name) in KNOWN_SKIP_COMBOS:
                skipped.append((site, ty_name))
                continue
            # Token is restricted to the move/drop-sensitive sites.
            # Copy-only sites (array_literal, for_loop_bind, extend_*,
            # result_err) are elided for Token.
            if ty_name == "token" and site not in TOKEN_SITES:
                continue
            # match_bind skips bitcopy types — Int has no ownership
            # story to exercise (trivial drop, no retain/release path).
            if site == "match_bind" and ty_info.get("is_bitcopy"):
                continue
            for flavor in ty_info["flavors"]:
                fname = fixture_name(site, ty_name, flavor)
                dirpath = root / fname
                dirpath.mkdir(parents=True, exist_ok=True)
                main_drift = render_fixture(site, ty_name, ty_info, flavor)
                expected = render_expected(site, ty_name, flavor)
                (dirpath / "main.drift").write_text(main_drift, encoding="utf-8")
                (dirpath / "expected.json").write_text(expected, encoding="utf-8")
                written.append(dirpath)
    return written, skipped


def main(argv: list[str]) -> int:
    out_root = FIXTURES_ROOT
    check_only = "--check" in argv
    if check_only:
        # Compare to existing and diff; exit non-zero on drift.
        expected: dict[str, str] = {}
        # Walk generator output in memory
        for site in SITES:
            for ty_name, ty_info in TYPE_INFO.items():
                if (site, ty_name) in KNOWN_SKIP_COMBOS:
                    continue
                if ty_name == "token" and site not in TOKEN_SITES:
                    continue
                if site == "match_bind" and ty_info.get("is_bitcopy"):
                    continue
                for flavor in ty_info["flavors"]:
                    fname = fixture_name(site, ty_name, flavor)
                    expected[f"{fname}/main.drift"] = render_fixture(site, ty_name, ty_info, flavor)
                    expected[f"{fname}/expected.json"] = render_expected(site, ty_name, flavor)
        drift_found = []
        for rel, content in expected.items():
            path = out_root / rel
            if not path.exists():
                drift_found.append(f"missing: {rel}")
                continue
            if path.read_text(encoding="utf-8") != content:
                drift_found.append(f"changed: {rel}")
        # Also flag stray om_* fixtures that the generator no longer emits
        # (e.g. left behind after a KNOWN_SKIP_COMBOS addition).
        expected_dirs = {rel.split("/", 1)[0] for rel in expected}
        for child in out_root.iterdir():
            if child.is_dir() and child.name.startswith("om_") and child.name not in expected_dirs:
                drift_found.append(f"stale: {child.name}")
        if drift_found:
            print("ownership_matrix is out of date. Run:", file=sys.stderr)
            print("  PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/_gen.py", file=sys.stderr)
            for msg in drift_found[:20]:
                print(f"  {msg}", file=sys.stderr)
            return 1
        print(f"ownership_matrix check: {len(expected) // 2} fixtures up to date")
        return 0
    written, skipped = emit_all(out_root)
    print(f"ownership_matrix: wrote {len(written)} fixtures under {out_root}")
    if skipped:
        print(f"ownership_matrix: skipped {len(skipped)} (site, type) combos due to known pre-existing compiler bugs:")
        for site, ty_name in skipped:
            reason = KNOWN_SKIP_COMBOS[(site, ty_name)]
            print(f"  - ({site}, {ty_name}): {reason.splitlines()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
