# Portal deployment

The deployment runs a browser-facing app and a worker API over a named fleet:

```text
Cloudflare -> cloudflared -> Traefik -> portal web -> PostgreSQL
                                      -> mounted master key

worker nodes -> Tailscale -> portal worker-api -> PostgreSQL
                                             -> local object store
```

`web` is the public application. `worker-api` is reachable only on the tailnet.
Workers call it over Tailscale and do not need direct PostgreSQL access.

The deployment uses a Dokploy Application for `web`, a Compose service for
`worker-api` because it must bind to one host address, and one Dokploy
Application per worker node. Worker applications run `portal worker`, expose no
port, and make outbound requests only.

## Edge and client identity

Cloudflare terminates TLS, applies WAF and bot controls, and forwards traffic
through the tunnel. The origin has no public listener. The application trusts
`CF-Connecting-IP` because the tunnel is the only route to it and ignores
`X-Forwarded-For`.

`PORTAL_PUBLIC_ORIGIN` controls HTTPS enforcement, secure cookies, and host
checking. `PORTAL_ENVIRONMENT` controls only local-development conveniences.
Production validation rejects an insecure origin and missing Turnstile
configuration.

## Master key

The master key file contains one key per line, newest first:

```text
<version> <urlsafe-base64 32 bytes>
```

It is mounted as a file rather than passed through the environment. The file
protects encrypted credentials and TOTP secrets from a database-only leak. It
does not protect a host that can read the file.

Create and rotate it with restrictive permissions:

```sh
install -m 600 /dev/null /etc/portal/master.key
uv run portal new-key --version v1 > /etc/portal/master.key

uv run portal new-key --version v2 > /tmp/next-key
cat /etc/portal/master.key >> /tmp/next-key
install -m 600 /tmp/next-key /etc/portal/master.key
```

Redeploy both listeners, run `uv run portal rewrap`, and remove an old key only
after the command reports zero rows using it. Back up the key separately from
PostgreSQL. A database backup without the key cannot restore encrypted
credentials.

## Worker connectivity

Workers enroll with `POST /enroll` using the shared bootstrap token. The API
returns a credential scoped to `PORTAL_WORKER_ID`; enrollment is idempotent.
Claims and publishes check that credential on every request, so revoking a
worker takes effect without waiting for a tailnet change.

The node needs `PORTAL_WORKER_ID`, `PORTAL_WORKER_API_URL`,
`PORTAL_WORKER_BOOTSTRAP_TOKEN`, and `PORTAL_WORKER_TAILSCALE_HOSTNAME`. Use
`portal enroll-worker --worker-id <id> --revoke` to revoke a node or to manage a
node that must not hold the bootstrap token.

Run browser automation in a non-root container with a read-only root filesystem
outside a scoped temporary directory, no listening ports, and the default
seccomp profile. Keep the worker credential and Tailscale client on the node,
outside that container.
