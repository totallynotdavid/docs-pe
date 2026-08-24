# Worker fleet bring-up

Status as of 2026-08-24. Self-enrollment and a git-connected `worker-api` are
real, built, tested code (see `docs/portal-deployment.md#worker-connectivity`
and `docs/portal-deployment.md#dokploy-services` for the design). This doc is
the runbook for standing up a node: what's automatic, and the one-time
Dokploy/platform quirks that aren't. `zeus` and `apollo` are live proof this
works end to end: both self-enrolled and started claiming real queued job
items within a minute of their first deploy.

The fleet is a fixed, small set of named nodes (`master` runs `web` and
`worker-api`; `zeus` and `apollo` run the worker agent), not autoscaled.
`aws` and `poseidon` exist as Tailscale nodes but are not part of the fleet
right now (see Resource identifiers).

## Resource identifiers

- Dokploy project `docs-pe`, environment `production`: `projectId
  7vIKTngpThRQOl6qLNj6z`, `environmentId LNBiAHi2juK4fbQQ58lzs`
- `web` application: `applicationId pwI2OMynqxYOYS4E8mHV5`, git-connected
  (`totallynotdavid/docs-pe`, branch `scale`, `packages/portal/Dockerfile`),
  runs on `master` (no `serverId`: it's the Dokploy host itself, tailnet
  `100.86.240.39`, `master.taila2cbc1.ts.net`)
- `worker-api` compose service: `composeId 4j2tZFo9wcw4v9IeEjsnc`,
  git-connected to the same repo/branch/Dockerfile as `web` via
  `docker-compose.worker-api.yml`, also on `master`
- `portal-worker-zeus`: `applicationId amnqjF44pHK7SQyTRp43p`, `serverId
  cmjQ_i2CZta51vPUVB3z4` (host `zeus`, tailnet `100.106.175.77`)
- `portal-worker-apollo`: `applicationId t1Uy5LneW11l6Q_76bvKl`, `serverId
  M4e66HVOFonytW8SGWCYb` (host `apollo`, tailnet `100.101.190.106`)
- `aws` (tailnet `100.73.201.73`) and `hermes` (tailnet `100.106.48.64`) are
  Tailscale nodes that exist but have no Dokploy server row and aren't part
  of the fleet. Don't assume a box named for a place or provider is the one
  meant here; check `tailscale status` against this list before targeting
  one. `aws` does still hold a live, unrevoked `portal_workers` credential
  (`aws-1`, enrolled 2026-08-14 from an earlier ad hoc fix, predates this
  runbook); nothing runs against it, but it hasn't been explicitly revoked
  either.
- A pre-existing Dokploy server row for `poseidon` (`serverId
  6tVjkiNoiy0GtLfMf6oXT`, `serverType: build`, user `dokploy`) predates this
  session. Its SSH key (`sshKeyId AW23Q6JMq4M5m2UT9tNTZ`, name "poseidon" in
  Dokploy, comment `dokploy` on the key itself) is the only SSH key Dokploy
  holds; it's reused for `zeus` and `apollo` below under username `dubu`, not
  `dokploy` (see step 4). Poseidon itself is offline
  (`tailscale status` shows it 3+ days stale) and not currently reachable.

## Adding a node

None of this is specific to the app; it's what a fresh box needs before
Dokploy can build and run a Swarm service on it. Once done, adding the next
node is these same seven steps, not new ones. Every step below was hit and
fixed live while bringing up `zeus` and `apollo` — this isn't a theoretical
procedure.

1. **Docker + docker group.** Needs root, one time:
   ```sh
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker <user>
   ```
   Confirm with a fresh SSH connection (group membership doesn't apply to an
   already-open session): `ssh <user>@<host> docker ps`.

2. **Passwordless sudo for the deploy user.** Not optional and not just for
   step 1: Dokploy runs root-level commands against a `deploy`-type server
   on an ongoing basis, so the SSH user it connects as needs standing
   `NOPASSWD` sudo, not just enough access to install Docker once.
   ```sh
   echo "<user> ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/<user>-dokploy
   ```
   Verify: `ssh <user>@<host> 'sudo -n true && echo ok'`.

3. **Pre-create `/etc/dokploy`, owned by the deploy user.** `server-setup`
   does not create this, but the deploy pipeline assumes it exists and runs
   a plain `mkdir -p /etc/dokploy/logs/<appName>` with no `sudo`, which fails
   with `mkdir: Permission denied` against root-owned `/etc`, instantly,
   with an empty deployment log (the real error only shows up in Dokploy's
   own container logs, not the deployment record).
   ```sh
   sudo mkdir -p /etc/dokploy && sudo chown <user>:<user> /etc/dokploy
   ```

