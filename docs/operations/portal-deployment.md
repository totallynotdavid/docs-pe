# Portal deployment

This guide covers the release sequence for a production portal. Read
[Portal topology](portal-topology.md) first for the services, volumes, and
network boundary of `docker-compose.core.yml`.

Deploy in this order:

1. Publish the portal image.
2. Create the core Compose application and its environment.
3. Deploy it. The `init` service installs the master key and applies migrations.
4. Provision the first administrator and back up the master key.
5. Enroll workers and verify a small job.

## Image

Every portal service and every worker runs the image named by `PORTAL_IMAGE`.
Build it from `packages/portal/Dockerfile` at the repository root, push it to a
registry that the core host and the worker nodes can pull from, and set
`PORTAL_IMAGE` to an immutable tag. `web`, `worker-api`, `init`, and the
workers must run the same tag.

For local runs, build and tag the image with:

```sh
mise run portal:docker:build
```

The task tags `PORTAL_IMAGE`, or `portal:local` when that is unset.

## Core application

Create a Dokploy Compose application. Choose the raw source and paste
`docker-compose.core.yml`. Add a domain on service `web`, port 8000, and route
it through the Cloudflare tunnel. Do not add a domain to any other service.

Set the application's environment. Dokploy writes it to the `.env` file that
Compose interpolates:

```env
PORTAL_IMAGE=<registry>/portal:<tag>
PORTAL_PUBLIC_ORIGIN=https://<public host>
PORTAL_TURNSTILE_SITE_KEY=<site key>
PORTAL_TURNSTILE_SECRET=<secret>
PORTAL_RESEND_API_KEY=<api key>
PORTAL_MAIL_FROM=<sender>
PORTAL_WORKER_BOOTSTRAP_TOKEN=<openssl rand -hex 32>
PORTAL_WORKER_DATABASE_ADDRESS=<core host tailnet name>:<POSTGRES_PUBLISHED_PORT>
POSTGRES_DB=portal
POSTGRES_USER=portal
POSTGRES_PASSWORD=<openssl rand -hex 32>
POSTGRES_PUBLISHED_PORT=5432
WORKER_API_PUBLISHED_PORT=8443
GARAGE_RPC_SECRET=<openssl rand -hex 32>
GARAGE_DEFAULT_ACCESS_KEY=GK<openssl rand -hex 12>
GARAGE_DEFAULT_SECRET_KEY=<openssl rand -hex 32>
GARAGE_DEFAULT_BUCKET=portal-objects
```

`POSTGRES_PASSWORD` is placed in a DSN without escaping. Use a URL-safe value,
such as the hex output above. Garage access key IDs are `GK` followed by 24
hexadecimal characters.

Production validation refuses to start `web` or `worker-api` unless
`PORTAL_PUBLIC_ORIGIN` uses HTTPS and the Turnstile keys, Resend key, sender,
bootstrap token, and `PORTAL_WORKER_DATABASE_ADDRESS` are set. The Compose file
derives the database DSN, master-key path, and object-store settings, so do not
set any `PORTAL_DATABASE_DSN` or `PORTAL_OBJECT_STORAGE_*` variable.

Garage stores its access key the first time it starts. Keep the Garage values
unchanged afterwards: Garage refuses to start when the stored key has a
different secret.

A second deployment on the same host needs its own published ports. Its
`PORTAL_WORKER_DATABASE_ADDRESS` names its own PostgreSQL port.

## Initial installation

Deploy the application. `init` creates master key `v1` in the `master-key`
volume and applies every migration. After it exits, `web` and `worker-api`
start. Dokploy uses the application's `appName` as the Compose project name, so
containers are named `<appName>-<service>-1` and volumes
`<appName>_<volume>`.

Provision the first administrator inside `web`, which already has the portal
environment:

