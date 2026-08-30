# Portal deployment

This guide covers the release sequence for the production portal. Read
[Portal topology](portal-topology.md) first when configuring Tailscale,
Dokploy, Compose, volumes, or service DNS.

Deploy in this order:

1. Prepare the network and secrets.
2. Apply the database schema.
3. Install or rotate the master key.
4. Deploy `web` and `worker-api` from the same revision.
5. Enroll workers and verify a small job.

## Prerequisites

Prepare the network and secrets described in [Portal topology](portal-topology.md),
including the Tailscale Services, ACL grants, shared network, master-key mount,
Cloudflare tunnel, and Resend credentials.

Copy `.env.example` and set every production value. The web and worker-api
processes require `PORTAL_PUBLIC_ORIGIN` to use HTTPS, both Turnstile keys,
`PORTAL_WORKER_BOOTSTRAP_TOKEN`, `PORTAL_RESEND_API_KEY`, `PORTAL_MAIL_FROM`,
and all four `PORTAL_OBJECT_STORAGE_*` variables.
`PORTAL_TLS_TERMINATED_UPSTREAM=true` when an upstream terminates TLS before
the application receives the request.

When using `docker-compose.worker-api.yml`, also set the host-side path Compose
interpolates and the sidecar's auth key:

```env
PORTAL_MASTER_KEY_FILE=/run/secrets/portal-master-key
PORTAL_MASTER_KEY_HOST_PATH=/etc/portal/master.key
PORTAL_DATABASE_DSN=postgresql://<user>:<password>@postgres:5432/<database>
PORTAL_OBJECT_STORAGE_ENDPOINT=http://minio:9000
TS_AUTHKEY_WORKER_API=tskey-auth-...
```

## Initial installation

Create the key directory before writing the key:

```sh
sudo install -d -m 700 /etc/portal
sudo uv run portal-admin key install \
  --path /etc/portal/master.key \
  --version v1
```

Mount the key at the path in `PORTAL_MASTER_KEY_FILE`, then run migrations and
provision the first administrator from the portal environment:

```sh
uv run --env-file .env portal-admin migrate
uv run --env-file .env portal-admin provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

The first invocation can stop after creating a pending administrator. Complete
second-factor enrollment at `/security/setup`, then rerun the command to create
the team. To validate and activate its first proxy credential, add
`--proxy-provider geonode` and set the corresponding
`PORTAL_PROVISION_GEONODE_<FIELD>` environment variables. The proxy validation
opens a real provider session and releases it before provisioning completes.

## Schema changes

`portal-admin migrate` applies each SQL file once and records its filename in
`portal_schema_migrations`. It is not a schema diff tool. Editing a filename that
is already recorded does not make that file run again.

The migration directory contains the baseline schema and numbered follow-up
changes. `portal_lookup_attempts` is part of the baseline
`001_portal.sql`, so an installation that already recorded `001` will not get
that table from `portal-admin migrate`. Do not deploy code that requires a new
table until the database has it.

For local development, regenerate the complete schema snapshot with:

```sh
mise run portal:schema
```

That task applies the local migrations and writes a generated schema snapshot
under `packages/portal/`, without the migration ledger. For production, use the
approved database schema-change process or add a new numbered migration when
the change is incremental. Back up PostgreSQL and verify the target tables and
grants before starting the new application image.

## Deploy services

Deploy `web` and `worker-api` from the same revision with the same database,
object store, and master-key file. Configure their commands as follows:

```text
web        python -m portal.web.app
worker-api python -m portal.worker.api
```

Expose only `web` through the tunnel. `worker-api` publishes no host port at
all; it's reachable only through `svc:worker-api` and the `tag:worker-fleet`
grant.

`worker-api` serves enrollment, credential reveals, and result publication.
Queue and slot traffic goes directly from each worker node to PostgreSQL.
The worker Compose resource builds the portal image and runs
`python -m portal.worker.agent`.

## Worker API capacity

Set `PORTAL_WORKER_API_WORKERS` to the number of worker-api processes. It
defaults to `4`. Each process creates its own application and PostgreSQL pool,
with a maximum of five connections, so the worker-api ceiling is five times the
configured process count before accounting for web, worker, admin, and operator
connections.

Increase the setting only when the host and PostgreSQL connection budget have
room, then redeploy the worker-api service. It is independent of the number of
lookup lanes on worker nodes.

## Worker enrollment

Use [worker fleet operations](worker-fleet.md) to prepare a node. A worker may
self-enroll on startup with `PORTAL_WORKER_BOOTSTRAP_TOKEN` and
`PORTAL_WORKER_TAILSCALE_HOSTNAME`, or use credentials issued by:

```sh
uv run --env-file .env portal-admin worker issue \
  --worker-id <worker-id> \
  --tailscale-hostname <tailnet-hostname>
```

The command shows both credentials once. Store them as
`PORTAL_WORKER_CREDENTIAL` and `PORTAL_WORKER_DATABASE_DSN` on the worker. A
fixed worker does not need `PORTAL_WORKER_BOOTSTRAP_TOKEN`. Revoke both
credentials when decommissioning the node:

```sh
uv run --env-file .env portal-admin worker revoke \
  --worker-id <worker-id>
```

## Key rotation

The master-key file stores versioned keys, newest first. Keep the old key until
all encrypted rows have been rewrapped:

```sh
sudo uv run portal-admin key rotate \
  --path /etc/portal/master.key \
  --version v2
uv run --env-file .env portal-admin key rewrap
```

Back up the key separately from PostgreSQL. A database backup without the key
cannot restore encrypted credentials. Remove an old key only after rewrap has
completed and the running processes use the new file.

## Verify a deployment

Check the public origin through Cloudflare, then check the private path from a
worker node. Confirm that:

- the login page loads and Turnstile verification works;
- the first administrator can enroll a second factor;
- the initial team has an active proxy credential;
- a small job reaches `running` and returns a result;
- worker heartbeats appear in `portal_workers`;
- the worker API is unreachable from the public network;
- a device on the tailnet without `tag:worker-fleet` cannot reach
  `svc:database`, `svc:objectstorage`, or `svc:worker-api`; and
- cancellation prevents a late worker publish.

Use [Portal operations](../../packages/portal/operations.md) for database
checks. Deploy a new revision only after its required schema is installed by
the migration or release-schema procedure, and keep the previous image
available for rollback.
