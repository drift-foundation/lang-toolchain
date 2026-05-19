# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Permissive trust-store shim for `tools/drift_deploy/` tests.

Used by tests that pre-date the trust-v1 cutover and still build
locks against fabricated kids (`ed25519:rebuilder`, `ed25519:test`,
...).  The v1 trust store exposes role-tagged methods
(`allowed_authors_for_module`, `allowed_certifiers_for_module`);
this shim returns an every-kid-OK sentinel for BOTH roles so the
trust gate becomes a pass-through.

Tests that specifically pin trust-gate rejection (untrusted kid,
revoked kid, wrong role) must build their own `TrustStore` (or a
stricter shim) -- do NOT use `PermissiveTrustStore` for those.
"""

from __future__ import annotations


class _EveryKidOkSet(set):
	"""Set-shaped object whose `in` membership is always True."""

	def __contains__(self, _item) -> bool:
		return True


_EVERY_KID_OK = _EveryKidOkSet()


class PermissiveTrustStore:
	"""Duck-typed v1 `TrustStore` stand-in -- allowlists every kid for
	both author and certifier roles.

	Does NOT subclass the real frozen-dataclass `TrustStore`; the
	verifier only touches `.allowed_authors_for_module(...)`,
	`.allowed_certifiers_for_module(...)`, and `.revoked_kids`, so
	a duck-typed shim is sufficient.
	"""

	__slots__ = ("revoked_kids",)

	def __init__(self) -> None:
		self.revoked_kids: set[str] = set()

	def allowed_authors_for_module(self, _module_id: str) -> set[str]:
		return _EVERY_KID_OK

	def allowed_certifiers_for_module(self, _module_id: str) -> set[str]:
		return _EVERY_KID_OK
