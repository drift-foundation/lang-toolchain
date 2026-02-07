# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.types_core import TypeKind
from lang.driftc.stage2 import mir_nodes as M


def test_optional_ops_removed_from_mir() -> None:
	"""Optional-specific MIR ops should not exist anymore."""
	assert not hasattr(M, "OptionalIsSome")
	assert not hasattr(M, "OptionalValue")


def test_typekind_has_no_optional() -> None:
	"""TypeKind should not carry an OPTIONAL variant."""
	assert "OPTIONAL" not in {kind.name for kind in TypeKind}
