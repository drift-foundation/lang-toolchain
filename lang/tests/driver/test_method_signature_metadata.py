from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_files_to_hir, parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules


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
module m;

struct Point(x: Int, y: Int);

implement Point {
	fn move_by(self: &mut Point, dx: Int, dy: Int) -> Void {
		self->x += dx;
		self->y += dy;
	}
}

fn main() nothrow -> Int{
	return 0;
}
""".lstrip(),
	)

	# Workspace path.
	_modules, _tt, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	_hirs, sigs, _fn_ids_by_name = flatten_modules(_modules)
	methods = [s for s in sigs.values() if getattr(s, "is_method", False)]
	assert methods
	for sig in methods:
		assert sig.impl_target_type_id is not None
		assert sig.self_mode is not None

	# Single-file path.
	module2, _tt2, _exc2, diags2 = parse_drift_files_to_hir([src])
	assert not diags2
	methods2 = [s for s in module2.signatures_by_id.values() if getattr(s, "is_method", False)]
	assert methods2
	for sig in methods2:
		assert sig.impl_target_type_id is not None
		assert sig.self_mode is not None
