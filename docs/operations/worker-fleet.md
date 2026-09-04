# Worker fleet

The fleet is a set of named worker nodes. Each node needs Tailscale access to
the worker API and PostgreSQL. The provisioning script configures the host,
registers it with Dokploy, creates its worker compose service, and waits for
the worker heartbeat.

The script is deployment-specific. Its project, repository, branch, tailnet, and
master host are configured in `ops/worker_node.py`; it is not a general Dokploy
or cloud provisioning tool. Review those values before using it for a different
environment.

The host must already have Tailscale installed, authenticated, and tagged with
`tag:worker-fleet`. The script does not change tailnet membership or ACLs.

## Commands

Set `DOKPLOY_URL` and `DOKPLOY_API_KEY` in the operator environment:

```sh
uv run ops/worker_node.py list
uv run ops/worker_node.py add --dry-run <name> <tailnet-ip>
uv run ops/worker_node.py add <name> <tailnet-ip>
uv run ops/worker_node.py remove <name> --dry-run
uv run ops/worker_node.py remove <name> --yes
```

The name must match the node's Tailscale hostname. The script derives the worker
ID, Dokploy Compose resource name, and worker hostname from it. Run `--dry-run`
before changing an existing node.

## Access boundary

Before `add`, the operator must be able to connect as `dubu` over Tailscale SSH
and run the required host commands through non-interactive `sudo`. Establish
that access through the host's normal provisioning or console process.

The script currently needs broad root privileges while it installs Docker,
creates `/etc/dokploy`, and configures Dokploy. Do not leave
a permanent unrestricted `NOPASSWD:ALL` rule on a production host. Use a
dedicated account and a time-limited or narrowly scoped host policy where the
environment permits it. Remove temporary access after provisioning and record
the resulting host policy.

## What `add` changes

The command checks and, when needed, configures Docker, `/etc/dokploy`, Dokploy
SSH access, the Dokploy server registration, and the `portal-worker-<name>`
Compose service (`docker-compose.worker.yml`, deployed via `docker compose`).
Unlike `web`, the worker fleet has no domain or build variance that requires a
Swarm application. It obtains the worker API address and bootstrap token from
the existing Dokploy deployment.

The worker self-enrolls when its container starts. Enrollment provisions the
API credential and the node's scoped PostgreSQL login, so no credential needs to
be copied by hand when the bootstrap configuration is present.

The provisioning script saves the worker environment as a `.env` file in the
Compose deployment directory because `docker-compose.worker.yml` reads that
file. Do not replace it with shell-only Compose variables: a deployment can
start without the worker receiving its enrollment settings.

The operation is idempotent after the SSH and sudo boundary. Rerun `add` after a
failed step; it reconciles the Dokploy compose service before deploying it and
waits for the new deployment to finish.

After deployment, `add` waits for a heartbeat newer than the verification check
started. An existing heartbeat does not prove that the new container is alive.
A transient SSH timeout during polling is retried as a missed observation. If
verification times out, inspect the Compose logs in Dokploy and rerun `add` only
after confirming whether the container started.

## Remove and decommission

`remove --yes` revokes the API credential and disables the worker's PostgreSQL
login, then stops and deletes the Dokploy compose service and Dokploy server
row. It does not remove Tailscale membership, Docker, SSH access, or host
data. Decommission those separately after confirming the node has no active
work.

Verify the node in `list` after adding or removing it.
