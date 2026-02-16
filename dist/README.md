# Local Distribution Repo (Dev Convenience)

This directory hosts a local Drift package repository for early development.

Repository root:

- `dist/release/`

It is a standard `drift publish` directory repository with:

- package artifacts (`*.dmp`, optional `*.sig`)
- `index.json` (format: `drift-index`, version `0`)

## Quick start

Initialize:

```bash
just dist-init
```

Publish a package:

```bash
just dist-publish build/pkg/std.dmp
```

Or directly:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift publish --dest-dir dist/release --allow-unsigned build/pkg/std.dmp
```

Show local index:

```bash
just dist-index
```

## Consuming from another repo

Create `drift-sources.json` in the consumer repo and point to this repo path:

```json
{
  "format": "drift-sources",
  "version": 0,
  "sources": [
    {
      "kind": "dir",
      "id": "local-drift-lang-dev",
      "priority": 0,
      "path": "/ABS/PATH/TO/drift-lang/dist/release"
    }
  ]
}
```

Then in the consumer:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift fetch --sources drift-sources.json
```

Notes:

- This flow is intentionally convenient for early iteration.
- For production/distribution, prefer signed packages and trust-policy enforcement.
