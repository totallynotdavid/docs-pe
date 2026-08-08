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

Deploy as a Docker Compose application, not a Swarm service: the worker API has
to be published on one specific host address, and Swarm's ingress mesh publishes
on every interface.

```yaml
services:
  web:
    image: portal
    command: ["sh", "-ec", "portal migrate && exec portal web"]
    env_file: [stack.env]
    volumes:
      - /etc/portal/master.key:/run/secrets/portal-master-key:ro
    networks: [dokploy-network]
    labels:
      - traefik.enable=true

  worker-api:
    image: portal
    command: ["portal", "worker-api"]
    env_file: [stack.env]
    environment:
      # The container's own interface. What keeps this off the internet is the
      # host binding below, not this value.
      PORTAL_WORKER_API_HOST: 0.0.0.0
    volumes:
      - /etc/portal/master.key:/run/secrets/portal-master-key:ro
    ports:
      - "100.x.y.z:8443:8443" # the host's tailnet address, never 0.0.0.0
    networks: [dokploy-network]

networks:
  dokploy-network:
    external: true
```

Only `web` carries a Traefik label, so only `web` is reachable through the
tunnel. Docker Compose respects the host IP in a `ports` entry, which is what
confines the worker API to the tailnet. Domains for Compose applications are
configured through Traefik labels and are read at deploy time, so a domain
change needs a redeploy rather than a reload.

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
restores nothing usable; the file without the database is inert. Losing it means
every stored proxy credential and every enrolled second factor has to be
re-entered.

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

Worker nodes are ephemeral. Provision them with a short-lived, single-use auth
key (https://tailscale.com/kb/1085/auth-keys) and the ephemeral node flag
(https://tailscale.com/kb/1111/ephemeral-nodes), so a node that disconnects
leaves the tailnet on its own and no long-lived worker machines accumulate.

Tailnet membership is necessary but not sufficient. Every claim and publish also
carries a per-worker bearer credential checked against `portal_workers`, so
revoking one compromised node takes effect on its next request instead of
waiting for ACL propagation:

```sh
uv run --env-file .env portal enroll-worker \
  --worker-id poseidon-1 --tailscale-hostname poseidon-1.tailnet.ts.net

uv run --env-file .env portal enroll-worker --worker-id poseidon-1 --revoke
```

The credential is printed once. Put it in `PORTAL_WORKER_CREDENTIAL` on that
node, alongside `PORTAL_WORKER_API_URL` and `PORTAL_WORKER_ID`.

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
