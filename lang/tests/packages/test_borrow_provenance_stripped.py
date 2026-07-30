# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""D9 (reject-redundant-call-borrows): source-spelling provenance is a
SOURCE-only concept — canonical DMIR never encodes it.

Contract pinned here:
- `_to_jsonable` omits `HBorrow.source_written` and `HBorrow.policy_class`
  entirely (not even as `false`/`null`);
- `from_jsonable` reconstructs an `HBorrow` with the dataclass defaults
  (`source_written=False`, `policy_class=None`) — which is exactly why
  every pre-rule package (encoded before the fields existed) remains
  valid with no payload-version bump: decoded borrows are
  compiler-shaped and never subject to the redundant-borrow rule.
"""
from __future__ import annotations

from lang.driftc.packages.provisional_dmir_v0 import _to_jsonable, from_jsonable
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.core.span import Span


def _dataclasses_by_name() -> dict[str, type]:
	out: dict[str, type] = {}
	for v in vars(H).values():
		if isinstance(v, type) and hasattr(v, "__dataclass_fields__"):
			out[v.__name__] = v
	out["Span"] = Span
	return out


def _mk_borrow() -> H.HBorrow:
	place = H.HPlaceExpr(base=H.HVar(name="x"), projections=[], loc=Span())
	return H.HBorrow(subject=place, is_mut=True, source_written=True, policy_class="redundant", materialized_rvalue=True)


def test_encode_strips_provenance_fields() -> None:
	enc = _to_jsonable(_mk_borrow())
	assert enc["_type"] == "HBorrow"
	assert "source_written" not in enc
	assert "policy_class" not in enc
	assert "materialized_rvalue" not in enc
	assert enc["is_mut"] is True


def test_decode_defaults_keep_pre_rule_packages_valid() -> None:
	enc = _to_jsonable(_mk_borrow())
	back = from_jsonable(enc, dataclasses_by_name=_dataclasses_by_name(), enums_by_name={})
	assert isinstance(back, H.HBorrow)
	assert back.source_written is False
	assert back.policy_class is None
	assert back.materialized_rvalue is False
	assert back.is_mut is True


def test_decode_tolerates_legacy_payload_without_new_fields() -> None:
	"""A payload written by a pre-rule toolchain simply lacks the keys."""
	enc = _to_jsonable(_mk_borrow())
	legacy = {k: v for k, v in enc.items() if k not in ("source_written", "policy_class")}
	back = from_jsonable(legacy, dataclasses_by_name=_dataclasses_by_name(), enums_by_name={})
	assert isinstance(back, H.HBorrow)
	assert back.source_written is False
	assert back.policy_class is None


# ── the approved D9 gate: encode → decode → RECOMPILE ─────────────────────


_EXPLICIT_BODY = """\
module main;

fn read_len(arg: &String) nothrow -> Int {
	return arg.byte_length();
}

pub fn main() nothrow -> Int {
	val s: String = "hello";
	return read_len(&s);
}
"""


def test_pre_rule_body_encode_decode_recompile(tmp_path) -> None:
	"""A pre-rule-shaped body (source-written borrow at a declared &String
	formal) is pushed through the PRODUCTION codec (encode_hir_funcs →
	decode_hir_funcs) and the decoded functions are recompiled through the
	full pipeline: the redundant-borrow rule must NOT fire (packages are
	source-policy-free per D9), and the program must compile cleanly."""
	from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc.packages.provisional_dmir_v0 import encode_hir_funcs, decode_hir_funcs
	from lang.driftc import driftc as D
	from lang.driftc.core.function_id import function_symbol

	src = tmp_path / "main.drift"
	src.write_text(_EXPLICIT_BODY)
	modules, type_table, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True
	)
	assert not pdiags, [d.message for d in pdiags]
	func_hirs, signatures, _ = flatten_modules(modules)
	main_syms = {i: function_symbol(i) for i in func_hirs if i.module == "main"}
	sig_by_sym = {main_syms[i]: signatures[i] for i in main_syms}
	blocks_by_sym = {main_syms[i]: func_hirs[i] for i in main_syms}
	# sanity: the parsed body really carries a source-written borrow
	from lang.driftc.stage1 import hir_nodes as H

	def _count_source_written(node, seen) -> int:
		if id(node) in seen:
			return 0
		seen.add(id(node))
		n = 1 if (isinstance(node, H.HBorrow) and node.source_written) else 0
		for f in getattr(node, "__dataclass_fields__", {}) or {}:
			v = getattr(node, f, None)
			if isinstance(v, (list, tuple)):
				for it in v:
					if hasattr(it, "__dataclass_fields__"):
						n += _count_source_written(it, seen)
			elif hasattr(v, "__dataclass_fields__"):
				n += _count_source_written(v, seen)
		return n

	assert sum(_count_source_written(b, set()) for b in blocks_by_sym.values()) >= 1
	encoded = encode_hir_funcs(module_id="main", signatures=sig_by_sym, hir_blocks=blocks_by_sym)
	decoded = decode_hir_funcs(encoded)
	assert decoded, "codec produced no bodies"
	assert sum(_count_source_written(b, set()) for b in decoded.values()) == 0, (
		"provenance leaked through the codec"
	)
	# recompile: decoded bodies replace the originals
	for i, sym in main_syms.items():
		if sym in decoded:
			func_hirs[i] = decoded[sym]
	main_id = [i for i, s in signatures.items() if i.name == "main" and not s.is_method][0]
	ir, checked = D.compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc,
		entry=function_symbol(main_id),
		type_table=type_table,
		module_exports=mexp,
		module_deps=mdeps,
		origin_by_fn_id={},
		enforce_entrypoint=True,
		reserved_namespace_policy=D.ReservedNamespacePolicy.ALLOW_DEV,
	)
	errors = [d for d in getattr(checked, "diagnostics", []) if getattr(d, "severity", None) == "error"]
	assert not any("E_REDUNDANT_ARG_BORROW" in (getattr(d, "code", "") or "") for d in errors), [d.message for d in errors]
	assert not errors, [d.message for d in errors]
	assert ir
