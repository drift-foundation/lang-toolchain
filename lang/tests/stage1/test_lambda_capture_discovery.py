# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.span import Span
from lang.driftc.stage1 import closures as C
from lang.driftc.stage1.capture_discovery import discover_captures
import lang.driftc.stage1.hir_nodes as H


def test_capture_discovery_collects_outer_locals_and_fields() -> None:
	outer_a = H.HVar(name="a", binding_id=1)
	outer_s_field = H.HField(subject=H.HVar(name="s", binding_id=2), name="f")
	lam = H.HLambda(
		params=[H.HParam(name="x", binding_id=10)],
		body_expr=H.HBinary(op=H.BinaryOp.ADD, left=outer_a, right=outer_s_field),
		body_block=None,
	)
	res = discover_captures(lam)
	keys = [c.key for c in res.captures]
	assert keys == [
		C.HCaptureKey(root_local=1, proj=()),
		C.HCaptureKey(root_local=2, proj=(C.HCaptureProj(field="f"),)),
	]
	assert [c.kind for c in res.captures] == [
		C.HCaptureKind.REF,
		C.HCaptureKind.REF,
	]
	assert res.diagnostics == []


def test_capture_discovery_rejects_index_projection() -> None:
	place = H.HPlaceExpr(
		base=H.HVar(name="arr", binding_id=3),
		projections=[H.HPlaceIndex(index=H.HLiteralInt(0))],
		loc=Span(),
	)
	lam = H.HLambda(params=[], body_expr=H.HBorrow(subject=place, is_mut=False), body_block=None)
	res = discover_captures(lam)
	assert any("field projections only" in d.message for d in res.diagnostics)
	assert res.captures == []


def test_capture_discovery_infers_kinds() -> None:
	outer_a = H.HVar(name="a", binding_id=1)
	outer_b_place = H.HPlaceExpr(base=H.HVar(name="b", binding_id=2), projections=[])
	outer_c_place = H.HPlaceExpr(base=H.HVar(name="c", binding_id=3), projections=[])
	outer_d_place = H.HPlaceExpr(base=H.HVar(name="d", binding_id=4), projections=[])
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=outer_a),
			H.HExprStmt(expr=H.HBorrow(subject=outer_b_place, is_mut=False)),
			H.HExprStmt(expr=H.HBorrow(subject=outer_c_place, is_mut=True)),
			H.HExprStmt(expr=H.HMove(subject=outer_d_place)),
		]
	)
	lam = H.HLambda(params=[], body_expr=None, body_block=block)
	res = discover_captures(lam)
	kinds = {c.key.root_local: c.kind for c in res.captures}
	assert kinds[1] is C.HCaptureKind.REF
	assert kinds[2] is C.HCaptureKind.REF
	assert kinds[3] is C.HCaptureKind.REF_MUT
	assert kinds[4] is C.HCaptureKind.MOVE
	assert res.diagnostics == []


def test_capture_discovery_rejects_overlapping_mutable_captures() -> None:
	outer_s = H.HPlaceExpr(base=H.HVar(name="s", binding_id=5), projections=[])
	outer_s_field = H.HPlaceExpr(
		base=H.HVar(name="s", binding_id=5),
		projections=[H.HPlaceField(name="f")],
	)
	block = H.HBlock(
		statements=[
			H.HExprStmt(expr=H.HBorrow(subject=outer_s, is_mut=True)),
			H.HExprStmt(expr=H.HBorrow(subject=outer_s_field, is_mut=False)),
		]
	)
	lam = H.HLambda(params=[], body_expr=None, body_block=block)
	res = discover_captures(lam)
	assert any("overlapping lambda captures" in d.message for d in res.diagnostics)


def test_capture_discovery_explicit_list_missing_capture_rejected() -> None:
	lam = H.HLambda(
		params=[],
		explicit_captures=[
			H.HExplicitCapture(name="a", kind="copy", binding_id=1, span=Span()),
		],
		body_expr=H.HBinary(
			op=H.BinaryOp.ADD,
			left=H.HVar(name="a", binding_id=1),
			right=H.HVar(name="b", binding_id=2),
		),
		body_block=None,
	)
	res = discover_captures(lam)
	assert any("not listed in captures" in d.message for d in res.diagnostics)
	assert [c.kind for c in res.captures] == [C.HCaptureKind.COPY]


def test_capture_discovery_rejects_implicit_projected_move_for_boxed_callback() -> None:
	"""Regression: a boxed-callback lambda (`capture_as_move=True`, set by
	`call_resolver.py` when wrapping a lambda as `core.callbackN`/
	`callback_throwN`) that reads a struct FIELD of an outer local — with no
	explicit `move`/`captures(...)` clause at all — must be REJECTED, not
	silently accepted as a MOVE-kind projected capture. `capture_as_move`
	defaults a plain field READ to MOVE (see the `elif use.read` branch),
	which used to bypass the "move captures of projections" rejection
	entirely because that check only looked at `use.move` (set only by an
	explicit `move` expression), never at the FINAL decided `kind`. MIR
	lowering's projected-capture branch (hir_to_mir.py `cap.key.proj`) only
	knows how to copy-read the projection into the env, not move-and-zero-
	back the source field, so silently accepting one lets the source
	struct's later drop re-drop the same field -> use-after-free (confirmed
	via ASAN: heap-use-after-free in drift_string_release, freed by a
	callback env drop thunk, on a struct field moved into a boxed callback
	inside another boxed callback's body then spawned onto a VT).

	This is an intentionally conservative, blanket rejection — it also
	rejects a Copy-typed projected field (e.g. `p.count: Int`) where the
	underlying copy-read would actually be safe. Narrowing that is a real
	lowering feature (the lambda-body prologue has no support for binding a
	projected capture key as distinct from its root local), deferred as a
	separate follow-up — see
	work/callback-env-uaf-ref-args/projected-copy-captures-followup.md.
	Do not loosen this check without that follow-up landing first."""
	outer_p_execute = H.HField(subject=H.HVar(name="p", binding_id=1), name="execute")
	lam = H.HLambda(params=[], body_expr=outer_p_execute, body_block=None)
	lam.capture_as_move = True
	res = discover_captures(lam)
	assert any("projections are not supported yet" in d.message for d in res.diagnostics)
	assert res.captures == []


def test_capture_discovery_explicit_list_duplicate_rejected() -> None:
	lam = H.HLambda(
		params=[],
		explicit_captures=[
			H.HExplicitCapture(name="a", kind="ref", binding_id=1, span=Span()),
			H.HExplicitCapture(name="a", kind="ref_mut", binding_id=1, span=Span()),
		],
		body_expr=H.HVar(name="a", binding_id=1),
		body_block=None,
	)
	res = discover_captures(lam)
	assert any("duplicate capture" in d.message for d in res.diagnostics)
