# Drift Build & Package Workflow (Draft)

This guide is a practical, end-to-end workflow for new developers:

1. Bootstrap and validate toolchain/compiler infra on a fresh clone.
2. Build a hello-world app (no package dependencies).
3. Build a library package (`.dmp`).
4. Build an app that depends on that package (unsigned local flow).
5. Sign the package and consume it with signature verification enabled.

Commands assume you run from repository root and use the local venv:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --help
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift --help
```

## 1. Bootstrap & validate (fresh clone)

The goal is to confirm your environment can run the full Drift pipeline (parser/checker/MIR/LLVM/codegen/runtime tests) before writing code.

### 1.1 Install prerequisites (Linux)

- Python 3.13+
- LLVM/Clang (repo notes currently recommend clang-15)
- `just`
- `pkg-config`
- `libdw-dev`, `libunwind-dev`, `libelf-dev`
- Optional: `ld.gold`

### 1.2 Create virtualenv and install Python deps

```bash
python3 -m venv .venv
./.venv/bin/python3 -m pip install -U pip
./.venv/bin/python3 -m pip install -r requirements.txt
```

### 1.3 Quick CLI sanity

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --help
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift --help
```

### 1.4 Verify dependency wiring

```bash
just deps-check
```

### 1.5 Run complete compiler + codegen test flow

```bash
just
```

Notes:
- `just` runs the default full staged test path (`lang-test`) after `deps-check`.
- This is the recommended gate before coding in Drift.
- If you want to focus only on codegen/e2e for quick iteration: `just lang-codegen-test`.

## 2. Hello app (no dependency package)

Create a simple app:

```bash
mkdir -p sandbox/hello
cat > sandbox/hello/main.drift <<'DRIFT'
module main

fn main() nothrow -> Int {
    return 0;
}
DRIFT
```

Build executable:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --stdlib-root stdlib sandbox/hello/main.drift -o sandbox/hello/hello_app
```

Run:

```bash
./sandbox/hello/hello_app
```

Notes:
- Entry defaults to `main::main`.
- For executable builds, keep your entry module as `module main`.

## 3. Build a library package (`.dmp`)

Create a tiny library module:

```bash
mkdir -p sandbox/libmath
cat > sandbox/libmath/mathlib.drift <<'DRIFT'
module mathlib

export { add };

pub fn add(a: Int, b: Int) -> Int {
    return a + b;
}
DRIFT
```

Emit package artifact:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc -M sandbox/libmath sandbox/libmath/mathlib.drift --package-id acme.math --package-version 0.1.0 --package-target test-target --emit-package sandbox/libmath/acme.math.dmp --json
```

You should now have:
- `sandbox/libmath/acme.math.dmp`

## 4. Consume the package from an app (unsigned local flow)

Create an app that imports the packaged module:

```bash
mkdir -p sandbox/app_unsigned
cat > sandbox/app_unsigned/main.drift <<'DRIFT'
module main

import mathlib as mathlib;

fn main() nothrow -> Int {
    return try mathlib.add(40, 2) catch { 0 };
}
DRIFT
```

Build by adding package root and allowing unsigned package from local path:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc -M sandbox/app_unsigned --package-root sandbox/libmath --allow-unsigned-from sandbox/libmath --stdlib-root stdlib sandbox/app_unsigned/main.drift -o sandbox/app_unsigned/app_unsigned
```

Run:

```bash
./sandbox/app_unsigned/app_unsigned
echo $?
```

Expected exit code: `42`.

## 5. Sign package and require signatures at compile time

This is the distribution-style flow.

### 4.1 Generate a signing key

```bash
mkdir -p sandbox/keys sandbox/drift
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift keygen --out sandbox/keys/acme.seed --print-pubkey --print-kid
```

Keep output values (`pubkey`, `kid`) for trust-store operations.

### 4.2 Sign the package

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift sign sandbox/libmath/acme.math.dmp --key sandbox/keys/acme.seed --include-pubkey
```

This produces:
- `sandbox/libmath/acme.math.dmp.sig`

### 4.3 Trust signer for namespace

Add key to trust store. Use your printed pubkey:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift trust add-key --trust-store sandbox/drift/trust.json --namespace mathlib.* --pubkey '<BASE64_PUBKEY>'
```

Inspect trust store:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift trust list --trust-store sandbox/drift/trust.json --json
```

### 4.4 Build app with signatures required

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc -M sandbox/app_unsigned --package-root sandbox/libmath --require-signatures --trust-store sandbox/drift/trust.json --stdlib-root stdlib sandbox/app_unsigned/main.drift -o sandbox/app_unsigned/app_signed
```

Run:

```bash
./sandbox/app_unsigned/app_signed
echo $?
```

Expected exit code: `42`.

## 6. Revoke key (negative check)

Revoke by `kid`:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift trust revoke --trust-store sandbox/drift/trust.json --kid '<KID>' --reason 'test revoke'
```

Rebuild with `--require-signatures` should now fail for that package.

## 7. Command checklist

- Build app: `lang.driftc ... -o <exe>`
- Emit package: `lang.driftc ... --emit-package <pkg.dmp> --package-id ... --package-version ... --package-target ...`
- Sign package: `lang.drift sign <pkg.dmp> --key <seed>`
- Trust signer: `lang.drift trust add-key --namespace <ns> --pubkey <base64>`
- Enforce signatures: `lang.driftc ... --require-signatures --trust-store <path>`

## 8. Common pitfalls

- `module main` is required for default executable entrypoint (`main::main`).
- Imported module ids must match what the package exports.
- If consuming unsigned local packages, include `--allow-unsigned-from <dir>`.
- For signed flow, `--require-signatures` and a trust store are both required.
- For portability-sensitive environments, if toolchain asks for pointer width, add `--target-word-bits 64`.

## 9. Next expansion ideas

- Multi-package dependency chain (`pkg A -> pkg B -> app`).
- Multiple signatures on one package (multisig acceptance policy).
- CI recipe for publish/sign/trust/consume verification.
