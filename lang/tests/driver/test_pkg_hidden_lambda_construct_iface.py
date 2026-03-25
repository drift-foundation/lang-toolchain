# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: hidden lambda callback target from stdlib generic instantiation
must be resolved when the instantiation is triggered by a package function.

Uses the real web-rest package from certified/current/libs if available.
Skips if packages are not installed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CERTIFIED_LIBS = Path.home() / "opt" / "drift" / "certified" / "current" / "libs"
TRUST_STORE_CANDIDATES = sorted(
	ROOT.parent.glob("build-orchestrator/build/runs/*/checkouts/drift-web/drift/trust.json"),
	reverse=True,
)


def _find_trust_store() -> Path | None:
	for p in TRUST_STORE_CANDIDATES:
		if p.exists():
			return p
	return None


_skip_no_pkgs = pytest.mark.skipif(
	not (CERTIFIED_LIBS / "web-rest").is_dir(),
	reason="certified web-rest package not installed",
)
_skip_no_trust = pytest.mark.skipif(
	_find_trust_store() is None,
	reason="no trust store found for web-rest",
)


REPRO_SOURCE = """\
module repro;

import std.core as core;
import std.concurrent as conc;
import web.rest as rest;

fn main() nothrow -> Int {
\tvar b = rest.new_app_builder();
\trest.bind(&mut b, "127.0.0.1", 0);
\tmatch rest.build_app(move b) {
\t\tcore.Result::Err(_) => { return 1; },
\t\tcore.Result::Ok(a) => {
\t\t\tvar app = move a;
\t\t\tmatch rest.start(move app, conc.Duration(millis = 100)) {
\t\t\t\tcore.Result::Err(_) => { return 2; },
\t\t\t\tcore.Result::Ok(srv) => {
\t\t\t\t\tvar running = move srv;
\t\t\t\t\tmatch rest.shutdown(&mut running) {
\t\t\t\t\t\tcore.Result::Ok(_) => {},
\t\t\t\t\t\tcore.Result::Err(_) => {}
\t\t\t\t\t}
\t\t\t\t\treturn 0;
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


@_skip_no_pkgs
@_skip_no_trust
def test_webrest_spawn_cb_hidden_lambda_resolved(tmp_path: Path) -> None:
	"""rest.start() codegen must resolve spawn_cb's hidden lambda callback target."""
	from lang.driftc.driftc import main as driftc_main

	src = tmp_path / "repro.drift"
	src.write_text(REPRO_SOURCE)
	out = tmp_path / "repro"
	trust = _find_trust_store()

	# List available package versions
	web_rest_versions = sorted((CERTIFIED_LIBS / "web-rest").iterdir())
	web_jwt_versions = sorted((CERTIFIED_LIBS / "web-jwt").iterdir())
	web_client_versions = sorted((CERTIFIED_LIBS / "web-client").iterdir())
	net_tls_versions = sorted((CERTIFIED_LIBS / "net-tls").iterdir())

	deps = []
	if web_rest_versions:
		deps.extend(["--dep", f"web-rest@{web_rest_versions[-1].name}"])
	if web_jwt_versions:
		deps.extend(["--dep", f"web-jwt@{web_jwt_versions[-1].name}"])
	if web_client_versions:
		deps.extend(["--dep", f"web-client@{web_client_versions[-1].name}"])
	if net_tls_versions:
		deps.extend(["--dep", f"net-tls@{net_tls_versions[-1].name}"])

	rc = driftc_main([
		str(src),
		"--target-word-bits", "64",
		"--package-root", str(CERTIFIED_LIBS),
		*deps,
		"--entry", "repro::main",
		"-o", str(out),
		"--trust-store", str(trust),
		"--json",
	])
	assert rc == 0, "compilation failed — hidden lambda callback target likely not resolved"
