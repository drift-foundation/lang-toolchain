# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from lang2.drift.dmir_pkg_v0 import read_identity_v0, sha256_hex
from lang2.drift.index_v0 import IndexEntry, load_index, save_index, upsert_entry
from lang2.drift.sign import load_sig_sidecar_v0


@dataclass(frozen=True)
class PublishOptions:
	dest_dir: Path
	package_paths: list[Path]
	force: bool = False
	allow_unsigned: bool = False


def publish_packages_v0(opts: PublishOptions) -> None:
	"""
Publish one or more packages into a directory "repository".

This is an offline operation: it copies `.dmp` + optional `.dmp.sig` sidecar and
updates `index.json` under `dest_dir`.

Pinned MVP rule: one version per package_id in a repository.
	"""
	if not opts.package_paths:
		raise ValueError("no packages provided")

	dest = opts.dest_dir
	dest.mkdir(parents=True, exist_ok=True)
	index_path = dest / "index.json"
	index_obj = load_index(index_path)

	for pkg_path in opts.package_paths:
		if not pkg_path.exists():
			raise ValueError(f"package not found: {pkg_path}")

		ident = read_identity_v0(pkg_path)
		pkg_bytes = pkg_path.read_bytes()
		pkg_sha = f"sha256:{sha256_hex(pkg_bytes)}"

		sidecar_path = Path(str(pkg_path) + ".sig")
		signers: list[str] = []
		unsigned = False
		if sidecar_path.exists():
			sf = load_sig_sidecar_v0(sidecar_path)
			signers = [s.kid for s in sf.signatures]
		else:
			if not opts.allow_unsigned:
				raise ValueError(f"missing sidecar for package (use --allow-unsigned): {pkg_path}")
			unsigned = True

		# Use a deterministic filename in the repository.
		base_name = f"{ident.package_id}-{ident.package_version}-{ident.target}.dmp"
		out_pkg = dest / base_name
		out_sig = dest / (base_name + ".sig")

		shutil.copyfile(pkg_path, out_pkg)
		if sidecar_path.exists():
			shutil.copyfile(sidecar_path, out_sig)

		entry = IndexEntry(
			package_id=ident.package_id,
			package_version=ident.package_version,
			target=ident.target,
			sha256=pkg_sha,
			filename=out_pkg.name,
			signers=signers,
			unsigned=unsigned,
		)
		upsert_entry(index_obj, entry=entry, force=opts.force)

	save_index(index_path, index_obj)

