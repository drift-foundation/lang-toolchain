# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.checker import FnSignature
from lang.driftc.packages.provisional_dmir_v0 import encode_signatures


def _sig(name: str) -> FnSignature:
	return FnSignature(
		name=name,
		param_type_ids=[],
		return_type_id=None,
		declared_can_throw=False,
	)


def test_encode_signatures_emits_all_entries_deterministically() -> None:
	keys = ["m::b", "m::a", "m::c"]
	sigs_a = {keys[0]: _sig("b"), keys[1]: _sig("a"), keys[2]: _sig("c")}
	sigs_b = {keys[2]: _sig("c"), keys[0]: _sig("b"), keys[1]: _sig("a")}

	out_a = encode_signatures(sigs_a, module_id="m")
	out_b = encode_signatures(sigs_b, module_id="m")

	assert list(out_a.keys()) == sorted(keys)
	assert out_a == out_b
