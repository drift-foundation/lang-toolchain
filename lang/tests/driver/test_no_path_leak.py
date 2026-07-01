import json
import re
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _run_driftc_json(argv: list[str], capsys) -> dict:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	assert rc != 0
	return payload


_UNIX_ABS_PATH = re.compile(r"(?<![A-Za-z0-9_./-])/(?:[^\\s/]+/)+[^\\s]+")
_WIN_ABS_PATH = re.compile(r"[A-Za-z]:\\\\[^\\s]+")


def _has_abs_path(text: str) -> bool:
	return _UNIX_ABS_PATH.search(text) is not None or _WIN_ABS_PATH.search(text) is not None


def test_driftc_no_path_leak_in_trust_store_error(tmp_path: Path, capsys) -> None:
	src = tmp_path / "main.drift"
	src.write_text("module main;\npub fn main() nothrow -> Int { return 0; }\n", encoding="utf-8")
	missing_trust = tmp_path / "missing-trust.json"
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir()

	payload = _run_driftc_json(
		[
			str(src),
			"--package-root",
			str(pkg_root),
			"--trust-store",
			str(missing_trust),
		],
		capsys,
	)
	blob = json.dumps(payload)
	assert "<trust-store>" in blob
	assert not _has_abs_path(blob)

	rc = driftc_main(
		[
			str(src),
			"--package-root",
			str(pkg_root),
			"--trust-store",
			str(missing_trust),
		]
	)
	err = capsys.readouterr().err
	assert rc != 0
	assert "<trust-store>" in err
	assert not _has_abs_path(err)
