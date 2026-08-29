# Portal deployment

The production portal has a public web application and a private worker API:

```text
browser -> Cloudflare -> cloudflared -> web
web -> PostgreSQL, object store, master key
worker-api -> PostgreSQL, object store, master key
worker node -> worker-api (Tailscale)
worker node -> PostgreSQL (scoped role)
```

The worker API has no public route. Workers reach it over the tailnet for
enrollment, credential reveals, and result publication. Queue, heartbeat, and
proxy-slot operations use the worker's scoped PostgreSQL role.

## Prerequisites

Prepare these resources before starting:

- PostgreSQL reachable by `web`, `worker-api`, and worker nodes.
- An S3-compatible bucket for uploaded inputs and result payloads, with a
  read/write credential shared by `web` and `worker-api`.
- A master-key file mounted read-only into both portal processes.
- A Cloudflare tunnel and public HTTPS origin for `web`.
- A Tailscale path and ACL from worker nodes to `worker-api`.
- Resend credentials for account setup and notification mail.

Copy `.env.example` and set every production value. The web and worker-api
processes require `PORTAL_PUBLIC_ORIGIN` to use HTTPS, both Turnstile keys,
`PORTAL_WORKER_BOOTSTRAP_TOKEN`, `PORTAL_RESEND_API_KEY`, `PORTAL_MAIL_FROM`,
and all four `PORTAL_OBJECT_STORAGE_*` variables.
`PORTAL_TLS_TERMINATED_UPSTREAM=true` when an upstream terminates TLS before
the application receives the request.

When using `docker-compose.worker-api.yml`, also set the host-side paths and
tailnet binding that Compose interpolates:

```env
PORTAL_MASTER_KEY_HOST_PATH=/etc/portal/master.key
PORTAL_WORKER_API_BIND_IP=100.64.0.10
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

Deploy `web` and `worker-api` from the same revision with the same database,
object store, and master-key file. Configure their commands as follows:

```text
web        python -m portal.web.app
worker-api python -m portal.worker.api
worker     python -m portal.worker.agent
```

Expose only `web` through the tunnel. Bind `worker-api` to the tailnet
interface or publish it only on a tailnet address.

`worker-api` serves enrollment, credential reveals, and result publication.
Queue and slot traffic goes directly from each worker node to PostgreSQL.

Budget PostgreSQL connections for every web, worker-api, worker, admin, and
operator process. Each worker also keeps a dedicated connection for
notifications. Recheck the budget when changing worker concurrency or the
number of service processes.

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
- the worker API is unreachable from the public network; and
- cancellation prevents a late worker publish.

Use [Portal operations](../../packages/portal/operations.md) for database
checks. Deploy a new revision only after migrations are applied, and keep the
previous image available for rollback.
