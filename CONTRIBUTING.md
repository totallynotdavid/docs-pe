# Contributing

The root project provides the development environment. Use the commands exposed
by `mise.toml` so local work uses the repository toolchain.

The root `uv.lock` covers development. `packages/cli/uv.lock` and
`packages/portal/uv.lock` cover their deployment environments. `mise run update`
updates all three.

```sh
mise install
mise run install
mise run format
mise run check
mise run test
```

For focused work, use the root environment:

```sh
uv run pytest tests/cli
uv run pytest tests/core/sites/osiptel/test_lookup.py::test_name
uv run mypy packages/portal
uv run ruff check packages/portal
uv run ops/check_docs.py
```

The full test command includes portal tests and a disposable PostgreSQL test
cluster. CI also checks all three lockfiles, Markdown links, source-owned
documentation contracts, HTML formatting, Ruff, and mypy. Do not use a system
Python, pytest, Ruff, or mypy.

## Before changing code

Read [Architecture](ARCHITECTURE.md) for package boundaries and durable
contracts. Read the package's `readme.md` before changing it. For a new site, follow
[Adding a site](docs/adding-a-site.md).

Documentation has one owner for each current fact. The root `readme.md` is the
product entry point, `docs/portal.md` owns the portal user workflow, package
READMEs own local development, Architecture owns runtime contracts, site notes
own wire behavior, operations guides own procedures, and reports own dated
measurements. Link to the owner instead of copying its rules.

## Commit messages

Use a specific, imperative subject with a lower-case scope and no period:

```text
docs: clarify the fetch state ledger
portal: isolate worker publish failures
ops: provision worker nodes idempotently
```

Use the body when the reason is not obvious. Explain the problem, the
constraint, and the resulting behavior. Mention a follow-up or issue when it
exists. Do not write a diary, deployment transcript, or generic subject such as
`update`, `refactor`, or `cleanup`.
