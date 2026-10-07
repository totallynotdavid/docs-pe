# Working in this repository

Read [Architecture](docs/architecture.md) for package boundaries and the code
map, then the readme of the package you change.
[Contributing](.github/CONTRIBUTING.md) has the toolchain, commands, lint rules,
comment and documentation rules, and commit messages. Follow all of it.

## Rules

- Add no import across the package boundaries in Architecture.
- Do not classify faults inside a site. The code map in Architecture names the
  owner.
- Read a run's progress from its state database, as
  [Troubleshooting](docs/troubleshooting.md) describes, never from a CSV or a
  log tail.
- Job output goes under `results/`, which is gitignored.
