# portal

The portal provides a web interface for submitting lookup jobs, sharing access
through teams, reusing previous results, and running work on a worker fleet.
Workers claim queue items through `worker-api`; they do not need PostgreSQL
credentials.

```sh
mise run dev
```

Read [portal deployment](../../docs/operations/portal-deployment.md) before
running it outside a local development environment.

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

Development may omit Turnstile, mail, and worker enrollment settings. Production
validation requires an HTTPS public origin, both Turnstile keys, a worker
bootstrap token, a Resend API key, and a sender address. The complete variable
set is maintained in [`.env.example`](../../.env.example).

## Commands

```text
portal web            serve the public web application
portal worker-api     serve the tailnet-only worker API
portal worker         claim and execute queue items
portal migrate        apply pending migrations
portal provision      create or verify the initial installation
portal bootstrap      provision the local development installation
portal enroll-worker  issue or revoke a worker credential
portal new-key        print a master-key line
portal rewrap         re-encrypt stored secrets with the active key
```

Run `uv run portal <command> --help` for command-specific options. Provisioning
uses environment-backed passwords and is safe to rerun:

```sh
uv run --env-file .env portal provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

Proxy credentials for provisioning use `PORTAL_PROVISION_<PROVIDER>_<FIELD>`
names generated from the provider schema.

## Runtime model

The portal creates one queue item per accepted document/site pair. Workers claim
items, execute `fetch`, and publish results under a lease fence. Cancellation
advances the fence so a late worker cannot publish into a cancelled job.

Team search exposes entries available to that team. Site admins and entitled
teams can use global search. Stored proxy credentials and TOTP secrets use
envelope encryption; passkey public keys are stored for verification. Master key
creation, backup, and rotation are documented in the
[deployment guide](../../docs/operations/portal-deployment.md#key-rotation).

For manual intervention, use the [SQL runbook](operations.md). It is for a
trusted operator and is not a replacement for application authorization.

Run portal tests with:

```sh
uv run pytest tests/portal
```
