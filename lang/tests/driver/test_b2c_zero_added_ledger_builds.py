# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B2+C S7 — zero-added-ledger-builds proof (instrumented build-count gate).

The B2+C architecture promise: the frozen decision plan reuses ledger A
(built once, `driftc.rebuild_after_cleanup_authoring`) and every emitter
consumes the frozen plan — NO B2+C consumer (`build_destructible_plan`,
`insert_string_arc`, `emit_return_cleanups`, `insert_overwrite_cleanup`)
forces an intermediate ledger rebuild, and the build-reason population
is EXACTLY the pre-B2+C fixed set:

    driftc.initial_build
    driftc.rebuild_after_match_cleanup_authoring
    driftc.rebuild_after_drop_flags
    driftc.rebuild_after_cleanup_authoring        (= ledger A)
    cleanup_authoring.in_pass_rebuild             (pre-existing internal)

(`drop_flags` keeps its pre-existing INTERNAL direct build — counted
separately below; it predates B2+C and is not a plan consumer.)  The
only B2+C-era addition is the audit-gated deferred `l_post` build,
owned by the DRIVER's finalize loop — exactly ONE per planned function,
never inside a consumer, absent entirely when the audit env is off.

Instrumentation covers EVERY bound build path in the real compile —
`ownership_ledger.build_ledger` (source; also what the driver's
runtime-local l_post import resolves to), `ledger_cache.build_ledger`
(all `build_and_attach_ledger` traffic), and the PRE-BOUND module
aliases `drop_flags.build_ledger` and
`cleanup_authoring.build_and_attach_ledger` (bound at their module
import, so patching only the source modules would miss them).  The gate
asserts reason-set EQUALITY (both directions), full raw-build
attribution (raw == attach + drop_flags-internal + expected l_post; any
other direct build fails), the audit-on/audit-off count relationship,
and zero build delta inside each wrapped consumer.  Negative teeth prove
the assertion helpers fail on a missing frozen reason, an extra reason,
an extra out-of-consumer raw build, and a missing l_post.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest


# The frozen pre-B2+C build-reason set (4 driver-level builds + cleanup_
# authoring's pre-existing in-pass rebuild, K-review 2026-05-15).  A NEW
# reason OR a missing reason on the reference fixture is an S7 violation.
_FROZEN_REASONS = {
	"driftc.initial_build",
	"driftc.rebuild_after_match_cleanup_authoring",
	"driftc.rebuild_after_drop_flags",
	"driftc.rebuild_after_cleanup_authoring",
	"cleanup_authoring.in_pass_rebuild",
}


def _check_reason_set(reasons, expected) -> None:
	"""EQUALITY check, both directions: a reason outside `expected` means
	a new rebuild was introduced; a missing expected reason means an
	expected build path silently disappeared (or the fixture stopped
	exercising it) — both fail."""
	got = set(reasons)
	extra = got - set(expected)
	missing = set(expected) - got
	assert not extra, (
		f"build-reasons outside the expected set: {sorted(extra)} "
		f"(a new ledger rebuild was introduced)"
	)
	assert not missing, (
		f"expected build-reasons never recorded: {sorted(missing)} "
		f"(an expected build path vanished or the fixture no longer "
		f"exercises it — the S7 accounting is broken either way)"
	)


def _check_attribution(raw, attach_count, df_internal, expected_lpost) -> None:
	"""Every raw ledger build must be attributable: build_and_attach
	traffic + drop_flags' internal direct build + exactly the expected
	number of driver-finalize l_post builds.  A surplus means a direct
	out-of-consumer build escaped the reason accounting; a deficit means
	an expected build (e.g. an l_post) went missing."""
	unattributed = raw - attach_count - df_internal
	assert unattributed == expected_lpost, (
		f"raw ledger-build attribution mismatch: raw={raw}, "
		f"attach={attach_count}, drop_flags_internal={df_internal} → "
		f"unattributed={unattributed}, expected l_post builds="
		f"{expected_lpost} (surplus = a direct build outside the frozen "
		f"paths; deficit = an expected build vanished)"
	)


