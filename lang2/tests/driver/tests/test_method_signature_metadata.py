from __future__ import annotations

from pathlib import Path

from lang2.driftc.parser import parse_drift_files_to_hir, parse_drift_workspace_to_hir


def _write(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text)


def test_method_signatures_have_receiver_metadata_workspace_and_single_file(tmp_path: Path) -> None:
	"""
	Lock the production invariant: method signatures always carry receiver metadata.

	Stage2 lowering has a legacy backstop for signatures missing impl_target_type_id,
	but the real compiler pipeline must not rely on it.
	"""
	src = tmp_path / "m" / "mod.drift"
	_write(
		src,
		"""
module m

struct Point(x: Int, y: Int)

implement Point {
	fn move_by(self: &mut Point, dx: Int, dy: Int) returns Void {
		self->x += dx
		self->y += dy
	}
}

fn main() returns Int {
	return 0
}
""".lstrip(),
	)

	# Workspace path.
	_hirs, sigs, _fn_ids_by_name, _tt, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir([src], module_paths=[tmp_path])
	assert not diags
	methods = [s for s in sigs.values() if getattr(s, "is_method", False)]
	assert methods
	for sig in methods:
		assert sig.impl_target_type_id is not None
		assert sig.self_mode is not None

	# Single-file path.
	_hirs2, sigs2, _fn_ids_by_name2, _tt2, _exc2, diags2 = parse_drift_files_to_hir([src])
	assert not diags2
	methods2 = [s for s in sigs2.values() if getattr(s, "is_method", False)]
	assert methods2
	for sig in methods2:
		assert sig.impl_target_type_id is not None
		assert sig.self_mode is not None
