# Worker fleet bring-up

Status as of 2026-08-24. Self-enrollment and a git-connected `worker-api` are
real, built, tested code (see `docs/portal-deployment.md#worker-connectivity`
and `docs/portal-deployment.md#dokploy-services` for the design). Adding or
removing a fleet node is scripted end to end in
[`ops/worker_node.py`](../ops/worker_node.py); this doc explains the fleet
model and what that script does, for reviewing a run before it happens or
troubleshooting one that failed partway through.

The fleet is a fixed, small set of named nodes (`master` runs `web` and
`worker-api`; `zeus`, `apollo`, and `hermes` run the worker agent), not
autoscaled. `hermes` (`100.106.48.64`) was brought up through
`ops/worker_node.py add hermes 100.106.48.64` as this pipeline's first live
test — it worked end to end on the first clean run, with one real bug found
and fixed along the way: `dokploy_container_id()` filtered by
`ancestor=dokploy/dokploy:v0.29.14`, which stopped matching after the local
image tag drifted off the running container sometime in the two weeks since
that container last restarted (the container kept running fine; only the
tag->id lookup went stale). Fixed to filter by the swarm service label
(`com.docker.swarm.service.name=dokploy`) instead, which doesn't depend on the
image tag staying put. `poseidon` has a stale Dokploy server row (`serverId
6tVjkiNoiy0GtLfMf6oXT`, username `dokploy` instead of `dubu`) left over from
before this pipeline existed; it's offline and not currently reachable. `aws`
was a fleet member during early development; its `portal_workers` credential
was revoked (`portal enroll-worker --worker-id aws-1 --revoke`) and it was
never registered with Dokploy, so no further cleanup is owed there.

## Usage

```sh
uv run ops/worker_node.py list                          # every node's state, in one shot
uv run ops/worker_node.py add <name> <tailnet-ip>        # add a node
uv run ops/worker_node.py add --dry-run <name> <ip>      # show the plan, touch nothing
uv run ops/worker_node.py remove <name> --yes            # remove a node
```

`<name>` is the short tailnet hostname (`hermes`, not the FQDN). The script
derives everything else: `PORTAL_WORKER_ID` (`<name>-1`), the Dokploy
Application name (`portal-worker-<name>`), and the node's FQDN
(`<name>.taila2cbc1.ts.net`).

Requires `DOKPLOY_URL` and `DOKPLOY_API_KEY` in the environment (Dokploy's own
REST API, `x-api-key` auth: `curl -H "x-api-key: $DOKPLOY_API_KEY"
"$DOKPLOY_URL/api/project.all"`), and an SSH agent already trusted by the
tailnet — the same access this pipeline uses to configure the box and to
reach `master` for the Dokploy-container and `portal_workers` steps.

### The one thing it can't do for you

`add` needs `dubu@<name>.taila2cbc1.ts.net` to already be reachable over SSH
with **passwordless sudo**. Getting a fresh box to that point is a one-time,
per-box, human-with-console-access step: create the `dubu` account, put its
SSH key on the box, and run

```sh
echo "dubu ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/dubu-dokploy
```

`add` checks for both up front and fails with the exact remediation command
if either is missing, rather than half-running. Everything after that point
is scripted and safe to re-run: every step checks whether it's already done
before acting, so a run that fails partway through is resumed by running
`add` again, not by figuring out by hand where it stopped.

## What `add` actually does

Each of these is one `Step` in `ops/worker_node.py` (`build_docker_steps`,
`build_swarm_steps`, `build_server_steps`, `build_application_steps`), in
this order. Platform quirks called out below were each hit and fixed live
standing up `zeus` and `apollo`; none of this is theoretical.

1. **Docker + docker group.** `curl -fsSL https://get.docker.com | sudo sh`,
   then `usermod -aG docker dubu`. Every later SSH call in the script is a
   fresh connection, so the new group membership is picked up automatically;
   no separate reconnect step is needed.

2. **`/etc/dokploy`, owned by `dubu`.** Dokploy's own `server-setup` does not
   create this, but the deploy pipeline assumes it exists and runs a plain
   `mkdir -p /etc/dokploy/logs/<appName>` with no `sudo` — that fails with
   `Permission denied` against root-owned `/etc`, instantly, with an empty
   deployment log. The real error only ever showed up in Dokploy's own
   container logs (`docker logs <dokploy-container-id>` on `master`), never
   in the deployment record itself.

