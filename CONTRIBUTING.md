# Contributing

This is a uv workspace. Use the commands exposed by `mise.toml` so local work
uses the repository toolchain:

```sh
mise install
mise run install
mise run format
mise run check
mise run test
```

For focused work, use the workspace environment:

```sh
uv run pytest tests/fetch
uv run pytest tests/fetch/sites/osiptel/test_lookup.py::test_name
uv run mypy packages/portal
uv run ruff check packages/portal
```

The full test command includes portal tests and a disposable PostgreSQL test
cluster. Do not use a system Python, pytest, Ruff, or mypy.

## Boundaries

- `fetch` owns unattended lookups, proxy providers, fault policy, and SQLite
  outcomes.
- `browser` owns Chrome sessions and browser-gated sites.
- `capture` owns request discovery with a real Chrome profile.
- `portal` owns HTTP routes, authentication, PostgreSQL state, and worker
  orchestration. It may import `fetch`.

Do not add imports between `capture`, `browser`, and `fetch`. Keep fault-to-
retry decisions in `fetch.domain.policy`, not in a site adapter.

## Documentation

The root README is the product entry point. `ARCHITECTURE.md` owns stable
system contracts. Package READMEs explain package purpose and command usage.
Site notes own current wire behavior. Operations guides own procedures.
Reports own dated measurements and incidents and are never a substitute for a
runtime contract.

Give every current fact one canonical home. If another guide needs it, link to
that home. Do not copy the same provider limit, state rule, or failure mode into
several files.

## Comments

Keep a comment when it explains an invariant, security boundary, external
system quirk, or failure behavior that the code cannot express. Delete comments
that name a block, repeat a function name, narrate ordinary control flow, or
describe an old implementation.

Put one idea beside the code it explains. Use the vocabulary of the local
module, avoid synchronization instructions that are not enforced, and avoid
numbers that are not actual constraints. Do not use em dashes.

## Commit messages

Use a specific, imperative subject with a lower-case scope and no period:

```text
docs: clarify the fetch state ledger
portal: isolate worker publish failures
ops: provision worker nodes idempotently
```

Use the body when the reason is not obvious. Explain the problem, the
constraint, and the resulting behavior. Mention a follow-up or issue when it
exists. Do not write a diary, deployment transcript, or generic subject such
as `update`, `refactor`, or `cleanup`.
