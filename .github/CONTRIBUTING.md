# Contributing

Read [Architecture](../docs/architecture.md) for package boundaries and the code
map, and the readme of the package you change. For a new site, follow
[Adding a site](../docs/adding-a-site.md).

## Set up

[mise](https://mise.jdx.dev) pins the toolchain in `mise.toml`, and `uv`
resolves the Python environment. Do not run a system `python`, `pytest`, `ruff`,
or `mypy`.

```sh
mise install
mise run install
```

## Commands

| Command                 | Does                                                                                     |
| ----------------------- | ---------------------------------------------------------------------------------------- |
| `mise run install`      | `uv sync --locked --all-groups`                                                          |
| `mise run install:core` | Reinstalls `core`, which installs non-editable, after you edit it                        |
| `mise run format`       | `ruff format`, `ruff check --fix`, and `djlint` on the portal templates                  |
| `mise run check`        | Documentation, Ruff, mypy, djlint, lockfiles, tests, and the `fetch` build               |
| `mise run test`         | `pytest` across every package, with a disposable PostgreSQL cluster and MinIO            |
| `mise run build`        | PyInstaller single binary for `fetch`                                                    |
| `mise run update`       | Updates the three lockfiles                                                              |
| `mise run dev`          | Starts PostgreSQL and MinIO, bootstraps, and runs the portal. Ctrl+C stops everything    |
| `mise run reset`        | Deletes the local PostgreSQL cluster and MinIO store, so the next `dev` starts clean     |
| `mise run infra:check`  | `shellcheck` and `terraform validate` for the [AWS test servers](../infra/aws/readme.md) |

The root `uv.lock` covers development. `packages/cli/uv.lock` and
`packages/portal/uv.lock` cover the deployment environments.

For focused work:

```sh
uv run pytest tests/cli
mise x -- uv run pytest tests/portal
uv run pytest tests/core/sites/osiptel/test_lookup.py::test_name
uv run mypy packages/portal
uv run ruff check packages/portal
uv run ops/check_docs.py
```

`tests/cli` needs no database. Run `mise run db:start objects:start` before
`tests/portal`; `mise x` supplies this checkout's ports.

CI runs the same checks, plus the lockfile checks. Tests live in `tests/`, one
subpackage per package. pytest runs with `asyncio_mode = "auto"` (root
`pyproject.toml`), so an async test is a plain `async def test_*` with no
decorator.

## Lint

Ruff uses the baseline rules in the root `pyproject.toml`. The portal has a
per-file ignore list there. Extend that list instead of adding `noqa` comments.

## Comments

A comment states an invariant, a constraint, or an external quirk. Do not write
one that restates the code or labels structure. One idea per comment, placed
beside the code it explains. Describe the current contract, not how the code
used to work. If code needs a comment to be followable, fix the code first.

## Documentation

Use no em dashes in comments or documents: a period, comma, colon, or
parentheses does the job. Each fact lives in one document, and every other
document links to it. The [documentation index](../docs/readme.md) lists the
owners. Run `uv run ops/check_docs.py` after editing Markdown, and format it
with `bunx prettier --print-width 80 --prose-wrap always --write '**/*.md'`.

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
