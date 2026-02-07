# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DriftIdentity:
	package_id: str | None = None
	version: str | None = None
	target: str | None = None

	def to_dict(self) -> dict[str, Any]:
		return {"package_id": self.package_id, "version": self.version, "target": self.target}


@dataclass(frozen=True)
class DriftError(Exception):
	"""
	A structured, serializable error for Drift tooling.

	Phase 5.3 goal: stable error codes + supply-chain useful context.
	"""

	reason_code: str
	message: str
	mode: str | None = None  # "lock" | "unlocked"
	identity: DriftIdentity | None = None
	claimed_identity: DriftIdentity | None = None
	observed_identity: DriftIdentity | None = None
	sha256_expected: str | None = None
	sha256_got: str | None = None
	source_id: str | None = None
	index_path: str | None = None
	artifact_path: str | None = None
	signer_kids: list[str] | None = None

	def __str__(self) -> str:
		return self.format_human()

	def to_dict(self) -> dict[str, Any]:
		return {
			"reason_code": self.reason_code,
			"message": self.message,
			"mode": self.mode,
			"identity": self.identity.to_dict() if self.identity is not None else None,
			"claimed_identity": self.claimed_identity.to_dict() if self.claimed_identity is not None else None,
			"observed_identity": self.observed_identity.to_dict() if self.observed_identity is not None else None,
			"sha256_expected": self.sha256_expected,
			"sha256_got": self.sha256_got,
			"source_id": self.source_id,
			"index_path": self.index_path,
			"artifact_path": self.artifact_path,
			"signer_kids": list(self.signer_kids) if self.signer_kids is not None else None,
		}

	def format_human(self) -> str:
		parts: list[str] = [f"[{self.reason_code}] {self.message}"]
		ident = self.identity or self.claimed_identity
		if ident is not None and ident.package_id:
			parts.append(f"identity=({ident.package_id}, {ident.version}, {ident.target})")
		if self.source_id:
			parts.append(f"source_id={self.source_id}")
		if self.artifact_path:
			parts.append(f"artifact_path={self.artifact_path}")
		if self.index_path:
			parts.append(f"index_path={self.index_path}")
		if self.sha256_expected or self.sha256_got:
			parts.append(f"sha256_expected={self.sha256_expected}")
			parts.append(f"sha256_got={self.sha256_got}")
		if self.claimed_identity is not None and self.observed_identity is not None:
			c = self.claimed_identity
			o = self.observed_identity
			parts.append(f"claimed=({c.package_id}, {c.version}, {c.target})")
			parts.append(f"observed=({o.package_id}, {o.version}, {o.target})")
		return " ".join(parts)

