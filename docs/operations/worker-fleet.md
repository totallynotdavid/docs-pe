# Worker fleet

The fleet is a set of named worker nodes. The provisioning script configures a
single-node Docker Swarm manager, registers the node with Dokploy, creates its
worker application, and waits for the worker heartbeat.

The script is deployment-specific. Its project, repository, branch, tailnet, and
master host are configured in `ops/worker_node.py`; it is not a general Dokploy
or cloud provisioning tool. Review those values before using it for a different
environment.

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
ID, Dokploy application name, and worker hostname from it. Run `--dry-run`
before changing an existing node.

## Access boundary

Before `add`, the operator must be able to connect as `dubu` over Tailscale SSH
and run the required host commands through non-interactive `sudo`. Establish
that access through the host's normal provisioning or console process.

The script currently needs broad root privileges while it installs Docker,
creates `/etc/dokploy`, initializes Swarm, and configures Dokploy. Do not leave
a permanent unrestricted `NOPASSWD:ALL` rule on a production host. Use a
dedicated account and a time-limited or narrowly scoped host policy where the
environment permits it. Remove temporary access after provisioning and record
the resulting host policy.

## What `add` changes

The command checks and, when needed, configures Docker, `/etc/dokploy`, a
single-node Swarm, the Dokploy overlay network, Dokploy SSH access, the Dokploy
server registration, and the `portal-worker-<name>` application. It obtains the
worker API address and bootstrap token from the existing Dokploy deployment.

The worker self-enrolls when its application starts. No worker credential needs
to be copied by hand when the bootstrap configuration is present.

The operation is idempotent after the SSH and sudo boundary. Rerun `add` after a
failed step and inspect the step that failed before changing the host.

## Remove and decommission

`remove --yes` revokes the worker credential, stops and deletes the Dokploy
application, and removes the Dokploy server row. It does not remove Tailscale
membership, Docker, Swarm state, SSH access, or host data. Decommission those
separately after confirming the node has no active work.

Configure Tailscale ACLs so the worker tag can reach the private worker API
port. Verify the node in `list` after adding or removing it.
