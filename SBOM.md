# Software Bill of Materials

Status: complete pre-release dependency inventory

Generated from `uv.lock` and `uv tree --all-groups` on 2026-10-14 with uv 0.12.1.
The final release must regenerate this inventory from the frozen release commit.

## Product

| Component | Version | Relationship | Environment |
| --- | ---: | --- | --- |
| praeco | 0.0.1 | root application | runtime |
| cryptography | 46.0.7 | direct dependency of praeco | runtime |
| cffi | 2.1.1 | dependency of cryptography | runtime |
| pycparser | 3.11 | dependency of cffi | runtime |

## Development graph

| Component | Version | Relationship |
| --- | ---: | --- |
| mypy | 1.18.2 | direct development dependency |
| mypy-extensions | 1.1.0 | dependency of mypy |
| pathspec | 1.1.1 | dependency of mypy |
| typing-extensions | 4.16.0 | dependency of mypy |
| pytest | 8.4.2 | direct development dependency |
| colorama | 0.4.6 | conditional Windows dependency of pytest |
| iniconfig | 2.3.1 | dependency of pytest |
| packaging | 26.3 | dependency of pytest |
| pluggy | 1.6.0 | dependency of pytest |
| Pygments | 2.21.0 | dependency of pytest |
| Ruff | 0.14.0 | direct development dependency |

## Isolated build graph

The build graph was resolved separately from the application environment with
`uv run --no-project --with hatchling==1.27.0`.

| Component | Version | Relationship |
| --- | ---: | --- |
| hatchling | 1.27.0 | build backend |
| packaging | 26.3 | dependency of hatchling |
| pathspec | 1.1.1 | dependency of hatchling |
| pluggy | 1.6.0 | dependency of hatchling |
| trove-classifiers | 2026.9.21.13 | dependency of hatchling |

## Verification source

- Exact resolution: `uv.lock`
- Human-readable tree: `uv tree --all-groups`
- Isolated build environment: `uv run --no-project --with hatchling==1.27.0`
- License summary: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

No vulnerability status is asserted by this file.
