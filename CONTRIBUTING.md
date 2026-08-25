# Contributing

This repository is a uv workspace. `mise.toml` pins the toolchain and owns the
commands used by CI.

```sh
mise install
mise run install
mise run format
mise run check
mise run test
```

For a focused test or check, use the workspace environment explicitly:

```sh
uv run pytest tests/fetch
uv run pytest tests/fetch/sites/osiptel/test_lookup.py::test_name
uv run mypy packages/portal
uv run ruff check packages/portal
```

Do not invoke an unpinned system copy of Python, pytest, Ruff, or mypy. Run
formatting before committing. A full test run includes the portal and its
PostgreSQL test cluster.

## Boundaries

- `fetch` owns unattended lookup execution, proxy providers, fault policy, and
  SQLite outcome state.
- `browser` owns Chrome sessions and browser-gated site behavior.
- `capture` owns the tools used to discover a site's request and response.
- `portal` owns HTTP handling, authentication, PostgreSQL queue state, and
  worker orchestration. It may import `fetch` as the execution library.

Do not import between `capture`, `browser`, and `fetch`; see
[the architecture](ARCHITECTURE.md#package-boundaries) for why. Keeping each
site's knowledge separate lets it evolve without turning the package boundary
into a second API.

Read [the architecture](ARCHITECTURE.md) and the package README before changing
a package. Site-specific wire behavior belongs in `docs/sites/`. Cross-cutting
operational behavior belongs in `docs/operations/`.

## Documentation

The root README is the product front door. `ARCHITECTURE.md` describes stable
system contracts. Operations guides describe procedures. Reports describe dated
measurements or incidents and are not normative references.

Document a current contract where a user or maintainer needs it. Link to the
canonical explanation instead of copying it into another guide. When a fact
changes, update its owner and the links that point to it.

## Comments

Keep a comment when it explains an invariant, a constraint, a security rule, or
an external behavior that the code cannot make obvious. Delete comments that
label a block, restate a name, narrate file layout, or describe a change that is
no longer visible in the current code.

Put one idea beside the code it explains. Prefer a clearer implementation when
the comment is only needed to make ordinary control flow readable. Do not use
comments to maintain a list of synchronized files or to preserve a rejected
implementation.

## Commit messages

Use a short, concrete, imperative subject. Keep it within one line and add a
scope when it clarifies the affected area:

```text
docs: correct the fetch site implementation guide
portal: isolate worker failures from claim and publish errors
ops: automate worker-node provisioning
```

Use the body for the problem, the constraint, and the resulting behavior. Add an
issue or follow-up when one exists. Do not use a commit subject as a diary,
deployment transcript, or changelog entry.