def _instrument(monkeypatch):
	"""Install the build-path instrumentation ONCE (stacking a second
	layer would leave the first run's wrappers in the chain and corrupt
	both runs' counts).  Returns (state, reset) — call `reset()` between
	compiles to reuse the same instrumented process."""
	from lang.driftc import driftc as D
	from lang.driftc.stage2 import cleanup_authoring as CA
	from lang.driftc.stage2 import drop_flags as DF
	from lang.driftc.stage2 import ledger_cache as LC
	from lang.driftc.stage2 import ownership_ledger as OL
	from lang.driftc.stage2 import overwrite_cleanup as OC
	from lang.driftc.stage2 import return_cleanup_emitter as RCE

	state = {
		"raw": 0,               # every build through ANY bound path
		"df_internal": 0,       # drop_flags' pre-existing direct build
		"reasons": [],          # every build_and_attach_ledger reason
		"consumer_deltas": {},  # per-B2+C-consumer raw-build delta
		"planned_fns": 0,       # build_destructible_plan invocations
	}

	def _reset():
		state["raw"] = 0
		state["df_internal"] = 0
		state["reasons"] = []
		state["consumer_deltas"] = {}
		state["planned_fns"] = 0

	_real_ol_build = OL.build_ledger

	def _counting_build(func, **kw):
		state["raw"] += 1
		return _real_ol_build(func, **kw)

	def _counting_build_df(func, **kw):
		state["raw"] += 1
		state["df_internal"] += 1
		return _real_ol_build(func, **kw)

	# Source module (fresh runtime imports, e.g. the driver's l_post),
	# ledger_cache's module-top alias (all build_and_attach traffic), AND
	# the PRE-BOUND drop_flags alias.
	monkeypatch.setattr(OL, "build_ledger", _counting_build)
	monkeypatch.setattr(LC, "build_ledger", _counting_build)
	monkeypatch.setattr(DF, "build_ledger", _counting_build_df)

	_real_attach = LC.build_and_attach_ledger

	def _recording_attach(func, *, drop_policy, reason="fresh-build"):
		state["reasons"].append(reason)
		return _real_attach(func, drop_policy=drop_policy, reason=reason)

	# Both bindings: ledger_cache itself (driver's runtime-local import)
	# AND cleanup_authoring's PRE-BOUND module-top alias.
	monkeypatch.setattr(LC, "build_and_attach_ledger", _recording_attach)
	monkeypatch.setattr(CA, "build_and_attach_ledger", _recording_attach)

	def _zero_delta(name, real, *, count_planned=False):
		def _wrapped(*a, **kw):
			if count_planned:
				state["planned_fns"] += 1
			before = state["raw"]
			out = real(*a, **kw)
			delta = state["raw"] - before
			state["consumer_deltas"][name] = (
				state["consumer_deltas"].get(name, 0) + delta
			)
			return out
		return _wrapped

	monkeypatch.setattr(
		D, "build_destructible_plan",
		_zero_delta("build_destructible_plan", D.build_destructible_plan,
			count_planned=True))
	monkeypatch.setattr(
		D, "insert_string_arc",
		_zero_delta("insert_string_arc", D.insert_string_arc))
	monkeypatch.setattr(
		RCE, "emit_return_cleanups",
		_zero_delta("emit_return_cleanups", RCE.emit_return_cleanups))
	monkeypatch.setattr(
		OC, "insert_overwrite_cleanup",
		_zero_delta("insert_overwrite_cleanup", OC.insert_overwrite_cleanup))

	return state, _reset


def _compile_fixture(tmp_path, monkeypatch, *, audit: bool):
	"""Compile the reference fixture through the REAL driver (the
	instrumentation must already be installed via `_instrument`)."""
	from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc import driftc as D
	from lang.driftc.core.function_id import function_symbol

	if audit:
		monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
		monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
	else:
		monkeypatch.delenv("DRIFT_STRING_ARC_AUDIT", raising=False)

	src = tmp_path / "main.drift"
	# Strings + a conditional so drop_flags / cleanup_authoring / the
	# String passes all genuinely run on the fixture.
	src.write_text(
		"module main;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar s = \"a\";\n"
		"\ts = \"b\";\n"
		"\tvar t = s + \"c\";\n"
		"\tif t.byte_length() > 0 { return 0; }\n"
		"\treturn 1;\n"
		"}\n"
	)
	modules, type_table, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True
	)
	assert not pdiags, [d.message for d in pdiags]
	func_hirs, signatures, _ = flatten_modules(modules)
	main_id = [i for i, s in signatures.items() if i.name == "main" and not s.is_method][0]
	origin = {}
	for m in modules.values():
		origin.update(m.origin_by_fn_id)
	ir, checked = D.compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc,
		entry=function_symbol(main_id),
		type_table=type_table,
		module_exports=mexp,
		module_deps=mdeps,
		origin_by_fn_id=origin,
		enforce_entrypoint=True,
		reserved_namespace_policy=D.ReservedNamespacePolicy.ALLOW_DEV,
	)
	return ir, checked


def _assert_clean_compile_and_consumers(ir, checked, state) -> None:
	errors = [d for d in getattr(checked, "diagnostics", [])
	          if getattr(d, "severity", None) == "error"]
	assert not errors, [d.message for d in errors]
	assert ir, "expected a successful compile"
	assert state["raw"] > 0, "instrumentation did not observe any ledger build"
	assert state["planned_fns"] > 0, "build_destructible_plan never ran"
	deltas = state["consumer_deltas"]
	assert set(deltas) == {
		"build_destructible_plan", "insert_string_arc",
		"emit_return_cleanups", "insert_overwrite_cleanup",
	}, f"a B2+C consumer never ran: {deltas}"
	offenders = {k: v for k, v in deltas.items() if v != 0}
	assert not offenders, (
		f"B2+C consumers forced intermediate ledger builds: {offenders} "
		f"(the frozen plan + ledger A must satisfy every consumer)"
	)


