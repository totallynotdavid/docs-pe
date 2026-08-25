# Portal

The portal is the web interface for fetch jobs. Teams upload documents, choose
sites and proxy credentials, follow progress, and download results. Workers
claim lookup work through the worker API.

```sh
mise run dev
```

The public application serves browsers. The worker API serves the worker fleet
over Tailscale. See the
[deployment guide](../../docs/operations/portal-deployment.md) for production
topology and worker enrollment.

## Local development

Set the database, origin, key file, and bootstrap values in `.env`:

```env
PORTAL_DATABASE_DSN=postgresql://postgres@127.0.0.1:5432/postgres
PORTAL_ENVIRONMENT=development
PORTAL_PUBLIC_ORIGIN=http://localhost:8000
PORTAL_TLS_TERMINATED_UPSTREAM=false
PORTAL_MASTER_KEY_FILE=.data/master.key
PORTAL_BOOTSTRAP_ADMIN_EMAIL=admin@example.org
PORTAL_BOOTSTRAP_ADMIN_PASSWORD=choose-a-local-password
PORTAL_BOOTSTRAP_TEAM_NAME=Equipo Lima
PORTAL_BOOTSTRAP_TEAM_SLUG=equipo-lima
```

`mise run dev` starts PostgreSQL, applies migrations, provisions the first admin
and team, and runs the web process. The first admin enrolls a TOTP app or
passkey at `/security/setup`. `mise run reset` removes the local database.

Turnstile may be empty in development. Production requires the site key, secret,
an HTTPS public origin, and the worker bootstrap token.

Run the portal tests with:

```sh
uv run pytest tests/portal
```

Each test database is isolated and disposable.

## Commands

```text
portal web            serve the browser-facing app
portal worker-api     serve the worker API
portal worker         claim and run work on a worker node
portal migrate        apply schema migrations
portal provision      create or verify the initial installation
portal bootstrap      provision from PORTAL_BOOTSTRAP_*
portal enroll-worker  issue or revoke a worker credential
portal new-key        print a master-key line
portal rewrap         move stored data keys onto the active key
```

Provision the first production admin with an environment-backed password:

```sh
uv run --env-file .env portal provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

The command is safe to rerun. Proxy credentials use
`PORTAL_PROVISION_<PROVIDER>_<FIELD>` variables from the provider schema.

## Authentication

Site admins must enroll TOTP or a passkey. Every user can manage their own
factors and recovery codes. Login accepts a password followed by TOTP, a
recovery code, or a user-verified passkey. A passkey can also start login on its
own.

Sessions expire after seven days and state-changing requests require CSRF and
same-origin checks. The audit log records authentication, refusals,
administrative changes, secret access, and worker credential changes.

## Jobs

Submit a CSV, select sites and a proxy credential, then review reusable results
before creating the job. Progress and results are available from the team page.
The queue permits five active jobs globally; a job submitted past that cap is
created in `queued` state and starts running once a slot frees up. Read
[Architecture](../../ARCHITECTURE.md#portal-lifecycle) for queue and worker
semantics.

Team search returns entries confirmed by that team. Global search is available
to site admins and teams with the global-search entitlement. Results use generic
columns and rows, so a site can add fields without a portal template change.

## Secrets

Proxy credentials and TOTP secrets use envelope encryption with a versioned
master key. Passkey public keys are stored for signature verification. See
[master-key operations](../../docs/operations/portal-deployment.md#master-key)
for creation, backup, and rotation.

For direct SQL intervention, see the [operations runbook](operations.md).

## Code map

| Path                  | Owns                                                    |
| --------------------- | ------------------------------------------------------- |
| `portal/web/`         | Litestar routes, sessions, templates, assets            |
| `portal/worker/`      | Worker API, agent, protocol, enrollment                 |
| `portal/application/` | Login, teams, credentials, jobs, provisioning           |
| `portal/domain/`      | Job, team, credential, and planning rules               |
| `portal/repository/`  | PostgreSQL access, one concern per module               |
| `portal/credentials/` | Master keyring and envelope encryption                  |
| `portal/ephemeral.py` | Expiring keyed state                                    |
| `portal/storage/`     | Immutable upload references                             |
| `portal/security.py`  | Password, session, TOTP, WebAuthn, and token primitives |

djlint lowercases matching tags when it reformats `portal/web/`'s Jinja
components, so check a component's name against real HTML elements before adding
one.

## Configuration

| Variable                           | Meaning                                           |
| ---------------------------------- | ------------------------------------------------- |
| `PORTAL_DATABASE_DSN`              | PostgreSQL connection string                      |
| `PORTAL_ENVIRONMENT`               | `development` or `production`                     |
| `PORTAL_PUBLIC_ORIGIN`             | Scheme and host used for HTTPS and host checks    |
| `PORTAL_TLS_TERMINATED_UPSTREAM`   | Whether an upstream terminates TLS                |
| `PORTAL_MASTER_KEY_FILE`           | Versioned master-key file                         |
| `PORTAL_OBJECT_ROOT`               | Directory for uploaded inputs and result payloads |
| `PORTAL_TURNSTILE_SITE_KEY`        | Login widget key                                  |
| `PORTAL_TURNSTILE_SECRET`          | Server-side Turnstile key                         |
| `PORTAL_WORKER_API_HOST`           | Worker API bind address                           |
| `PORTAL_WORKER_API_PORT`           | Worker API port                                   |
| `PORTAL_WORKER_BOOTSTRAP_TOKEN`    | Token for worker self-enrollment                  |
| `PORTAL_WORKER_API_URL`            | Worker API URL on a worker node                   |
| `PORTAL_WORKER_ID`                 | Stable worker identity                            |
| `PORTAL_WORKER_TAILSCALE_HOSTNAME` | Worker tailnet hostname                           |
| `PORTAL_WORKER_CONCURRENCY`        | Number of worker lanes                            |
| `PORTAL_BOOTSTRAP_*`               | First local admin and team                        |

Use `PORTAL_WORKER_CREDENTIAL` instead of self-enrollment when a worker must not
hold the bootstrap token.
