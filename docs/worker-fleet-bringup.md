# Worker fleet bring-up

Status as of 2026-08-14. This used to be a record of manual steps taken to
unblock one stuck job by hand (`nohup` on a box, `docker exec` into `web` to
run `portal enroll-worker`, a Dokploy compose service pinned to an
already-built local image tag). All of that is superseded: self-enrollment
and a git-connected `worker-api` are now real, built, tested code (see
`docs/portal-deployment.md#worker-connectivity` and
`docs/portal-deployment.md#dokploy-services` for the design). This doc is now
the runbook for standing up a node, kept short because the whole point was to
make that not require a runbook like the old one.

The fleet is a fixed, small set of named nodes (`master` runs `web` and
`worker-api`; `aws`, `poseidon`, `zeus` run the worker agent), not autoscaled.
Adding `poseidon`/`zeus` back once they're up again is the same three steps
as adding any new node.

## Resource identifiers

- Dokploy project `docs-pe`, environment `production`: `projectId
  7vIKTngpThRQOl6qLNj6z`, `environmentId LNBiAHi2juK4fbQQ58lzs`
- `web` application: `applicationId pwI2OMynqxYOYS4E8mHV5`, container name
  prefix `app-hack-virtual-driver-31w662`, git-connected
  (`totallynotdavid/docs-pe`, branch `scale`, `packages/portal/Dockerfile`),
  runs on host `master` (tailnet `100.86.240.39`,
  `master.taila2cbc1.ts.net`)
- `worker-api` compose service: `composeId 4j2tZFo9wcw4v9IeEjsnc`, container
  name prefix `compose-index-bluetooth-microchip-uzxs38`, git-connected to the
  same repo/branch/Dockerfile as `web` via the checked-in
  `docker-compose.worker-api.yml`, also on `master`
- A pre-existing Dokploy server row for `poseidon` (`serverId
  6tVjkiNoiy0GtLfMf6oXT`, `serverType: build`, user `dokploy`) predates this
  session; it is not currently used by anything below and poseidon is down.

## Adding a node

1. Get Docker running on the box and `dubu` (or whichever user Dokploy will
   SSH in as) into the `docker` group. This needs root, one time, and isn't
   automatable from here: `aws` (and presumably `poseidon`/`zeus` again once
   they're back) has no Docker installed and `dubu` only has interactive
   (password) sudo, not passwordless. Someone with the box's password has to
   run this once:
   ```sh
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker dubu
   ```
   Confirm with a fresh SSH connection (group membership doesn't apply to an
   already-open session): `ssh dubu@<host> docker ps`.

2. Register the box as a Dokploy server (`server-create`): name, tailnet IP,
   port 22, an SSH user that can reach it. Tailscale SSH is what actually
   gates who can log in as whom here, not the key content: connecting as a
   fresh `dokploy` user was refused with `tailnet policy does not permit you
   to SSH as user "dokploy"` even holding the right key, while `dubu` was
   allowed straight through. Either get a tailnet SSH ACL rule added for a
   `dokploy` user, or register the server using `dubu` and a dedicated key
   appended to `dubu`'s own `~/.ssh/authorized_keys` (no root needed for
   that part, `dubu` can write its own authorized_keys). Then
   `server-setup` to have Dokploy finish provisioning (Swarm join etc.).

3. Create a Dokploy Application on that server: git-connected to the same
   repository/branch/Dockerfile as `web`, `command: ["portal", "worker"]`, no
   domain, no port (the agent only makes outbound calls). Env:
   - `PORTAL_WORKER_API_URL=http://100.86.240.39:8443`
   - `PORTAL_WORKER_ID=<name>-1`
   - `PORTAL_WORKER_BOOTSTRAP_TOKEN=<same value as web and worker-api>`
   - `PORTAL_WORKER_TAILSCALE_HOSTNAME=<name>.taila2cbc1.ts.net`

   Deploy. The agent self-enrolls on start (`POST /enroll`, idempotent by
   `PORTAL_WORKER_ID`), so there is nothing to run by hand on the node and no
   credential to copy anywhere: `portal_workers` gets a row and the audit log
   an entry the first time it starts, and it re-mints its own credential on
   every subsequent restart or redeploy.

That's the whole thing once step 1 is done somewhere: no SSH exec, no
`portal enroll-worker`, no object storage mount on the node (only `master`
needs that, since results are published to `worker-api` over HTTP, not
written locally), no bespoke systemd unit (Dokploy is the supervisor).

## What's still manual

- **Step 1 above** (Docker + docker group) needs a human with root on each
  new box. Not scriptable from here without that access.
- **Tailscale ACL hardening.** `tag:worker` -> `tag:portal-worker-api:8443`
  scoping (see `docs/portal-deployment.md#worker-connectivity`) is not
  configured; every node currently reaches every other node by tailnet
  default policy. Fine at this size, a real gap the moment that's no longer
  true. Tailscale admin console only, not reachable from here.
- **Bootstrap token rotation.** No tooling for it yet. Today that would mean
  updating `PORTAL_WORKER_BOOTSTRAP_TOKEN` on `web`, `worker-api`, and every
  node's env by hand, then redeploying each. Not needed unless the token
  leaks; worth a `portal rotate-bootstrap-token` command if it ever is.

## Unrelated thing noticed, not fixed

Job completion tried to send an email and got `422 Unprocessable Entity`
from Resend. The bootstrap admin email is the placeholder `admin@localhost`,
which is almost certainly not a deliverable address. Separate from
everything above; just flagging it.
