# Portal deployment

How the portal is exposed on Dokploy, and what has to exist outside the
repository for the code to hold up. Application behaviour lives in
[packages/portal/readme.md](../packages/portal/readme.md); manual database
intervention lives in [packages/portal/operations.md](../packages/portal/operations.md).

## Topology

```
Browser ──▶ Cloudflare (TLS, WAF, Bot Management, Turnstile, edge rate limits)
              │ Cloudflare Tunnel
              ▼
          cloudflared on the Dokploy host  ── no inbound port is open
              ▼
          Traefik (Dokploy's ingress)
              ▼
          portal web ──┬─▶ Postgres (accounts, teams, jobs, credentials,
                       │             audit log, sessions and rate limits)
                       └─▶ master key file (mounted, never in the environment)

Worker fleet (Tailscale nodes, tag:worker) ──▶ portal worker-api
              │  published on the host's tailnet address only
              ├─▶ Postgres (worker-scoped queries only)
              ├─▶ master key file (credential reveal at claim time)
              └─▶ Object storage
```

The tunnel is why there is no origin firewall section here. `cloudflared` dials
out to Cloudflare, so the host publishes no inbound port for web traffic at all,
and Authenticated Origin Pull has nothing to authenticate: there is no origin
listener to reach. Every request the app sees arrived through Cloudflare because
that is the only path that exists.

That is also what makes `CF-Connecting-IP` trustworthy. Cloudflare sets it after
stripping any client-supplied copy, and no request can reach the app another
way, so it is the only header the application reads for the client address.
`X-Forwarded-For` is never consulted: it arrives on every request and is the
header an attacker would forge to move into a fresh rate-limit bucket. The app
does not run uvicorn with `proxy_headers` for the same reason, so nothing
downstream has to be allowlisted.

`portal web` and `portal worker-api` are two processes from the same image: two
entrypoints, two route tables, two listeners. The worker API has no DNS record
and no tunnel route, so Cloudflare never sees worker traffic and the public app
never serves a worker route.

`PORTAL_ENVIRONMENT` does not gate HTTPS enforcement, Secure cookies, or host
checking. Those follow `PORTAL_PUBLIC_ORIGIN`, which is where the deployment
declares how it is reached. The environment gates only what makes a laptop
workable, and `PortalSettings.validate()` refuses each of them in production: a
plain-http origin and a missing Turnstile configuration.

## Dokploy services

`web` is a plain Dokploy Application: git-connected, built from
[packages/portal/Dockerfile](../packages/portal/Dockerfile), the image's own
`CMD` runs unmodified (`portal migrate && portal provision ... && exec portal
web`). Nothing about it is compose-specific.

`worker-api` has to be published on one specific host address, and Swarm's
ingress mesh publishes on every interface, so it cannot be a Dokploy
Application; it deploys from the checked-in
[docker-compose.worker-api.yml](../docker-compose.worker-api.yml) as a plain
Docker Compose service instead. The compose file builds from the same
Dockerfile as `web` (so the two are never out of sync) and reads its
host-specific values (`PORTAL_OBJECT_ROOT_HOST`, `PORTAL_MASTER_KEY_HOST_PATH`,
`PORTAL_WORKER_API_BIND_IP`) from Dokploy's env rather than hardcoding them, so
the file itself stays portable across installations.

Only `web` carries a Traefik label, so only `web` is reachable through the
tunnel. Docker Compose respects the host IP in a `ports` entry, which is what
confines the worker API to the tailnet. Domains for Compose applications are
configured through Traefik labels and are read at deploy time, so a domain
change needs a redeploy rather than a reload.

Each worker node is a third kind of deployment, and it needs neither of the
above: a plain Dokploy Application (a Swarm service, same as `web`), git-
connected to the same repository, `command: ["portal", "worker"]`, no domain
and no port. It doesn't need the Compose exception `worker-api` needs because
it never listens on anything, only makes outbound calls to `worker-api` over
Tailscale. Being an ordinary Application means a worker node gets what `web`
already gets for free: redeploy on push and restart on crash, with nothing
per-node to maintain by hand.

Run `portal migrate` before the web process rather than as a separate step: it
is idempotent and the ledger makes a second run a no-op.

## Cloudflare

- WAF managed rules in block mode.
- Bot Management, plus a Turnstile widget on `/login`. The token is verified
  server-side against `https://challenges.cloudflare.com/turnstile/v0/siteverify`
  before the credential check runs, and verification fails closed: an
  unreachable siteverify makes logins unavailable rather than optional. Docs:
  https://developers.cloudflare.com/turnstile/
- An edge rate limiting rule on `/login` (around 20 req/min per client
  fingerprint) as a volumetric backstop. It is not the primary control; the
  per-account and per-source counters in the application are.

The content security policy allows `https://challenges.cloudflare.com` on
`script-src` and `frame-src` and nothing else beyond `'self'`. That is the only
external origin the pages load, and a stricter policy would leave the widget
unable to render, which fails login closed permanently rather than temporarily.

## The master key