```sh
export PORTAL_PROVISION_ADMIN_PASSWORD='<password>'
docker exec -e PORTAL_PROVISION_ADMIN_PASSWORD <appName>-web-1 \
  portal-admin provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

The first invocation can stop after creating a pending administrator. Complete
second-factor enrollment at `/security/setup`, then rerun the command to create
the team. To validate and activate its first proxy credential, add
`--proxy-provider geonode` and pass the corresponding
`PORTAL_PROVISION_GEONODE_<FIELD>` variables with `-e`. The proxy validation
opens a real provider session and releases it before provisioning completes.

Back up the master key outside the host:

```sh
docker cp <appName>-web-1:/app/.data/keys/master.key ./master.key
```

A database backup without the key cannot restore encrypted credentials.

## Schema changes

`portal-admin migrate` applies each SQL file once and records its filename in
`portal_schema_migrations`. It is not a schema diff tool. Editing a filename that
is already recorded does not make that file run again. `init` runs it on every
deploy, before the new `web` and `worker-api` containers start.

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
grants before deploying the new image.

## Worker API capacity

Set `PORTAL_WORKER_API_WORKERS` to the number of worker-api processes. It
defaults to `4`. Each process creates its own application and PostgreSQL pool,
with a maximum of five connections, so the worker-api ceiling is five times the
configured process count before accounting for web, worker, admin, and operator
connections.

Increase the setting only when the host and PostgreSQL connection budget have
room, then redeploy. It is independent of the number of lookup lanes on worker
nodes.

## Worker enrollment

Use [worker fleet operations](worker-fleet.md) to prepare a node. A node runs
`docker-compose.worker.yml` with this environment:

```env
PORTAL_IMAGE=<registry>/portal:<tag>
PORTAL_WORKER_API_URL=http://<core host tailnet name>:<WORKER_API_PUBLISHED_PORT>
PORTAL_WORKER_BOOTSTRAP_TOKEN=<the core's bootstrap token>
PORTAL_WORKER_ID=<worker id>
PORTAL_WORKER_TAILSCALE_HOSTNAME=<node tailnet name>
```

The worker enrolls when it starts. Enrollment returns its API credential and a
DSN for its scoped PostgreSQL login, addressed through
`PORTAL_WORKER_DATABASE_ADDRESS`.

A node that runs the agent without the bootstrap token uses fixed credentials
issued by:

```sh
docker exec <appName>-worker-api-1 portal-admin worker issue \
  --worker-id <worker-id> \
  --tailscale-hostname <node tailnet name>
```

The command shows both credentials once. Set them as
`PORTAL_WORKER_CREDENTIAL` and `PORTAL_WORKER_DATABASE_DSN` in the node's
worker environment and leave `PORTAL_WORKER_BOOTSTRAP_TOKEN` unset. The agent
refuses to start when neither the token nor both credentials are present.
Revoke both credentials when decommissioning the node:

```sh
docker exec <appName>-worker-api-1 portal-admin worker revoke \
  --worker-id <worker-id>
```

## Key rotation

The master-key file stores versioned keys, newest first. Keep the old key until
all encrypted rows have been rewrapped. `web` and `worker-api` mount the key
read-only, so rotate it through the volume:

```sh
docker run --rm -v <appName>_master-key:/app/.data/keys "$PORTAL_IMAGE" \
  portal-admin key rotate --path /app/.data/keys/master.key --version v2
