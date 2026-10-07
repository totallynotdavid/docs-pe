# Portal deployment

This guide covers the release sequence for a production portal. Read
[Portal topology](portal-topology.md) first for the services, volumes, and
network boundary of `docker-compose.core.yml`.

A deployment is a Dokploy project: the core Compose application on the Dokploy
host and one worker Compose application on each worker server. `ops/deploy.py`
creates and updates all of it by name, so the same command installs a new
deployment, upgrades one, and adds workers.

The Dokploy host runs Dokploy and the portal core together, which do not fit in
2 GB of memory: on a 2 GB server the host stopped answering SSH after the first
deploy. 8 GB works.

Deploy in this order:

1. Let CI publish the portal image.
2. Create a registry token.
3. Write the deployment's env file.
4. Run `ops/deploy.py` with the token. It signs every server in to the registry,
   and the `init` service installs the master key and applies migrations.
5. Provision the first administrator and back up the master key.
6. Verify a small job.

## Image

CI (`.github/workflows/ci.yml`) builds `packages/portal/Dockerfile` on every
push to `master` and `scale`, after the checks pass, and pushes
`ghcr.io/<owner>/<repository>/portal:<commit SHA>`. Deploy by that tag. The
deploy tool gives `web`, `worker-api`, `init`, and every worker the same image.

For local runs, build and tag the image with:

```sh
mise run portal:docker:build
```

The task tags `PORTAL_IMAGE`, or `portal:local` when that is unset.

### Registry access

Dokploy deploys Compose applications with the Docker login of the host that runs
them. Create a GitHub personal access token (classic) with only the
`read:packages` scope, and give it to the deploy tool with the GitHub user that
owns it:

```sh
export REGISTRY_USERNAME=<github user>
export REGISTRY_PASSWORD=<token>
```

The registry host comes from `--image`. Before it deploys an application, the
tool creates or updates a Dokploy registry entry for that host and has Dokploy
sign the application's server in: the Dokploy host for `core`, each worker's own
server for its application. The entries are named `<deployment>` and
`<deployment>/<worker>`, so a new worker's first deployment pulls its image with
no manual step, and a rerun after a failed deploy signs in again. An image
without a registry host, such as `portal:local`, is pulled without a login, and
so is every image when the two variables are unset.

## Env file

The deployment's settings live in one env file outside the repository. The tool
stores it as the core application's environment, which Compose interpolates:

```env
PORTAL_PUBLIC_ORIGIN=https://<public host>
PORTAL_TURNSTILE_SITE_KEY=<site key>
PORTAL_TURNSTILE_SECRET=<secret>
PORTAL_RESEND_API_KEY=<api key>
PORTAL_MAIL_FROM=<sender>
PORTAL_WORKER_BOOTSTRAP_TOKEN=<openssl rand -hex 32>
PORTAL_WORKER_DATABASE_ADDRESS=<core host tailnet address>:<POSTGRES_PUBLISHED_PORT>
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
set any `PORTAL_DATABASE_DSN` or `PORTAL_OBJECT_STORAGE_*` variable. The image
comes from `--image`, so do not set `PORTAL_IMAGE` either.

Garage stores its access key the first time it starts. Keep the Garage values
unchanged afterwards: Garage refuses to start when the stored key has a
different secret.

A second deployment on the same host needs its own published ports. Its
`PORTAL_WORKER_DATABASE_ADDRESS` names its own PostgreSQL port.

## Deploy

Run the tool with Dokploy's URL and an API key created in Dokploy's profile
settings:

```sh
export DOKPLOY_URL=https://<dokploy host>
export DOKPLOY_API_KEY=<api key>
uv run python -m ops.deploy deploy <deployment> \
  --image ghcr.io/<owner>/<repository>/portal:<commit SHA> \
  --env-file <path>/core.env \
  --worker <worker>=<worker tailnet address>