Stored secrets use envelope encryption: a fresh AES-256-GCM data key per
payload, wrapped by a master key, with only the wrapped key stored beside the
ciphertext. `PORTAL_MASTER_KEY_FILE` names a file holding one key per line as
`<version> <urlsafe-base64 32 bytes>`, newest first.

It is a mounted file rather than an environment variable on purpose. Dokploy's
environment is visible in `docker inspect`, in the Dokploy UI, in
`/proc/<pid>/environ`, and it is inherited by every subprocess a worker spawns,
which includes the fetch process running site code. A file read once at startup
is in none of those.

What this protects is a leaked database dump or backup archive, since the key is
in neither. It does not protect against someone who already holds the host: the
host must read the key to serve a request. A hosted key service would narrow
that case and add a per-decrypt audit trail, and neither is reachable while the
application and the database share a machine.

Create the key with the file mode set before anything is written to it:

```sh
install -m 600 /dev/null /etc/portal/master.key
uv run portal new-key --version v1 > /etc/portal/master.key
```

Back it up separately from the database. A database backup without this file
restores nothing usable; the file without the database is inert. Losing it
means every stored proxy credential and every enrolled TOTP secret has to be
re-entered. Passkeys are unaffected: only a public key is stored, never
enveloped under the master key, so a lost key file does not strand them.

### Rotating it

```sh
uv run portal new-key --version v2 > /tmp/next
cat /etc/portal/master.key >> /tmp/next
install -m 600 /tmp/next /etc/portal/master.key && rm /tmp/next
```

Redeploy so both processes load the new keyring, then move the stored data keys
onto it and drop the retired line:

```sh
uv run --env-file .env portal rewrap   # reports how many rows moved
sed -i '/^v1 /d' /etc/portal/master.key
```

`portal rewrap` never reads or rewrites payload ciphertext, only the wrapped
data keys, so it is a pass over 60-byte blobs however large the payloads are. It
is safe to interrupt and rerun: every row names the key that wraps it, so a
partial run leaves nothing unreadable. Do not delete a key line until a rewrap
reports zero rows left on it, or the secrets it wrapped become unreadable and
the application says so loudly rather than silently failing a claim.

## Worker connectivity

Workers reach the worker API only over Tailscale. Tag the nodes and scope them
to the one port they need:

```json
{
  "tagOwners": {
    "tag:worker": ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["tag:worker"],
      "dst": ["tag:portal-worker-api:8443"]
    }
  ]
}
```

Docs: ACLs https://tailscale.com/kb/1018/acls, tags
https://tailscale.com/kb/1068/acl-tags

This installation's worker fleet is a fixed, small set of named nodes, not
autoscaled, so there is no ephemeral-node machinery here. Join each node to
the tailnet once, tag it `tag:worker`
(https://tailscale.com/kb/1068/acl-tags), and leave it joined for the life of
the box. Adding a node later means enrolling one more named machine the same
way, not building a provisioning pipeline for a fleet whose size changes.

Tailnet membership is necessary but not sufficient. Every claim and publish also
carries a per-worker bearer credential checked against `portal_workers`, so
revoking one compromised node takes effect on its next request instead of
waiting for ACL propagation.

A node gets that credential by self-enrolling. `portal worker` calls
`POST /enroll` on `worker-api` with `PORTAL_WORKER_BOOTSTRAP_TOKEN` as its
bearer token and gets back a credential minted for its `PORTAL_WORKER_ID`. This
runs on every start, not only the first: issuing is idempotent by worker_id
(`portal_workers` upserts on conflict), so a restarted or redeployed node
re-mints its own credential instead of a human running `portal enroll-worker`
and copying a value that is shown once. `PORTAL_WORKER_BOOTSTRAP_TOKEN` is one
shared secret across the fleet, configured identically on `worker-api` and on
every node; it only ever mints a worker-scoped credential, nothing else.

A node's environment needs `PORTAL_WORKER_ID`, `PORTAL_WORKER_API_URL`,
`PORTAL_WORKER_BOOTSTRAP_TOKEN`, and `PORTAL_WORKER_TAILSCALE_HOSTNAME`;
nothing on that node has to be provisioned by hand.

`portal enroll-worker` still exists for what self-enrollment does not cover:
issuing a fixed `PORTAL_WORKER_CREDENTIAL` for a node that should not hold the
bootstrap token, and revoking a node immediately rather than waiting for it to
restart:

```sh
uv run --env-file .env portal enroll-worker --worker-id aws-1 --revoke
```

Run the browser automation for each job in a locked-down container: non-root
user, read-only root filesystem outside a scoped tmp dir, no listening ports,
default seccomp profile. The Tailscale client and the worker's credential live
outside the container, so an RCE in the browser gets the container rather than
the node's tailnet identity.

## What is not deployed here

Redis. Sessions, rate-limit counters, and one-time tokens are rows in
`portal_ephemeral`, swept on a timer. At this installation's size a second
stateful service adds a failure mode that stops logins and buys no measurable
headroom; the reasoning is in
[packages/portal/readme.md](../packages/portal/readme.md#ephemeral-state).
