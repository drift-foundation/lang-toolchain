# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import parser as p
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.core.types_core import TypeParamId
from lang.driftc.traits.world import build_trait_world


def test_trait_world_collects_traits_and_impls_with_qualified_heads() -> None:
	prog = p.parse_program(
		"""
trait Debug { fn fmt(self: Int) -> String }

implement Debug for a.Point { fn fmt(self: a.Point) -> String { return ""; } }
implement Debug for b.Point { fn fmt(self: b.Point) -> String { return ""; } }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	assert world.diagnostics == []
	assert len(world.traits) == 1
	assert len(world.impls) == 2
	assert len(world.impls_by_trait_target) == 2


def test_trait_world_reports_unknown_trait_in_require() -> None:
	prog = p.parse_program(
		"""
struct File require Self is Missing { }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	assert any("unknown trait" in d.message for d in world.diagnostics)


def test_trait_world_allows_unqualified_trait_in_struct_require() -> None:
	prog = p.parse_program(
		"""
trait Debug { fn fmt(self: Int) -> String }
struct File require Self is Debug { }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	assert world.diagnostics == []


def test_trait_world_rejects_overlapping_impls_for_same_head() -> None:
	prog = p.parse_program(
		"""
trait Debug { fn fmt(self: Int) -> String }

implement Debug for Box<T> { fn fmt(self: Box<T>) -> String { return ""; } }
implement Debug for Box<U> { fn fmt(self: Box<U>) -> String { return ""; } }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	assert any("overlapping impls" in d.message for d in world.diagnostics)


def test_trait_world_rejects_self_in_function_require() -> None:
	prog = p.parse_program(
		"""
trait Debug { fn fmt(self: Int) -> String }

fn use_file() -> Int require Self is Debug { return 0; }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	assert any("function require clause cannot use 'Self'" in d.message for d in world.diagnostics)


def test_trait_world_lowers_fn_require_subjects_to_typeparam_ids(tmp_path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
trait Debug { fn fmt(self: Int) -> String }

fn apply<T>(x: T) -> Int require T is Debug { return 0; }
"""
	)
	module, table, _excs, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	world = table.trait_worlds.get("main")
	assert world is not None
	fn_id = next(fid for fid in module.signatures_by_id.keys() if fid.name == "apply")
	req = world.requires_by_fn.get(fn_id)
	assert req is not None
	if hasattr(req, "subject"):
		assert isinstance(req.subject, TypeParamId)
	elif hasattr(req, "left"):
		assert isinstance(req.left.subject, TypeParamId)
