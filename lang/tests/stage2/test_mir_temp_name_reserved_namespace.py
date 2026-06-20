# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: MIR temporary names must not be expressible as source identifiers.

Root cause of the codegen CORE_BUG "phi with mixed incoming types {ptr,
drift.int}" (DriftQuery M3.3): `MirBuilder.new_temp()` produced bare `t<N>`
names (`t1`, `t2`, ...) which a user source variable could equal exactly, so
`func.local_types[name]` was written by BOTH the user local and a same-named
compiler temp; the temp's (often ref/ptr) type clobbered the user variable's
`Int` type, corrupting codegen at the SSA join.

The fix is NOT "prefix with `__`": the grammar does not actually reserve the
double-underscore namespace — `var __t1 = ...` parses today (and stdlib exports
`__test_*` hooks) — so `__t<N>` would merely move the collision to user
`__t<N>`. The robust fix is to mint temporaries with a character that the
grammar's identifier rule (`NAME`, `lang/driftc/parser/grammar.lark`, charset
`[A-Za-z0-9_]`) cannot produce, so NO source identifier — present or future —
can equal a compiler temp or any string embedding one.

This test pins that property directly and grammar-independently: every
`new_temp()` output must contain at least one character OUTSIDE `[A-Za-z0-9_]`,
which is a sufficient condition for "not a source-expressible identifier"
regardless of the keyword set or the `(?<!__)` lookbehind in the NAME rule.
"""

from __future__ import annotations

import re

from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.stage2.hir_to_mir import MirBuilder


# A source identifier is composed ENTIRELY of these characters (per the NAME
# token in grammar.lark: both alternatives draw only from `[A-Za-z0-9_]`). Any
# string containing a character outside this set therefore cannot be produced as
# a source identifier, no matter how the keyword/lookbehind rules evolve.
_SOURCE_IDENT_CHARS = re.compile(r"^[A-Za-z0-9_]+$")


def _builder() -> MirBuilder:
	fn_id = FunctionId(module="m", name="f", ordinal=0)
	return MirBuilder(function_symbol(fn_id), fn_id=fn_id)


def test_new_temp_is_not_a_source_expressible_identifier() -> None:
	b = _builder()
	names = [b.new_temp() for _ in range(200)]
	# Distinct.
	assert len(set(names)) == len(names)
	for nm in names:
		# The defining guarantee: a temp contains a non-source character, so no
		# source identifier (or any larger compiler name embedding the temp) can
		# ever equal it. This is what closes the `func.local_types` collision.
		assert not _SOURCE_IDENT_CHARS.match(nm), (
			f"new_temp produced {nm!r}, which is entirely within the source "
			f"identifier charset [A-Za-z0-9_] and could collide with a user local"
		)


def test_new_temp_uses_the_chosen_dot_marker() -> None:
	# Pin the concrete scheme too, so an accidental change to a still-safe-but-
	# different form is a deliberate, reviewed decision (the `.` is what the
	# codegen alloca-name and `_bb` hardening assume).
	b = _builder()
	for _ in range(50):
		nm = b.new_temp()
		assert nm.startswith(".t"), f"expected `.t<N>` temp form, got {nm!r}"
		assert "." in nm  # the non-source marker


def test_new_temp_cannot_collide_with_plausible_user_locals() -> None:
	# The collisions seen / feared in the field: user vars named t1, t2 (the
	# original bug), __t1, __t2 (the `__`-prefix near-miss), _t1 (single
	# underscore), and the higher-numbered forms the temp counter reaches in
	# large functions (the DriftQuery dump showed temp ids past t68 / t163).
	b = _builder()
	produced = {b.new_temp() for _ in range(400)}
	for user_name in ("t1", "t2", "t68", "t163", "__t1", "__t2", "_t1", "_t5", "t400"):
		assert user_name not in produced, (
			f"new_temp output collides with a plausible user local {user_name!r}"
		)