3. **Make the box its own single-node Swarm manager, with its own
   `dokploy-network`.** This is the opposite of the natural assumption (join
   every server into one Swarm led by `master`), but it's what this Dokploy
   version actually does: when an Application has a `serverId`, Dokploy
   builds *and* runs `docker service create/update` directly on that box via
   SSH, not via `master`. That requires the box to be a Swarm *manager*
   itself, with its own overlay network since it isn't sharing `master`'s.
   Joining as a worker instead fails at deploy time with `This node is not a
   swarm manager`, *after* the image has already built successfully — the
   build step works fine against a worker; only the service create/update
   step needs manager rights on the box itself.

   (`master`'s own Swarm advertises itself at its Docker bridge IP
   `172.17.0.1` rather than its tailnet address, which is a separate reason a
   plain `cluster.addWorker` wouldn't have worked here regardless — one more
   sign the shared-Swarm model isn't what this installation's Dokploy
   expects.)

4. **Dokploy's SSH key in `dubu`'s `authorized_keys`.** Tailscale SSH ACLs
   gate who may log in as whom by *destination username*, not by which key
   is presented: connecting as a fresh `dokploy` user was refused with
   `tailnet policy does not permit you to SSH as user "dokploy"` even holding
   the right key, while `dubu` was let straight through. So the existing
   Dokploy-managed key (name `poseidon` in Dokploy, `sshKeyId
   AW23Q6JMq4M5m2UT9tNTZ`, reused for every node) gets appended to `dubu`'s
   own `authorized_keys` rather than a `dokploy` user being created.

5. **Register the server with Dokploy** (`server.create`, `serverType:
   deploy`, username `dubu`), then `server.validate` and `server.setup`.

6. **Seed the node's SSH host key into Dokploy's own container's
   `known_hosts`.** Dokploy's deploy pipeline runs `docker -H
   ssh://dubu@<host>` from *inside its own container*, which has its own
   `/root/.ssh`, separate from any host user's, and is not TOFU
   (`StrictHostKeyChecking` is on) — a first-time host fails the SSH
   handshake instantly with no useful deployment log. This directory is
   **not a mounted volume**: it resets if the `dokploy` service container
   itself is ever redeployed or restarted, so an instant, log-less deploy
   failure for a node that worked before likely means this step needs
   redoing (`add` detects and redoes it automatically; it's idempotent).

7. **Create the Dokploy Application**: git-connected to the same
   repository/branch/Dockerfile as `web`, `command: portal worker`, no domain,
   no port (the agent only makes outbound calls). Env is `PORTAL_WORKER_ID`,
   `PORTAL_WORKER_API_URL`, `PORTAL_WORKER_TAILSCALE_HOSTNAME`, and
   `PORTAL_WORKER_BOOTSTRAP_TOKEN` — the last one read back from `worker-api`'s
   own Dokploy environment rather than asked of the operator, so the one
   shared secret stays defined in exactly one place. Deploy, then poll
   `portal_workers` for a fresh heartbeat before reporting success: the agent
   self-enrolls on start (`POST /enroll`, idempotent by `PORTAL_WORKER_ID`),
   so there is nothing to run by hand on the node and no credential to copy
   anywhere.

## What `remove` does

Revokes the `portal_workers` credential (`portal enroll-worker --worker-id
<name>-1 --revoke`, run inside the live `worker-api` container on `master`),
then deletes the Dokploy Application and the Dokploy server row. That's full
removal from the fleet — matches how `aws` was cleaned up. It does **not**
touch the box's Tailscale membership, Docker/Swarm state, or SSH access;
decommissioning a box's tailnet presence entirely is a separate, larger
decision than "stop it being a fleet member."

## What's still manual

- **Placing `dubu`'s SSH key and NOPASSWD sudo on a fresh box** (see above) —
  inherent to needing SSH access to exist before this pipeline can use it.
- **Tailscale ACL hardening.** `tag:worker` -> `tag:portal-worker-api:8443`
  scoping (see `docs/portal-deployment.md#worker-connectivity`) is not
  configured; every node currently reaches every other node by tailnet
  default policy. Fine at this size, a real gap the moment that's no longer
  true. Tailscale admin console only, not reachable from here.
- **Bootstrap token rotation.** No tooling for it yet. Today that would mean
  updating `PORTAL_WORKER_BOOTSTRAP_TOKEN` on `web`, `worker-api`, and every
  node's env by hand, then redeploying each. Not needed unless the token
  leaks; worth adding to `ops/worker_node.py` if it ever is.
- **`poseidon`'s stale server row and `hermes`** — see above.

## Unrelated thing noticed, not fixed

Job completion tried to send an email and got `422 Unprocessable Entity`
from Resend. The bootstrap admin email is the placeholder `admin@localhost`,
which is almost certainly not a deliverable address. Separate from
everything above; just flagging it.
