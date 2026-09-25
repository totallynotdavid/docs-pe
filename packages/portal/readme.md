# Portal package

The portal package contains the web application, worker API, worker agent, and
PostgreSQL repositories. The user workflow is documented in the [portal user
guide](../../docs/portal.md). This page is for local development and maintainers.

```sh
mise run dev
```

See [portal deployment](../../docs/operations/portal-deployment.md) for a
non-local deployment.

## Local development

Start with the repository template:

```sh
cp .env.example .env
mise run dev
```

Local development starts PostgreSQL, applies migrations, provisions the first
administrator and team, and runs the web process. The first administrator
finishes TOTP or passkey enrollment at `/security`. `mise run reset`
removes the disposable local database.

### Ports

Each checkout, including each git worktree, runs its own PostgreSQL, MinIO, and
web process, so two can run `mise run dev` and `mise run test` together.
`mise.toml` hashes the checkout path into a slot from 0 to 999 and adds it to a
fixed base per server, which keeps the ports the same across restarts:

| Server | Base | Variable |
| --- | --- | --- |
| Web | 23000 | `PORTAL_DEV_WEB_PORT` |
| PostgreSQL | 24000 | `PORTAL_DEV_POSTGRES_PORT` |
| MinIO API | 25000 | `PORTAL_DEV_MINIO_PORT` |
| MinIO console | 26000 | `PORTAL_DEV_MINIO_CONSOLE_PORT` |

`mise env` prints this checkout's values. The same file sets
`PORTAL_DATABASE_DSN`, `PORTAL_WORKER_DATABASE_ADDRESS`,
`PORTAL_OBJECT_STORAGE_ENDPOINT`, and `PORTAL_PUBLIC_ORIGIN` from them, so
`.env` does not carry those four. Tests read the ports the same way;
`PORTAL_TEST_DSN` and `PORTAL_TEST_OBJECT_STORAGE_ENDPOINT` still override them.

`db:start` and `objects:start` reuse a server only when it is this checkout's
own: PostgreSQL's `.data/postgres/postmaster.pid` names the port, and MinIO's pid
is in `.data/minio.pid`. A port held by anything else fails the task with a
line naming the port. Two checkouts whose paths land on the same slot collide
this way too; move one of them.

Development may omit Turnstile, mail, and worker enrollment settings. See
[`.env.example`](../../.env.example) and the deployment guide for production
configuration.

## Commands

```text
uv run portal-admin --help

uv run python -m portal.web.app       serve the web application
uv run python -m portal.worker.api    serve the private worker API
uv run python -m portal.worker.agent  run a worker
```

Provisioning uses environment-backed passwords and is safe to rerun:

```sh
mise x -- uv run --env-file .env portal-admin provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

Proxy credentials for provisioning use `PORTAL_PROVISION_<PROVIDER>_<FIELD>`
names generated from the provider schema.

Worker-api capacity and master-key rotation are covered in the
[portal deployment guide](../../docs/operations/portal-deployment.md).

For manual intervention, use the [SQL runbook](operations.md). It is for a
trusted operator and is not a replacement for application authorization.

Run portal tests with:

```sh
mise run db:start
mise run objects:start
mise x -- uv run pytest tests/portal
```

`mise x` supplies this checkout's ports; see [Ports](#ports).
