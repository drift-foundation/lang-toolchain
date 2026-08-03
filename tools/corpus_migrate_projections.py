#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""One-time format migration: land projections.json in the reviewed baseline.

Mechanically converts the last approved candidate's checked-in per-fixture
counters into the reviewed baseline's projections.json, so a clean clone can
seed its developer cache without a full compile.  This is a format migration of
ALREADY-APPROVED evidence, NOT recertification — it performs no compile.

Before writing, it PROVES:
  * the historical candidate manifest AND aggregate are byte-identical to the
    live reviewed baseline (same approved evidence);
  * projection keys exactly equal manifest.universe.compiled_ok;
  * no failed fixture has a projection;
  * merging every per-fixture projection reproduces aggregate.json exactly.

Any failure aborts without writing.  The tool takes explicit file paths and does
NOT touch Git — extract the historical candidate files however you like (e.g.
`git show <rev>:<path>`) and pass them in.  No fingerprint is fabricated; the
first real `ownership-corpus-promote` generates it.

Usage:
  corpus_migrate_projections.py --fixture-counters FC.json \\
      --candidate-manifest M.json --candidate-aggregate A.json \\
      --baseline-dir lang/tests/ownership_corpus/reviewed-baseline
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _merge(parts) -> dict:
	total: dict[str, int] = {}
	for part in parts:
		for k, v in part.items():
			total[k] = total.get(k, 0) + v
	return dict(sorted(total.items()))


def migrate(fixture_counters: Path, cand_manifest: Path, cand_aggregate: Path,
            baseline_dir: Path, out=sys.stderr) -> int:
	base_manifest = baseline_dir / "manifest.json"
	base_aggregate = baseline_dir / "aggregate.json"
	projections_path = baseline_dir / "projections.json"

	# Proof 1: the historical candidate evidence IS the live reviewed baseline.
	if cand_manifest.read_bytes() != base_manifest.read_bytes():
		print("ABORT: candidate manifest.json != live baseline manifest.json", file=out)
		return 2
	if cand_aggregate.read_bytes() != base_aggregate.read_bytes():
		print("ABORT: candidate aggregate.json != live baseline aggregate.json", file=out)
		return 2

	fc = json.loads(fixture_counters.read_text())
	if not isinstance(fc, dict) or not isinstance(fc.get("fixtures"), dict):
		print("ABORT: fixture-counters.json has no 'fixtures' object", file=out)
		return 2
	projections = {name: dict(sorted(counts.items()))
	               for name, counts in fc["fixtures"].items()}

	universe = json.loads(base_manifest.read_text())["universe"]
	compiled_ok = set(universe["compiled_ok"])
	failed = set(universe["failed"])
	aggregate = json.loads(base_aggregate.read_text())["counters"]

	# Proof 2: projection keys exactly equal compiled_ok.
	if set(projections) != compiled_ok:
		missing = sorted(compiled_ok - set(projections))
		extra = sorted(set(projections) - compiled_ok)
		print(f"ABORT: projection keys != compiled_ok (missing={missing[:5]}..., "
		      f"extra={extra[:5]}...)", file=out)
		return 2
	# Proof 3: no failed fixture carries a projection.
	if set(projections) & failed:
		print(f"ABORT: failed fixtures carry projections: "
		      f"{sorted(set(projections) & failed)[:5]}", file=out)
		return 2
	# Proof 4: merging every per-fixture projection reproduces the aggregate.
	merged = _merge(projections.values())
	if merged != dict(sorted(aggregate.items())):
		print("ABORT: merged per-fixture projections != aggregate.json", file=out)
		return 2

	# Land projections.json canonically (sorted, trailing newline) — atomically.
	tmp = projections_path.parent / ".projections.json.tmp"
	tmp.write_text(json.dumps(dict(sorted(projections.items())), indent=2, sort_keys=True) + "\n")
	import os
	os.replace(tmp, projections_path)
	print(f"migrated {len(projections)} per-fixture projections -> {projections_path} "
	      f"(all four proofs passed); no fingerprint fabricated.", file=out)
	return 0


def main(argv=None) -> int:
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--fixture-counters", required=True, type=Path)
	ap.add_argument("--candidate-manifest", required=True, type=Path)
	ap.add_argument("--candidate-aggregate", required=True, type=Path)
	ap.add_argument("--baseline-dir", required=True, type=Path)
	args = ap.parse_args(argv)
	return migrate(args.fixture_counters, args.candidate_manifest,
	               args.candidate_aggregate, args.baseline_dir)


if __name__ == "__main__":
	raise SystemExit(main())
