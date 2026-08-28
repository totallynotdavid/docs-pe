# Portal deployment

The production portal has a public web application and a private worker API:

```text
browser -> Cloudflare -> cloudflared -> web -> PostgreSQL
                                      -> object store
                                      -> master key

worker node -> Tailscale -> worker-api -> PostgreSQL
                                      -> object store
                                      -> master key
```

The worker API has no public route. Workers call it over the tailnet and do not
need direct database credentials. The deployment uses the container images and
environment expected by `packages/portal/portal`.

## Prerequisites

Prepare these resources before starting:

- PostgreSQL reachable by both `web` and `worker-api`.
- A persistent object directory for uploaded inputs and result payloads.
- A master-key file mounted read-only into both portal processes.
- A Cloudflare tunnel and public HTTPS origin for `web`.
- A Tailscale path and ACL from worker nodes to `worker-api`.
- Resend credentials for account setup and notification mail.

Copy `.env.example` and set every production value. Production validation
requires `PORTAL_PUBLIC_ORIGIN` to use HTTPS, both Turnstile keys,
`PORTAL_WORKER_BOOTSTRAP_TOKEN`, `PORTAL_RESEND_API_KEY`, and
`PORTAL_MAIL_FROM`. `PORTAL_TLS_TERMINATED_UPSTREAM=true` when an upstream
terminates TLS before the application receives the request.

## Initial installation

Create the key directory before writing the key:

```sh
sudo install -d -m 700 /etc/portal
umask 077
uv run --env-file .env portal new-key --version v1 > /tmp/portal.master.key
sudo install -m 600 /tmp/portal.master.key /etc/portal/master.key
rm /tmp/portal.master.key
```

Mount the key at the path in `PORTAL_MASTER_KEY_FILE`, then run migrations and
provision the first administrator from the portal environment:

```sh
uv run --env-file .env portal migrate
uv run --env-file .env portal provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

Deploy `web` and `worker-api` from the same revision with the same database,
object store, and master-key file. Expose only `web` through the tunnel. Bind
`worker-api` to the tailnet interface or publish it only on a tailnet address.

## Worker enrollment

Use [worker fleet operations](worker-fleet.md) to prepare a node. A worker may
self-enroll on startup with `PORTAL_WORKER_BOOTSTRAP_TOKEN` and
`PORTAL_WORKER_TAILSCALE_HOSTNAME`, or use a fixed credential issued by:

```sh
uv run --env-file .env portal enroll-worker \
  --worker-id <worker-id> \
  --tailscale-hostname <tailnet-hostname>
```

The issued credential is shown once. Store it as `PORTAL_WORKER_CREDENTIAL` on
the worker and omit the bootstrap token when the node should not be able to
enroll itself. Revoke it when decommissioning the node:

```sh
uv run --env-file .env portal enroll-worker \
  --worker-id <worker-id> \
  --revoke
```

## Key rotation

The master-key file stores versioned keys, newest first. Keep the old key until
all encrypted rows have been rewrapped:

```sh
uv run --env-file .env portal new-key --version v2 > /tmp/master.key.next
cat /etc/portal/master.key >> /tmp/master.key.next
sudo install -m 600 /tmp/master.key.next /etc/portal/master.key
uv run --env-file .env portal rewrap
```

Back up the key separately from PostgreSQL. A database backup without the key
cannot restore encrypted credentials. Remove an old key only after rewrap has
completed and the running processes use the new file.

## Verify a deployment

Check the public origin through Cloudflare, then check the private path from a
worker node. Confirm that:

- the login page loads and Turnstile verification works;
- the first administrator can enroll a second factor;
- a small job reaches `running` and returns a result;
- `portal worker` heartbeats appear in `portal_workers`;
- the worker API is unreachable from the public network; and
- cancellation prevents a late worker publish.

Use [Portal operations](../../packages/portal/operations.md) for database
checks. Deploy a new revision only after migrations are applied, and keep the
previous image available for rollback.