docker restart <appName>-web-1 <appName>-worker-api-1
docker exec <appName>-worker-api-1 portal-admin key rewrap
```

Back up the new file. Remove an old key only after rewrap has completed and the
running processes use the new file.

## Verify a deployment

Check the public origin through Cloudflare, then check the private path from a
worker node. Confirm that:

- `init` exited with status 0, and `web`, `worker-api`, `postgres`, and
  `garage` are healthy;
- the login page loads and Turnstile verification works;
- the first administrator can enroll a second factor;
- the initial team has an active proxy credential;
- a small job reaches `running` and returns a result;
- worker heartbeats appear in `portal_workers`;
- the published PostgreSQL and worker API ports do not answer from the public
  network or from a tailnet device outside this deployment's worker grant; and
- cancellation prevents a late worker publish.

Use [Portal operations](../../packages/portal/operations.md) for database
checks. Keep the previous image tag available for rollback.

## Migrating from the previous layout

The previous layout ran PostgreSQL, MinIO, and `worker-api` as separate
Compose applications on the shared `dokploy-network`, published them through
Tailscale sidecars, ran `web` as a Dokploy application, and kept the master key
in a host file. Move one installation to the core file once, during a
maintenance window, on the host that ran it.

1. Create the core Compose application as described above, with the new
   environment, and note its `appName`. Do not deploy it yet: a deploy against
   empty volumes installs a new master key. Put the same variables in a
   `core.env` file on the host, save the Compose text as `core.yml` next to it,
   and define:

   ```sh
   core() { docker compose -p <appName> --env-file core.env -f core.yml "$@"; }
   ```

2. Stop the workers, the old `web` application, and the old `worker-api` so
   that nothing writes during the copy.

3. Dump the worker roles and the database from the old PostgreSQL container.
   The roles filter keeps the worker base role, every scoped worker login with
   its password hash, and their memberships:

   ```sh
   docker exec <old postgres container> pg_dumpall -U <old user> --roles-only \
     | grep -E '^(CREATE|ALTER) ROLE "?portal_worker|^GRANT portal_worker_base TO ' \
     | sed 's/ GRANTED BY [^;]*;/;/' > roles.sql
   docker exec <old postgres container> pg_dump -U <old user> -Fc <old database> > portal.dump
   ```

4. Start the new PostgreSQL and Garage, then restore the roles before the
   database so that its grants resolve:

   ```sh
   core up -d --wait postgres garage
   core exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < roles.sql
   core exec -T postgres sh -c 'pg_restore --no-owner --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < portal.dump
   ```

5. Carry the master key into the new volume:

   ```sh
   sudo cat <old master-key host path> \
     | core run --rm --no-deps -T init sh -c 'umask 077 && cat > "$PORTAL_MASTER_KEY_FILE"'
   ```

6. Copy the objects with an S3-aware copy. rclone runs on both project
   networks, reading the old store by its container name:

   ```sh
   docker create --name portal-object-copy --network dokploy-network \
     -e RCLONE_CONFIG_OLD_TYPE=s3 -e RCLONE_CONFIG_OLD_PROVIDER=Minio \
     -e RCLONE_CONFIG_OLD_ENDPOINT=http://<old minio container>:9000 \
     -e RCLONE_CONFIG_OLD_ACCESS_KEY_ID=<old access key> \
     -e RCLONE_CONFIG_OLD_SECRET_ACCESS_KEY=<old secret key> \
     -e RCLONE_CONFIG_NEW_TYPE=s3 -e RCLONE_CONFIG_NEW_PROVIDER=Other \
     -e RCLONE_CONFIG_NEW_ENDPOINT=http://<appName>-garage-1:3900 \
     -e RCLONE_CONFIG_NEW_REGION=garage \
     -e RCLONE_CONFIG_NEW_ACCESS_KEY_ID=<GARAGE_DEFAULT_ACCESS_KEY> \
     -e RCLONE_CONFIG_NEW_SECRET_ACCESS_KEY=<GARAGE_DEFAULT_SECRET_KEY> \
     --entrypoint sh rclone/rclone:1.75.1 -c \
     'rclone copy --checksum old:<old bucket> new:<GARAGE_DEFAULT_BUCKET> && rclone check old:<old bucket> new:<GARAGE_DEFAULT_BUCKET>'
   docker network connect <appName>_default portal-object-copy
   docker start -a portal-object-copy
   docker rm portal-object-copy
   ```

   `rclone check` must report no differences.

7. Deploy the application from Dokploy. `init` finds the key and applies only
   migrations the old database had not recorded. Confirm the checks in
   [Verify a deployment](#verify-a-deployment) that do not need a worker, and
   that an existing job's result downloads.

8. Re-point the workers. Set `PORTAL_IMAGE` and `PORTAL_WORKER_API_URL` in each
   node's worker environment and redeploy it; the node enrolls again against
   the restored database. On a node with fixed credentials, replace the host
   and port in `PORTAL_WORKER_DATABASE_DSN` with
   `PORTAL_WORKER_DATABASE_ADDRESS`.

9. After a small job succeeds, delete the old `web`, PostgreSQL, object
   storage, and `worker-api` applications, their Tailscale Services and
   sidecar devices, and their ACL entries. Keep the old volumes, `roles.sql`,
   and `portal.dump` until the new deployment has run through a backup cycle,
   then delete them and the old host key file.