```

`<deployment>` names the Dokploy project; repeat `--worker` for each worker. The
tool finds or creates, by name:

- the project `<deployment>` and its production environment;
- the Compose application `core`, from the text of `docker-compose.core.yml` and
  the env file, with a domain for `PORTAL_PUBLIC_ORIGIN`'s host on service
  `web`, port 8000;
- for workers, an SSH key named `<deployment>` that Dokploy generates;
- for each worker, the server `<deployment>/<worker>`, reached as root over SSH
  at the given address, and the Compose application `worker-<worker>`, from
  `docker-compose.worker.yml`;
- with registry credentials, the registry entries described under
  [Registry access](#registry-access).

It then deploys each application whose file, environment, or domain changed, or
whose last deployment did not finish, and waits for each deployment. Each line
of output names one object and what happened to it. A second run with the same
inputs prints only `ok` lines. An upgrade is the same command with the new tag.

A worker's environment comes from the core's: the worker API URL uses the host
of `PORTAL_WORKER_DATABASE_ADDRESS` and `WORKER_API_PUBLISHED_PORT`, the worker
ID and tailnet hostname are its name, and it enrolls with the bootstrap token.
Name each worker after its tailnet machine name.

The domain makes Dokploy's Traefik route the public host to `web` over plain
HTTP. TLS ends in front of Traefik, at the Cloudflare edge (see
[Portal topology](portal-topology.md#traffic-boundaries)).

`--no-deploy` creates and updates the objects without deploying anything; the
next run without it deploys every application that has not run.

`list` prints each object of a deployment: project, Compose applications with
their `appName` and status, and servers with their address.

```sh
uv run python -m ops.deploy list <deployment>
```

### Worker servers

A worker server needs Docker and root SSH from the Dokploy host at the address
given to `--worker`. The tool does not run Dokploy's server setup, which would
install Traefik, Swarm, and builders a worker never uses. It checks that Docker
answers and stops otherwise.

The first run that has workers creates the deployment's SSH key and prints its
public key. Add that key to root's `authorized_keys` on each worker server. A
server reached through Tailscale SSH needs no key when the tailnet policy lets
the core host in as root.

Each worker also needs tailnet access to the core host's published PostgreSQL
and worker API ports (see [Portal topology](portal-topology.md)).

### Remove a worker

```sh
uv run python -m ops.deploy remove-worker <deployment> <worker>
```

The tool stops the worker's application, revokes the worker in the portal, then
deletes the application, its server, and its registry entry. The revoke runs
`portal-admin worker revoke` in `worker-api` as a Dokploy schedule, so the
worker's API credential and PostgreSQL login stop working before its server is
gone. A removal that fails partway resumes when run again. Stop naming the
worker in `--worker` on later deploys; a deploy that names it enrolls it again.

## Initial installation

The first deploy runs `init`, which creates master key `v1` in the `master-key`
volume and applies every migration. After it exits, `web` and `worker-api`
start. Dokploy uses the application's `appName`, shown by `list`, as the Compose
project name, so containers are named `<appName>-<service>-1` and volumes
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

The first invocation can stop after creating a pending administrator. Sign in,
enroll a second factor at `/security`, then rerun the command to create the
team. To validate and activate its first proxy credential, add
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
`portal_schema_migrations`. It is not a schema diff tool. Editing a filename
that is already recorded does not make that file run again. `init` runs it on
every deploy, before the new `web` and `worker-api` containers start.

The migration directory contains the baseline schema and numbered follow-up
changes.

For local development, regenerate the complete schema snapshot with:

```sh
mise run portal:schema
```

That task applies the local migrations and writes a generated schema snapshot
under `packages/portal/`, without the migration ledger. For production, use the
approved database schema-change process or add a new numbered migration when the
change is incremental. Back up PostgreSQL and verify the target tables and
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

## Workers outside Dokploy

A worker enrolls when it starts. Enrollment returns its API credential and a DSN
for its scoped PostgreSQL login, addressed through
`PORTAL_WORKER_DATABASE_ADDRESS`.

A node that runs the agent without the bootstrap token uses fixed credentials
issued by:

```sh
uv run python -m ops.deploy worker issue <deployment> <worker-id> \
  --tailscale-hostname <node tailnet name>