4. **Register the box** (`server-create`): name, tailnet IP, port 22,
   `sshKeyId` = the existing "poseidon" key, `serverType: deploy`. Use
   **`dubu`** as the username, not `dokploy`: Tailscale SSH gates who can log
   in as whom by ACL policy, not key content. Connecting as a fresh `dokploy`
   user was refused with `tailnet policy does not permit you to SSH as user
   "dokploy"` even holding the right key, while `dubu` was allowed straight
   through. That means appending the Dokploy key to `dubu`'s own
   `~/.ssh/authorized_keys` (no root needed for that part):
   ```sh
   echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGnrRCyNUWZLl7CZOwubdxpYUQoPwEQhcnfNuqO+OEqz dokploy" >> ~/.ssh/authorized_keys
   ```
   Then `server-validate` (confirms Docker + sudo are visible) and
   `server-setup`.

5. **Make the box its own single-node Swarm manager, with its own
   `dokploy-network`.** This is the opposite of what seems natural (you'd
   expect Dokploy to join every server into one shared Swarm led by
   `master`), but it's what this Dokploy version actually does: when an
   Application has a `serverId`, Dokploy builds *and* runs `docker service
   create/update` directly on that box via SSH, not via `master`. That
   command requires a Swarm *manager*, so the box has to be a manager of
   some Swarm, and it needs its own `dokploy-network` overlay because it
   isn't sharing `master`'s.
   ```sh
   ssh <user>@<host> docker swarm init --advertise-addr <tailnet-ip>
   ssh <user>@<host> docker network create --driver overlay --attachable dokploy-network
   ```
   Do **not** `docker swarm join` this box to `master`'s Swarm as a worker;
   that was the first thing tried here and it fails at deploy time with
   `This node is not a swarm manager` after the image builds successfully
   (the build step works fine against a worker; only the service
   create/update step needs manager rights on the box itself). If a box was
   already joined as a worker, `docker swarm leave --force` first.

   (Separately: `master`'s own Swarm advertises itself at its Docker bridge
   IP `172.17.0.1` rather than its tailnet address, which is why
   `cluster-addWorker` 500s here regardless — one more sign this
   multi-manager-Swarm approach isn't the one this installation's Dokploy
   expects. Not worth fixing given the risk of touching a running Swarm's
   advertise address for a workaround that turned out to be the wrong model
   anyway.)

6. **Add the node's SSH host key to Dokploy's own known_hosts.** Dokploy's
   deploy pipeline runs `docker -H ssh://<user>@<host>` from inside its own
   container, which has its own `/root/.ssh` separate from any host user's.
   It is not TOFU (`StrictHostKeyChecking` is on), so a first-time host
   fails the handshake instantly with no useful deployment log. This
   directory is **not a mounted volume** — it resets if the `dokploy`
   service container itself is ever redeployed or restarted, so if a future
   node-add hits an instant, log-less deploy failure, do this again first:
   ```sh
   CID=$(ssh dubu@master.taila2cbc1.ts.net "docker ps -q --filter ancestor=dokploy/dokploy:v0.29.14")
   ssh dubu@master.taila2cbc1.ts.net "ssh-keyscan -H <tailnet-ip>" | \
     ssh dubu@master.taila2cbc1.ts.net "docker exec -i $CID sh -c 'mkdir -p /root/.ssh && cat >> /root/.ssh/known_hosts'"
   ```

7. **Create the Dokploy Application**: git-connected to the same
   repository/branch/Dockerfile as `web`, `command: portal worker`, no
   domain, no port (the agent only makes outbound calls). Env:
   - `PORTAL_WORKER_API_URL=http://100.86.240.39:8443`
   - `PORTAL_WORKER_ID=<name>-1`
   - `PORTAL_WORKER_BOOTSTRAP_TOKEN=<same value as web and worker-api>`
   - `PORTAL_WORKER_TAILSCALE_HOSTNAME=<name>.taila2cbc1.ts.net`

   Deploy. The agent self-enrolls on start (`POST /enroll`, idempotent by
   `PORTAL_WORKER_ID`), so there is nothing to run by hand on the node and
   no credential to copy anywhere. Confirm with `docker service logs
   <appName>` on the node (should show it claiming job items, not just
   sitting idle) and a row in `portal_workers` (`SELECT worker_id,
   tailscale_hostname, revoked_at FROM portal_workers;` against
   `docspe_portal` on `master`).

Steps 1-3 need a human with root on the box, once. Steps 4-7 are ordinary
Dokploy operations once 1-3 and the platform quirks in 5 and 6 are worked
around; nothing there is per-node custom work, no bespoke systemd unit, no
object storage mount on the node (only `master` needs that, since results
are published to `worker-api` over HTTP, not written locally).

## What's still manual

- **Steps 1-3 above** need a human with root on each new box.
- **The single-node-Swarm setup (step 5)** and **the known_hosts seeding
  (step 6)** are Dokploy/platform quirks in this installation, not
  application code; they'll recur for every future node (e.g. re-adding
  `poseidon`) until fixed at the platform level, which isn't planned given
  the risk of touching a running Swarm's advertise address or Dokploy's
  container internals for a workaround.
- **`aws-1`'s stale credential.** Not revoked; flagged above under Resource
  identifiers, decision deferred to whoever wants `aws` gone for good.
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