def test_zero_added_ledger_builds_audit_off_vs_on(tmp_path, monkeypatch) -> None:
	"""The full S7 proof on one fixture, both audit modes, with the real
	count relationship between them:

	  * audit OFF: reason set == the frozen set (equality), and EVERY raw
	    build is attributed (attach + drop_flags internal, ZERO l_post,
	    zero unattributed) — no B2+C-era direct build exists;
	  * audit ON: same reason multiset, same drop_flags count, consumers
	    still zero-delta, and EXACTLY ONE additional raw build per
	    planned function (the driver-finalize l_post) — nowhere else;
	  * cross-mode: raw_on == raw_off + planned_fns."""
	state, reset = _instrument(monkeypatch)

	ir_off, checked_off = _compile_fixture(tmp_path, monkeypatch, audit=False)
	off = {k: (dict(v) if isinstance(v, dict) else
		(list(v) if isinstance(v, list) else v)) for k, v in state.items()}
	_assert_clean_compile_and_consumers(ir_off, checked_off, off)
	_check_reason_set(off["reasons"], _FROZEN_REASONS)
	_check_attribution(
		off["raw"], len(off["reasons"]), off["df_internal"], expected_lpost=0)

	reset()
	ir_on, checked_on = _compile_fixture(tmp_path, monkeypatch, audit=True)
	on = {k: (dict(v) if isinstance(v, dict) else
		(list(v) if isinstance(v, list) else v)) for k, v in state.items()}
	_assert_clean_compile_and_consumers(ir_on, checked_on, on)
	_check_reason_set(on["reasons"], _FROZEN_REASONS)
	_check_attribution(
		on["raw"], len(on["reasons"]), on["df_internal"],
		expected_lpost=on["planned_fns"])

	# Cross-mode relationship: the audit adds EXACTLY the per-fn l_post
	# builds and nothing else — same attach-reason multiset, same
	# drop_flags internal count, same planned-fn population.
	assert Counter(on["reasons"]) == Counter(off["reasons"]), (
		"audit mode changed the build_and_attach reason population"
	)
	assert on["df_internal"] == off["df_internal"]
	assert on["planned_fns"] == off["planned_fns"]
	assert on["raw"] == off["raw"] + on["planned_fns"], (
		f"audit-on raw builds ({on['raw']}) != audit-off ({off['raw']}) + "
		f"one l_post per planned fn ({on['planned_fns']})"
	)


# ── negative teeth: the assertion helpers actually bite ───────────────


def test_reason_set_check_fails_on_missing_frozen_reason() -> None:
	present = sorted(_FROZEN_REASONS)[:-1]  # drop one expected reason
	with pytest.raises(AssertionError, match="never recorded"):
		_check_reason_set(present, _FROZEN_REASONS)


def test_reason_set_check_fails_on_extra_reason() -> None:
	with pytest.raises(AssertionError, match="outside the expected set"):
		_check_reason_set(
			list(_FROZEN_REASONS) + ["driftc.sneaky_new_rebuild"],
			_FROZEN_REASONS)


def test_attribution_check_fails_on_extra_out_of_consumer_build() -> None:
	# 10 raw, 7 attach, 2 drop_flags, 0 expected l_post → 1 stray build.
	with pytest.raises(AssertionError, match="attribution mismatch"):
		_check_attribution(10, 7, 2, expected_lpost=0)


def test_attribution_check_fails_on_missing_lpost_build() -> None:
	# audit-on expected 3 l_post builds but only 2 raw remain unattributed.
	with pytest.raises(AssertionError, match="attribution mismatch"):
		_check_attribution(11, 7, 2, expected_lpost=3)


def test_b2c_modules_cannot_build_ledgers_source_pin() -> None:
	"""Source pin over the COMPLETE B2+C plan-window/consumer surface —
	including string_arc (a named consumer) and the R8 plan-window module:
	none of them may even NAME a ledger-build entry point, so a
	consumer-side rebuild cannot be reintroduced without tripping this
	pin (even through a pre-bound alias the runtime counter would miss)."""
	import lang.driftc.stage2.cleanup_plan as m1
	import lang.driftc.stage2.cleanup_payloads as m2
	import lang.driftc.stage2.destructible_authority as m3
	import lang.driftc.stage2.destructible_planner as m4
	import lang.driftc.stage2.return_cleanup_emitter as m5
	import lang.driftc.stage2.overwrite_cleanup as m6
	import lang.driftc.stage2.string_arc as m7
	import lang.driftc.stage2.string_ownership_analysis as m8
	for mod in (m1, m2, m3, m4, m5, m6, m7, m8):
		src = Path(mod.__file__).read_text()
		for needle in ("build_ledger", "build_and_attach_ledger"):
			assert needle not in src, (
				f"{Path(mod.__file__).name} names {needle!r} — B2+C "
				f"plan-window/consumer modules must consume the frozen "
				f"plan / ledger A, never build ledgers"
			)
