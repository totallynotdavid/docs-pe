# Worker fleet

The fleet is a set of named, long-lived worker nodes. The provisioning script
makes a node a single-node Swarm manager, registers it with Dokploy, creates its
worker application, and waits for a heartbeat.

## Commands

```sh
uv run ops/worker_node.py list
uv run ops/worker_node.py add --dry-run <name> <tailnet-ip>
uv run ops/worker_node.py add <name> <tailnet-ip>
uv run ops/worker_node.py remove <name> --yes
```

Set `DOKPLOY_URL` and `DOKPLOY_API_KEY` first. The short name must match the
tailnet hostname. The script derives the worker ID, Dokploy application name,
and worker hostname from it.

## Before adding a node

The node must already be reachable as `dubu` over Tailscale SSH, with the
operator's key installed and passwordless sudo available. Bootstrap that access
with console access before running the script:

```sh
echo "dubu ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/dubu-dokploy
```

The script is idempotent after this boundary. Rerun `add` after a failed step.

## What provisioning configures

`add` installs Docker, prepares `/etc/dokploy`, initializes the node's Swarm and
Dokploy network, installs Dokploy's SSH key for `dubu`, registers and validates
the server, seeds the Dokploy container's `known_hosts`, and creates the
git-connected `portal worker` application.

The worker API address and bootstrap token come from the existing portal
deployment. The node self-enrolls on startup, so no worker credential is copied
by hand.

## Removing a node

`remove` revokes the worker credential and deletes the Dokploy Application and
server row. It does not remove Tailscale membership, Docker, Swarm state, or SSH
access from the machine. Decommission those separately when the host is no
longer needed.

## Network

Configure Tailscale ACLs so `tag:worker` can reach the worker API port.
