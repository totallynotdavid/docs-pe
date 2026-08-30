# Portal package

The portal package contains the web application, worker API, worker agent, and
PostgreSQL repositories. The user workflow is documented in the [portal user
guide](../../docs/portal.md). This page is for local development and maintainers.

Workers claim queue items through a scoped PostgreSQL role. They use `worker-api`
for enrollment, credential reveals, and result publication.

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
finishes TOTP or passkey enrollment at `/security/setup`. `mise run reset`
removes the disposable local database.

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
uv run --env-file .env portal-admin provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

Proxy credentials for provisioning use `PORTAL_PROVISION_<PROVIDER>_<FIELD>`
names generated from the provider schema.

Worker-api process count and its PostgreSQL connection budget are covered in
the [portal deployment guide](../../docs/operations/portal-deployment.md#worker-api-capacity).

Master-key setup and rotation are in the [deployment guide](../../docs/operations/portal-deployment.md#key-rotation).

For manual intervention, use the [SQL runbook](operations.md). It is for a
trusted operator and is not a replacement for application authorization.

Run portal tests with:

```sh
uv run pytest tests/portal
```
