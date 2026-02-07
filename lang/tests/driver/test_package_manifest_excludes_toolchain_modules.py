from __future__ import annotations

import tempfile
from pathlib import Path

from lang.tests.driver.test_driftc_package_v0 import _emit_lib_pkg
from lang.driftc.packages.provider_v0 import load_package_v0


def test_package_manifest_excludes_toolchain_modules() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		pkg_path = _emit_lib_pkg(tmp, module_id="acme.lib")
		pkg = load_package_v0(pkg_path)
		mod_ids = [m.get("module_id") for m in pkg.manifest.get("modules", []) if isinstance(m, dict)]
		for mid in mod_ids:
			assert isinstance(mid, str)
			assert not mid.startswith(("std.", "lang.", "drift.")), mid