```

The tool asks Dokploy for the deployment's core application and finds its
`worker-api` container with `docker`, so run it on the Dokploy host or set
`DOCKER_HOST=ssh://root@<dokploy host>`. It stops when it finds no container or
more than one. The tailnet name defaults to the worker ID. The credentials go
straight to your terminal and are never sent through Dokploy, whose logs keep
command output.

The command shows both credentials once. Set them as `PORTAL_WORKER_CREDENTIAL`
and `PORTAL_WORKER_DATABASE_DSN` in the node's worker environment and leave
`PORTAL_WORKER_BOOTSTRAP_TOKEN` unset. The agent refuses to start when neither
the token nor both credentials are present. Revoke both credentials when
decommissioning the node:

```sh
uv run python -m ops.deploy worker revoke <deployment> <worker-id>
```

## Key rotation

The master-key file stores versioned keys, newest first. Rotation adds a new
active key, restarts the processes onto it, and rewraps every stored secret with
it:

```sh
uv run python -m ops.deploy rotate-key <deployment> v2
```

The tool records the version on the core application and redeploys it. `init`
then prepends the new key to the key file, and `web` and `worker-api` restart
with it. The tool finally runs `portal-admin key rewrap` in `worker-api` as a
Dokploy schedule and waits for it. The version names a key, so use one that the
file has never held; the key itself never leaves the host.

Rerun the command after any failure: it changes nothing that already holds and
rewraps again, and rewrapping is idempotent. The env file must not set
`PORTAL_MASTER_KEY_VERSION`; later deploys keep the recorded version.

Back up the new file as under [Initial installation](#initial-installation).
Remove an old key line only after the command has finished.

## Verify a deployment

Check the public origin through Cloudflare, then check the private path from a
worker node. Confirm that:

- `init` exited with status 0, and `web`, `worker-api`, `postgres`, and `garage`
  are healthy;
- the login page loads and Turnstile verification works;
- the first administrator can enroll a second factor;
- the initial team has an active proxy credential;
- a small job reaches `running` and returns a result;
- worker heartbeats appear in `portal_workers`;
- the published PostgreSQL and worker API ports do not answer from the public
  network or from a tailnet device outside this deployment's worker grant; and
- cancellation prevents a late worker publish.

[Troubleshooting](troubleshooting.md) has read-only database queries. Keep the
previous image tag available for rollback.

## End-to-end test

`scripts/e2e-portal.sh` is the hook `scripts/e2e-aws.sh` runs as `E2E_AWS_HOOK`,
on a core server with Dokploy and a worker server, both on the tailnet. With no
browser and no console, it:

1. builds the image on core from the working tree and loads it on the worker;
2. creates Dokploy's first administrator and an API key through Dokploy's
   internal routes, and refuses any Dokploy but v0.30.7, the version those
   routes were checked on;
3. generates every secret for the run, and runs `ops/deploy.py` twice, the
   second time requiring only `ok` lines;
4. serves the portal over HTTPS with `tailscale serve` on core, in production
   mode with Cloudflare's always-pass Turnstile test keys and a placeholder
   Resend key;
5. provisions the first administrator, enrolls TOTP, and validates the proxy
   credential;
6. submits one lookup from the worker server through the tailnet, and waits
   until the worker has completed it and its result is in Garage;
7. removes the worker and checks that the portal revoked it.

On failure it exits non-zero and names the failing step. The run's secrets stay
in `$TMPDIR/docs-pe-e2e/<core host>` on the caller, so the hook can run again
against the same servers.

```sh
E2E_PROXY_ENV=<file> E2E_AWS_HOOK=scripts/e2e-portal.sh scripts/e2e-aws.sh all
```

`E2E_PROXY_ENV` names a file with unquoted `PORTAL_PROVISION_GEONODE_USERNAME=`
and `PORTAL_PROVISION_GEONODE_PASSWORD=` lines. `E2E_JOB_SOURCE` and
`E2E_JOB_DOCUMENT` choose the lookup; the default is `osiptel` for SUNAT's own
RUC. The tailnet needs MagicDNS, HTTPS certificates, and Serve enabled, and must
let the test servers reach each other as root over SSH.
